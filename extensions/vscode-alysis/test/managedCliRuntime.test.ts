import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import {
  classifyInstallLockOpenError,
  ExecutableProbe,
  HttpResponse,
  HttpTransport,
  ManagedCliArtifact,
  ManagedCliError,
  ManagedCliManifest,
  ManagedCliRuntime,
  ManagedCliTarget,
  managedCliSignedRecord,
  parseManagedCliManifest,
  resolveManagedCliTarget,
  isUnsafeHostname,
  selectRuntime
} from "../src/runtime/ManagedCliRuntime";

const CONTENT_V1 = Buffer.from("managed-cli-v1");
const CONTENT_V2 = Buffer.from("managed-cli-v2");

test("resolveManagedCliTarget covers every supported desktop target and rejects unknown ABIs", () => {
  const matrix: Array<[NodeJS.Platform, string, ManagedCliTarget]> = [
    ["win32", "x64", "win32-x64"],
    ["win32", "arm64", "win32-arm64"],
    ["darwin", "x64", "darwin-x64"],
    ["darwin", "arm64", "darwin-arm64"],
    ["linux", "x64", "linux-x64"],
    ["linux", "arm64", "linux-arm64"]
  ];
  for (const [platform, arch, expected] of matrix) {
    assert.equal(resolveManagedCliTarget(platform, arch), expected);
  }
  assert.throws(() => resolveManagedCliTarget("linux", "arm"), hasCode("UNSUPPORTED_TARGET"));
  assert.throws(() => resolveManagedCliTarget("freebsd", "x64"), hasCode("UNSUPPORTED_TARGET"));
});

