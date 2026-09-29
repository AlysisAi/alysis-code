import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import path from "node:path";

import type * as vscode from "vscode";
import type { HostActionExecutionContext } from "../src/hostActions/HostActionAdapters";

const vscodeStub = {
  TaskScope: { Global: 1, Workspace: 2 },
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 }
};
const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const {
  VsCodeTasksHostAdapter
} = require("../src/hostActions/VsCodeTasksHostAdapter") as typeof import("../src/hostActions/VsCodeTasksHostAdapter");
moduleLoader._load = originalLoad;

test("Tasks catalog is workspace-scoped, deterministic, filtered, and capped at 100", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(...Array.from({ length: 105 }, (_, index) => task(`Build ${index}`, "npm", ROOT_FOLDER)));
  fixture.tasks.push(task("Sibling", "npm", OTHER_FOLDER));
  fixture.tasks.push(task("Test", "shell", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);

  const first = await adapter.execute("tasks.list", { task_type: "npm" }, context());
  const second = await adapter.execute("tasks.list", { task_type: "npm" }, context());
  const rows = first.tasks as Array<Record<string, unknown>>;

  assert.equal(rows.length, 100);
  assert.equal(first.truncated, true);
  assert.deepEqual(first, second);
  assert.equal(rows.some((row) => row.label === "Sibling"), false);
  assert.ok(rows.every((row) => String(row.id).startsWith("task_")));
  adapter.dispose();
});

test("Tasks run observes terminal exit code and returns a redacted bounded diagnostics delta", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Compile", "npm", ROOT_FOLDER));
  fixture.onExecute = (execution) => {
    setImmediate(() => {
      fixture.diagnostics = [[fileUri(path.join(WORKSPACE_ROOT, "src", "app.ts")), [diagnostic(
        "Bearer abcdefghijklmnopqrstuvwxyz and sk-test-secret-value-123456",
        "source-sk-test-secret-value-123456"
      )]]];
      fixture.emitEndProcess(execution, 2);
    });
  };
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  const listed = await adapter.execute("tasks.list", {}, context());
  const taskId = String((listed.tasks as Array<Record<string, unknown>>)[0].id);

  const result = await adapter.execute("tasks.run", { task_id: taskId }, context());
  const delta = result.diagnostics_delta as Record<string, unknown>;
  const item = (delta.items as Array<Record<string, unknown>>)[0];

  assert.equal(result.state, "failed");
  assert.equal(result.exit_code, 2);
  assert.equal(delta.added, 1);
  assert.equal(delta.truncated, false);
  assert.doesNotMatch(JSON.stringify(item), /abcdefghijklmnop|sk-test-secret-value-123456/);
  assert.match(JSON.stringify(item), /<redacted>/);
  adapter.dispose();
});

test("long-running Tasks return started, can be terminated once, and retain ended identity", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Watch", "npm", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  const listed = await adapter.execute("tasks.list", {}, context());
  const taskId = String((listed.tasks as Array<Record<string, unknown>>)[0].id);
  const result = await adapter.execute("tasks.run", { task_id: taskId }, context(Date.now() + 120));
  assert.equal(result.state, "started");
  const executionId = String(result.execution_id);

  const terminated = await adapter.execute("tasks.terminate", { execution_id: executionId }, context());
  const repeated = await adapter.execute("tasks.terminate", { execution_id: executionId }, context());
  assert.deepEqual(terminated, { execution_id: executionId, terminated: true, state: "terminated" });
  assert.deepEqual(repeated, { execution_id: executionId, terminated: false, state: "already_ended" });
  assert.equal(fixture.executions[0].terminateCount, 1);
  adapter.dispose();
});

