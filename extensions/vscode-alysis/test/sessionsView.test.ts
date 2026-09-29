import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

class FakeTreeItem {
  public description?: string;
  public tooltip?: string;
  public iconPath?: unknown;
  public contextValue?: string;
  public constructor(public label: string, public collapsibleState: unknown) {}
}

class FakeEventEmitter {
  public event = (): { dispose(): void } => ({ dispose: () => undefined });
  public fire(): void {
    // no-op for the test
  }
}

const vscodeStub = {
  TreeItem: FakeTreeItem,
  TreeItemCollapsibleState: { None: 0 },
  EventEmitter: FakeEventEmitter,
  ThemeIcon: class {
    public constructor(public id: string) {}
  }
};

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { SessionsViewProvider } = require("../src/views/sessionsView") as typeof import("../src/views/sessionsView");

function summary(overrides: Record<string, unknown>): any {
  return {
    session_id: "s1",
    workspace_root: "/workspace",
    mode: "review",
    closed: false,
    active_job: null,
    last_job: null,
    ...overrides
  };
}

test("SessionsViewProvider distinguishes active / retained / closed sessions by metadata and contextValue", () => {
  const view = new SessionsViewProvider();
  view.setSessions([
    summary({ session_id: "live" }),
    summary({ session_id: "old" }),
    summary({ session_id: "done", closed: true })
  ]);
  view.setActiveSession("live");

  const items = view.getChildren();
  assert.equal(items.length, 3);
  const byId = (id: string) => items.find((item) => item.sessionId === id);
  // The live session row gets the "active" contextValue so its menus offer the active-only actions.
  assert.equal(byId("live")?.contextValue, "alysisSessionActive");
  assert.equal(byId("live")?.sessionId, "live");
  assert.equal(byId("live")?.sessionState, "active");
  assert.equal(byId("live")?.canMutateLiveSession, true);
  assert.equal(byId("live")?.canResumeIntoLiveSession, false);
  assert.equal(byId("live")?.label, "Current task");
  assert.equal(byId("live")?.description, "Review · Active");
  assert.equal(byId("old")?.contextValue, "alysisSessionRetained");
  assert.equal(byId("old")?.sessionState, "retained");
  assert.equal(byId("old")?.canMutateLiveSession, false);
  assert.equal(byId("old")?.canResumeIntoLiveSession, true);
  assert.equal(byId("done")?.contextValue, "alysisSessionClosed");
  assert.equal(byId("done")?.sessionState, "closed");
});

test("SessionsViewProvider marks failed and historical retained sessions explicitly", () => {
  const view = new SessionsViewProvider();
  view.setSessions([
    summary({ session_id: "failed", last_job: { job_id: "job-failed", session_id: "failed", status: "failed" } }),
    summary({ session_id: "history", last_job: { job_id: "job-ok", session_id: "history", status: "completed" } })
  ]);

  const items = view.getChildren();
  const byId = (id: string) => items.find((item) => item.sessionId === id);

  assert.equal(byId("failed")?.sessionState, "failed");
  assert.equal(byId("failed")?.contextValue, "alysisSessionFailed");
  assert.equal(byId("history")?.sessionState, "historical");
  assert.equal(byId("history")?.contextValue, "alysisSessionHistorical");
});

test("SessionsViewProvider returns [] when there is no recent work", () => {
  const view = new SessionsViewProvider();
  assert.deepEqual(view.getChildren(), []);
  view.setSessions([summary({ session_id: "s1" })]);
  view.setSessions([]);
  assert.deepEqual(view.getChildren(), [], "empty list yields no recent-work rows");
});

test("SessionsViewProvider marks no row active until the live session is known", () => {
  const view = new SessionsViewProvider();
  view.setSessions([summary({ session_id: "live" })]);
  // No setActiveSession yet -> not "active" (so active-only menus do not appear).
  assert.equal(view.getChildren()[0]?.contextValue, "alysisSessionRetained");
  assert.equal(view.getChildren()[0]?.sessionState, "retained");
});

test("session titles redact credentials before truncating and persisting the prompt", () => {
  let persisted: Record<string, string> = {};
  const view = new SessionsViewProvider({ get: () => undefined, update: (titles) => { persisted = titles; } });
  const secret = "sk-proj-" + "abc123XYZ".repeat(20);
  view.rememberTitle("secret-task", `Debug this key ${secret}`);
  assert.equal(view.titleFor("secret-task"), "Debug this key <redacted>");
  assert.deepEqual(persisted, { "secret-task": "Debug this key <redacted>" });
});

test("previously stored credential-bearing titles are sanitized on reload", () => {
  let persisted: Record<string, string> | undefined;
  const view = new SessionsViewProvider({
    get: () => ({ legacy: "Fix Authorization: Bearer abc1234567890" }),
    update: (titles) => { persisted = titles; }
  });
  assert.equal(view.titleFor("legacy"), "Fix Authorization: <redacted>");
  assert.deepEqual(persisted, { legacy: "Fix Authorization: <redacted>" });
});

test("title persistence failures do not interrupt a task or reject asynchronously", async () => {
  for (const update of [() => { throw new Error("storage unavailable"); }, () => Promise.reject(new Error("storage unavailable"))]) {
    const view = new SessionsViewProvider({ get: () => undefined, update });
    assert.doesNotThrow(() => view.rememberTitle("task", "Keep the task working"));
    await new Promise<void>((resolve) => setImmediate(resolve));
    assert.equal(view.titleFor("task"), "Keep the task working");
  }
});

test("task history survives bridge restart and window reload without stale live jobs", () => {
  let stored: unknown;
  const store = {
    get: () => ({ task: "Fix the fixture" }), update: () => undefined,
    getSessions: () => stored, updateSessions: (rows: unknown) => { stored = rows; }
  };
  const first = new SessionsViewProvider(store);
  first.setSessions([summary({ session_id: "task", active_job: {
    job_id: "running", status: "running", error: "private protocol payload"
  } })]);
  first.setActiveSession("task");
  first.setActiveSession(undefined);
  first.setSessions([]);
  const reloaded = new SessionsViewProvider(store);
  assert.equal(reloaded.getChildren().length, 1);
  assert.equal(reloaded.getChildren()[0].label, "Fix the fixture");
  assert.equal(reloaded.getChildren()[0].canMutateLiveSession, false);
  assert.equal(reloaded.getChildren()[0].canResumeIntoLiveSession, true);
  assert.equal(JSON.stringify(stored).includes("private protocol payload"), false);
  assert.equal(JSON.stringify(stored).includes("running"), false);
  reloaded.setSessions([summary({ session_id: "new" })]);
  assert.deepEqual(reloaded.snapshot().sessions.map((row) => row.session_id), ["new", "task"]);
});

test("stored history is bounded and malformed references cannot become task rows", () => {
  const view = new SessionsViewProvider({
    get: () => undefined, update: () => undefined,
    getSessions: () => [null, { session_id: "../escape", workspace_root: "/workspace", mode: "auto" },
      ...Array.from({ length: 205 }, (_, i) => summary({ session_id: `task-${i}` }))],
    updateSessions: () => undefined
  });
  assert.equal(view.getChildren().length, 200);
  assert.ok(view.getChildren().every((row) => !row.canMutateLiveSession));
  view.rememberSession(summary({ session_id: "latest" }));
  assert.equal(view.getChildren().length, 200);
  assert.equal(view.getChildren()[0].sessionId, "latest");
});