test("manifest parsing is strict about schema, compatibility, target uniqueness, and path safety", () => {
  const valid = manifest("runtime-1", "1.0.0", CONTENT_V1);
  assert.equal(parseManagedCliManifest(valid).artifacts[0].target, "linux-x64");

  assert.throws(
    () => parseManagedCliManifest({ ...valid, misspelledCompatibility: {} }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({ ...valid, schemaVersion: 1 }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({ ...valid, release: { ...valid.release, sourceCommit: "bad" } }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({ ...valid, signingKeyId: "../bad" }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({
      ...valid,
      artifacts: [{ ...valid.artifacts[0], sbomSha256: "bad" }]
    }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({
      ...valid,
      artifacts: [{
        ...valid.artifacts[0],
        nativeSignature: {
          policy: "not-applicable",
          signerIdentity: "not-applicable"
        }
      }]
    }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({
      ...valid,
      artifacts: [{
        ...valid.artifacts[0],
        nativeSignature: { ...valid.artifacts[0].nativeSignature, unexpectedEvidence: true }
      }]
    }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({
      ...valid,
      artifacts: [{
        ...valid.artifacts[0],
        nativeSignature: { policy: "authenticode", signerIdentity: "test" }
      }]
    }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({ ...valid, artifactVersion: "../escape" }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({ ...valid, artifacts: [{ ...valid.artifacts[0], executable: "../alysis" }] }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({ ...valid, artifacts: [valid.artifacts[0], valid.artifacts[0]] }),
    hasCode("INVALID_MANIFEST")
  );
  assert.throws(
    () => parseManagedCliManifest({
      ...valid,
      cliVersion: "3.0.0",
      compatibility: { ...valid.compatibility, cli: { min: "1.0.0", max: "2.0.0" } }
    }),
    hasCode("INVALID_MANIFEST")
  );
});

test("manifest and redirect validation reject unsafe URLs, downgrade, private hosts, and traversal", async (t) => {
  const valid = manifest("runtime-url", "1.0.0", CONTENT_V1);
  for (const url of [
    "http://artifacts.example.com/alysis",
    "https://user:secret@artifacts.example.com/alysis",
    "https://localhost/alysis",
    "https://127.0.0.1/alysis",
    "https://artifacts.example.com/a/../alysis",
    "https://artifacts.example.com/a/%2e%2e/alysis",
    "https://artifacts.example.com:8443/alysis"
  ]) {
    assert.throws(
      () => parseManagedCliManifest({ ...valid, artifacts: [{ ...valid.artifacts[0], url }] }),
      hasCode("UNSAFE_URL"),
      url
    );
  }
  assert.throws(
    () => parseManagedCliManifest(valid, { trustedDownloadHosts: ["releases.alysis.dev"] }),
    hasCode("UNSAFE_URL")
  );

  const root = await tempRoot(t);
  const transport: HttpTransport = {
    async open(): Promise<HttpResponse> {
      return { statusCode: 302, headers: { location: "http://artifacts.example.com/downgraded" }, body: chunks() };
    }
  };
  const runtime = manager(root, transport, contentProbe());
  await assert.rejects(() => runtime.install(valid), hasCode("UNSAFE_URL"));
});

test("install streams, verifies, probes, and atomically activates an immutable side-by-side runtime", async (t) => {
  const root = await tempRoot(t);
  const transport = new ContentTransport(new Map([["runtime-1", CONTENT_V1]]));
  const runtime = manager(root, transport, contentProbe());

  const installed = await runtime.install(manifest("runtime-1", "1.0.0", CONTENT_V1));

  assert.equal(installed.artifactVersion, "runtime-1");
  assert.equal(installed.cliVersion, "1.0.0");
  assert.equal(await readFile(installed.executablePath, "utf8"), CONTENT_V1.toString());
  assert.equal((await runtime.getActive()).executablePath, installed.executablePath);
  assert.equal(transport.opens, 1);
  const pointer = JSON.parse(await readFile(path.join(root, "current.json"), "utf8")) as Record<string, unknown>;
  assert.equal(pointer.artifactVersion, "runtime-1");
  assert.equal(pointer.executable, "alysis");
});

test("bundled install copies only a verified direct child of the configured bundle root", async (t) => {
  const root = await tempRoot(t);
  const bundleRoot = path.join(root, "bundle");
  const storageRoot = path.join(root, "storage");
  await mkdir(bundleRoot);
  const bundledExecutable = path.join(bundleRoot, "alysis");
  await writeFile(bundledExecutable, CONTENT_V1);
  const release = manifest("runtime-bundled", "1.0.0", CONTENT_V1, {
    signature: "trusted-bundled-signature"
  });
  const runtime = manager(storageRoot, new ContentTransport(new Map()), contentProbe(), {
    bundledRuntimeRoot: bundleRoot,
    requireSignature: true,
    signatureVerifier: async () => true
  });

  const installed = await runtime.installBundled(release, bundledExecutable);

  assert.equal(installed.artifactVersion, "runtime-bundled");
  assert.equal(installed.signatureVerified, true);
  assert.equal(await readFile(installed.executablePath, "utf8"), CONTENT_V1.toString());
  assert.notEqual(installed.executablePath, bundledExecutable);
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-bundled");
});

test("bundled install rejects sources outside its pinned root and tampered bundle bytes", async (t) => {
  const root = await tempRoot(t);
  const bundleRoot = path.join(root, "bundle");
  const storageRoot = path.join(root, "storage");
  await mkdir(bundleRoot);
  const outside = path.join(root, "alysis");
  await writeFile(outside, CONTENT_V1);
  const release = manifest("runtime-bundled-invalid", "1.0.0", CONTENT_V1);
  const runtime = manager(storageRoot, new ContentTransport(new Map()), contentProbe(), {
    bundledRuntimeRoot: bundleRoot
  });

  await assert.rejects(
    () => runtime.installBundled(release, outside),
    hasCode("BUNDLED_RUNTIME_INVALID")
  );

  const bundledExecutable = path.join(bundleRoot, "alysis");
  await writeFile(bundledExecutable, Buffer.from("tampered"));
  await assert.rejects(
    () => runtime.installBundled(release, bundledExecutable),
    hasCode("EXECUTABLE_INVALID")
  );
  await assert.rejects(() => runtime.getActive(), hasCode("NO_ACTIVE_RUNTIME"));
});

test("tampered bytes fail closed and never create or activate a runtime", async (t) => {
  const root = await tempRoot(t);
  const transport = new ContentTransport(new Map([["runtime-tampered", Buffer.from("tampered")]]));
  const runtime = manager(root, transport, contentProbe());

  await assert.rejects(
    () => runtime.install(manifest("runtime-tampered", "1.0.0", CONTENT_V1, { declaredSize: 8 })),
    hasCode("HASH_MISMATCH")
  );
  await assert.rejects(() => runtime.getActive(), hasCode("NO_ACTIVE_RUNTIME"));
  assert.deepEqual(await readdir(path.join(root, ".tmp")), []);
  assert.deepEqual(await readdir(path.join(root, "versions")), []);
});

test("signature policy is injectable and fails closed before executable activation", async (t) => {
  const root = await tempRoot(t);
  const signed = manifest("runtime-signed", "1.0.0", CONTENT_V1, { signature: "signed-release-payload" });
  let verifierCalls = 0;
  const runtime = new ManagedCliRuntime({
    ...baseOptions(root, new ContentTransport(new Map([["runtime-signed", CONTENT_V1]])), contentProbe()),
    requireSignature: true,
    signatureVerifier: async (input) => {
      verifierCalls += 1;
      assert.equal(await readFile(input.filePath, "utf8"), CONTENT_V1.toString());
      return false;
    }
  });
  await assert.rejects(() => runtime.install(signed), hasCode("SIGNATURE_INVALID"));
  assert.equal(verifierCalls, 1);
  await assert.rejects(() => runtime.getActive(), hasCode("NO_ACTIVE_RUNTIME"));

  const missingSignature = manager(
    await tempRoot(t),
    new ContentTransport(new Map([["runtime-unsigned", CONTENT_V1]])),
    contentProbe(),
    { requireSignature: true, signatureVerifier: async () => true }
  );
  await assert.rejects(
    () => missingSignature.install(manifest("runtime-unsigned", "1.0.0", CONTENT_V1)),
    hasCode("SIGNATURE_REQUIRED")
  );
});

test("strict production policy persists and rechecks signature-verification provenance", async (t) => {
  const root = await tempRoot(t);
  const release = manifest("runtime-trusted", "1.0.0", CONTENT_V1, { signature: "trusted-release-signature" });
  const runtime = manager(
    root,
    new ContentTransport(new Map([["runtime-trusted", CONTENT_V1]])),
    contentProbe(),
    { requireSignature: true, signatureVerifier: async () => true }
  );

  const installed = await runtime.install(release);
  assert.equal(installed.signatureVerified, true);
  assert.equal((await runtime.getActive()).signatureVerified, true);

  const noVerifier = manager(
    root,
    new ContentTransport(new Map()),
    contentProbe(),
    { requireSignature: true, signatureVerifier: undefined }
  );
  await assert.rejects(() => noVerifier.getActive(), hasCode("SIGNATURE_REQUIRED"));
  const rejectingVerifier = manager(
    root,
    new ContentTransport(new Map()),
    contentProbe(),
    { requireSignature: true, signatureVerifier: async () => false }
  );
  await assert.rejects(() => rejectingVerifier.getActive(), hasCode("SIGNATURE_INVALID"));

  const metadataPath = path.join(root, "versions", "runtime-trusted", "linux-x64", "runtime.json");
  const metadata = JSON.parse(await readFile(metadataPath, "utf8")) as Record<string, unknown>;
  metadata.signatureVerified = false;
  await writeFile(metadataPath, `${JSON.stringify(metadata)}\n`, "utf8");
  const pointerPath = path.join(root, "current.json");
  const pointer = JSON.parse(await readFile(pointerPath, "utf8")) as Record<string, unknown>;
  pointer.signatureVerified = false;
  await writeFile(pointerPath, `${JSON.stringify(pointer)}\n`, "utf8");
  await assert.rejects(() => runtime.getActive(), hasCode("SIGNATURE_REQUIRED"));
});

test("installed metadata persists and revalidates the complete signed release policy", async (t) => {
  const root = await tempRoot(t);
  const release = manifest("runtime-policy", "1.0.0", CONTENT_V1, { signature: "trusted-release-signature" });
  const expectedRecord = JSON.stringify(managedCliSignedRecord(release, release.artifacts[0]));
  const purposes: string[] = [];
  const runtime = manager(
    root,
    new ContentTransport(new Map([["runtime-policy", CONTENT_V1]])),
    contentProbe(),
    {
      requireSignature: true,
      signatureVerifier: async (input) => {
        purposes.push(input.purpose);
        return JSON.stringify(input.record) === expectedRecord;
      }
    }
  );
  await runtime.install(release);
  await runtime.getActive();
  assert.deepEqual(purposes, ["install", "installed", "installed"]);

  const metadataPath = path.join(root, "versions", "runtime-policy", "linux-x64", "runtime.json");
  const original = JSON.parse(await readFile(metadataPath, "utf8")) as Record<string, unknown>;
  assert.deepEqual(original.release, release.release);
  assert.deepEqual(original.compatibility, release.compatibility);
  assert.equal(original.signingKeyId, release.signingKeyId);
  assert.deepEqual(original.provenance, release.provenance);
  assert.equal(original.sbomSha256, release.artifacts[0].sbomSha256);
  assert.deepEqual(original.nativeSignature, release.artifacts[0].nativeSignature);

  type MutableMetadata = {
    release: { sourceCommit: string };
    compatibility: { extension: { max: string } };
    signingKeyId: string;
    provenance: { builderId: string };
    sbomSha256: string;
    nativeSignature: { evidenceSha256: string; signerIdentity: string };
  };
  const mutations: Array<(metadata: MutableMetadata) => void> = [
    (metadata) => { metadata.release.sourceCommit = "b".repeat(40); },
    (metadata) => { metadata.compatibility.extension.max = "0.3.0"; },
    (metadata) => { metadata.signingKeyId = "other-key"; },
    (metadata) => { metadata.provenance.builderId = metadata.provenance.builderId.replace("v1.0.0", "v1.0.1"); },
    (metadata) => { metadata.sbomSha256 = "c".repeat(64); },
    (metadata) => { metadata.nativeSignature.evidenceSha256 = "d".repeat(64); },
    (metadata) => { metadata.nativeSignature.signerIdentity = "tampered"; }
  ];
  for (const mutate of mutations) {
    const tampered = structuredClone(original) as MutableMetadata;
    mutate(tampered);
    await writeFile(metadataPath, `${JSON.stringify(tampered)}\n`, "utf8");
    await assert.rejects(() => runtime.getActive());
  }
  await writeFile(metadataPath, `${JSON.stringify(original)}\n`, "utf8");
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-policy");
});

test("bundled reconciliation upgrades atomically, retains LKG, and blocks package downgrade", async (t) => {
  const root = await tempRoot(t);
  const bundleRoot = path.join(root, "bundle");
  const storageRoot = path.join(root, "storage");
  await mkdir(bundleRoot);
  const bundledExecutable = path.join(bundleRoot, "alysis");
  const runtime = manager(storageRoot, new ContentTransport(new Map()), contentProbe(), {
    bundledRuntimeRoot: bundleRoot,
    requireSignature: true,
    signatureVerifier: async () => true
  });

  await writeFile(bundledExecutable, CONTENT_V1);
  await runtime.installBundled(
    manifest("runtime-bundled-1", "1.0.0", CONTENT_V1, { signature: "A".repeat(16) }),
    bundledExecutable
  );
  await writeFile(bundledExecutable, CONTENT_V2);
  await runtime.installBundled(
    manifest("runtime-bundled-2", "2.0.0", CONTENT_V2, { signature: "B".repeat(16) }),
    bundledExecutable
  );
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-bundled-2");
  assert.equal((await runtime.rollback()).artifactVersion, "runtime-bundled-1");
  assert.equal((await runtime.rollback()).artifactVersion, "runtime-bundled-2");

  await writeFile(bundledExecutable, CONTENT_V1);
  await assert.rejects(
    () => runtime.installBundled(
      manifest("runtime-bundled-old", "1.0.0", CONTENT_V1, { signature: "C".repeat(16) }),
      bundledExecutable
    ),
    hasCode("DOWNGRADE_BLOCKED")
  );
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-bundled-2");
});

test("a failed bundled upgrade preserves the active signed LKG candidate", async (t) => {
  const root = await tempRoot(t);
  const bundleRoot = path.join(root, "bundle");
  const storageRoot = path.join(root, "storage");
  await mkdir(bundleRoot);
  const bundledExecutable = path.join(bundleRoot, "alysis");
  const runtime = manager(storageRoot, new ContentTransport(new Map()), contentProbe(), {
    bundledRuntimeRoot: bundleRoot,
    requireSignature: true,
    signatureVerifier: async (input) => input.record.artifactVersion !== "runtime-bundled-bad"
  });

  await writeFile(bundledExecutable, CONTENT_V1);
  await runtime.installBundled(
    manifest("runtime-bundled-good", "1.0.0", CONTENT_V1, { signature: "A".repeat(16) }),
    bundledExecutable
  );
  await writeFile(bundledExecutable, CONTENT_V2);
  await assert.rejects(
    () => runtime.installBundled(
      manifest("runtime-bundled-bad", "2.0.0", CONTENT_V2, { signature: "B".repeat(16) }),
      bundledExecutable
    ),
    hasCode("SIGNATURE_INVALID")
  );
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-bundled-good");
});

test("an interrupted upgrade preserves current and removes partial owned files", async (t) => {
  const root = await tempRoot(t);
  const firstTransport = new ContentTransport(new Map([["runtime-1", CONTENT_V1]]));
  const firstRuntime = manager(root, firstTransport, contentProbe());
  const first = await firstRuntime.install(manifest("runtime-1", "1.0.0", CONTENT_V1));

  const controller = new AbortController();
  const transport: HttpTransport = {
    async open(): Promise<HttpResponse> {
      return {
        statusCode: 200,
        headers: { "content-length": String(CONTENT_V2.length) },
        body: (async function* () {
          yield CONTENT_V2.subarray(0, 4);
          controller.abort();
          yield CONTENT_V2.subarray(4);
        })()
      };
    }
  };
  const runtime = manager(root, transport, contentProbe());
  await assert.rejects(
    () => runtime.install(manifest("runtime-2", "2.0.0", CONTENT_V2), controller.signal),
    hasCode("CANCELLED")
  );
  assert.equal((await runtime.getActive()).executablePath, first.executablePath);
  assert.deepEqual(await readdir(path.join(root, ".tmp")), []);
});

test("concurrent installers serialize on the inter-process lock and download once", async (t) => {
  const root = await tempRoot(t);
  let releaseDownload: (() => void) | undefined;
  let notifyOpened: (() => void) | undefined;
  const opened = new Promise<void>((resolve) => { notifyOpened = resolve; });
  const gate = new Promise<void>((resolve) => { releaseDownload = resolve; });
  let opens = 0;
  const transport: HttpTransport = {
    async open(): Promise<HttpResponse> {
      opens += 1;
      notifyOpened?.();
      await gate;
      return response(CONTENT_V1);
    }
  };
  const firstManager = manager(root, transport, contentProbe());
  const secondManager = manager(root, transport, contentProbe());
  const release = manifest("runtime-concurrent", "1.0.0", CONTENT_V1);

  const first = firstManager.install(release);
  await opened;
  const second = secondManager.install(release);
  releaseDownload?.();
  const [left, right] = await Promise.all([first, second]);

  assert.equal(left.executablePath, right.executablePath);
  assert.equal(opens, 1);
  await assert.rejects(readFile(path.join(root, ".install.lock")), (error: unknown) =>
    error instanceof Error && (error as NodeJS.ErrnoException).code === "ENOENT"
  );
});

test("install lock open errors distinguish Windows contention from unsafe paths", async (t) => {
  const root = await tempRoot(t);
  const lockPath = path.join(root, ".install.lock");
  const openError = (code: string): NodeJS.ErrnoException =>
    Object.assign(new Error(`synthetic ${code}`), { code });

  assert.equal(await classifyInstallLockOpenError(openError("EEXIST"), lockPath), "contended");
  assert.equal(await classifyInstallLockOpenError(openError("EIO"), lockPath), "fatal");

  await writeFile(lockPath, "live lock\n", "utf8");
  assert.equal(await classifyInstallLockOpenError(openError("EPERM"), lockPath), "contended");

  await rm(lockPath);
  assert.equal(await classifyInstallLockOpenError(openError("EACCES"), lockPath), "vanished");

  await mkdir(lockPath);
  assert.equal(await classifyInstallLockOpenError(openError("EBUSY"), lockPath), "fatal");
});

test("rollback validates and atomically returns to the last known good version", async (t) => {
  const root = await tempRoot(t);
  const transport = new ContentTransport(new Map([
    ["runtime-1", CONTENT_V1],
    ["runtime-2", CONTENT_V2]
  ]));
  const runtime = manager(root, transport, contentProbe());
  const first = await runtime.install(manifest("runtime-1", "1.0.0", CONTENT_V1));
  const second = await runtime.install(manifest("runtime-2", "2.0.0", CONTENT_V2));
  assert.equal((await runtime.getActive()).executablePath, second.executablePath);

  const rolledBack = await runtime.rollback();

  assert.equal(rolledBack.executablePath, first.executablePath);
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-1");
  assert.equal((await runtime.rollback()).artifactVersion, "runtime-2", "rollback pointer supports a validated recovery toggle");
});

test("install blocks signed or bundled downgrade while explicit rollback remains available", async (t) => {
  const root = await tempRoot(t);
  const transport = new ContentTransport(new Map([
    ["runtime-1", CONTENT_V1],
    ["runtime-2", CONTENT_V2]
  ]));
  const runtime = manager(root, transport, contentProbe());
  await runtime.install(manifest("runtime-1", "1.0.0", CONTENT_V1));
  await runtime.install(manifest("runtime-2", "2.0.0", CONTENT_V2));

  await assert.rejects(
    () => runtime.install(manifest("runtime-old-republished", "1.0.0", CONTENT_V1)),
    hasCode("DOWNGRADE_BLOCKED")
  );
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-2");
  assert.equal((await runtime.rollback()).artifactVersion, "runtime-1");
});

test("existing immutable version cannot be replaced by a different manifest", async (t) => {
  const root = await tempRoot(t);
  const transport = new ContentTransport(new Map([["runtime-immutable", CONTENT_V1]]));
  const runtime = manager(root, transport, contentProbe());
  await runtime.install(manifest("runtime-immutable", "1.0.0", CONTENT_V1));

  await assert.rejects(
    () => runtime.install(manifest("runtime-immutable", "1.0.0", CONTENT_V2)),
    hasCode("IMMUTABLE_VERSION_CONFLICT")
  );
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-immutable");
  assert.equal(transport.opens, 1);
});

test("failed executable validation never activates downloaded bytes", async (t) => {
  const root = await tempRoot(t);
  const runtime = manager(
    root,
    new ContentTransport(new Map([["runtime-bad-exe", CONTENT_V1]])),
    async () => ({ ok: false, reason: "not an Alysis Code CLI" })
  );
  await assert.rejects(
    () => runtime.install(manifest("runtime-bad-exe", "1.0.0", CONTENT_V1)),
    hasCode("EXECUTABLE_INVALID")
  );
  await assert.rejects(() => runtime.getActive(), hasCode("NO_ACTIVE_RUNTIME"));
  assert.deepEqual(await readdir(path.join(root, "versions")), []);
});

test("development override is explicitly non-production and must be absolute", () => {
  const managed = selectRuntime("/storage/runtime/alysis");
  assert.deepEqual(managed, {
    executablePath: "/storage/runtime/alysis",
    origin: "managed",
    production: true
  });
  const override = selectRuntime("/storage/runtime/alysis", "/work/dev/alysis");
  assert.equal(override.origin, "development-override");
  assert.equal(override.production, false);
  assert.match(override.warning ?? "", /not managed, verified, or supported/i);
  assert.throws(() => selectRuntime("/managed", "./workspace/alysis"), hasCode("PATH_SAFETY_VIOLATION"));
});

class ContentTransport implements HttpTransport {
  opens = 0;

  constructor(private readonly contentByRelease: ReadonlyMap<string, Buffer>) {}

  async open(url: URL): Promise<HttpResponse> {
    this.opens += 1;
    const release = url.pathname.split("/").filter(Boolean).at(-1) ?? "";
    const content = this.contentByRelease.get(release);
    if (!content) {
      return { statusCode: 404, headers: {}, body: chunks() };
    }
    return response(content);
  }
}

test("overlapping lock users inside one process serialize instead of timing out on their own lock", async (t) => {
  const root = await tempRoot(t);
  // A clock that jumps a minute per read: ANY real contention on the on-disk lock inside this
  // process fails its deadline check on the first retry, so the assertions below only pass when the
  // two operations never contend. removeStaleLock cannot help — the lock holder's pid is alive.
  let clock = 0;
  const runtime = manager(
    root,
    new ContentTransport(new Map([["runtime-lock", CONTENT_V1]])),
    contentProbe(),
    { lockTimeoutMs: 1, lockPollMs: 1, now: () => (clock += 60_000) }
  );

  const [installed, removed] = await Promise.all([
    runtime.install(manifest("runtime-lock", "1.0.0", CONTENT_V1)),
    runtime.cleanupOldVersions(2)
  ]);

  assert.equal(installed.artifactVersion, "runtime-lock");
  assert.deepEqual(removed, []);
  assert.equal((await runtime.getActive()).artifactVersion, "runtime-lock");
});

test("pointerFingerprint tracks the current pointer and is absent before any install", async (t) => {
  const root = await tempRoot(t);
  const runtime = manager(root, new ContentTransport(new Map([["runtime-fp", CONTENT_V1]])), contentProbe());

  assert.equal(await runtime.pointerFingerprint(), undefined);
  await runtime.install(manifest("runtime-fp", "1.0.0", CONTENT_V1));
  const fingerprint = await runtime.pointerFingerprint();
  assert.equal(typeof fingerprint, "string");
  assert.equal(await runtime.pointerFingerprint(), fingerprint, "an untouched pointer keeps its identity");
});

function manager(
  storageRoot: string,
  transport: HttpTransport,
  probe: ExecutableProbe,
  extra: Partial<ConstructorParameters<typeof ManagedCliRuntime>[0]> = {}
): ManagedCliRuntime {
  const base = baseOptions(storageRoot, transport, probe);
  return new ManagedCliRuntime({
    ...base,
    ...extra,
    releasePolicy: extra.releasePolicy ?? base.releasePolicy
  });
}

function baseOptions(storageRoot: string, transport: HttpTransport, executableProbe: ExecutableProbe) {
  return {
    storageRoot,
    platform: "linux" as const,
    arch: "x64",
    compatibility: {
      extensionVersion: "0.1.1",
      protocol: { min: "1.0.0", max: "1.9.0" }
    },
    releasePolicy: {
      sourceRepository: "https://source.example.com/alysis-code",
      provenanceIssuer: "https://issuer.example.com",
      provenanceWorkflow: "https://source.example.com/alysis-code/.github/workflows/release.yml"
    },
    trustedDownloadHosts: ["artifacts.example.com"],
    transport,
    executableProbe,
    lockPollMs: 1,
    lockTimeoutMs: 2_000
  };
}

function manifest(
  artifactVersion: string,
  cliVersion: string,
  content: Buffer,
  options: { declaredSize?: number; signature?: string } = {}
): ManagedCliManifest {
  const artifact: ManagedCliArtifact = {
    target: "linux-x64",
    url: `https://artifacts.example.com/${artifactVersion}`,
    sha256: createHash("sha256").update(content).digest("hex"),
    size: options.declaredSize ?? content.length,
    executable: "alysis",
    sbomSha256: "b".repeat(64),
    nativeSignature: {
      evidenceSha256: "c".repeat(64),
      policy: "not-applicable",
      signerIdentity: "not-applicable"
    },
    ...(options.signature ? { signature: options.signature } : {})
  };
  return {
    schemaVersion: 3,
    release: {
      tag: `v${cliVersion}`,
      sourceRepository: "https://source.example.com/alysis-code",
      sourceCommit: "a".repeat(40)
    },
    artifactVersion,
    cliVersion,
    compatibility: {
      extension: { min: "0.1.0", max: "0.2.0" },
      protocol: { min: "1.0.0", max: "1.9.0" },
      cli: { min: cliVersion, max: cliVersion }
    },
    signingKeyId: "test-release-key",
    provenance: {
      issuer: "https://issuer.example.com",
      builderId: `https://source.example.com/alysis-code/.github/workflows/release.yml@refs/tags/v${cliVersion}`
    },
    artifacts: [artifact]
  };
}

function contentProbe(): ExecutableProbe {
  return async (executablePath) => {
    const content = await readFile(executablePath, "utf8");
    if (content === CONTENT_V1.toString()) {
      return { ok: true, cliVersion: "1.0.0", protocolVersion: "1.0.0" };
    }
    if (content === CONTENT_V2.toString()) {
      return { ok: true, cliVersion: "2.0.0", protocolVersion: "1.0.0" };
    }
    return { ok: false, reason: "unknown executable" };
  };
}

function response(content: Buffer): HttpResponse {
  return {
    statusCode: 200,
    headers: { "content-length": String(content.length) },
    body: chunks(content)
  };
}

async function* chunks(...items: Buffer[]): AsyncGenerator<Uint8Array> {
  for (const item of items) {
    yield item;
  }
}

function hasCode(expected: string): (error: unknown) => boolean {
  return (error: unknown): boolean => error instanceof ManagedCliError && error.code === expected;
}

async function tempRoot(t: { after(callback: () => Promise<void>): void }): Promise<string> {
  const root = await mkdtemp(path.join(tmpdir(), "alysis-runtime-test-"));
  t.after(async () => { await rm(root, { recursive: true, force: true }); });
  return root;
}

test("isUnsafeHostname covers loopback, private, link-local, and their IPv6 spellings", () => {
  // Node's URL parser normalizes decimal/hex/octal IPv4 before this ever runs, so the dotted-quad
  // cases below also cover https://2130706433/ and friends.
  for (const unsafe of [
    "localhost", "app.localhost", "printer.local",
    "127.0.0.1", "127.1.2.3", "0.0.0.0",
    "10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.1",
    "169.254.169.254",            // cloud instance metadata
    "224.0.0.1", "255.255.255.255",
    "::1", "[::1]", "::",
    "fe80::1", "[fe80::1]",
    "fc00::1", "fd12:3456::1",    // unique local, the IPv6 RFC1918
    "::ffff:7f00:1",              // IPv4-mapped loopback, as Node normalizes it
    "[::ffff:7f00:1]",
    "::ffff:169.254.169.254"
  ]) {
    assert.equal(isUnsafeHostname(unsafe), true, unsafe);
  }

  for (const safe of [
    "example.com", "api.github.com", "objects.githubusercontent.com",
    "8.8.8.8", "1.1.1.1", "172.32.0.1", "192.169.0.1", "2606:4700::1111"
  ]) {
    assert.equal(isUnsafeHostname(safe), false, safe);
  }
});