test("cancelling Tasks run terminates the owned execution and rejects deterministically", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Serve", "npm", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  const listed = await adapter.execute("tasks.list", {}, context());
  const taskId = String((listed.tasks as Array<Record<string, unknown>>)[0].id);
  const abort = new AbortController();
  const running = adapter.execute("tasks.run", { task_id: taskId }, context(Date.now() + 30_000, abort.signal));
  await new Promise<void>((resolve) => setImmediate(resolve));
  abort.abort();

  await assert.rejects(running, (error: any) => error?.code === "host_action_cancelled");
  assert.equal(fixture.executions[0].terminateCount, 1);
  adapter.dispose();
});

test("adapter disposal terminates every still-owned task execution", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Dev server", "npm", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  const listed = await adapter.execute("tasks.list", {}, context());
  const taskId = String((listed.tasks as Array<Record<string, unknown>>)[0].id);
  await adapter.execute("tasks.run", { task_id: taskId }, context(Date.now() + 110));

  adapter.dispose();

  assert.equal(fixture.executions[0].terminateCount, 1);
});

test("Tasks status observes running and terminal lifecycle for the exact owning session", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Watch", "npm", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  const listed = await adapter.execute("tasks.list", {}, context());
  const taskId = String((listed.tasks as Array<Record<string, unknown>>)[0].id);
  const started = await adapter.execute("tasks.run", { task_id: taskId }, context(Date.now() + 110));
  const executionId = String(started.execution_id);

  const running = await adapter.execute("tasks.status", { execution_id: executionId }, context());
  assert.equal((running.executions as Array<Record<string, unknown>>)[0].state, "running");
  const otherSession = { ...context(), sessionId: "other-session" };
  const hidden = await adapter.execute("tasks.status", { execution_id: executionId }, otherSession);
  assert.deepEqual(hidden, { executions: [], truncated: false });

  fixture.diagnostics = [[fileUri(path.join(WORKSPACE_ROOT, "src", "watch.ts")), [diagnostic(
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
    "watcher"
  )]]];
  fixture.emitEndProcess(fixture.executions[0], 0);
  const completed = await adapter.execute("tasks.status", { execution_id: executionId }, context());
  const completedRow = (completed.executions as Array<Record<string, unknown>>)[0];
  assert.equal(completedRow.state, "completed");
  assert.equal(completedRow.exit_code, 0);
  assert.equal((completedRow.diagnostics_delta as Record<string, unknown>).added, 1);
  assert.doesNotMatch(JSON.stringify(completedRow.diagnostics_delta), /abcdefghijklmnopqrstuvwxyz/);
  adapter.dispose();
});

test("session invalidation terminates only executions owned by the exact session and fence", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Watch", "npm", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  const listed = await adapter.execute("tasks.list", {}, context());
  const taskId = String((listed.tasks as Array<Record<string, unknown>>)[0].id);
  await adapter.execute("tasks.run", { task_id: taskId }, context(Date.now() + 110));

  adapter.invalidateSession("session-1", "wf_wrong");
  assert.equal(fixture.executions[0].terminateCount, 0);
  adapter.invalidateSession("session-1", "wf_test");
  assert.equal(fixture.executions[0].terminateCount, 1);
  adapter.dispose();
});

test("Tasks reject unknown arguments and task ids without executing anything", async () => {
  const fixture = taskFixture();
  fixture.tasks.push(task("Compile", "npm", ROOT_FOLDER));
  const adapter = new VsCodeTasksHostAdapter(fixture.api);
  await assert.rejects(
    adapter.execute("tasks.list", { unexpected: true }, context()),
    (error: any) => error?.code === "invalid_arguments"
  );
  await assert.rejects(
    adapter.execute("tasks.run", { task_id: "task_missing" }, context()),
    (error: any) => error?.code === "task_not_found"
  );
  assert.equal(fixture.executions.length, 0);
  adapter.dispose();
});

