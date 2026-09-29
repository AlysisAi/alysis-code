import assert from "node:assert/strict";
import test from "node:test";

import { CliRunner } from "../src/client/CliDiscovery";
import {
  discoverCliCandidates,
  pythonScriptDirsFromInterpreter,
  validateCliExecutable
} from "../src/client/cliLocator";
import { REQUIRED_BRIDGE_METHODS } from "../src/client/AlysisProtocol";

test("discoverCliCandidates collects setting, PATH, virtualenv, and common-dir candidates", () => {
  const onPath = new Set(["/custom/alysis", "/sys/bin/alysis"]);
  const onDisk = new Set(["/venv/bin/alysis", "/home/u/.local/bin/alysis"]);
  const candidates = discoverCliCandidates({
    platform: "linux",
    env: { PATH: "/sys/bin", VIRTUAL_ENV: "/venv", HOME: "/home/u" },
    homeDir: "/home/u",
    cliPathSetting: "/custom/alysis",
    fileExists: (candidate) => onDisk.has(candidate),
    executableResolver: {
      platform: "linux",
      env: { PATH: "/sys/bin" },
      isExecutableFile: (candidate) => onPath.has(candidate)
    }
  });

  const bySource = new Map(candidates.map((candidate) => [candidate.source, candidate.cliPath]));
  assert.equal(bySource.get("setting"), "/custom/alysis");
  assert.equal(bySource.get("path"), "/sys/bin/alysis");
  assert.equal(bySource.get("virtualenv"), "/venv/bin/alysis");
  assert.equal(bySource.get("common-dir"), "/home/u/.local/bin/alysis");
  // Setting is the highest-priority candidate.
  assert.equal(candidates[0].source, "setting");
});

test("discoverCliCandidates de-duplicates a path that appears from multiple sources", () => {
  const candidates = discoverCliCandidates({
    platform: "linux",
    env: { PATH: "/sys/bin", HOME: "/home/u" },
    homeDir: "/home/u",
    cliPathSetting: "/sys/bin/alysis", // same as the PATH resolution
    fileExists: () => false,
    executableResolver: {
      platform: "linux",
      env: { PATH: "/sys/bin" },
      isExecutableFile: (candidate) => candidate === "/sys/bin/alysis"
    }
  });

  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].cliPath, "/sys/bin/alysis");
  // First writer wins: the setting source is kept.
  assert.equal(candidates[0].source, "setting");
});

test("discoverCliCandidates probes caller-supplied Python script dirs", () => {
  const candidates = discoverCliCandidates({
    platform: "linux",
    env: { PATH: "", HOME: "/home/u" },
    homeDir: "/home/u",
    extraDirs: ["/opt/py/bin"],
    fileExists: (candidate) => candidate === "/opt/py/bin/alysis",
    executableResolver: { platform: "linux", env: { PATH: "" }, isExecutableFile: () => false }
  });

  assert.equal(candidates.length, 1);
  assert.equal(candidates[0].source, "python-scripts");
  assert.equal(candidates[0].cliPath, "/opt/py/bin/alysis");
});

test("validateCliExecutable passes a healthy binary and surfaces version info", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 0.1.4\n", stderr: "", exitCode: 0 };
    }
    if (args.join(" ") === "ide-bridge health") {
      return { stdout: JSON.stringify(healthPayload()), stderr: "", exitCode: 0 };
    }
    throw new Error(`unexpected args: ${args.join(" ")}`);
  };

  const result = await validateCliExecutable(runner, "/opt/alysis");
  assert.equal(result.ok, true);
  assert.equal(result.cliPath, "/opt/alysis");
  assert.equal(result.alysisVersion, "0.1.4");
  assert.equal(result.protocolVersion, "1");
  assert.match(result.version ?? "", /alysis 0\.1\.4/);
});

test("validateCliExecutable fails a non-zero --version without throwing", async () => {
  const runner: CliRunner = async () => ({ stdout: "", stderr: "boom", exitCode: 3 });
  const result = await validateCliExecutable(runner, "/bad/alysis");
  assert.equal(result.ok, false);
  assert.equal(result.code, "cli_nonzero");
});

test("validateCliExecutable detects a CLI too old for ide-bridge health", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 0.0.9\n", stderr: "", exitCode: 0 };
    }
    return { stdout: "", stderr: "Error: No such command 'ide-bridge'.", exitCode: 2 };
  };
  const result = await validateCliExecutable(runner, "/old/alysis");
  assert.equal(result.ok, false);
  assert.equal(result.code, "ide_bridge_missing");
});

