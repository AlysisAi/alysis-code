import { readFile, realpath, stat } from "node:fs/promises";
import path from "node:path";

import {
  InstalledRuntime,
  ManagedCliError,
  ManagedCliManifest,
  parseManagedCliManifest,
  resolveManagedCliTarget
} from "./ManagedCliRuntime";

const MAX_BUNDLED_MANIFEST_BYTES = 1024 * 1024;

export interface BundledRuntimeInstaller {
  installBundled(
    manifest: ManagedCliManifest,
    executablePath: string,
    signal?: AbortSignal
  ): Promise<InstalledRuntime>;
}

export async function installBundledManagedCli(
  bundleRoot: string,
  installer: BundledRuntimeInstaller,
  trustedDownloadHosts: readonly string[],
  signal?: AbortSignal
): Promise<InstalledRuntime> {
  if (!path.isAbsolute(bundleRoot) || bundleRoot.includes("\0")) {
    throw new ManagedCliError("BUNDLED_RUNTIME_INVALID", "Managed CLI bundle root is not an absolute safe path.");
  }
  if (signal?.aborted) {
    throw new ManagedCliError("CANCELLED", "Bundled managed CLI installation was cancelled.");
  }
  try {
    const rootReal = await realpath(bundleRoot);
    const manifestPath = path.join(rootReal, "manifest.json");
    const manifestReal = await realpath(manifestPath);
    if (path.dirname(manifestReal) !== rootReal || path.basename(manifestReal) !== "manifest.json") {
      throw new ManagedCliError("BUNDLED_RUNTIME_INVALID", "Bundled manifest escaped its trusted directory.");
    }
    const manifestStat = await stat(manifestReal);
    if (!manifestStat.isFile() || manifestStat.size <= 0 || manifestStat.size > MAX_BUNDLED_MANIFEST_BYTES) {
      throw new ManagedCliError("BUNDLED_RUNTIME_INVALID", "Bundled manifest is empty, oversized, or not a file.");
    }
    const manifest = parseManagedCliManifest(
      JSON.parse(await readFile(manifestReal, "utf8")) as unknown,
      { trustedDownloadHosts }
    );
    const target = resolveManagedCliTarget();
    const artifact = manifest.artifacts.find((candidate) => candidate.target === target);
    if (!artifact) {
      throw new ManagedCliError(
        "UNSUPPORTED_TARGET",
        `Bundled managed CLI manifest has no artifact for ${target}.`
      );
    }
    const executablePath = path.join(rootReal, artifact.executable);
    return await installer.installBundled(manifest, executablePath, signal);
  } catch (error) {
    if (error instanceof ManagedCliError) {
      throw error;
    }
    // "This build ships no bundle" is a different fact from "this build ships a broken bundle": the
    // first is recoverable by installing a CLI, the second is a tampering/packaging failure. Keeping
    // them apart is what lets the coordinator show an actionable install message instead of
    // "BUNDLED_RUNTIME_INVALID" with no next step.
    if (isMissingPathError(error)) {
      throw new ManagedCliError(
        "BUNDLED_RUNTIME_MISSING",
        "This build does not ship a bundled managed Alysis Code CLI.",
        error
      );
    }
    throw new ManagedCliError(
      "BUNDLED_RUNTIME_INVALID",
      "Bundled managed CLI manifest could not be read or validated.",
      error
    );
  }
}

function isMissingPathError(error: unknown): boolean {
  const code = (error as NodeJS.ErrnoException | undefined)?.code;
  return code === "ENOENT" || code === "ENOTDIR";
}
