import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { constants as fsConstants } from "node:fs";
import {
  access,
  chmod,
  copyFile,
  lstat,
  mkdir,
  open,
  readFile,
  readdir,
  realpath,
  rename,
  rm,
  stat,
  unlink,
  writeFile
} from "node:fs/promises";
import https from "node:https";
import path from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const MANIFEST_SCHEMA_VERSION = 3;
const SHA256_PATTERN = /^[a-f0-9]{64}$/;
const WINDOWS_SIGNER_IDENTITY_PATTERN = /^sha256:[a-f0-9]{64}$/;
const APPLE_SIGNER_IDENTITY_PATTERN = /^Developer ID Application: [^\r\n]{1,384} \([A-Z0-9]{10}\)$/;
const SOURCE_COMMIT_PATTERN = /^[a-f0-9]{40}$/;
const RELEASE_TAG_PATTERN = /^v[0-9]+(?:\.[0-9]+){2}(?:[-+][A-Za-z0-9.-]+)?$/;
const SAFE_IDENTIFIER_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$/;
const SAFE_EXECUTABLE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
const REDIRECT_STATUS_CODES = new Set([301, 302, 303, 307, 308]);
const MAX_MANIFEST_ARTIFACTS = 12;
const DEFAULT_MAX_DOWNLOAD_BYTES = 256 * 1024 * 1024;

export type ManagedCliTarget =
  | "win32-x64"
  | "win32-arm64"
  | "darwin-x64"
  | "darwin-arm64"
  | "linux-x64"
  | "linux-arm64";

export type ManagedCliErrorCode =
  | "CANCELLED"
  | "INVALID_MANIFEST"
  | "INCOMPATIBLE_EXTENSION"
  | "INCOMPATIBLE_PROTOCOL"
  | "INCOMPATIBLE_CLI"
  | "UNSUPPORTED_TARGET"
  | "UNSAFE_URL"
  | "TOO_MANY_REDIRECTS"
  | "DOWNLOAD_FAILED"
  | "DOWNLOAD_TOO_LARGE"
  | "SIZE_MISMATCH"
  | "HASH_MISMATCH"
  | "SIGNATURE_REQUIRED"
  | "SIGNATURE_INVALID"
  | "DOWNGRADE_BLOCKED"
  | "LOCK_TIMEOUT"
  | "IMMUTABLE_VERSION_CONFLICT"
  | "EXECUTABLE_INVALID"
  | "BUNDLED_RUNTIME_INVALID"
  // Recoverable: the build simply ships no bundle, as opposed to shipping a broken one.
  | "BUNDLED_RUNTIME_MISSING"
  | "NO_ACTIVE_RUNTIME"
  | "NO_ROLLBACK_RUNTIME"
  | "PATH_SAFETY_VIOLATION"
  | "IO_ERROR";

export class ManagedCliError extends Error {
  constructor(
    readonly code: ManagedCliErrorCode,
    message: string,
    readonly cause?: unknown
  ) {
    super(message);
    this.name = "ManagedCliError";
  }
}

export interface VersionRange {
  min: string;
  max: string;
}

export interface ManagedCliArtifact {
  target: ManagedCliTarget;
  url: string;
  sha256: string;
  size: number;
  executable: string;
  sbomSha256: string;
  nativeSignature: ManagedCliNativeSignature;
  signature?: string;
}

export type ManagedCliNativeSignaturePolicy =
  | "authenticode"
  | "apple-developer-id-notarized"
  | "not-applicable";

export interface ManagedCliNativeSignature {
  evidenceSha256: string;
  policy: ManagedCliNativeSignaturePolicy;
  signerIdentity: string;
}

export interface ManagedCliReleaseIdentity {
  tag: string;
  sourceRepository: string;
  sourceCommit: string;
}

export interface ManagedCliProvenanceIdentity {
  issuer: string;
  builderId: string;
}

export interface ManagedCliManifest {
  schemaVersion: 3;
  release: ManagedCliReleaseIdentity;
  artifactVersion: string;
  cliVersion: string;
  compatibility: {
    extension: VersionRange;
    protocol: VersionRange;
    cli: VersionRange;
  };
  signingKeyId: string;
  provenance: ManagedCliProvenanceIdentity;
  artifacts: ManagedCliArtifact[];
}

export interface ManagedCliSignedRecord {
  schemaVersion: 3;
  release: ManagedCliReleaseIdentity;
  artifactVersion: string;
  cliVersion: string;
  compatibility: ManagedCliManifest["compatibility"];
  signingKeyId: string;
  provenance: ManagedCliProvenanceIdentity;
  artifact: Omit<ManagedCliArtifact, "signature">;
}

export interface RuntimeCompatibility {
  extensionVersion: string;
  protocol: VersionRange;
}

export interface ManagedCliReleasePolicy {
  sourceRepository: string;
  provenanceIssuer: string;
  provenanceWorkflow: string;
}

export interface HttpResponse {
  statusCode: number;
  headers: Readonly<Record<string, string | undefined>>;
  body: AsyncIterable<Uint8Array | string>;
  dispose?: () => void;
}

export interface HttpTransport {
  open(url: URL, signal?: AbortSignal): Promise<HttpResponse>;
}

export interface ExecutableProbeResult {
  ok: boolean;
  cliVersion?: string;
  protocolVersion?: string;
  reason?: string;
}

export type ExecutableProbe = (executablePath: string, signal?: AbortSignal) => Promise<ExecutableProbeResult>;

export interface SignatureVerificationInput {
  filePath: string;
  signature: string;
  record: ManagedCliSignedRecord;
  purpose: "install" | "installed";
  signal?: AbortSignal;
}

export type SignatureVerifier = (input: SignatureVerificationInput) => Promise<boolean>;

export interface ManagedCliRuntimeOptions {
  storageRoot: string;
  bundledRuntimeRoot?: string;
  compatibility: RuntimeCompatibility;
  releasePolicy: ManagedCliReleasePolicy;
  platform?: NodeJS.Platform;
  arch?: string;
  transport?: HttpTransport;
  executableProbe?: ExecutableProbe;
  signatureVerifier?: SignatureVerifier;
  requireSignature?: boolean;
  trustedDownloadHosts?: readonly string[];
  maxDownloadBytes?: number;
  maxRedirects?: number;
  lockTimeoutMs?: number;
  lockPollMs?: number;
  lockStaleMs?: number;
  isProcessAlive?: (pid: number) => boolean;
  now?: () => number;
  randomId?: () => string;
  delay?: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
}

export interface InstalledRuntime {
  release?: ManagedCliReleaseIdentity;
  artifactVersion: string;
  cliVersion: string;
  protocolVersion: string;
  target: ManagedCliTarget;
  executablePath: string;
  sha256: string;
  size: number;
  signatureVerified: boolean;
}

interface RuntimePointer {
  schemaVersion: 1;
  artifactVersion: string;
  cliVersion: string;
  protocolVersion: string;
  target: ManagedCliTarget;
  executable: string;
  sha256: string;
  size: number;
  signatureVerified: boolean;
  activatedAt: string;
}

interface InstalledMetadata {
  schemaVersion: 3;
  release: ManagedCliReleaseIdentity;
  artifactVersion: string;
  cliVersion: string;
  protocolVersion: string;
  target: ManagedCliTarget;
  executable: string;
  sha256: string;
  size: number;
  sourceUrl: string;
  compatibility: ManagedCliManifest["compatibility"];
  signingKeyId: string;
  provenance: ManagedCliProvenanceIdentity;
  sbomSha256: string;
  nativeSignature: ManagedCliNativeSignature;
  signature?: string;
  signatureVerified: boolean;
}

interface LockRecord {
  schemaVersion: 1;
  token: string;
  pid: number;
  createdAt: number;
}

export interface RuntimeSelection {
  executablePath: string;
  origin: "managed" | "development-override";
  production: boolean;
  warning?: string;
}

/**
 * Resolve the extension's managed-runtime target without guessing unsupported ABIs.
 */
export function resolveManagedCliTarget(
  platform: NodeJS.Platform = process.platform,
  arch: string = process.arch
): ManagedCliTarget {
  if ((platform === "win32" || platform === "darwin" || platform === "linux") &&
      (arch === "x64" || arch === "arm64")) {
    return `${platform}-${arch}` as ManagedCliTarget;
  }
  throw new ManagedCliError("UNSUPPORTED_TARGET", `Managed Alysis Code CLI is not published for ${platform}-${arch}.`);
}

/**
 * Parse a release manifest as a strict, closed schema. Unknown properties are rejected so a
 * misspelled security or compatibility field can never be silently ignored.
 */