test("validateCliExecutable reports a missing binary (ENOENT) as cli_missing", async () => {
  const runner: CliRunner = async () => {
    const error = new Error("spawn ENOENT") as NodeJS.ErrnoException;
    error.code = "ENOENT";
    throw error;
  };
  const result = await validateCliExecutable(runner, "/nope/alysis");
  assert.equal(result.ok, false);
  assert.equal(result.code, "cli_missing");
});

test("validateCliExecutable redacts secrets from health failure output", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 0.1.4\n", stderr: "", exitCode: 0 };
    }
    return { stdout: "", stderr: "auth failed: Bearer sk-abcdefghijklmnop", exitCode: 1 };
  };
  const result = await validateCliExecutable(runner, "/opt/alysis");
  assert.equal(result.ok, false);
  assert.equal(result.code, "health_nonzero");
  assert.doesNotMatch(result.message ?? "", /sk-abcdefghijklmnop/);
});

test("discoverCliCandidates handles win32 exe name, Scripts subdir, APPDATA dir, and case-insensitive dedup", () => {
  const venvExe = "C:\\venv\\Scripts\\alysis.exe";
  const appdataExe = "C:\\Users\\u\\AppData\\Roaming\\Python\\Scripts\\alysis.exe";
  const onDisk = new Set([venvExe, appdataExe]);
  const candidates = discoverCliCandidates({
    platform: "win32",
    env: { Path: "", VIRTUAL_ENV: "C:\\venv", APPDATA: "C:\\Users\\u\\AppData\\Roaming", USERPROFILE: "C:\\Users\\u" },
    homeDir: "C:\\Users\\u",
    cliPathSetting: "c:\\venv\\scripts\\alysis.exe", // same exe, lowercase
    fileExists: (candidate) => onDisk.has(candidate),
    executableResolver: {
      platform: "win32",
      env: { Path: "" },
      isExecutableFile: (candidate) => candidate.toLowerCase() === "c:\\venv\\scripts\\alysis.exe"
    }
  });

  // The virtualenv probe builds C:\venv\Scripts\alysis.exe (Scripts subdir + .exe) and dedups
  // case-insensitively against the lowercase setting candidate -> one entry, setting wins.
  const venv = candidates.filter((candidate) => candidate.cliPath.toLowerCase() === "c:\\venv\\scripts\\alysis.exe");
  assert.equal(venv.length, 1);
  assert.equal(venv[0].source, "setting");
  // The APPDATA\Python\Scripts common dir is probed.
  assert.ok(candidates.some((candidate) => candidate.source === "common-dir" && candidate.cliPath === appdataExe));
});

test("validateCliExecutable maps an unhealthy (exit-0) bridge payload to its structured code", async () => {
  const runner: CliRunner = async (_command, args) => {
    if (args.join(" ") === "--version") {
      return { stdout: "alysis 9.9.9\n", stderr: "", exitCode: 0 };
    }
    // Exit 0 but the wrong protocol version -> parseHealthJson throws a BridgeHealthError.
    return {
      stdout: JSON.stringify({ ok: true, protocol_version: "999", alysis_version: "9.9.9", capabilities: {} }),
      stderr: "",
      exitCode: 0
    };
  };
  const result = await validateCliExecutable(runner, "/future/alysis");
  assert.equal(result.ok, false);
  assert.equal(result.code, "unsupported_protocol_version");
});

test("pythonScriptDirsFromInterpreter derives the interpreter dir and guards bare/relative values", () => {
  assert.deepEqual(pythonScriptDirsFromInterpreter("/home/u/venv/bin/python", "linux"), ["/home/u/venv/bin"]);
  assert.deepEqual(pythonScriptDirsFromInterpreter("C:\\venv\\Scripts\\python.exe", "win32"), ["C:\\venv\\Scripts"]);
  assert.deepEqual(pythonScriptDirsFromInterpreter("", "linux"), []);
  assert.deepEqual(pythonScriptDirsFromInterpreter("python", "linux"), []);
  assert.deepEqual(pythonScriptDirsFromInterpreter("python3", "linux"), []);
  assert.deepEqual(pythonScriptDirsFromInterpreter("relative/python", "linux"), []);
});

function healthPayload(): Record<string, unknown> {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.1.4",
    protocol_version: "1",
    capabilities: {
      protocol_version: "1",
      methods: [...REQUIRED_BRIDGE_METHODS],
      events: ["message_delta"],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl"
    }
  };
}
