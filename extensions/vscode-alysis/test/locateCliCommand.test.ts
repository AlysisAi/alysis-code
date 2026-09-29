import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import type { CliRunner } from "../src/client/CliDiscovery";
import { REQUIRED_BRIDGE_METHODS } from "../src/client/AlysisProtocol";

const disposable = { dispose: () => undefined };
const ConfigurationTarget = { Global: 1, Workspace: 2, WorkspaceFolder: 3 };

const state = {
  isTrusted: true,
  workspaceFolders: [] as Array<{ uri: { fsPath: string } }>,
  quickPick: [] as unknown[],
  openDialog: [] as Array<Array<{ fsPath: string }> | undefined>,
  inputBox: [] as Array<string | undefined>,
  configUpdates: [] as Array<{ key: string; value: unknown; target: unknown }>,
  info: [] as string[],
  warn: [] as string[],
  error: [] as string[],
  reconnects: 0
};

const vscodeStub = {
  ConfigurationTarget,
  ProgressLocation: { Notification: 15 },
  commands: { registerCommand: () => disposable },
  workspace: {
    get isTrusted() {
      return state.isTrusted;
    },
    get workspaceFolders() {
      return state.workspaceFolders.length ? state.workspaceFolders : undefined;
    },
    getConfiguration: () => ({
      update: async (key: string, value: unknown, target: unknown) => {
        state.configUpdates.push({ key, value, target });
      }
    })
  },
  window: {
    showQuickPick: async () => state.quickPick.shift(),
    showOpenDialog: async () => state.openDialog.shift(),
    showInputBox: async () => state.inputBox.shift(),
    showInformationMessage: async (message: string) => {
      state.info.push(message);
    },
    showWarningMessage: async (message: string) => {
      state.warn.push(message);
    },
    showErrorMessage: async (message: string) => {
      state.error.push(message);
    },
    withProgress: async (_options: unknown, task: (...args: unknown[]) => Promise<unknown>) => task(undefined, undefined)
  }
};

const loader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = loader._load;
loader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { runLocateCli } = require("../src/commands/locateCli") as typeof import("../src/commands/locateCli");
loader._load = originalLoad;

function reset(): void {
  state.isTrusted = true;
  state.workspaceFolders = [];
  state.quickPick = [];
  state.openDialog = [];
  state.inputBox = [];
  state.configUpdates = [];
  state.info = [];
  state.warn = [];
  state.error = [];
  state.reconnects = 0;
}

const okRunner: CliRunner = async (_command, args) => {
  if (args.join(" ") === "--version") {
    return { stdout: "alysis 0.1.4\n", stderr: "", exitCode: 0 };
  }
  return {
    stdout: JSON.stringify({
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
    }),
    stderr: "",
    exitCode: 0
  };
};

function makeDeps(runner: CliRunner): { deps: any; calls: () => number } {
  let runnerCalls = 0;
  const wrapped: CliRunner = (command, args, options) => {
    runnerCalls += 1;
    return runner(command, args, options);
  };
  return {
    deps: {
      runner: wrapped,
      getConfig: () => ({ cliPath: "" }),
      reconnect: () => {
        state.reconnects += 1;
      },
      pythonScriptDirs: () => []
    },
    calls: () => runnerCalls
  };
}

test("runLocateCli refuses an untrusted workspace-local binary without spawning or saving", async () => {
  reset();
  state.isTrusted = false;
  state.workspaceFolders = [{ uri: { fsPath: "/ws" } }];
  state.quickPick = [{ action: "candidate", cliPath: "/ws/bin/alysis" }];
  const { deps, calls } = makeDeps(okRunner);

  await runLocateCli(deps);

  assert.equal(calls(), 0, "must not spawn the workspace-local binary");
  assert.equal(state.configUpdates.length, 0, "must not save");
  assert.equal(state.warn.length, 1, "warns the user");
  assert.equal(state.reconnects, 0);
});

test("runLocateCli does not save when validation fails", async () => {
  reset();
  state.isTrusted = true;
  state.quickPick = [{ action: "candidate", cliPath: "/opt/alysis" }];
  const badRunner: CliRunner = async () => ({ stdout: "", stderr: "boom", exitCode: 2 });
  const { deps } = makeDeps(badRunner);

  await runLocateCli(deps);

  assert.equal(state.configUpdates.length, 0, "invalid binary is never saved");
  assert.equal(state.error.length, 1);
  assert.equal(state.reconnects, 0);
});

test("runLocateCli validates, saves cliPath globally (no workspace), and reconnects", async () => {
  reset();
  state.isTrusted = true;
  state.workspaceFolders = []; // no workspace -> Global target, no scope prompt
  state.quickPick = [{ action: "candidate", cliPath: "/opt/alysis" }];
  const { deps, calls } = makeDeps(okRunner);

  await runLocateCli(deps);

  assert.equal(calls(), 2, "spawned --version + ide-bridge health");
  assert.equal(state.configUpdates.length, 1);
  assert.deepEqual(state.configUpdates[0], {
    key: "cliPath",
    value: "/opt/alysis",
    target: ConfigurationTarget.Global
  });
  assert.equal(state.reconnects, 1);
  assert.equal(state.info.length, 1);
});

test("runLocateCli forces global save scope when the workspace is untrusted", async () => {
  reset();
  state.isTrusted = false;
  state.workspaceFolders = [{ uri: { fsPath: "/ws" } }];
  // Chosen binary is OUTSIDE the workspace, so the trust gate passes; but an untrusted
  // workspace-scoped cliPath would be ignored, so the save must go to Global without prompting.
  state.quickPick = [{ action: "candidate", cliPath: "/opt/alysis" }];
  const { deps } = makeDeps(okRunner);

  await runLocateCli(deps);

  assert.equal(state.configUpdates.length, 1);
  assert.equal(state.configUpdates[0].target, ConfigurationTarget.Global);
});