export function parseManagedCliManifest(
  input: unknown,
  options: { trustedDownloadHosts?: readonly string[] } = {}
): ManagedCliManifest {
  const root = expectObject(input, "manifest");
  expectOnlyKeys(
    root,
    ["schemaVersion", "release", "artifactVersion", "cliVersion", "compatibility", "signingKeyId", "provenance", "artifacts"],
    "manifest"
  );
  if (root.schemaVersion !== MANIFEST_SCHEMA_VERSION) {
    invalidManifest(`manifest.schemaVersion must be ${MANIFEST_SCHEMA_VERSION}`);
  }
  const release = parseReleaseIdentity(root.release, "manifest.release");
  const artifactVersion = expectSafeIdentifier(root.artifactVersion, "manifest.artifactVersion");
  const cliVersion = expectVersion(root.cliVersion, "manifest.cliVersion");
  const compatibilityObject = expectObject(root.compatibility, "manifest.compatibility");
  expectOnlyKeys(compatibilityObject, ["extension", "protocol", "cli"], "manifest.compatibility");
  const compatibility = {
    extension: parseVersionRange(compatibilityObject.extension, "manifest.compatibility.extension"),
    protocol: parseVersionRange(compatibilityObject.protocol, "manifest.compatibility.protocol"),
    cli: parseVersionRange(compatibilityObject.cli, "manifest.compatibility.cli")
  };
  if (!versionInRange(cliVersion, compatibility.cli)) {
    invalidManifest("manifest.cliVersion is outside manifest.compatibility.cli");
  }
  if (release.tag !== `v${cliVersion}`) {
    invalidManifest("manifest.release.tag must identify manifest.cliVersion");
  }
  const signingKeyId = expectSafeIdentifier(root.signingKeyId, "manifest.signingKeyId");
  const provenance = parseProvenanceIdentity(root.provenance, "manifest.provenance");
  if (!Array.isArray(root.artifacts) || root.artifacts.length === 0 || root.artifacts.length > MAX_MANIFEST_ARTIFACTS) {
    invalidManifest(`manifest.artifacts must contain between 1 and ${MAX_MANIFEST_ARTIFACTS} entries`);
  }
  const targets = new Set<string>();
  const artifacts = root.artifacts.map((rawArtifact, index) => {
    const label = `manifest.artifacts[${index}]`;
    const object = expectObject(rawArtifact, label);
    expectOnlyKeys(
      object,
      ["target", "url", "sha256", "size", "executable", "sbomSha256", "nativeSignature", "signature"],
      label
    );
    const target = expectTarget(object.target, `${label}.target`);
    if (targets.has(target)) {
      invalidManifest(`manifest contains duplicate target ${target}`);
    }
    targets.add(target);
    const url = expectString(object.url, `${label}.url`);
    validateArtifactUrl(url, options.trustedDownloadHosts);
    const sha256 = expectString(object.sha256, `${label}.sha256`).toLowerCase();
    if (!SHA256_PATTERN.test(sha256)) {
      invalidManifest(`${label}.sha256 must be a lowercase 64-character SHA-256 digest`);
    }
    const size = object.size;
    if (!Number.isSafeInteger(size) || Number(size) <= 0) {
      invalidManifest(`${label}.size must be a positive safe integer`);
    }
    const executable = expectString(object.executable, `${label}.executable`);
    if (!SAFE_EXECUTABLE_PATTERN.test(executable) || path.posix.basename(executable) !== executable || path.win32.basename(executable) !== executable) {
      invalidManifest(`${label}.executable must be a safe filename, not a path`);
    }
    const sbomSha256 = expectString(object.sbomSha256, `${label}.sbomSha256`).toLowerCase();
    if (!SHA256_PATTERN.test(sbomSha256)) {
      invalidManifest(`${label}.sbomSha256 must be a lowercase 64-character SHA-256 digest`);
    }
    const nativeSignature = parseNativeSignature(object.nativeSignature, target, `${label}.nativeSignature`);
    const signature = object.signature === undefined ? undefined : expectString(object.signature, `${label}.signature`);
    if (signature !== undefined && (signature.length < 16 || signature.length > 16_384)) {
      invalidManifest(`${label}.signature has an invalid length`);
    }
    return { target, url, sha256, size: Number(size), executable, sbomSha256, nativeSignature, signature };
  });
  return {
    schemaVersion: 3,
    release,
    artifactVersion,
    cliVersion,
    compatibility,
    signingKeyId,
    provenance,
    artifacts
  };
}

export function managedCliSignedRecord(
  manifest: ManagedCliManifest,
  artifact: ManagedCliArtifact
): ManagedCliSignedRecord {
  const { signature: _signature, ...unsignedArtifact } = artifact;
  return {
    schemaVersion: 3,
    release: manifest.release,
    artifactVersion: manifest.artifactVersion,
    cliVersion: manifest.cliVersion,
    compatibility: manifest.compatibility,
    signingKeyId: manifest.signingKeyId,
    provenance: manifest.provenance,
    artifact: unsignedArtifact
  };
}

export function selectRuntime(
  managedExecutablePath: string,
  developmentOverride?: string
): RuntimeSelection {
  const override = developmentOverride?.trim();
  if (!override) {
    return { executablePath: managedExecutablePath, origin: "managed", production: true };
  }
  if (!path.isAbsolute(override) || override.includes("\0")) {
    throw new ManagedCliError("PATH_SAFETY_VIOLATION", "A development CLI override must be an absolute filesystem path.");
  }
  return {
    executablePath: path.normalize(override),
    origin: "development-override",
    production: false,
    warning: "Development CLI override is active. This runtime is not managed, verified, or supported for production."
  };
}

export class ManagedCliRuntime {
  private readonly root: string;
  private readonly versionsRoot: string;
  private readonly tempRoot: string;
  private readonly currentPointerPath: string;
  private readonly lastKnownGoodPointerPath: string;
  private readonly lockPath: string;
  private readonly target: ManagedCliTarget;
  private readonly transport: HttpTransport;
  private readonly probe: ExecutableProbe;
  private readonly now: () => number;
  private readonly randomId: () => string;
  private readonly delay: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
  /** Serializes this process's own lock acquisitions; the file lock stays the cross-process barrier. */
  private inProcessLock: Promise<void> = Promise.resolve();

  constructor(private readonly options: ManagedCliRuntimeOptions) {
    if (!path.isAbsolute(options.storageRoot) || options.storageRoot.includes("\0")) {
      throw new ManagedCliError("PATH_SAFETY_VIOLATION", "Managed runtime storageRoot must be an absolute path.");
    }
    this.root = path.resolve(options.storageRoot);
    this.versionsRoot = path.join(this.root, "versions");
    this.tempRoot = path.join(this.root, ".tmp");
    this.currentPointerPath = path.join(this.root, "current.json");
    this.lastKnownGoodPointerPath = path.join(this.root, "last-known-good.json");
    this.lockPath = path.join(this.root, ".install.lock");
    this.target = resolveManagedCliTarget(options.platform, options.arch);
    this.transport = options.transport ?? new HttpsTransport();
    this.probe = options.executableProbe ?? createDefaultExecutableProbe();
    this.now = options.now ?? Date.now;
    this.randomId = options.randomId ?? randomUUID;
    this.delay = options.delay ?? cancellableDelay;
    validateRange(options.compatibility.protocol, "runtime protocol compatibility");
    expectVersion(options.compatibility.extensionVersion, "runtime extension version");
    expectHttpsIdentity(options.releasePolicy.sourceRepository, "runtime release-policy source repository");
    expectHttpsIdentity(options.releasePolicy.provenanceIssuer, "runtime release-policy provenance issuer");
    expectHttpsIdentity(options.releasePolicy.provenanceWorkflow, "runtime release-policy provenance workflow");
  }

  async install(rawManifest: unknown, signal?: AbortSignal): Promise<InstalledRuntime> {
    throwIfCancelled(signal);
    const manifest = parseManagedCliManifest(rawManifest, {
      trustedDownloadHosts: this.options.trustedDownloadHosts
    });
    this.validateCompatibility(manifest);
    const artifact = manifest.artifacts.find((candidate) => candidate.target === this.target);
    if (!artifact) {
      throw new ManagedCliError("UNSUPPORTED_TARGET", `Release ${manifest.artifactVersion} has no ${this.target} artifact.`);
    }
    await this.ensureRoots();
    return await this.withInstallLock(async () => {
      throwIfCancelled(signal);
      await this.assertNotDowngrade(manifest, signal);
      const existing = await this.readInstalledMetadata(manifest.artifactVersion, this.target);
      if (existing) {
        this.assertImmutable(existing, manifest, artifact);
        const installed = await this.validateInstalled(existing, signal);
        await this.activate(installed);
        return installed;
      }
      return await this.prepareValidateAndActivate(
        manifest,
        artifact,
        (partialPath) => this.download(artifact, partialPath, signal),
        signal
      );
    }, signal);
  }

  async installBundled(
    rawManifest: unknown,
    bundledExecutablePath: string,
    signal?: AbortSignal
  ): Promise<InstalledRuntime> {
    throwIfCancelled(signal);
    const manifest = parseManagedCliManifest(rawManifest, {
      trustedDownloadHosts: this.options.trustedDownloadHosts
    });
    this.validateCompatibility(manifest);
    const artifact = manifest.artifacts.find((candidate) => candidate.target === this.target);
    if (!artifact) {
      throw new ManagedCliError(
        "UNSUPPORTED_TARGET",
        `Bundled release ${manifest.artifactVersion} has no ${this.target} artifact.`
      );
    }
    const bundledSource = await this.validateBundledSource(bundledExecutablePath, artifact);
    await this.ensureRoots();
    return await this.withInstallLock(async () => {
      throwIfCancelled(signal);
      await this.assertNotDowngrade(manifest, signal);
      const existing = await this.readInstalledMetadata(manifest.artifactVersion, this.target);
      if (existing) {
        this.assertImmutable(existing, manifest, artifact);
        const installed = await this.validateInstalled(existing, signal);
        await this.activate(installed);
        return installed;
      }
      return await this.prepareValidateAndActivate(
        manifest,
        artifact,
        async (partialPath) => {
          await copyFile(bundledSource, partialPath, fsConstants.COPYFILE_EXCL);
          await verifyFile(partialPath, artifact.size, artifact.sha256);
          const handle = await open(partialPath, "r+");
          try {
            await handle.sync();
          } finally {
            await handle.close();
          }
        },
        signal
      );
    }, signal);
  }

  async getActive(signal?: AbortSignal): Promise<InstalledRuntime> {
    throwIfCancelled(signal);
    const pointer = await this.readPointer(this.currentPointerPath);
    if (!pointer) {
      throw new ManagedCliError("NO_ACTIVE_RUNTIME", "No managed Alysis Code CLI runtime is active.");
    }
    return await this.validatePointer(pointer, signal);
  }

