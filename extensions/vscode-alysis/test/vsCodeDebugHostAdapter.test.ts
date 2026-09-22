import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import type * as vscode from "vscode";
import type { HostActionExecutionContext } from "../src/hostActions/HostActionAdapters";

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  return request === "vscode" ? {} : originalLoad.call(this, request, parent, isMain);
};
const {
  VsCodeDebugHostAdapter
} = require("../src/hostActions/VsCodeDebugHostAdapter") as typeof import("../src/hostActions/VsCodeDebugHostAdapter");
moduleLoader._load = originalLoad;

test("Debug configuration discovery is workspace-fenced, deterministic, and capped", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(...Array.from({ length: 105 }, (_, index) => config(`Launch ${index}`)));
  fixture.configurations.push({ name: "Invalid", type: "node", request: "unknown" });
  const adapter = new VsCodeDebugHostAdapter(fixture.api);

  const first = await adapter.execute("debug.list", {}, context());
  const second = await adapter.execute("debug.list", {}, context());
  const rows = first.configurations as Array<Record<string, unknown>>;

  assert.equal(rows.length, 100);
  assert.equal(first.truncated, true);
  assert.deepEqual(first, second);
  assert.ok(rows.every((row) => String(row.id).startsWith("debug_")));
  assert.ok(rows.every((row) => row.workspace_folder === "workspace"));
  adapter.dispose();
});

test("Debug start returns only the matching observed workspace session", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);

  const result = await adapter.execute("debug.start", { configuration_id: configurationId }, context());

  assert.deepEqual(result, {
    debug_session_id: "debug-session-1",
    configuration_id: configurationId,
    state: "started"
  });
  assert.equal(fixture.startCalls, 1);
  adapter.dispose();
});

test("Debug status and stop preserve running-to-stopped lifecycle without cross-workspace access", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);
  await adapter.execute("debug.start", { configuration_id: configurationId }, context());

  const running = await adapter.execute("debug.status", { debug_session_id: "debug-session-1" }, context());
  assert.equal((running.sessions as Array<Record<string, unknown>>)[0].state, "running");
  const stopped = await adapter.execute("debug.stop", { debug_session_id: "debug-session-1" }, context());
  const status = await adapter.execute("debug.status", { debug_session_id: "debug-session-1" }, context());
  const repeated = await adapter.execute("debug.stop", { debug_session_id: "debug-session-1" }, context());

  assert.deepEqual(stopped, { debug_session_id: "debug-session-1", stopped: true, state: "stopped" });
  assert.equal((status.sessions as Array<Record<string, unknown>>)[0].state, "stopped");
  assert.deepEqual(repeated, { debug_session_id: "debug-session-1", stopped: false, state: "already_ended" });
  assert.equal(fixture.stopCalls, 1);
  adapter.dispose();
});

test("Debug refuses unknown configurations and sessions without starting or stopping anything", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  const adapter = new VsCodeDebugHostAdapter(fixture.api);

  await assert.rejects(
    adapter.execute("debug.start", { configuration_id: "debug_missing" }, context()),
    (error: any) => error?.code === "debug_configuration_not_found"
  );
  const stop = await adapter.execute("debug.stop", { debug_session_id: "debug-missing" }, context());
  assert.deepEqual(stop, { debug_session_id: "debug-missing", stopped: false, state: "not_found" });
  assert.equal(fixture.startCalls, 0);
  assert.equal(fixture.stopCalls, 0);
  adapter.dispose();
});

test("Debug cancellation fences a late start event", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  fixture.deferStart = true;
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);
  const abort = new AbortController();
  const starting = adapter.execute(
    "debug.start",
    { configuration_id: configurationId },
    context(Date.now() + 30_000, abort.signal)
  );
  await new Promise<void>((resolve) => setImmediate(resolve));
  abort.abort();
  fixture.releaseStart?.();

  await assert.rejects(starting, (error: any) => error?.code === "host_action_cancelled");
  assert.equal(fixture.stopCalls, 1, "a debug session started during cancellation must be stopped");
  adapter.dispose();
});

