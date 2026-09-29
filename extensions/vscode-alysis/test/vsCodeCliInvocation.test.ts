import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { resolveWindowsVsCodeCliWrapper } from "./integration/vsCodeCliInvocation";

test("Windows VS Code CLI wrapper resolves to direct Code.exe without shell execution", () => {
  withFixture((root) => {
    const executable = join(root, "Code.exe");
    const wrapper = join(root, "bin", "code.cmd");
    const cliScript = join(root, "build-id", "resources", "app", "out", "cli.js");
    for (const directory of [join(root, "bin"), join(root, "build-id", "resources", "app", "out")]) {
      mkdirSync(directory, { recursive: true });
    }
    writeFileSync(executable, "code", "utf8");
    writeFileSync(cliScript, "cli", "utf8");
    const source = [
      "@echo off",
      "set ELECTRON_RUN_AS_NODE=1",
      '"%~dp0..\\Code.exe" "%~dp0..\\build-id\\resources\\app\\out\\cli.js" %*'
    ].join("\r\n");
    writeFileSync(wrapper, source, "utf8");

    const invocation = resolveWindowsVsCodeCliWrapper(executable, wrapper, source, ["--fixed"]);

    assert.equal(invocation.command, executable);
    assert.deepEqual(invocation.prefixArgs, [cliScript, "--fixed"]);
    assert.deepEqual(invocation.environment, {
      ELECTRON_RUN_AS_NODE: "1",
      VSCODE_DEV: ""
    });
  });
});

test("Windows VS Code CLI wrapper rejects ambiguous or rewritten script targets", () => {
  withFixture((root) => {
    const executable = join(root, "Code.exe");
    const wrapper = join(root, "bin", "code.cmd");
    mkdirSync(join(root, "bin"), { recursive: true });
    writeFileSync(executable, "code", "utf8");
    writeFileSync(wrapper, "malformed", "utf8");

    assert.throws(
      () => resolveWindowsVsCodeCliWrapper(executable, wrapper, "malformed"),
      /one exact cli\.js invocation/
    );
  });
});

function withFixture(callback: (root: string) => void): void {
  const root = mkdtempSync(join(tmpdir(), "alysis-code-cli-"));
  try {
    callback(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}
