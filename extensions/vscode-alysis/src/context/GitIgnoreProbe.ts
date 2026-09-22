import { execFile } from "node:child_process";
import { stat } from "node:fs/promises";
import path from "node:path";

import { resolveExecutablePath } from "../client/ExecutableResolver";

export interface IgnoreProbe {
  isIgnored(workspaceRoot: string, candidatePath: string): Promise<boolean>;
}
export interface GitIgnoreProbeOptions {
  readonly gitExecutable?: string;
  readonly timeoutMs?: number;
  /** Injectable PATH resolution. Defaults to an explicit PATH walk that never falls back to a cwd. */
  readonly resolveExecutable?: (command: string) => string | null;
}

/**
 * Evaluates Git ignore rules without a shell. Paths are passed through NUL-
 * delimited stdin, so filenames cannot become command options. Repository
 * detection is cached and command output is never surfaced or persisted.
 */
export class GitIgnoreProbe implements IgnoreProbe {
  private readonly gitExecutable: string;
  private readonly timeoutMs: number;
  private readonly resolveExecutable: (command: string) => string | null;
  private resolvedExecutable: string | null | undefined;
  private readonly repositoryCache = new Map<string, Promise<boolean>>();

  public constructor(options: GitIgnoreProbeOptions = {}) {
    this.gitExecutable = options.gitExecutable?.trim() || "git";
    this.timeoutMs = options.timeoutMs ?? 2_000;
    this.resolveExecutable =
      options.resolveExecutable ?? ((command) => resolveExecutablePath(command).resolvedPath);
    if (!Number.isSafeInteger(this.timeoutMs) || this.timeoutMs < 100 || this.timeoutMs > 10_000) {
      throw new Error("Git ignore probe timeout must be between 100 and 10000 milliseconds.");
    }
  }

  public async isIgnored(workspaceRoot: string, candidatePath: string): Promise<boolean> {
    const root = path.resolve(workspaceRoot);
    const candidate = path.resolve(candidatePath);
    const relative = path.relative(root, candidate);
    if (!relative || relative === ".") {
      return false;
    }
    if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new Error("Ignore probe candidate must be inside its workspace root.");
    }
    if (!await this.isRepository(root)) {
      return false;
    }
    const executable = this.executablePath();
    if (!executable) {
      throw new Error("Git ignore policy could not evaluate the candidate path.");
    }
    const result = await runGit(
      executable,
      root,
      ["check-ignore", "--no-index", "--quiet", "-z", "--stdin"],
      `${relative.split(path.sep).join("/")}\u0000`,
      this.timeoutMs
    );
    if (result === 0) {
      return true;
    }
    if (result === 1) {
      return false;
    }
    throw new Error("Git ignore policy could not evaluate the candidate path.");
  }

  private async isRepository(root: string): Promise<boolean> {
    let pending = this.repositoryCache.get(root);
    if (!pending) {
      pending = this.detectRepository(root);
      this.repositoryCache.set(root, pending);
    }
    return pending;
  }

  private async detectRepository(root: string): Promise<boolean> {
    const executable = this.executablePath();
    const result = executable
      ? await runGit(
        executable,
        root,
        ["rev-parse", "--is-inside-work-tree"],
        undefined,
        this.timeoutMs
      )
      : -1;
    if (result === 0) {
      return true;
    }
    if (result === 128) {
      return false;
    }
    if (result === -1) {
      // Missing Git is harmless for ordinary folders but must fail closed when
      // the workspace itself visibly contains Git metadata.
      try {
        await stat(path.join(root, ".git"));
      } catch {
        return false;
      }
    }
    throw new Error("Git repository detection failed.");
  }

  /**
   * Resolve Git to an absolute path exactly once. libuv's Windows process spawner searches the
   * child's cwd before PATH, and every probe runs with cwd set to the workspace root, so a bare
   * "git" would execute a repo-planted git.exe. resolveExecutablePath walks PATH explicitly and
   * never falls back to a cwd; an unresolvable executable is reported as missing so the fail-closed
   * path in detectRepository still applies.
   */
  private executablePath(): string | null {
    if (this.resolvedExecutable === undefined) {
      this.resolvedExecutable = this.resolveExecutable(this.gitExecutable);
    }
    return this.resolvedExecutable;
  }
}

function runGit(
  executable: string,
  cwd: string,
  args: readonly string[],
  input: string | undefined,
  timeoutMs: number
): Promise<number> {
  return new Promise((resolve) => {
    const child = execFile(
      executable,
      [...args],
      {
        cwd,
        windowsHide: true,
        timeout: timeoutMs,
        maxBuffer: 8 * 1024,
        encoding: "utf8"
      },
      (error) => {
        if (!error) {
          resolve(0);
          return;
        }
        const code = (error as NodeJS.ErrnoException & { code?: number | string }).code;
        if (typeof code === "number") {
          resolve(code);
          return;
        }
        resolve(code === "ENOENT" ? -1 : -2);
      }
    );
    child.once("error", (error: NodeJS.ErrnoException) => {
      if (error.code === "ENOENT") {
        resolve(-1);
      }
    });
    if (input !== undefined) {
      child.stdin?.end(input, "utf8");
    }
  });
}
