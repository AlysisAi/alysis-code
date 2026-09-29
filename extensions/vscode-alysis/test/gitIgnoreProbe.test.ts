import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { TestContext } from "node:test";
import { promisify } from "node:util";

import { resolveExecutablePath } from "../src/client/ExecutableResolver";
import { GitIgnoreProbe } from "../src/context/GitIgnoreProbe";

const execFileAsync = promisify(execFile);

test("GitIgnoreProbe applies nested, negated, directory, and option-looking patterns", async (t) => {
  const root = await repository(t);
  await writeFile(path.join(root, ".gitignore"), "*.log\n!important.log\nignored/\n--upload-pack=*\n", "utf8");
  await writeFile(path.join(root, "debug.log"), "ignored", "utf8");
  await writeFile(path.join(root, "important.log"), "included", "utf8");
  await mkdir(path.join(root, "ignored"));
  await writeFile(path.join(root, "ignored", "data.ts"), "ignored", "utf8");
  await writeFile(path.join(root, "--upload-pack=evil"), "ignored", "utf8");
  const probe = new GitIgnoreProbe();

  assert.equal(await probe.isIgnored(root, path.join(root, "debug.log")), true);
  assert.equal(await probe.isIgnored(root, path.join(root, "important.log")), false);
  assert.equal(await probe.isIgnored(root, path.join(root, "ignored", "data.ts")), true);
  assert.equal(await probe.isIgnored(root, path.join(root, "--upload-pack=evil")), true);
});

test("GitIgnoreProbe returns false outside Git and rejects containment escapes", async (t) => {
  const root = await temporaryDirectory(t);
  const candidate = path.join(root, "ordinary.txt");
  await writeFile(candidate, "ordinary", "utf8");
  const probe = new GitIgnoreProbe();

  assert.equal(await probe.isIgnored(root, candidate), false);
  await assert.rejects(() => probe.isIgnored(root, path.join(root, "..", "outside.txt")), /inside/);
});

test("GitIgnoreProbe fails closed when Git metadata exists but the configured executable is missing", async (t) => {
  const root = await repository(t);
  const candidate = path.join(root, "ordinary.txt");
  await writeFile(candidate, "ordinary", "utf8");
  const probe = new GitIgnoreProbe({ gitExecutable: "definitely-missing-alysis-git" });

  await assert.rejects(() => probe.isIgnored(root, candidate), /detection failed/);
});

test("GitIgnoreProbe spawns the absolute Git it resolved once, never a bare name next to the workspace", async (t) => {
  const root = await repository(t);
  await writeFile(path.join(root, ".gitignore"), "*.log\n", "utf8");
  await writeFile(path.join(root, "debug.log"), "ignored", "utf8");
  // A repo-planted "git" must be unreachable: the probe resolves through PATH and passes the
  // absolute result to execFile, because libuv searches the child's cwd (the workspace root) first.
  await writeFile(path.join(root, "git"), "#!/bin/sh\nexit 0\n", { mode: 0o755 });
  await writeFile(path.join(root, "git.exe"), "planted", "utf8");
  const requested: string[] = [];
  const probe = new GitIgnoreProbe({
    resolveExecutable: (command) => {
      requested.push(command);
      return resolveExecutablePath(command).resolvedPath;
    }
  });

  assert.equal(await probe.isIgnored(root, path.join(root, "debug.log")), true);
  assert.equal(await probe.isIgnored(root, path.join(root, "kept.ts")), false);
  assert.deepEqual(requested, ["git"], "Git is resolved once and cached for every later probe");
});

test("GitIgnoreProbe fails closed when resolution yields a path that cannot be executed", async (t) => {
  const root = await repository(t);
  await writeFile(path.join(root, "ordinary.txt"), "ordinary", "utf8");
  const probe = new GitIgnoreProbe({
    resolveExecutable: () => path.join(root, "definitely-not-git")
  });

  await assert.rejects(() => probe.isIgnored(root, path.join(root, "ordinary.txt")), /detection failed/);
});

async function repository(t: TestContext): Promise<string> {
  const root = await temporaryDirectory(t);
  await execFileAsync("git", ["init", "--quiet"], { cwd: root, windowsHide: true });
  return root;
}
async function temporaryDirectory(t: TestContext): Promise<string> {
  const root = await mkdtemp(path.join(tmpdir(), "alysis-ignore-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}