// The adapter decides workspace containment with path.relative, so these fixtures must use the
// host platform's separators. Hard-coded Windows paths silently fail every containment check on
// POSIX, which is what CI runs on.
const WORKSPACE_ROOT = path.resolve(path.sep === "\\" ? "C:\\workspace" : "/workspace");
const OTHER_ROOT = path.resolve(path.sep === "\\" ? "C:\\other" : "/other");
const ROOT_FOLDER = folder(WORKSPACE_ROOT, "workspace");
const OTHER_FOLDER = folder(OTHER_ROOT, "other");

function task(name: string, type: string, scope: vscode.WorkspaceFolder): vscode.Task {
  return {
    definition: { type, script: name },
    scope,
    name,
    detail: `${name} details`,
    source: type,
    isBackground: false,
    presentationOptions: {},
    problemMatchers: [],
    runOptions: {}
  } as unknown as vscode.Task;
}

function taskFixture() {
  const tasks: vscode.Task[] = [];
  const endListeners = new Set<(event: { execution: vscode.TaskExecution }) => unknown>();
  const processEndListeners = new Set<(event: vscode.TaskProcessEndEvent) => unknown>();
  const executions: Array<vscode.TaskExecution & { terminateCount: number }> = [];
  const fixture = {
    tasks,
    executions,
    diagnostics: [] as Array<[vscode.Uri, readonly vscode.Diagnostic[]]>,
    onExecute: undefined as ((execution: vscode.TaskExecution) => void) | undefined,
    emitEndProcess(execution: vscode.TaskExecution, exitCode?: number) {
      for (const listener of [...processEndListeners]) {
        listener({ execution, exitCode });
      }
      for (const listener of [...endListeners]) {
        listener({ execution });
      }
    },
    api: undefined as unknown as import("../src/hostActions/VsCodeTasksHostAdapter").VsCodeTasksApi
  };
  fixture.api = {
    fetchTasks: async (filter) => tasks.filter((candidate) => !filter?.type || candidate.definition.type === filter.type),
    executeTask: async (selected) => {
      const execution = {
        task: selected,
        terminateCount: 0,
        terminate() { this.terminateCount += 1; }
      } as vscode.TaskExecution & { terminateCount: number };
      executions.push(execution);
      fixture.onExecute?.(execution);
      return execution;
    },
    get taskExecutions() { return executions; },
    onDidEndTask: (listener) => {
      endListeners.add(listener);
      return { dispose: () => endListeners.delete(listener) };
    },
    onDidEndTaskProcess: (listener) => {
      processEndListeners.add(listener);
      return { dispose: () => processEndListeners.delete(listener) };
    },
    diagnostics: () => fixture.diagnostics,
    workspaceFolder: (uri) =>
      uri.fsPath.toLowerCase().startsWith(WORKSPACE_ROOT.toLowerCase()) ? ROOT_FOLDER : OTHER_FOLDER,
    workspaceFolderCount: () => 2
  };
  return fixture;
}

function context(
  deadlineMs = Date.now() + 5_000,
  signal: AbortSignal = new AbortController().signal
): HostActionExecutionContext {
  return {
    sessionId: "session-1",
    workspaceFence: "wf_test",
    workspace: { root: WORKSPACE_ROOT, scheme: "file", authority: "", name: "workspace" },
    signal,
    deadlineMs,
    maxResultBytes: 65_536
  };
}

function folder(fsPath: string, name: string): vscode.WorkspaceFolder {
  return { uri: fileUri(fsPath), name, index: 0 } as vscode.WorkspaceFolder;
}

function fileUri(fsPath: string): vscode.Uri {
  return {
    scheme: "file",
    authority: "",
    fsPath,
    toString: () => `file:///${fsPath.replaceAll("\\", "/")}`
  } as vscode.Uri;
}

function diagnostic(message: string, source: string): vscode.Diagnostic {
  return {
    range: { start: { line: 2, character: 1 }, end: { line: 2, character: 5 } },
    message,
    severity: 0,
    source,
    code: "TS1"
  } as vscode.Diagnostic;
}