  /**
   * Cheap identity of the current pointer (size + mtime). Callers use it to skip a full hash/signature
   * revalidation when nothing on disk changed since the last successful validation; it is never a
   * substitute for validation itself, only a way to avoid repeating one that already passed.
   */
  async pointerFingerprint(): Promise<string | undefined> {
    try {
      const info = await stat(this.currentPointerPath);
      if (!info.isFile()) {
        return undefined;
      }
      return `${info.size}:${info.mtimeMs}`;
    } catch {
      return undefined;
    }
  }

  async rollback(signal?: AbortSignal): Promise<InstalledRuntime> {
    await this.ensureRoots();
    return await this.withInstallLock(async () => {
      throwIfCancelled(signal);
      const fallback = await this.readPointer(this.lastKnownGoodPointerPath);
      if (!fallback) {
        throw new ManagedCliError("NO_ROLLBACK_RUNTIME", "No last-known-good managed runtime is available.");
      }
      const validatedFallback = await this.validatePointer(fallback, signal);
      const current = await this.readPointer(this.currentPointerPath);
      await this.writePointer(this.currentPointerPath, fallback);
      if (current) {
        try {
          await this.validatePointer(current, signal);
          await this.writePointer(this.lastKnownGoodPointerPath, current);
        } catch (error) {
          if (error instanceof ManagedCliError && error.code === "CANCELLED") {
            throw error;
          }
          // An invalid current runtime must never replace a known-good rollback target.
        }
      }
      return validatedFallback;
    }, signal);
  }

  async cleanupOldVersions(maxRetained: number): Promise<string[]> {
    if (!Number.isSafeInteger(maxRetained) || maxRetained < 0) {
      throw new ManagedCliError("PATH_SAFETY_VIOLATION", "maxRetained must be a non-negative integer.");
    }
    await this.ensureRoots();
    return await this.withInstallLock(async () => {
      const protectedVersions = new Set<string>();
      for (const pointerPath of [this.currentPointerPath, this.lastKnownGoodPointerPath]) {
        const pointer = await this.readPointer(pointerPath);
        if (pointer) {
          protectedVersions.add(pointer.artifactVersion);
        }
      }
      const entries = await readdir(this.versionsRoot, { withFileTypes: true });
      const removable: Array<{ name: string; modified: number }> = [];
      for (const entry of entries) {
        if (!entry.isDirectory() || entry.isSymbolicLink() || !SAFE_IDENTIFIER_PATTERN.test(entry.name) || protectedVersions.has(entry.name)) {
          continue;
        }
        const ownedPath = this.ownedVersionPath(entry.name);
        const info = await stat(ownedPath);
        removable.push({ name: entry.name, modified: info.mtimeMs });
      }
      removable.sort((left, right) => right.modified - left.modified || left.name.localeCompare(right.name));
      const removed: string[] = [];
      for (const candidate of removable.slice(maxRetained)) {
        const ownedPath = this.ownedVersionPath(candidate.name);
        await rm(ownedPath, { recursive: true, force: false });
        removed.push(candidate.name);
      }
      return removed;
    });
  }

  private validateCompatibility(manifest: ManagedCliManifest): void {
    const expectedBuilder = `${this.options.releasePolicy.provenanceWorkflow}@refs/tags/${manifest.release.tag}`;
    if (manifest.release.sourceRepository !== this.options.releasePolicy.sourceRepository ||
        manifest.provenance.issuer !== this.options.releasePolicy.provenanceIssuer ||
        manifest.provenance.builderId !== expectedBuilder) {
      throw new ManagedCliError(
        "INVALID_MANIFEST",
        `Runtime ${manifest.artifactVersion} does not match the pinned release-source and provenance policy.`
      );
    }
    if (!versionInRange(this.options.compatibility.extensionVersion, manifest.compatibility.extension)) {
      throw new ManagedCliError(
        "INCOMPATIBLE_EXTENSION",
        `Extension ${this.options.compatibility.extensionVersion} is not supported by runtime ${manifest.artifactVersion}.`
      );
    }
    if (!rangesOverlap(this.options.compatibility.protocol, manifest.compatibility.protocol)) {
      throw new ManagedCliError(
        "INCOMPATIBLE_PROTOCOL",
        `Runtime ${manifest.artifactVersion} does not share a supported IDE protocol version with this extension.`
      );
    }
    if (!versionInRange(manifest.cliVersion, manifest.compatibility.cli)) {
      throw new ManagedCliError("INCOMPATIBLE_CLI", `CLI ${manifest.cliVersion} is outside its declared compatibility range.`);
    }
  }

  private async assertNotDowngrade(
    manifest: ManagedCliManifest,
    signal?: AbortSignal
  ): Promise<void> {
    const current = await this.readPointer(this.currentPointerPath);
    if (!current) {
      return;
    }
    try {
      await this.validatePointer(current, signal);
    } catch (error) {
      if (error instanceof ManagedCliError && error.code === "CANCELLED") {
        throw error;
      }
      // A broken pointer is handled by the coordinator's validated recovery path and must not
      // become a forged high-water mark that permanently prevents a clean repair install.
      return;
    }
    if (compareVersions(manifest.cliVersion, current.cliVersion) < 0) {
      throw new ManagedCliError(
        "DOWNGRADE_BLOCKED",
        `Managed CLI ${manifest.cliVersion} is older than active CLI ${current.cliVersion}. Use the explicit validated rollback operation instead.`
      );
    }
  }

  private async ensureRoots(): Promise<void> {
    try {
      await mkdir(this.versionsRoot, { recursive: true, mode: 0o700 });
      await mkdir(this.tempRoot, { recursive: true, mode: 0o700 });
    } catch (error) {
      throw mapIoError("Could not initialize managed runtime storage.", error);
    }
  }

  private async prepareValidateAndActivate(
    manifest: ManagedCliManifest,
    artifact: ManagedCliArtifact,
    preparePartial: (partialPath: string) => Promise<void>,
    signal?: AbortSignal
  ): Promise<InstalledRuntime> {
    const nonce = safeNonce(this.randomId());
    const partialPath = this.ownedTempPath(`download-${nonce}.part`);
    const stagingPath = this.ownedTempPath(`stage-${nonce}`);
    try {
      await preparePartial(partialPath);
      const signatureVerified = await this.verifySignature(manifest, artifact, partialPath, signal);
      throwIfCancelled(signal);
      await mkdir(stagingPath, { mode: 0o700 });
      const stagingTargetPath = path.join(stagingPath, artifact.target);
      assertDirectChild(stagingPath, stagingTargetPath);
      await mkdir(stagingTargetPath, { mode: 0o700 });
      const stagedExecutable = path.join(stagingTargetPath, artifact.executable);
      assertDirectChild(stagingTargetPath, stagedExecutable);
      await rename(partialPath, stagedExecutable);
      if (this.target.startsWith("win32-") === false) {
        await chmod(stagedExecutable, 0o700);
      }
      const probe = await this.validateExecutable(stagedExecutable, manifest, signal);
      const metadata: InstalledMetadata = {
        schemaVersion: 3,
        release: manifest.release,
        artifactVersion: manifest.artifactVersion,
        cliVersion: manifest.cliVersion,
        protocolVersion: probe.protocolVersion,
        target: artifact.target,
        executable: artifact.executable,
        sha256: artifact.sha256,
        size: artifact.size,
        sourceUrl: artifact.url,
        compatibility: manifest.compatibility,
        signingKeyId: manifest.signingKeyId,
        provenance: manifest.provenance,
        sbomSha256: artifact.sbomSha256,
        nativeSignature: artifact.nativeSignature,
        signature: artifact.signature,
        signatureVerified
      };
      await writeFile(path.join(stagingTargetPath, "runtime.json"), `${JSON.stringify(metadata)}\n`, {
        encoding: "utf8",
        flag: "wx",
        mode: 0o600
      });
      const versionPath = this.ownedVersionPath(manifest.artifactVersion);
      await mkdir(versionPath, { recursive: true, mode: 0o700 });
      const installedTargetPath = path.join(versionPath, artifact.target);
      assertDirectChild(versionPath, installedTargetPath);
      try {
        await rename(stagingTargetPath, installedTargetPath);
      } catch (error) {
        if (!isNodeError(error, "EEXIST") && !isNodeError(error, "ENOTEMPTY")) {
          throw error;
        }
        const existing = await this.readInstalledMetadata(manifest.artifactVersion, this.target);
        if (!existing) {
          throw new ManagedCliError("IMMUTABLE_VERSION_CONFLICT", `Runtime ${manifest.artifactVersion} already exists without valid metadata.`);
        }
        this.assertImmutable(existing, manifest, artifact);
      }
      const installedMetadata = await this.readInstalledMetadata(manifest.artifactVersion, this.target);
      if (!installedMetadata) {
        throw new ManagedCliError("IO_ERROR", "Promoted runtime metadata could not be read.");
      }
      const installed = await this.validateInstalled(installedMetadata, signal);
      await this.activate(installed);
      return installed;
    } catch (error) {
      if (error instanceof ManagedCliError) {
        throw error;
      }
      throw mapIoError(`Could not install managed runtime ${manifest.artifactVersion}.`, error);
    } finally {
      await this.removeOwnedTempPath(partialPath);
      await this.removeOwnedTempPath(stagingPath);
    }
  }

