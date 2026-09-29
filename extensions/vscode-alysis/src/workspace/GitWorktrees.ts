import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import * as fs from "node:fs/promises";
import path from "node:path";

export interface GitWorkspaceState {
  available: boolean;
  root: string;
  branch: string;
  isWorktree: boolean;
  additions: number;
  deletions: number;
  files: number;
  untracked: number;
  reason: string;
  busy?: boolean;
  canMove?: boolean;
}

export const emptyGitWorkspace = (reason = "Open a trusted Git repository to use worktrees."): GitWorkspaceState => ({
  available: false, root: "", branch: "", isWorktree: false,
  additions: 0, deletions: 0, files: 0, untracked: 0, reason
});

/** Keep Git diagnostics actionable without rendering paths, configuration, or arbitrary stderr. */
export function gitWorkspaceFailureReason(error: unknown): string {
  const message = error instanceof Error ? error.message : "";
  if (/detected dubious ownership/i.test(message)) {
    return "Git rejected this repository's ownership. Review repository trust in VS Code Source Control before using worktrees.";
  }
  if (/Open the Git repository root/.test(message)) return message;
  if (/not a git repository/i.test(message)) return "Open a Git repository to use worktrees.";
  if (/Needed a single revision|unknown revision.*HEAD|ambiguous argument 'HEAD'/i.test(message)) {
    return "Create the first commit in this repository before using worktrees.";
  }
  if (/ENOENT|not recognized as an internal or external command/i.test(message)) {
    return "Git could not be started. Check that Git is installed and available to VS Code.";
  }
  return "Git could not inspect this workspace. Check repository access and Git availability, then try again.";
}

/** Argument arrays only; no shell, checkout hooks, staging, or resets in the source checkout. */
export function runGit(root: string, args: string[], input?: Buffer): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const env = { ...process.env, GIT_OPTIONAL_LOCKS: "0", GIT_TERMINAL_PROMPT: "0" };
    for (const key of ["GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"]) delete (env as NodeJS.ProcessEnv)[key];
    const child = execFile("git", ["-c", "core.quotePath=false", ...args], {
      cwd: root, windowsHide: true, timeout: 30_000, maxBuffer: 32 * 1024 * 1024,
      encoding: "buffer", env
    }, (error, stdout, stderr) => {
      if (error) reject(new Error(stderr.toString().trim() || (error.code === "ENOENT" ? "Git executable not found (ENOENT)." : `Git could not complete ${args[0]}.`)));
      else resolve(stdout);
    });
    // Git may exit before reading stdin (e.g. a conflicted index).
    child.stdin?.on("error", () => undefined);
    child.stdin?.end(input);
  });
}

const gitText = async (root: string, args: string[]) => (await runGit(root, args)).toString().trim();

export async function inspectGitWorkspace(root: string): Promise<GitWorkspaceState> {
  const top = await fs.realpath(await gitText(root, ["rev-parse", "--show-toplevel"]));
  if (path.relative(top, await fs.realpath(root))) {
    throw new Error("Open the Git repository root to create a worktree for this session.");
  }
  const [head, branch, gitDir, commonDir, diff, untracked] = await Promise.all([
    gitText(root, ["rev-parse", "--verify", "HEAD"]),
    gitText(root, ["rev-parse", "--abbrev-ref", "HEAD"]),
    gitText(root, ["rev-parse", "--absolute-git-dir"]),
    gitText(root, ["rev-parse", "--git-common-dir"]),
    runGit(root, ["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--numstat", "-z", "HEAD", "--"]),
    runGit(root, ["ls-files", "--others", "--exclude-standard", "-z"])
  ]);
  const rows = diff.toString().split("\0").filter(Boolean);
  let additions = 0;
  let deletions = 0;
  for (const row of rows) {
    const fields = row.split("\t");
    additions += Number(fields[0]) || 0;
    deletions += Number(fields[1]) || 0;
  }
  const newFiles = untracked.toString().split("\0").filter(Boolean).length;
  return {
    available: true, root: top, branch: branch === "HEAD" ? `Detached ${head.slice(0, 7)}` : branch,
    isWorktree: path.relative(path.resolve(root, commonDir), gitDir) !== "",
    additions, deletions, files: rows.length + newFiles, untracked: newFiles, reason: ""
  };
}

