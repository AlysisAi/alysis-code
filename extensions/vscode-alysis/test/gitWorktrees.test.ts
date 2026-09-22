import assert from "node:assert/strict";
import { mkdtemp, writeFile, readFile, mkdir, chmod } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { createGitWorktree, gitWorkspaceFailureReason, inspectGitWorkspace, runGit } from "../src/workspace/GitWorktrees";

test("Git ownership failures explain access rather than asking for a nonexistent first commit", () => {
  const reason = gitWorkspaceFailureReason(new Error("fatal: detected dubious ownership in repository at '/private/location'\nTo add an exception: git config --global --add safe.directory /private/location"));
  assert.match(reason, /ownership/);
  assert.doesNotMatch(reason, /first commit|private\/location|safe.directory/);
  assert.match(gitWorkspaceFailureReason(new Error("fatal: Needed a single revision")), /first commit/);
  assert.match(gitWorkspaceFailureReason(new Error("fatal: not a git repository")), /Open a Git repository/);
  assert.match(gitWorkspaceFailureReason(new Error("Git executable not found (ENOENT).")), /installed/);
  assert.doesNotMatch(gitWorkspaceFailureReason(new Error("secret arbitrary stderr")), /secret arbitrary/);
});

async function fixture() {
  const parent = await mkdtemp(path.join(os.tmpdir(), "alysis-worktree-test-"));
  const root = path.join(parent, "repo with spaces");
  await mkdir(root);
  await runGit(root, ["init", "-b", "main"]);
  await runGit(root, ["config", "user.email", "test@example.invalid"]);
  await runGit(root, ["config", "user.name", "Test"]);
  await runGit(root, ["config", "core.autocrlf", "false"]);
  await writeFile(path.join(root, "file.txt"), "one\ntwo\n");
  await writeFile(path.join(root, "delete.txt"), "delete\n");
  await writeFile(path.join(root, ".gitignore"), "ignored/\n");
  await runGit(root, ["add", "."]);
  await runGit(root, ["-c", "commit.gpgsign=false", "commit", "-m", "initial"]);
  return root;
}

test("real worktree copies staged, unstaged, binary, deleted and Unicode files, preserving the source index", async () => {
  const root = await fixture();
  await writeFile(path.join(root, "file.txt"), "staged\ntwo\n");
  await runGit(root, ["add", "file.txt"]);
  await writeFile(path.join(root, "file.txt"), "staged\nunstaged\nextra\n");
  await runGit(root, ["rm", "delete.txt"]);
  await writeFile(path.join(root, "new ά name.txt"), "new\n");
  await writeFile(path.join(root, "binary.bin"), Buffer.from([0, 255, 18, 0]));
  await mkdir(path.join(root, "ignored"));
  await writeFile(path.join(root, "ignored", "private.txt"), "not copied");
  const beforeStatus = await runGit(root, ["status", "--porcelain=v1", "-z"]);
  const beforeIndex = await readFile(path.join(root, ".git", "index"));
  const created = await createGitWorktree(root, "alysis/copy", true);
  assert.deepEqual(await runGit(created.root, ["status", "--porcelain=v1", "-z"]), beforeStatus);
  assert.deepEqual(await readFile(path.join(created.root, "file.txt")), await readFile(path.join(root, "file.txt")));
  assert.deepEqual(await readFile(path.join(created.root, "binary.bin")), Buffer.from([0, 255, 18, 0]));
  await assert.rejects(readFile(path.join(created.root, "ignored", "private.txt")), /ENOENT/);
  assert.deepEqual(await runGit(root, ["status", "--porcelain=v1", "-z"]), beforeStatus);
  assert.deepEqual(await readFile(path.join(root, ".git", "index")), beforeIndex);
  const stats = await inspectGitWorkspace(created.root);
  assert.equal(stats.isWorktree, true);
  assert.equal(stats.branch, "alysis/copy");
  assert.equal(stats.additions, 3);
  assert.equal(stats.deletions, 3);
  assert.equal(stats.untracked, 2);
  assert.equal(stats.files, 4);
});

test("fresh worktree uses HEAD and does not execute post-checkout hooks", async () => {
  const root = await fixture();
  await writeFile(path.join(root, "file.txt"), "local change\n");
  await writeFile(path.join(root, ".git", "hooks", "post-checkout"), "#!/bin/sh\necho hook-ran > hook-marker\n");
  await chmod(path.join(root, ".git", "hooks", "post-checkout"), 0o755);
  const created = await createGitWorktree(root, "alysis/clean", false);
  assert.equal(await readFile(path.join(created.root, "file.txt"), "utf8"), "one\ntwo\n");
  assert.equal((await runGit(created.root, ["status", "--porcelain"])).length, 0);
  assert.equal(await readFile(path.join(root, "file.txt"), "utf8"), "local change\n");
  await assert.rejects(readFile(path.join(created.root, "hook-marker")), /ENOENT/);
});

test("invalid/existing branch and nested workspace fail without touching original files", async () => {
  const root = await fixture();
  await mkdir(path.join(root, "nested"));
  await assert.rejects(createGitWorktree(root, "--force", false), /branch name/);
  await assert.rejects(createGitWorktree(root, "main", false), /already exists/);
  await assert.rejects(createGitWorktree(path.join(root, "nested"), "alysis/no", false), /repository root/);
  assert.equal(await readFile(path.join(root, "file.txt"), "utf8"), "one\ntwo\n");
});