  private async validateBundledSource(
    bundledExecutablePath: string,
    artifact: ManagedCliArtifact
  ): Promise<string> {
    const configuredRoot = this.options.bundledRuntimeRoot;
    if (!configuredRoot || !path.isAbsolute(configuredRoot) || configuredRoot.includes("\0")) {
      throw new ManagedCliError(
        "BUNDLED_RUNTIME_INVALID",
        "Bundled runtime installation is unavailable because no trusted bundle root is configured."
      );
    }
    if (!path.isAbsolute(bundledExecutablePath) || bundledExecutablePath.includes("\0")) {
      throw new ManagedCliError(
        "BUNDLED_RUNTIME_INVALID",
        "Bundled runtime executable must be an absolute path."
      );
    }
    try {
      const [rootReal, sourceReal] = await Promise.all([
        realpath(configuredRoot),
        realpath(bundledExecutablePath)
      ]);
      const relative = path.relative(rootReal, sourceReal);
      if (
        !relative ||
        relative.startsWith(`..${path.sep}`) ||
        relative === ".." ||
        path.isAbsolute(relative) ||
        path.dirname(relative) !== "." ||
        path.basename(sourceReal) !== artifact.executable
      ) {
        throw new ManagedCliError(
          "BUNDLED_RUNTIME_INVALID",
          "Bundled runtime executable is outside the trusted bundle root or has an unexpected name."
        );
      }
      return sourceReal;
    } catch (error) {
      if (error instanceof ManagedCliError) {
        throw error;
      }
      throw new ManagedCliError(
        "BUNDLED_RUNTIME_INVALID",
        "Bundled runtime executable is missing or cannot be resolved safely.",
        error
      );
    }
  }

  private async download(artifact: ManagedCliArtifact, outputPath: string, signal?: AbortSignal): Promise<void> {
    const maxBytes = Math.min(this.options.maxDownloadBytes ?? DEFAULT_MAX_DOWNLOAD_BYTES, DEFAULT_MAX_DOWNLOAD_BYTES);
    if (!Number.isSafeInteger(maxBytes) || maxBytes <= 0) {
      throw new ManagedCliError("DOWNLOAD_TOO_LARGE", "The configured download byte limit is invalid.");
    }
    if (artifact.size > maxBytes) {
      throw new ManagedCliError("DOWNLOAD_TOO_LARGE", `Artifact declares ${artifact.size} bytes, above the ${maxBytes} byte limit.`);
    }
    const response = await this.openWithRedirects(new URL(artifact.url), signal);
    if (response.statusCode !== 200) {
      throw new ManagedCliError("DOWNLOAD_FAILED", `Artifact download returned HTTP ${response.statusCode}.`);
    }
    const declaredLength = parseContentLength(response.headers["content-length"]);
    if (declaredLength !== undefined && declaredLength !== artifact.size) {
      throw new ManagedCliError("SIZE_MISMATCH", `Artifact Content-Length ${declaredLength} does not match manifest size ${artifact.size}.`);
    }
    const handle = await open(outputPath, "wx", 0o600);
    const hash = createHash("sha256");
    let bytes = 0;
    try {
      for await (const rawChunk of response.body) {
        throwIfCancelled(signal);
        const chunk = typeof rawChunk === "string" ? Buffer.from(rawChunk) : Buffer.from(rawChunk);
        bytes += chunk.length;
        if (bytes > artifact.size || bytes > maxBytes) {
          throw new ManagedCliError("DOWNLOAD_TOO_LARGE", "Artifact stream exceeded its declared or configured byte limit.");
        }
        hash.update(chunk);
        await handle.write(chunk);
      }
      await handle.sync();
    } finally {
      await handle.close();
    }
    if (bytes !== artifact.size) {
      throw new ManagedCliError("SIZE_MISMATCH", `Downloaded ${bytes} bytes; manifest declares ${artifact.size}.`);
    }
    if (hash.digest("hex") !== artifact.sha256) {
      throw new ManagedCliError("HASH_MISMATCH", "Downloaded Alysis Code CLI failed SHA-256 verification.");
    }
  }

  private async openWithRedirects(initialUrl: URL, signal?: AbortSignal): Promise<HttpResponse> {
    const maxRedirects = this.options.maxRedirects ?? 5;
    let current = initialUrl;
    for (let redirect = 0; redirect <= maxRedirects; redirect += 1) {
      throwIfCancelled(signal);
      validateArtifactUrl(current.toString(), this.options.trustedDownloadHosts);
      let response: HttpResponse;
      try {
        response = await this.transport.open(current, signal);
      } catch (error) {
        if (isAbortError(error) || signal?.aborted) {
          throw new ManagedCliError("CANCELLED", "Managed runtime installation was cancelled.", error);
        }
        throw new ManagedCliError("DOWNLOAD_FAILED", `Could not download ${redactUrl(current)}.`, error);
      }
      if (!REDIRECT_STATUS_CODES.has(response.statusCode)) {
        return response;
      }
      response.dispose?.();
      if (redirect === maxRedirects) {
        throw new ManagedCliError("TOO_MANY_REDIRECTS", "Artifact download exceeded the redirect limit.");
      }
      const location = response.headers.location;
      if (!location) {
        throw new ManagedCliError("DOWNLOAD_FAILED", "Artifact redirect did not include a Location header.");
      }
      let redirected: URL;
      try {
        redirected = new URL(location, current);
      } catch (error) {
        throw new ManagedCliError("UNSAFE_URL", "Artifact redirect Location is not a valid URL.", error);
      }
      validateArtifactUrl(redirected.toString(), this.options.trustedDownloadHosts);
      current = redirected;
    }
    throw new ManagedCliError("TOO_MANY_REDIRECTS", "Artifact download exceeded the redirect limit.");
  }

  private async verifySignature(
    manifest: ManagedCliManifest,
    artifact: ManagedCliArtifact,
    filePath: string,
    signal?: AbortSignal
  ): Promise<boolean> {
    throwIfCancelled(signal);
    if (!artifact.signature) {
      if (this.options.requireSignature) {
        throw new ManagedCliError("SIGNATURE_REQUIRED", "Release policy requires a signed managed CLI artifact.");
      }
      return false;
    }
    if (!this.options.signatureVerifier) {
      if (this.options.requireSignature) {
        throw new ManagedCliError("SIGNATURE_REQUIRED", "A signed artifact was supplied but no release signature verifier is configured.");
      }
      return false;
    }
    const valid = await this.options.signatureVerifier({
      filePath,
      signature: artifact.signature,
      record: managedCliSignedRecord(manifest, artifact),
      purpose: "install",
      signal
    });
    throwIfCancelled(signal);
    if (!valid) {
      throw new ManagedCliError("SIGNATURE_INVALID", "Managed CLI artifact signature verification failed.");
    }
    return true;
  }

  private async validateExecutable(
    executablePath: string,
    manifest: ManagedCliManifest,
    signal?: AbortSignal
  ): Promise<{ cliVersion: string; protocolVersion: string }> {
    throwIfCancelled(signal);
    let result: ExecutableProbeResult;
    try {
      result = await this.probe(executablePath, signal);
    } catch (error) {
      if (isAbortError(error) || signal?.aborted) {
        throw new ManagedCliError("CANCELLED", "Managed runtime validation was cancelled.", error);
      }
      throw new ManagedCliError("EXECUTABLE_INVALID", "Managed CLI executable probe failed.", error);
    }
    if (!result.ok || !result.cliVersion || !result.protocolVersion) {
      throw new ManagedCliError("EXECUTABLE_INVALID", result.reason ?? "Managed CLI executable did not pass its health probe.");
    }
    if (compareVersions(result.cliVersion, manifest.cliVersion) !== 0) {
      throw new ManagedCliError(
        "INCOMPATIBLE_CLI",
        `Downloaded executable reports CLI ${result.cliVersion}; manifest declares ${manifest.cliVersion}.`
      );
    }
    if (!versionInRange(result.protocolVersion, manifest.compatibility.protocol) ||
        !versionInRange(result.protocolVersion, this.options.compatibility.protocol)) {
      throw new ManagedCliError(
        "INCOMPATIBLE_PROTOCOL",
        `Downloaded executable protocol ${result.protocolVersion} is not supported by this extension.`
      );
    }
    return { cliVersion: result.cliVersion, protocolVersion: result.protocolVersion };
  }

  private async activate(runtime: InstalledRuntime): Promise<void> {
    const next = pointerFromRuntime(runtime, this.root, this.now());
    const current = await this.readPointer(this.currentPointerPath);
    if (current && (current.artifactVersion !== next.artifactVersion || current.target !== next.target)) {
      try {
        await this.validatePointer(current);
        await this.writePointer(this.lastKnownGoodPointerPath, current);
      } catch (error) {
        if (error instanceof ManagedCliError && error.code === "CANCELLED") {
          throw error;
        }
        // Do not persist a broken current pointer as the rollback target.
      }
    }
    await this.writePointer(this.currentPointerPath, next);
  }

  private async validateInstalled(metadata: InstalledMetadata, signal?: AbortSignal): Promise<InstalledRuntime> {
    const executablePath = this.executablePath(metadata.artifactVersion, metadata.target, metadata.executable);
    await verifyFile(executablePath, metadata.size, metadata.sha256);
    const artifact: ManagedCliArtifact = {
      target: metadata.target,
      url: metadata.sourceUrl,
      sha256: metadata.sha256,
      size: metadata.size,
      executable: metadata.executable,
      sbomSha256: metadata.sbomSha256,
      nativeSignature: metadata.nativeSignature,
      signature: metadata.signature
    };
    const manifestShape: ManagedCliManifest = {
      schemaVersion: 3,
      release: metadata.release,
      artifactVersion: metadata.artifactVersion,
      cliVersion: metadata.cliVersion,
      compatibility: metadata.compatibility,
      signingKeyId: metadata.signingKeyId,
      provenance: metadata.provenance,
      artifacts: [artifact]
    };
    this.validateCompatibility(manifestShape);
    if (this.options.requireSignature) {
      if (!metadata.signatureVerified || !metadata.signature || !this.options.signatureVerifier) {
        throw new ManagedCliError(
          "SIGNATURE_REQUIRED",
          "Managed runtime cannot be authenticated by the configured release-signature policy."
        );
      }
      const signatureValid = await this.options.signatureVerifier({
        filePath: executablePath,
        signature: metadata.signature,
        record: managedCliSignedRecord(manifestShape, artifact),
        purpose: "installed",
        signal
      });
      throwIfCancelled(signal);
      if (!signatureValid) {
        throw new ManagedCliError("SIGNATURE_INVALID", "Managed runtime release signature is invalid.");
      }
    }
    const probe = await this.validateExecutable(executablePath, manifestShape, signal);
    return {
      release: metadata.release,
      artifactVersion: metadata.artifactVersion,
      cliVersion: probe.cliVersion,
      protocolVersion: probe.protocolVersion,
      target: metadata.target,
      executablePath,
      sha256: metadata.sha256,
      size: metadata.size,
      signatureVerified: metadata.signatureVerified
    };
  }