test("Debug start waits for an onDidStart event that arrives after startDebugging resolves", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  fixture.delayStartEvent = true;
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);

  const starting = adapter.execute("debug.start", { configuration_id: configurationId }, context());
  await new Promise<void>((resolve) => setImmediate(resolve));
  fixture.releaseStartEvent?.();
  const result = await starting;

  assert.equal(result.debug_session_id, "debug-session-1");
  assert.equal(fixture.stopCalls, 0);
  adapter.dispose();
});

test("a debug start event arriving after timeout is stopped as an unowned orphan", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  fixture.delayStartEvent = true;
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);

  await assert.rejects(
    adapter.execute("debug.start", { configuration_id: configurationId }, context(Date.now() + 120)),
    (error: any) => error?.code === "debug_start_timeout"
  );
  fixture.releaseStartEvent?.();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(fixture.stopCalls, 1);
  assert.deepEqual(await adapter.execute("debug.status", {}, context()), { sessions: [], truncated: false });
  adapter.dispose();
});

test("late-start cleanup only stops the session its own request started", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  fixture.delayStartEvent = true;
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);

  await assert.rejects(
    adapter.execute("debug.start", { configuration_id: configurationId }, context(Date.now() + 120)),
    (error: any) => error?.code === "debug_start_timeout"
  );

  // The user starts an identically named session inside the 5s cleanup window.
  fixture.emitStarted(debugSession("user-session", "Launch app", "node", ROOT_FOLDER));
  assert.equal(fixture.stopCalls, 0, "an unrelated same-named session must never be killed");

  fixture.releaseStartEvent?.();
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(fixture.stopCalls, 1);
  adapter.dispose();
});

test("user-started debug sessions are never exposed or stoppable by host actions", async () => {
  const fixture = debugFixture();
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  fixture.emitStarted(debugSession("user-session", "User launch", "node", ROOT_FOLDER));

  const status = await adapter.execute("debug.status", {}, context());
  const stop = await adapter.execute("debug.stop", { debug_session_id: "user-session" }, context());

  assert.deepEqual(status, { sessions: [], truncated: false });
  assert.deepEqual(stop, { debug_session_id: "user-session", stopped: false, state: "not_found" });
  assert.equal(fixture.stopCalls, 0);
  adapter.dispose();
});

test("Debug disposal and exact session invalidation stop every owned session", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);
  await adapter.execute("debug.start", { configuration_id: configurationId }, context());

  adapter.invalidateSession("session-1", "wf_wrong");
  assert.equal(fixture.stopCalls, 0);
  adapter.invalidateSession("session-1", "wf_test");
  assert.equal(fixture.stopCalls, 1);

  await adapter.execute("debug.start", { configuration_id: configurationId }, context());
  adapter.dispose();
  assert.equal(fixture.stopCalls, 2);
});

test("Debug stop never reports success when VS Code omits termination", async () => {
  const fixture = debugFixture();
  fixture.configurations.push(config("Launch app"));
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  const listed = await adapter.execute("debug.list", {}, context());
  const configurationId = String((listed.configurations as Array<Record<string, unknown>>)[0].id);
  await adapter.execute("debug.start", { configuration_id: configurationId }, context());
  fixture.stopEmitsTermination = false;

  await assert.rejects(
    adapter.execute("debug.stop", { debug_session_id: "debug-session-1" }, context(Date.now() + 120)),
    (error: any) => error?.code === "debug_stop_timeout" && error?.retryable === true
  );
  const status = await adapter.execute("debug.status", {}, context());
  assert.equal((status.sessions as Array<Record<string, unknown>>)[0].state, "running");
  adapter.dispose();
});

test("Debug rejects argument smuggling and never exposes breakpoint mutation actions", async () => {
  const fixture = debugFixture();
  const adapter = new VsCodeDebugHostAdapter(fixture.api);
  assert.deepEqual(adapter.actions, ["debug.list", "debug.start", "debug.stop", "debug.status"]);
  await assert.rejects(
    adapter.execute("debug.list", { breakpoints: [{ line: 1 }] }, context()),
    (error: any) => error?.code === "invalid_arguments"
  );
  adapter.dispose();
});

