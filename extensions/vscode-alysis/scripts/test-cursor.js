"use strict";
const { existsSync } = require("node:fs");
const { join, resolve } = require("node:path");
const { homedir } = require("node:os");
const { spawn } = require("node:child_process");
const candidates = [process.env.CURSOR_EXECUTABLE_PATH];
if (process.platform === "win32") {
  candidates.push(join(process.env.LOCALAPPDATA || join(homedir(), "AppData", "Local"), "Programs", "cursor", "Cursor.exe"));
} else if (process.platform === "darwin") {
  candidates.push("/Applications/Cursor.app/Contents/MacOS/Cursor", join(homedir(), "Applications/Cursor.app/Contents/MacOS/Cursor"));
} else candidates.push("/usr/bin/cursor", "/opt/Cursor/cursor", "/opt/cursor/cursor");
const executable = candidates.find(candidate => candidate && existsSync(candidate));
if (!executable) throw new Error("Set CURSOR_EXECUTABLE_PATH to an installed Cursor executable. This test never downloads an editor.");
const child = spawn(process.execPath, [resolve(__dirname, "../out/test/integration/runTest.js")], {
  env: { ...process.env, VSCODE_TEST_EXECUTABLE_PATH: executable }, stdio: "inherit", windowsHide: true
});
child.once("error", error => { console.error(error.message); process.exitCode = 1; });
child.once("exit", code => { process.exitCode = code ?? 1; });