  private async validatePointer(pointer: RuntimePointer, signal?: AbortSignal): Promise<InstalledRuntime> {
    const metadata = await this.readInstalledMetadata(pointer.artifactVersion, pointer.target);
    if (!metadata ||
        metadata.cliVersion !== pointer.cliVersion ||
        metadata.protocolVersion !== pointer.protocolVersion ||
        metadata.executable !== pointer.executable ||
        metadata.sha256 !== pointer.sha256 ||
        metadata.size !== pointer.size ||
        metadata.signatureVerified !== pointer.signatureVerified) {
      throw new ManagedCliError("EXECUTABLE_INVALID", "Managed runtime pointer does not match immutable installed metadata.");
    }
    return await this.validateInstalled(metadata, signal);
  }

  private assertImmutable(
    metadata: InstalledMetadata,
    manifest: ManagedCliManifest,
    artifact: ManagedCliArtifact
  ): void {
    if (metadata.release.tag !== manifest.release.tag ||
        metadata.release.sourceRepository !== manifest.release.sourceRepository ||
        metadata.release.sourceCommit !== manifest.release.sourceCommit ||
        metadata.artifactVersion !== manifest.artifactVersion ||
        metadata.cliVersion !== manifest.cliVersion ||
        !sameVersionRange(metadata.compatibility.extension, manifest.compatibility.extension) ||
        !sameVersionRange(metadata.compatibility.protocol, manifest.compatibility.protocol) ||
        !sameVersionRange(metadata.compatibility.cli, manifest.compatibility.cli) ||
        metadata.signingKeyId !== manifest.signingKeyId ||
        metadata.provenance.issuer !== manifest.provenance.issuer ||
        metadata.provenance.builderId !== manifest.provenance.builderId ||
        metadata.target !== artifact.target ||
        metadata.executable !== artifact.executable ||
        metadata.sha256 !== artifact.sha256 ||
        metadata.sbomSha256 !== artifact.sbomSha256 ||
        metadata.nativeSignature.evidenceSha256 !== artifact.nativeSignature.evidenceSha256 ||
        metadata.nativeSignature.policy !== artifact.nativeSignature.policy ||
        metadata.nativeSignature.signerIdentity !== artifact.nativeSignature.signerIdentity ||
        metadata.size !== artifact.size ||
        metadata.sourceUrl !== artifact.url ||
        metadata.signature !== artifact.signature ||
        (this.options.requireSignature && !metadata.signatureVerified)) {
      throw new ManagedCliError(
        "IMMUTABLE_VERSION_CONFLICT",
        `Installed runtime ${manifest.artifactVersion} differs from the immutable release manifest.`
      );
    }
  }

  private async readInstalledMetadata(
    artifactVersion: string,
    target: ManagedCliTarget
  ): Promise<InstalledMetadata | undefined> {
    const versionPath = this.ownedVersionPath(artifactVersion);
    const targetPath = path.join(versionPath, target);
    assertDirectChild(versionPath, targetPath);
    const metadataPath = path.join(targetPath, "runtime.json");
    assertDirectChild(targetPath, metadataPath);
    const raw = await readTextIfExists(metadataPath);
    if (raw === undefined) {
      return undefined;
    }
    try {
      return parseInstalledMetadata(
        JSON.parse(raw),
        artifactVersion,
        target,
        this.options.trustedDownloadHosts
      );
    } catch (error) {
      if (error instanceof ManagedCliError) {
        throw error;
      }
      throw new ManagedCliError("IMMUTABLE_VERSION_CONFLICT", "Installed runtime metadata is malformed.", error);
    }
  }

  private executablePath(artifactVersion: string, target: ManagedCliTarget, executable: string): string {
    const versionPath = this.ownedVersionPath(artifactVersion);
    const targetPath = path.join(versionPath, target);
    assertDirectChild(versionPath, targetPath);
    const executablePath = path.join(targetPath, executable);
    assertDirectChild(targetPath, executablePath);
    return executablePath;
  }

  private ownedVersionPath(artifactVersion: string): string {
    if (!SAFE_IDENTIFIER_PATTERN.test(artifactVersion)) {
      throw new ManagedCliError("PATH_SAFETY_VIOLATION", "Unsafe managed runtime version path.");
    }
    const candidate = path.join(this.versionsRoot, artifactVersion);
    assertDirectChild(this.versionsRoot, candidate);
    return candidate;
  }

  private ownedTempPath(name: string): string {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,180}$/.test(name)) {
      throw new ManagedCliError("PATH_SAFETY_VIOLATION", "Unsafe managed runtime temporary path.");
    }
    const candidate = path.join(this.tempRoot, name);
    assertDirectChild(this.tempRoot, candidate);
    return candidate;
  }

  private async removeOwnedTempPath(candidate: string): Promise<void> {
    assertDirectChild(this.tempRoot, candidate);
    try {
      await rm(candidate, { recursive: true, force: true });
    } catch {
      // Best-effort cleanup. A future install only deletes independently validated owned paths.
    }
  }

  private async readPointer(pointerPath: string): Promise<RuntimePointer | undefined> {
    assertDirectChild(this.root, pointerPath);
    const raw = await readTextIfExists(pointerPath);
    if (raw === undefined) {
      return undefined;
    }
    try {
      return parseRuntimePointer(JSON.parse(raw));
    } catch (error) {
      if (error instanceof ManagedCliError) {
        throw error;
      }
      throw new ManagedCliError("EXECUTABLE_INVALID", `Managed runtime pointer ${path.basename(pointerPath)} is malformed.`, error);
    }
  }

  private async writePointer(pointerPath: string, pointer: RuntimePointer): Promise<void> {
    assertDirectChild(this.root, pointerPath);
    const temporaryPath = path.join(this.root, `.${path.basename(pointerPath)}.${safeNonce(this.randomId())}.tmp`);
    assertDirectChild(this.root, temporaryPath);
    try {
      const handle = await open(temporaryPath, "wx", 0o600);
      try {
        await handle.writeFile(`${JSON.stringify(pointer)}\n`, "utf8");
        await handle.sync();
      } finally {
        await handle.close();
      }
      await rename(temporaryPath, pointerPath);
    } finally {
      try {
        await unlink(temporaryPath);
      } catch (error) {
        if (!isNodeError(error, "ENOENT")) {
          // The atomic rename already succeeded or failed; leave unrelated paths untouched.
        }
      }
    }
  }

  private async withInstallLock<T>(operation: () => Promise<T>, signal?: AbortSignal): Promise<T> {
    // The on-disk lock is an INTER-process barrier. Without this in-process gate the same Extension
    // Host contends with itself (removeStaleLock correctly refuses to clear its own live pid), burns
    // the whole lock timeout, and reports "another managed runtime installation" when the only other
    // installer is this very process.
    const releaseInProcess = await this.acquireInProcessLock(signal);
    try {
      return await this.withOnDiskInstallLock(operation, signal);
    } finally {
      releaseInProcess();
    }
  }

  private async acquireInProcessLock(signal?: AbortSignal): Promise<() => void> {
    const previous = this.inProcessLock;
    let release!: () => void;
    this.inProcessLock = new Promise<void>((resolve) => {
      release = resolve;
    });
    try {
      await previous;
      throwIfCancelled(signal);
    } catch (error) {
      release();
      throw error;
    }
    return release;
  }

  private async withOnDiskInstallLock<T>(operation: () => Promise<T>, signal?: AbortSignal): Promise<T> {
    const token = safeNonce(this.randomId());
    const deadline = this.now() + (this.options.lockTimeoutMs ?? 30_000);
    const pollMs = this.options.lockPollMs ?? 50;
    const staleMs = this.options.lockStaleMs ?? 120_000;
    while (true) {
      throwIfCancelled(signal);
      try {
        const handle = await open(this.lockPath, "wx", 0o600);
        const record: LockRecord = { schemaVersion: 1, token, pid: process.pid, createdAt: this.now() };
        try {
          await handle.writeFile(`${JSON.stringify(record)}\n`, "utf8");
          await handle.sync();
        } finally {
          await handle.close();
        }
        break;
      } catch (error) {
        const lockState = await classifyInstallLockOpenError(error, this.lockPath);
        if (lockState === "fatal") {
          throw mapIoError("Could not acquire managed runtime install lock.", error);
        }
        if (lockState === "contended") {
          await this.removeStaleLock(staleMs);
        }
        if (this.now() >= deadline) {
          throw new ManagedCliError("LOCK_TIMEOUT", "Timed out waiting for another managed runtime installation.");
        }
        await this.delay(pollMs, signal);
      }
    }
    try {
      return await operation();
    } finally {
      await this.releaseLock(token);
    }
  }

  private async removeStaleLock(staleMs: number): Promise<void> {
    let raw: string | undefined;
    try {
      raw = await readTextIfExists(this.lockPath);
    } catch (error) {
      // Windows can deny reads while the winning process is still flushing/closing its newly-created
      // lock file. That is evidence of a live lock, not an installation failure or stale-lock signal.
      if (isNodeError(error, "EPERM") || isNodeError(error, "EACCES") || isNodeError(error, "EBUSY")) {
        return;
      }
      throw error;
    }
    if (raw === undefined) {
      return;
    }
    let record: LockRecord;
    try {
      record = parseLockRecord(JSON.parse(raw));
    } catch {
      let info;
      try {
        info = await stat(this.lockPath);
      } catch (error) {
        if (isNodeError(error, "ENOENT") || isNodeError(error, "EPERM") || isNodeError(error, "EACCES")) {
          return;
        }
        throw error;
      }
      if (this.now() - info.mtimeMs <= staleMs) {
        return;
      }
      await unlinkIfExists(this.lockPath);
      return;
    }
    const isAlive = (this.options.isProcessAlive ?? defaultIsProcessAlive)(record.pid);
    if (!isAlive && this.now() - record.createdAt > staleMs) {
      await unlinkIfExists(this.lockPath);
    }
  }

  private async releaseLock(token: string): Promise<void> {
    const raw = await readTextIfExists(this.lockPath);
    if (raw === undefined) {
      return;
    }
    try {
      const record = parseLockRecord(JSON.parse(raw));
      if (record.token === token) {
        await unlinkIfExists(this.lockPath);
      }
    } catch {
      // Never delete an inter-process lock unless ownership can be proven by its token.
    }
  }
}