function debugFixture() {
  const startListeners = new Set<(session: vscode.DebugSession) => unknown>();
  const terminateListeners = new Set<(session: vscode.DebugSession) => unknown>();
  const configurations: vscode.DebugConfiguration[] = [];
  const running = new Map<string, vscode.DebugSession>();
  const fixture = {
    configurations,
    startCalls: 0,
    stopCalls: 0,
    deferStart: false,
    delayStartEvent: false,
    stopEmitsTermination: true,
    releaseStart: undefined as (() => void) | undefined,
    releaseStartEvent: undefined as (() => void) | undefined,
    emitStarted(session: vscode.DebugSession) {
      running.set(session.id, session);
      for (const listener of [...startListeners]) {
        listener(session);
      }
    },
    emitTerminated(session: vscode.DebugSession) {
      running.delete(session.id);
      for (const listener of [...terminateListeners]) {
        listener(session);
      }
    },
    api: undefined as unknown as import("../src/hostActions/VsCodeDebugHostAdapter").VsCodeDebugApi
  };
  fixture.api = {
    workspaceFolders: () => [ROOT_FOLDER, OTHER_FOLDER],
    configurations: (folder) => folder.name === "workspace" ? configurations : [config("Sibling")],
    startDebugging: async (folder, configuration) => {
      fixture.startCalls += 1;
      if (fixture.deferStart) {
        await new Promise<void>((resolve) => { fixture.releaseStart = resolve; });
      }
      // VS Code publishes the resolved configuration it was handed, including caller-added keys.
      const session = debugSession(
        "debug-session-1",
        configuration.name,
        configuration.type,
        folder,
        configuration
      );
      if (fixture.delayStartEvent) {
        fixture.releaseStartEvent = () => fixture.emitStarted(session);
      } else {
        fixture.emitStarted(session);
      }
      return true;
    },
    stopDebugging: async (session) => {
      fixture.stopCalls += 1;
      if (fixture.stopEmitsTermination) {
        fixture.emitTerminated(session);
      }
    },
    onDidStartDebugSession: (listener) => {
      startListeners.add(listener);
      return { dispose: () => startListeners.delete(listener) };
    },
    onDidTerminateDebugSession: (listener) => {
      terminateListeners.add(listener);
      return { dispose: () => terminateListeners.delete(listener) };
    }
  };
  return fixture;
}

const ROOT_FOLDER = folder("C:\\workspace", "workspace", 0);
const OTHER_FOLDER = folder("C:\\other", "other", 1);

function config(name: string): vscode.DebugConfiguration {
  return { name, type: "node", request: "launch", program: "${workspaceFolder}/app.js" };
}

function debugSession(
  id: string,
  name: string,
  type: string,
  workspaceFolder: vscode.WorkspaceFolder,
  configuration: vscode.DebugConfiguration = config(name)
): vscode.DebugSession {
  return { id, name, type, workspaceFolder, configuration } as vscode.DebugSession;
}

function folder(fsPath: string, name: string, index: number): vscode.WorkspaceFolder {
  return { uri: fileUri(fsPath), name, index } as vscode.WorkspaceFolder;
}

function fileUri(fsPath: string): vscode.Uri {
  return {
    scheme: "file",
    authority: "",
    fsPath,
    toString: () => `file:///${fsPath.replaceAll("\\", "/")}`
  } as vscode.Uri;
}

function context(
  deadlineMs = Date.now() + 5_000,
  signal: AbortSignal = new AbortController().signal
): HostActionExecutionContext {
  return {
    sessionId: "session-1",
    workspaceFence: "wf_test",
    workspace: { root: "C:\\workspace", scheme: "file", authority: "", name: "workspace" },
    signal,
    deadlineMs,
    maxResultBytes: 65_536
  };
}