export interface CreatedWorktree { root: string; branch: string }

export async function createGitWorktree(root: string, branch: string, includeChanges: boolean): Promise<CreatedWorktree> {
  await inspectGitWorkspace(root);
  if (!branch || branch.startsWith("-") || branch.length > 150) throw new Error("Enter a valid new branch name.");
  await runGit(root, ["check-ref-format", "--branch", branch]);
  const conflicts = await runGit(root, ["ls-files", "--unmerged", "-z"]);
  if (conflicts.length) throw new Error("Resolve merge conflicts before creating a worktree.");
  const head = await gitText(root, ["rev-parse", "HEAD"]);
  const staged = includeChanges ? await runGit(root, ["diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-renames", "--cached", "HEAD", "--"]) : Buffer.alloc(0);
  const unstaged = includeChanges ? await runGit(root, ["diff", "--no-ext-diff", "--no-textconv", "--binary", "--no-renames", "--"]) : Buffer.alloc(0);
  const names = includeChanges ? (await runGit(root, ["ls-files", "--others", "--exclude-standard", "-z"])).toString().split("\0").filter(Boolean) : [];
  // Reject dirty submodules: a parent patch cannot faithfully copy their working files.
  if (includeChanges && (await gitText(root, ["submodule", "status", "--recursive"]))) {
    throw new Error("Copying local changes from repositories with submodules is not supported. Use New Worktree from the current commit.");
  }
  if (names.length > 2000) throw new Error("More than 2,000 new files. Commit them first, or start from the current commit.");
  const newFiles: { name: string; data: Buffer; mode: number }[] = [];
  let bytes = staged.length + unstaged.length;
  for (const name of names) {
    const file = path.resolve(root, name);
    const real = await fs.realpath(file);
    const rel = path.relative(await fs.realpath(root), real);
    const stat = await fs.lstat(file);
    if (rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel) || stat.isSymbolicLink() || !stat.isFile()) {
      throw new Error(`Cannot safely copy new file: ${name}. Commit it first.`);
    }
    bytes += stat.size;
    if (bytes > 32 * 1024 * 1024) throw new Error("Local changes exceed 32 MB. Commit them first, or start from the current commit.");
    newFiles.push({ name, data: await fs.readFile(file), mode: stat.mode });
  }
  // Siblings keep .git, node_modules, secrets ignored by Git, and large caches out of the copy.
  const target = path.join(path.dirname(root), `${path.basename(root)}-alysis-${randomUUID().slice(0, 8)}`);
  await runGit(root, ["worktree", "add", "--no-checkout", "-b", branch, target, head]);
  try {
    await runGit(target, ["read-tree", "--reset", "-u", "HEAD"]);
    if (staged.length) await runGit(target, ["apply", "--binary", "--index", "--whitespace=nowarn", "-"], staged);
    if (unstaged.length) await runGit(target, ["apply", "--binary", "--whitespace=nowarn", "-"], unstaged);
    for (const file of newFiles) {
      const destination = path.join(target, file.name);
      await fs.mkdir(path.dirname(destination), { recursive: true });
      // A new worktree must never follow a tracked symlink into another checkout.
      const parent = await fs.realpath(path.dirname(destination));
      const rel = path.relative(await fs.realpath(target), parent);
      if (rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) throw new Error(`Unsafe destination: ${file.name}`);
      await fs.writeFile(destination, file.data, { flag: "wx", mode: file.mode });
    }
  } catch (error) {
    throw new Error(`Worktree preserved at ${target}; your original files are unchanged. ${error instanceof Error ? error.message : String(error)}`);
  }
  return { root: target, branch };
}