type InstallLockOpenState = "contended" | "vanished" | "fatal";

/**
 * Windows can report EPERM/EACCES/EBUSY, rather than EEXIST, while another
 * process is creating or flushing an exclusive lock file. Treat that as
 * contention only when the exact lock path is a real regular file. If it
 * disappeared during the check, retry acquisition without attempting stale
 * cleanup. Directories, symlinks, and unrelated permission failures remain
 * fatal instead of being disguised as lock timeouts.
 */
export async function classifyInstallLockOpenError(
  error: unknown,
  lockPath: string
): Promise<InstallLockOpenState> {
  if (isNodeError(error, "EEXIST")) {
    return "contended";
  }
  if (!isNodeError(error, "EPERM") && !isNodeError(error, "EACCES") && !isNodeError(error, "EBUSY")) {
    return "fatal";
  }
  try {
    const info = await lstat(lockPath);
    return info.isFile() && !info.isSymbolicLink() ? "contended" : "fatal";
  } catch (inspectionError) {
    return isNodeError(inspectionError, "ENOENT") ? "vanished" : "fatal";
  }
}

class HttpsTransport implements HttpTransport {
  async open(url: URL, signal?: AbortSignal): Promise<HttpResponse> {
    return await new Promise<HttpResponse>((resolve, reject) => {
      const request = https.get(url, { headers: { "accept": "application/octet-stream", "user-agent": "Alysis Code-VSCode-Runtime" } });
      const cancel = () => request.destroy(abortError());
      signal?.addEventListener("abort", cancel, { once: true });
      request.once("error", reject);
      request.once("response", (response) => {
        signal?.removeEventListener("abort", cancel);
        const cancelResponse = () => response.destroy(abortError());
        signal?.addEventListener("abort", cancelResponse, { once: true });
        response.once("close", () => signal?.removeEventListener("abort", cancelResponse));
        const headers: Record<string, string | undefined> = {};
        for (const [key, value] of Object.entries(response.headers)) {
          headers[key.toLowerCase()] = Array.isArray(value) ? value.join(", ") : value;
        }
        resolve({ statusCode: response.statusCode ?? 0, headers, body: response, dispose: () => response.destroy() });
      });
    });
  }
}

export function createDefaultExecutableProbe(timeoutMs = 15_000): ExecutableProbe {
  return async (executablePath, signal) => {
    throwIfCancelled(signal);
    const environment = sanitizedProbeEnvironment();
    try {
      const versionResult = await execFileAsync(executablePath, ["--version"], {
        encoding: "utf8",
        windowsHide: true,
        timeout: timeoutMs,
        maxBuffer: 1024 * 1024,
        env: environment,
        signal
      });
      const versionMatch = String(versionResult.stdout).match(/(?:alysis\s+)?([0-9]+(?:\.[0-9A-Za-z-]+)+)/i);
      if (!versionMatch) {
        return { ok: false, reason: "CLI --version output did not contain a version." };
      }
      const healthResult = await execFileAsync(executablePath, ["ide-bridge", "health"], {
        encoding: "utf8",
        windowsHide: true,
        timeout: timeoutMs,
        maxBuffer: 1024 * 1024,
        env: environment,
        signal
      });
      const health = JSON.parse(String(healthResult.stdout)) as Record<string, unknown>;
      const protocolVersion = typeof health.protocol_version === "string"
        ? health.protocol_version
        : typeof health.protocolVersion === "string" ? health.protocolVersion : undefined;
      if (!protocolVersion) {
        return { ok: false, reason: "IDE bridge health did not report protocol_version." };
      }
      return { ok: true, cliVersion: versionMatch[1], protocolVersion };
    } catch (error) {
      if (isAbortError(error) || signal?.aborted) {
        throw abortError();
      }
      return { ok: false, reason: "CLI version or IDE bridge health probe failed." };
    }
  };
}

function sanitizedProbeEnvironment(): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" };
  const credentialPattern = /(?:^|_)(?:api_?key|access_?key|secret|token|password|passwd|credentials?|authorization)(?:_|$)/i;
  for (const key of Object.keys(environment)) {
    if (credentialPattern.test(key)) {
      delete environment[key];
    }
  }
  return environment;
}

function parseReleaseIdentity(input: unknown, label: string): ManagedCliReleaseIdentity {
  const object = expectObject(input, label);
  expectOnlyKeys(object, ["tag", "sourceRepository", "sourceCommit"], label);
  const tag = expectString(object.tag, `${label}.tag`);
  const sourceRepository = expectHttpsIdentity(object.sourceRepository, `${label}.sourceRepository`);
  const sourceCommit = expectString(object.sourceCommit, `${label}.sourceCommit`);
  if (!RELEASE_TAG_PATTERN.test(tag)) {
    invalidManifest(`${label}.tag is not a canonical release tag`);
  }
  if (!SOURCE_COMMIT_PATTERN.test(sourceCommit)) {
    invalidManifest(`${label}.sourceCommit must be a lowercase 40-character Git commit SHA`);
  }
  return { tag, sourceRepository, sourceCommit };
}

function parseProvenanceIdentity(input: unknown, label: string): ManagedCliProvenanceIdentity {
  const object = expectObject(input, label);
  expectOnlyKeys(object, ["issuer", "builderId"], label);
  return {
    issuer: expectHttpsIdentity(object.issuer, `${label}.issuer`),
    builderId: expectHttpsIdentity(object.builderId, `${label}.builderId`)
  };
}

function parseNativeSignature(
  input: unknown,
  target: ManagedCliTarget,
  label: string
): ManagedCliNativeSignature {
  const object = expectObject(input, label);
  expectOnlyKeys(object, ["evidenceSha256", "policy", "signerIdentity"], label);
  const evidenceSha256 = expectString(object.evidenceSha256, `${label}.evidenceSha256`);
  if (!SHA256_PATTERN.test(evidenceSha256)) {
    invalidManifest(`${label}.evidenceSha256 must be a lowercase 64-character SHA-256 digest`);
  }
  const policy = expectString(object.policy, `${label}.policy`) as ManagedCliNativeSignaturePolicy;
  const signerIdentity = expectString(object.signerIdentity, `${label}.signerIdentity`);
  const expected: ManagedCliNativeSignaturePolicy = target.startsWith("win32-")
    ? "authenticode"
    : target.startsWith("darwin-")
      ? "apple-developer-id-notarized"
      : "not-applicable";
  if (policy !== expected) {
    invalidManifest(`${label}.policy must be ${expected} for ${target}`);
  }
  if (signerIdentity.length > 512 || /[\u0000-\u001f]/.test(signerIdentity)) {
    invalidManifest(`${label}.signerIdentity is invalid`);
  }
  if (policy === "not-applicable" && signerIdentity !== "not-applicable") {
    invalidManifest(`${label}.signerIdentity must be not-applicable for Linux`);
  }
  if (policy === "authenticode" && !WINDOWS_SIGNER_IDENTITY_PATTERN.test(signerIdentity)) {
    invalidManifest(`${label}.signerIdentity must be a SHA-256 certificate digest for Windows`);
  }
  if (policy === "apple-developer-id-notarized" && !APPLE_SIGNER_IDENTITY_PATTERN.test(signerIdentity)) {
    invalidManifest(`${label}.signerIdentity must be a canonical Developer ID Application identity for macOS`);
  }
  return { evidenceSha256, policy, signerIdentity };
}

function expectHttpsIdentity(input: unknown, label: string): string {
  const value = expectString(input, label);
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    invalidManifest(`${label} must be a canonical HTTPS identity`);
  }
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash) {
    invalidManifest(`${label} must be a credential-free canonical HTTPS identity`);
  }
  return value;
}

