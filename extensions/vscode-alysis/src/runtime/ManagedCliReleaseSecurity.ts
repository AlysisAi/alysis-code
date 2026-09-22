import { createPublicKey, KeyObject, verify } from "node:crypto";

import {
  ManagedCliError,
  ManagedCliSignedRecord,
  SignatureVerifier,
  SignatureVerificationInput
} from "./ManagedCliRuntime";

/**
 * Signature domain separator for v3 attestations.
 *
 * Deliberately NOT renamed with the rest of the rebrand: this string is hashed
 * into every signature already published, so changing it would make every
 * existing signed release fail verification. It is frozen for the life of
 * schema v3; a future v4 can adopt the new name and migrate deliberately.
 * Must stay byte-identical to DOMAIN in scripts/release/build_managed_cli_manifest.py.
 */
const RELEASE_ATTESTATION_DOMAIN = "sylliptor-managed-cli-release-v3";
const BASE64_SIGNATURE = /^[A-Za-z0-9+/]+={0,2}$/;
const SAFE_KEY_ID = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;

export type ManagedCliTrustStatus = "active" | "retiring" | "revoked";

export interface ManagedCliReleaseTrustKey {
  keyId: string;
  publicKeyPem: string;
  status: ManagedCliTrustStatus;
}

/**
 * Offline release trust anchor. The matching private key is kept out of the repository and must be
 * supplied only through the protected release-signing boundary.
 */
export const MANAGED_CLI_RELEASE_KEY_ID = "alysis-release-2026-01";

/**
 * The same signing key under the name it carried before the rebrand.
 *
 * Releases published as Sylliptor recorded `signingKeyId:
 * "sylliptor-release-2026-01"`, and the verifier looks its trust anchor up by
 * that id. Dropping the entry would make every already-published managed CLI
 * fail signature verification even though the key never changed.
 */
export const MANAGED_CLI_RELEASE_LEGACY_KEY_ID = "sylliptor-release-2026-01";

export const MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM = `-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEIkL6ug73ecxgDbVsqHrnzQU7rbjt
oLsB1iQ8y6L6I98bwKFQJE8puAEkpuWiuaNi+nTjO2BcwKU8ttcMtO6cFQ==
-----END PUBLIC KEY-----`;

export const MANAGED_CLI_RELEASE_TRUST_SET: readonly ManagedCliReleaseTrustKey[] = Object.freeze([
  Object.freeze({
    keyId: MANAGED_CLI_RELEASE_KEY_ID,
    publicKeyPem: MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM,
    status: "active" as const
  }),
  Object.freeze({
    keyId: MANAGED_CLI_RELEASE_LEGACY_KEY_ID,
    publicKeyPem: MANAGED_CLI_RELEASE_PUBLIC_KEY_PEM,
    // Same key material, retiring name: accepted for verification, never
    // selected for new signatures.
    status: "retiring" as const
  })
]);

export function managedCliReleaseAttestation(record: ManagedCliSignedRecord): Buffer {
  return Buffer.from(
    `${RELEASE_ATTESTATION_DOMAIN}\n${canonicalJson(record)}\n`,
    "utf8"
  );
}

export function createManagedCliReleaseSignatureVerifier(
  trustSet: readonly ManagedCliReleaseTrustKey[] = MANAGED_CLI_RELEASE_TRUST_SET
): SignatureVerifier {
  const keys = validatedTrustSet(trustSet);
  return async (input: SignatureVerificationInput): Promise<boolean> => {
    if (input.signal?.aborted) {
      throw new ManagedCliError("CANCELLED", "Managed runtime signature verification was cancelled.");
    }
    const trusted = keys.get(input.record.signingKeyId);
    if (!trusted || trusted.status === "revoked") {
      return false;
    }
    if (trusted.status === "retiring" && input.purpose === "install") {
      return false;
    }
    if (
      input.signature.length < 80 ||
      input.signature.length > 256 ||
      !BASE64_SIGNATURE.test(input.signature) ||
      input.signature.length % 4 !== 0
    ) {
      return false;
    }
    const signature = Buffer.from(input.signature, "base64");
    if (signature.length < 64 || signature.length > 80) {
      return false;
    }
    return verify(
      "sha256",
      managedCliReleaseAttestation(input.record),
      trusted.publicKey,
      signature
    );
  };
}

function validatedTrustSet(
  trustSet: readonly ManagedCliReleaseTrustKey[]
): ReadonlyMap<string, { publicKey: KeyObject; status: ManagedCliTrustStatus }> {
  if (trustSet.length === 0) {
    throw new ManagedCliError("SIGNATURE_REQUIRED", "Managed runtime release trust set is empty.");
  }
  const result = new Map<string, { publicKey: KeyObject; status: ManagedCliTrustStatus }>();
  for (const key of trustSet) {
    if (!SAFE_KEY_ID.test(key.keyId) || result.has(key.keyId)) {
      throw new ManagedCliError("SIGNATURE_REQUIRED", "Managed runtime release trust set contains an invalid or duplicate key id.");
    }
    if (key.status !== "active" && key.status !== "retiring" && key.status !== "revoked") {
      throw new ManagedCliError("SIGNATURE_REQUIRED", "Managed runtime release trust set contains an invalid key status.");
    }
    let publicKey: KeyObject;
    try {
      publicKey = createPublicKey(key.publicKeyPem);
    } catch (error) {
      throw new ManagedCliError("SIGNATURE_REQUIRED", `Managed runtime trust key ${key.keyId} is invalid.`, error);
    }
    if (publicKey.asymmetricKeyType !== "ec" || publicKey.asymmetricKeyDetails?.namedCurve !== "prime256v1") {
      throw new ManagedCliError("SIGNATURE_REQUIRED", `Managed runtime trust key ${key.keyId} must be ECDSA P-256.`);
    }
    result.set(key.keyId, { publicKey, status: key.status });
  }
  return result;
}

function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "string" || typeof value === "boolean" || typeof value === "number") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const entries = Object.keys(record)
      .sort()
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(record[key])}`);
    return `{${entries.join(",")}}`;
  }
  throw new ManagedCliError("SIGNATURE_INVALID", "Managed runtime signed record is not canonicalizable.");
}
