import { constants, statSync, accessSync, realpathSync } from "node:fs";
import path from "node:path";

export interface ExecutableResolution {
  command: string;
  resolvedPath: string | null;
}

export interface ExecutableResolverOptions {
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  cwd?: string;
  pathDelimiter?: string;
  isExecutableFile?: (candidate: string) => boolean;
}

export function resolveExecutablePath(
  command: string,
  options: ExecutableResolverOptions = {}
): ExecutableResolution {
  const platform = options.platform ?? process.platform;
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const trimmed = command.trim();
  if (trimmed.length === 0) {
    return { command, resolvedPath: null };
  }
  const cwd = options.cwd ?? process.cwd();
  const isExecutable = options.isExecutableFile ?? ((candidate: string) => isExecutableFile(candidate, platform));

  if (hasPathSeparator(trimmed) || pathApi.isAbsolute(trimmed)) {
    const candidate = pathApi.isAbsolute(trimmed) ? trimmed : pathApi.resolve(cwd, trimmed);
    return { command, resolvedPath: firstExecutable(candidate, platform, options.env ?? process.env, isExecutable) };
  }

  const env = options.env ?? process.env;
  const delimiter = options.pathDelimiter ?? (platform === "win32" ? ";" : path.delimiter);
  // An empty PATH entry means "the current directory" to the Windows shell. Honouring that would let
  // a repo-planted executable win over a real installation, so empty entries are dropped instead.
  // Windows PATH entries may also be quoted ("C:\Program Files\..."); the quotes are not part of the
  // directory name and would make every candidate below fail to stat.
  const pathEntries = String(env.PATH ?? env.Path ?? env.path ?? "")
    .split(delimiter)
    .map((entry) => (platform === "win32" ? stripSurroundingQuotes(entry) : entry))
    .filter((entry) => entry.length > 0);
  for (const entry of pathEntries) {
    const base = pathApi.join(entry, trimmed);
    const resolved = firstExecutable(base, platform, env, isExecutable);
    if (resolved) {
      return { command, resolvedPath: resolved };
    }
  }
  return { command, resolvedPath: null };
}

/**
 * Node >= 18.20 / 20.12 refuses to spawn `.bat`/`.cmd` files without `shell: true` (the
 * CVE-2024-27980 fix), and every affected user sees a bare "spawn EINVAL". PATHEXT resolution
 * happily returns those wrappers for pipx/conda/npm-style installs, so callers check the resolved
 * path before spawning and surface this instead. Enabling a shell is not an option: it would
 * reintroduce the command-injection surface the fix closed.
 */
export function unsupportedShimReason(
  resolvedPath: string,
  platform: NodeJS.Platform = process.platform
): string | null {
  if (platform !== "win32" || resolvedPath.length === 0) {
    return null;
  }
  const extension = path.win32.extname(resolvedPath).toLowerCase();
  if (extension !== ".cmd" && extension !== ".bat") {
    return null;
  }
  return (
    `The Alysis Code CLI resolved to the Windows ${extension} wrapper "${resolvedPath}". ` +
    "Node.js cannot launch .cmd or .bat wrappers, so the bridge would fail with \"spawn EINVAL\". " +
    "Set alysis.cliPath to the real executable (for example the alysis.exe in the same " +
    "Scripts directory, or the executable inside the pipx/conda environment) instead of the wrapper."
  );
}

export function isPathInsideAnyWorkspace(
  candidate: string,
  workspaceRoots: readonly string[],
  platform: NodeJS.Platform = process.platform
): boolean {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  const resolvedCandidate = realPathForCompare(candidate, pathApi);
  const normalizeCase = (value: string) => (platform === "win32" ? value.toLowerCase() : value);
  const candidateForCompare = normalizeCase(resolvedCandidate);
  return workspaceRoots.some((root) => {
    const resolvedRoot = normalizeCase(realPathForCompare(root, pathApi));
    const relative = pathApi.relative(resolvedRoot, candidateForCompare);
    return relative === "" || (!relative.startsWith("..") && !pathApi.isAbsolute(relative));
  });
}

export function isRelativePathLike(value: string, platform: NodeJS.Platform = process.platform): boolean {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return !pathApi.isAbsolute(value) && hasPathSeparator(value);
}

export function isAbsolutePathLike(value: string, platform: NodeJS.Platform = process.platform): boolean {
  const pathApi = platform === "win32" ? path.win32 : path.posix;
  return pathApi.isAbsolute(value);
}

function hasPathSeparator(value: string): boolean {
  return value.includes("/") || value.includes("\\");
}

function stripSurroundingQuotes(entry: string): string {
  if (entry.length >= 2 && entry.startsWith("\"") && entry.endsWith("\"")) {
    return entry.slice(1, -1);
  }
  return entry;
}

function firstExecutable(
  candidate: string,
  platform: NodeJS.Platform,
  env: NodeJS.ProcessEnv,
  isExecutable: (candidate: string) => boolean
): string | null {
  for (const expanded of executableCandidates(candidate, platform, env)) {
    if (isExecutable(expanded)) {
      return realExecutablePath(expanded);
    }
  }
  return null;
}

function realExecutablePath(candidate: string): string {
  try {
    return realpathSync.native(candidate);
  } catch {
    return candidate;
  }
}

function realPathForCompare(candidate: string, pathApi: typeof path.posix): string {
  try {
    return realpathSync.native(candidate);
  } catch {
    return pathApi.resolve(candidate);
  }
}

function executableCandidates(candidate: string, platform: NodeJS.Platform, env: NodeJS.ProcessEnv): string[] {
  if (platform !== "win32") {
    return [candidate];
  }
  const ext = path.win32.extname(candidate);
  if (ext.length > 0) {
    return [candidate];
  }
  const pathext = String((env.PATHEXT ?? env.Pathext ?? ".COM;.EXE;.BAT;.CMD"))
    .split(";")
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  return [
    ...pathext.map((extname) => `${candidate}${extname.startsWith(".") ? extname : `.${extname}`}`),
    candidate
  ];
}

function isExecutableFile(candidate: string, platform: NodeJS.Platform): boolean {
  try {
    const stat = statSync(candidate);
    if (!stat.isFile()) {
      return false;
    }
    if (platform !== "win32") {
      accessSync(candidate, constants.X_OK);
    }
    return true;
  } catch {
    return false;
  }
}