function validateArtifactUrl(rawUrl: string, trustedHosts?: readonly string[]): URL {
  const authorityMarker = rawUrl.indexOf("://");
  const rawPathStart = authorityMarker < 0 ? -1 : rawUrl.indexOf("/", authorityMarker + 3);
  if (rawPathStart >= 0) {
    const rawPath = rawUrl.slice(rawPathStart).split(/[?#]/, 1)[0];
    let decodedPath: string;
    try {
      decodedPath = decodeURIComponent(rawPath);
    } catch (error) {
      throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifact URL contains invalid percent encoding.", error);
    }
    if (decodedPath.split(/[\\/]/).some((segment) => segment === "." || segment === "..") || decodedPath.includes("\\")) {
      throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifact URL contains path traversal.");
    }
  }
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch (error) {
    throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifact URL is invalid.", error);
  }
  if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443") || url.hash) {
    throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifacts require credential-free HTTPS URLs on the standard TLS port.");
  }
  const hostname = url.hostname.toLowerCase().replace(/\.$/, "");
  if (isUnsafeHostname(hostname)) {
    throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifact URL resolves to a forbidden local or private host name.");
  }
  if (trustedHosts && trustedHosts.length > 0) {
    const trusted = trustedHosts.map((host) => host.toLowerCase().replace(/\.$/, ""));
    if (!trusted.some((host) => hostname === host || hostname.endsWith(`.${host}`))) {
      throw new ManagedCliError("UNSAFE_URL", `Managed CLI artifact host ${hostname} is not an approved release host.`);
    }
  }
  for (const encodedSegment of url.pathname.split("/")) {
    let segment: string;
    try {
      segment = decodeURIComponent(encodedSegment);
    } catch (error) {
      throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifact URL contains invalid percent encoding.", error);
    }
    if (segment === "." || segment === ".." || segment.includes("/") || segment.includes("\\") || segment.includes("\0")) {
      throw new ManagedCliError("UNSAFE_URL", "Managed CLI artifact URL contains path traversal.");
    }
  }
  return url;
}

/**
 * True for hostnames that must never be reached across a trust boundary: loopback, link-local
 * (including the 169.254.169.254 cloud metadata endpoint), RFC1918, multicast, and their IPv6
 * equivalents. Exported because the managed browser needs the same judgement for scope-limited
 * navigation, and a second copy of this list would drift.
 *
 * Node's WHATWG URL parser normalizes decimal/hex/octal IPv4 before this sees it, so
 * `https://2130706433/` arrives as `127.0.0.1`.
 */
export function isUnsafeHostname(hostname: string): boolean {
  if (!hostname || hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local")) {
    return true;
  }
  const bare = hostname.startsWith("[") && hostname.endsWith("]") ? hostname.slice(1, -1) : hostname;
  const lower = bare.toLowerCase();
  if (lower === "::1" || lower === "::" || lower.startsWith("fe80:")) {
    return true;
  }
  // Unique local addresses (fc00::/7) are the IPv6 analogue of RFC1918.
  if (lower.startsWith("fc") || lower.startsWith("fd")) {
    if (/^f[cd][0-9a-f]{0,2}:/.test(lower)) {
      return true;
    }
  }
  // IPv4-mapped IPv6 (::ffff:127.0.0.1, normalized by Node to ::ffff:7f00:1) must inherit the
  // IPv4 verdict rather than sliding through as an opaque IPv6 literal.
  const mapped = lower.match(/^::ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/);
  if (mapped) {
    const high = Number.parseInt(mapped[1], 16);
    const low = Number.parseInt(mapped[2], 16);
    return isUnsafeHostname(
      `${(high >> 8) & 0xff}.${high & 0xff}.${(low >> 8) & 0xff}.${low & 0xff}`
    );
  }
  const mappedDotted = lower.match(/^::ffff:(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})$/);
  if (mappedDotted) {
    return isUnsafeHostname(mappedDotted[1]);
  }
  const ipv4 = bare.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (!ipv4) {
    return false;
  }
  const octets = ipv4.slice(1).map(Number);
  if (octets.some((octet) => octet > 255)) {
    return true;
  }
  return octets[0] === 0 || octets[0] === 10 || octets[0] === 127 || octets[0] >= 224 ||
    (octets[0] === 169 && octets[1] === 254) ||
    (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31) ||
    (octets[0] === 192 && octets[1] === 168);
}

function parseInstalledMetadata(
  input: unknown,
  artifactVersion: string,
  target: ManagedCliTarget,
  trustedDownloadHosts?: readonly string[]
): InstalledMetadata {
  const object = expectObject(input, "installed runtime metadata", "IMMUTABLE_VERSION_CONFLICT");
  expectOnlyKeys(
    object,
    [
      "schemaVersion", "release", "artifactVersion", "cliVersion", "protocolVersion", "target",
      "executable", "sha256", "size", "sourceUrl", "compatibility", "signingKeyId",
      "provenance", "sbomSha256", "nativeSignature", "signature", "signatureVerified"
    ],
    "installed runtime metadata",
    "IMMUTABLE_VERSION_CONFLICT"
  );
  if (object.schemaVersion !== 3) {
    throw new ManagedCliError("IMMUTABLE_VERSION_CONFLICT", "Installed runtime metadata schema is obsolete or invalid.");
  }
  const parsedTarget = expectTarget(object.target, "installed runtime metadata.target");
  const compatibilityObject = expectObject(object.compatibility, "installed runtime metadata.compatibility");
  expectOnlyKeys(compatibilityObject, ["extension", "protocol", "cli"], "installed runtime metadata.compatibility");
  const parsed: InstalledMetadata = {
    schemaVersion: 3,
    release: parseReleaseIdentity(object.release, "installed runtime metadata.release"),
    artifactVersion: expectSafeIdentifier(object.artifactVersion, "installed runtime metadata.artifactVersion"),
    cliVersion: expectVersion(object.cliVersion, "installed runtime metadata.cliVersion"),
    protocolVersion: expectVersion(object.protocolVersion, "installed runtime metadata.protocolVersion"),
    target: parsedTarget,
    executable: expectString(object.executable, "installed runtime metadata.executable"),
    sha256: expectString(object.sha256, "installed runtime metadata.sha256"),
    size: expectPositiveInteger(object.size, "installed runtime metadata.size"),
    sourceUrl: expectString(object.sourceUrl, "installed runtime metadata.sourceUrl"),
    compatibility: {
      extension: parseVersionRange(compatibilityObject.extension, "installed runtime metadata.compatibility.extension"),
      protocol: parseVersionRange(compatibilityObject.protocol, "installed runtime metadata.compatibility.protocol"),
      cli: parseVersionRange(compatibilityObject.cli, "installed runtime metadata.compatibility.cli")
    },
    signingKeyId: expectSafeIdentifier(object.signingKeyId, "installed runtime metadata.signingKeyId"),
    provenance: parseProvenanceIdentity(object.provenance, "installed runtime metadata.provenance"),
    sbomSha256: expectString(object.sbomSha256, "installed runtime metadata.sbomSha256"),
    nativeSignature: parseNativeSignature(
      object.nativeSignature,
      parsedTarget,
      "installed runtime metadata.nativeSignature"
    ),
    signature: object.signature === undefined
      ? undefined
      : expectString(object.signature, "installed runtime metadata.signature"),
    signatureVerified: expectBoolean(object.signatureVerified, "installed runtime metadata.signatureVerified")
  };
  if (parsed.signature !== undefined && (parsed.signature.length < 16 || parsed.signature.length > 16_384)) {
    throw new ManagedCliError("IMMUTABLE_VERSION_CONFLICT", "Installed runtime signature metadata is invalid.");
  }
  if (parsed.artifactVersion !== artifactVersion || parsed.target !== target ||
      !SAFE_EXECUTABLE_PATTERN.test(parsed.executable) || !SHA256_PATTERN.test(parsed.sha256) ||
      !SHA256_PATTERN.test(parsed.sbomSha256) || parsed.release.tag !== `v${parsed.cliVersion}` ||
      !versionInRange(parsed.cliVersion, parsed.compatibility.cli) ||
      !versionInRange(parsed.protocolVersion, parsed.compatibility.protocol)) {
    throw new ManagedCliError("IMMUTABLE_VERSION_CONFLICT", "Installed runtime metadata does not match its owned path.");
  }
  validateArtifactUrl(parsed.sourceUrl, trustedDownloadHosts);
  return parsed;
}

function parseRuntimePointer(input: unknown): RuntimePointer {
  const object = expectObject(input, "runtime pointer", "EXECUTABLE_INVALID");
  expectOnlyKeys(
    object,
    ["schemaVersion", "artifactVersion", "cliVersion", "protocolVersion", "target", "executable", "sha256", "size", "signatureVerified", "activatedAt"],
    "runtime pointer",
    "EXECUTABLE_INVALID"
  );
  const pointer: RuntimePointer = {
    schemaVersion: expectExactOne(object.schemaVersion, "runtime pointer.schemaVersion"),
    artifactVersion: expectSafeIdentifier(object.artifactVersion, "runtime pointer.artifactVersion"),
    cliVersion: expectVersion(object.cliVersion, "runtime pointer.cliVersion"),
    protocolVersion: expectVersion(object.protocolVersion, "runtime pointer.protocolVersion"),
    target: expectTarget(object.target, "runtime pointer.target"),
    executable: expectString(object.executable, "runtime pointer.executable"),
    sha256: expectString(object.sha256, "runtime pointer.sha256"),
    size: expectPositiveInteger(object.size, "runtime pointer.size"),
    signatureVerified: expectBoolean(object.signatureVerified, "runtime pointer.signatureVerified"),
    activatedAt: expectString(object.activatedAt, "runtime pointer.activatedAt")
  };
  if (!SAFE_EXECUTABLE_PATTERN.test(pointer.executable) || !SHA256_PATTERN.test(pointer.sha256) ||
      !Number.isFinite(Date.parse(pointer.activatedAt))) {
    throw new ManagedCliError("EXECUTABLE_INVALID", "Managed runtime pointer contains unsafe or invalid values.");
  }
  return pointer;
}

function parseLockRecord(input: unknown): LockRecord {
  const object = expectObject(input, "runtime lock");
  expectOnlyKeys(object, ["schemaVersion", "token", "pid", "createdAt"], "runtime lock");
  return {
    schemaVersion: expectExactOne(object.schemaVersion, "runtime lock.schemaVersion"),
    token: expectSafeIdentifier(object.token, "runtime lock.token"),
    pid: expectPositiveInteger(object.pid, "runtime lock.pid"),
    createdAt: expectPositiveInteger(object.createdAt, "runtime lock.createdAt")
  };
}

function pointerFromRuntime(runtime: InstalledRuntime, root: string, now: number): RuntimePointer {
  const relative = path.relative(path.join(root, "versions", runtime.artifactVersion, runtime.target), runtime.executablePath);
  if (!SAFE_EXECUTABLE_PATTERN.test(relative) || path.basename(relative) !== relative) {
    throw new ManagedCliError("PATH_SAFETY_VIOLATION", "Managed runtime executable is outside its immutable version directory.");
  }
  return {
    schemaVersion: 1,
    artifactVersion: runtime.artifactVersion,
    cliVersion: runtime.cliVersion,
    protocolVersion: runtime.protocolVersion,
    target: runtime.target,
    executable: relative,
    sha256: runtime.sha256,
    size: runtime.size,
    signatureVerified: runtime.signatureVerified,
    activatedAt: new Date(now).toISOString()
  };
}

async function verifyFile(filePath: string, expectedSize: number, expectedHash: string): Promise<void> {
  try {
    const info = await stat(filePath);
    if (!info.isFile() || info.size !== expectedSize) {
      throw new ManagedCliError("EXECUTABLE_INVALID", "Managed runtime executable size does not match immutable metadata.");
    }
    await access(filePath, fsConstants.R_OK);
    const handle = await open(filePath, "r");
    const hash = createHash("sha256");
    try {
      const buffer = Buffer.allocUnsafe(64 * 1024);
      while (true) {
        const read = await handle.read(buffer, 0, buffer.length, null);
        if (read.bytesRead === 0) {
          break;
        }
        hash.update(buffer.subarray(0, read.bytesRead));
      }
    } finally {
      await handle.close();
    }
    if (hash.digest("hex") !== expectedHash) {
      throw new ManagedCliError("EXECUTABLE_INVALID", "Managed runtime executable hash does not match immutable metadata.");
    }
  } catch (error) {
    if (error instanceof ManagedCliError) {
      throw error;
    }
    throw new ManagedCliError("EXECUTABLE_INVALID", "Managed runtime executable is missing or unreadable.", error);
  }
}

function parseVersionRange(input: unknown, label: string): VersionRange {
  const object = expectObject(input, label);
  expectOnlyKeys(object, ["min", "max"], label);
  const range = { min: expectVersion(object.min, `${label}.min`), max: expectVersion(object.max, `${label}.max`) };
  validateRange(range, label);
  return range;
}

function sameVersionRange(left: VersionRange, right: VersionRange): boolean {
  return left.min === right.min && left.max === right.max;
}

function validateRange(range: VersionRange, label: string): void {
  expectVersion(range.min, `${label}.min`);
  expectVersion(range.max, `${label}.max`);
  if (compareVersions(range.min, range.max) > 0) {
    invalidManifest(`${label}.min must not be greater than max`);
  }
}

function versionInRange(version: string, range: VersionRange): boolean {
  return compareVersions(version, range.min) >= 0 && compareVersions(version, range.max) <= 0;
}

function rangesOverlap(left: VersionRange, right: VersionRange): boolean {
  return compareVersions(left.min, right.max) <= 0 && compareVersions(right.min, left.max) <= 0;
}

function compareVersions(left: string, right: string): number {
  const leftParts = normalizeVersionParts(left);
  const rightParts = normalizeVersionParts(right);
  const length = Math.max(leftParts.length, rightParts.length);
  for (let index = 0; index < length; index += 1) {
    const leftPart = leftParts[index] ?? { numeric: true, value: "0" };
    const rightPart = rightParts[index] ?? { numeric: true, value: "0" };
    if (leftPart.numeric && rightPart.numeric) {
      const leftNumber = BigInt(leftPart.value);
      const rightNumber = BigInt(rightPart.value);
      if (leftNumber !== rightNumber) {
        return leftNumber < rightNumber ? -1 : 1;
      }
      continue;
    }
    if (leftPart.numeric !== rightPart.numeric) {
      return leftPart.numeric ? 1 : -1;
    }
    const comparison = leftPart.value.localeCompare(rightPart.value);
    if (comparison !== 0) {
      return comparison < 0 ? -1 : 1;
    }
  }
  return 0;
}

function normalizeVersionParts(version: string): Array<{ numeric: boolean; value: string }> {
  const normalized = version.trim().replace(/^v/i, "");
  return normalized.split(/[.+-]/).filter(Boolean).map((value) => ({ numeric: /^\d+$/.test(value), value }));
}

function expectVersion(input: unknown, label: string): string {
  const value = expectString(input, label);
  if (!/^v?\d+(?:\.[0-9A-Za-z-]+){0,5}(?:[+-][0-9A-Za-z.-]+)?$/.test(value) || value.length > 128) {
    invalidManifest(`${label} is not a supported version string`);
  }
  return value.replace(/^v/i, "");
}

function expectTarget(input: unknown, label: string): ManagedCliTarget {
  const value = expectString(input, label);
  const targets: readonly ManagedCliTarget[] = [
    "win32-x64", "win32-arm64", "darwin-x64", "darwin-arm64", "linux-x64", "linux-arm64"
  ];
  if (!targets.includes(value as ManagedCliTarget)) {
    invalidManifest(`${label} is not a supported managed CLI target`);
  }
  return value as ManagedCliTarget;
}

function expectSafeIdentifier(input: unknown, label: string): string {
  const value = expectString(input, label);
  if (!SAFE_IDENTIFIER_PATTERN.test(value) || value === "." || value === "..") {
    invalidManifest(`${label} contains unsafe path characters`);
  }
  return value;
}

function expectString(input: unknown, label: string): string {
  if (typeof input !== "string" || input.length === 0) {
    invalidManifest(`${label} must be a non-empty string`);
  }
  return input as string;
}

function expectPositiveInteger(input: unknown, label: string): number {
  if (!Number.isSafeInteger(input) || Number(input) <= 0) {
    invalidManifest(`${label} must be a positive safe integer`);
  }
  return Number(input);
}

function expectBoolean(input: unknown, label: string): boolean {
  if (typeof input !== "boolean") {
    invalidManifest(`${label} must be a boolean`);
  }
  return input;
}

function expectExactOne(input: unknown, label: string): 1 {
  if (input !== 1) {
    invalidManifest(`${label} must be 1`);
  }
  return 1;
}

function expectObject(
  input: unknown,
  label: string,
  code: ManagedCliErrorCode = "INVALID_MANIFEST"
): Record<string, unknown> {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new ManagedCliError(code, `${label} must be an object.`);
  }
  const prototype = Object.getPrototypeOf(input);
  if (prototype !== Object.prototype && prototype !== null) {
    throw new ManagedCliError(code, `${label} must be a plain object.`);
  }
  return input as Record<string, unknown>;
}

function expectOnlyKeys(
  object: Record<string, unknown>,
  allowed: readonly string[],
  label: string,
  code: ManagedCliErrorCode = "INVALID_MANIFEST"
): void {
  const unknown = Object.keys(object).filter((key) => !allowed.includes(key));
  if (unknown.length > 0) {
    throw new ManagedCliError(code, `${label} contains unknown field ${unknown[0]}.`);
  }
}

function invalidManifest(message: string): never {
  throw new ManagedCliError("INVALID_MANIFEST", message);
}

function assertDirectChild(parent: string, candidate: string): void {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  if (!relative || relative.startsWith("..") || path.isAbsolute(relative) || relative.includes(path.sep)) {
    throw new ManagedCliError("PATH_SAFETY_VIOLATION", "Managed runtime operation escaped its owned directory.");
  }
}

function safeNonce(input: string): string {
  const normalized = input.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
  if (!normalized) {
    throw new ManagedCliError("PATH_SAFETY_VIOLATION", "Managed runtime random identifier was unsafe.");
  }
  return normalized;
}

function parseContentLength(input: string | undefined): number | undefined {
  if (input === undefined) {
    return undefined;
  }
  if (!/^\d+$/.test(input)) {
    throw new ManagedCliError("DOWNLOAD_FAILED", "Artifact Content-Length header is invalid.");
  }
  const parsed = Number(input);
  if (!Number.isSafeInteger(parsed)) {
    throw new ManagedCliError("DOWNLOAD_FAILED", "Artifact Content-Length header is too large.");
  }
  return parsed;
}

async function readTextIfExists(filePath: string): Promise<string | undefined> {
  try {
    return await readFile(filePath, "utf8");
  } catch (error) {
    if (isNodeError(error, "ENOENT")) {
      return undefined;
    }
    throw error;
  }
}

async function unlinkIfExists(filePath: string): Promise<void> {
  try {
    await unlink(filePath);
  } catch (error) {
    if (!isNodeError(error, "ENOENT")) {
      throw error;
    }
  }
}

function defaultIsProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return isNodeError(error, "EPERM");
  }
}

function cancellableDelay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(abortError());
      return;
    }
    const timeout = setTimeout(() => {
      signal?.removeEventListener("abort", cancel);
      resolve();
    }, milliseconds);
    const cancel = () => {
      clearTimeout(timeout);
      reject(abortError());
    };
    signal?.addEventListener("abort", cancel, { once: true });
  });
}

function throwIfCancelled(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw new ManagedCliError("CANCELLED", "Managed runtime operation was cancelled.");
  }
}

function abortError(): Error {
  const error = new Error("Operation aborted");
  error.name = "AbortError";
  return error;
}

function isAbortError(error: unknown): boolean {
  return error instanceof Error && (error.name === "AbortError" || error.name === "AbortError");
}

function isNodeError(error: unknown, code: string): error is NodeJS.ErrnoException {
  return error instanceof Error && (error as NodeJS.ErrnoException).code === code;
}

function mapIoError(message: string, error: unknown): ManagedCliError {
  return new ManagedCliError("IO_ERROR", message, error);
}

function redactUrl(url: URL): string {
  return `${url.protocol}//${url.hostname}${url.pathname}`;
}
