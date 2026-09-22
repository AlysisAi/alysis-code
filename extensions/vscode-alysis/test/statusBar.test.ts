import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

let item: {
  command?: string;
  text?: string;
  tooltip?: { value: string; isTrusted?: unknown };
  visible: boolean;
  disposed: boolean;
  show(): void;
  hide(): void;
  dispose(): void;
};

class FakeMarkdownString {
  public isTrusted: unknown;
  public constructor(public readonly value: string) {}
}

class FakeEventEmitter<T> {
  public readonly event = () => ({ dispose: () => undefined });
  public fire(_value: T): void {}
  public dispose(): void {}
}

const vscodeStub = {
  StatusBarAlignment: { Left: 1 },
  EventEmitter: FakeEventEmitter,
  MarkdownString: FakeMarkdownString,
  window: {
    createStatusBarItem: () => {
      item = {
        visible: false,
        disposed: false,
        show() { this.visible = true; },
        hide() { this.visible = false; },
        dispose() { this.disposed = true; }
      };
      return item;
    }
  }
};

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const { AlysisStatusBar } = require("../src/status/statusBar") as typeof import("../src/status/statusBar");
moduleLoader._load = originalLoad;

test("status bar starts idle and opens the primary cockpit", () => {
  const status = new AlysisStatusBar();
  assert.deepEqual(status.snapshot(), {
    state: "idle",
    text: "$(sparkle) Alysis Code",
    tooltip: "Alysis Code is idle; the local engine will connect when needed",
    visible: true
  });
  assert.equal(item.command, "alysis.openChat");
  assert.equal(item.visible, true);
  status.dispose();
});

test("status bar presents missing CLI as a setup state", () => {
  const status = new AlysisStatusBar();
  status.setMissingCli();
  assert.deepEqual(status.snapshot(), { state: "missingCli", text: "$(circle-outline) Alysis Code: Setup", tooltip: "Alysis Code needs help finding its local engine", visible: true });
  status.dispose();
});

test("status bar presents a connected bridge as ready", () => {
  const status = new AlysisStatusBar();
  status.setBridgeOk();
  assert.equal(status.snapshot().state, "bridgeOk");
  assert.match(status.snapshot().tooltip, /connected and ready/);
  status.dispose();
});

test("status bar presents active work with the job identifier", () => {
  const status = new AlysisStatusBar();
  status.setActiveRun("job-42");
  assert.deepEqual(status.snapshot(), { state: "activeRun", text: "$(sync~spin) Alysis Code: Working", tooltip: "Alysis Code is working on your task", visible: true });
  status.dispose();
});

test("status bar presents approval as an explicit review state", () => {
  const status = new AlysisStatusBar();
  status.setApprovalNeeded();
  assert.equal(status.snapshot().state, "approvalNeeded");
  assert.match(status.snapshot().tooltip, /waiting for your review/);
  status.dispose();
});

test("status bar presents untrusted workspaces as limited", () => {
  const status = new AlysisStatusBar();
  status.setUntrustedWorkspace();
  assert.deepEqual(status.snapshot(), { state: "untrustedWorkspace", text: "$(shield) Alysis Code: Limited", tooltip: "This folder is in read-only mode until you trust it", visible: true });
  status.dispose();
});

test("status bar presents failures and trusted tooltip commands only", () => {
  const status = new AlysisStatusBar();
  status.setError("Provider unavailable");
  assert.equal(status.snapshot().state, "error");
  assert.equal(status.snapshot().tooltip, "Provider unavailable");
  assert.deepEqual(item.tooltip?.isTrusted, { enabledCommands: ["alysis.openChat", "alysis.showBridgeHealth"] });
  status.dispose();
});

test("status bar visibility and disposal follow configuration lifecycle", () => {
  const status = new AlysisStatusBar();
  status.setVisible(false);
  assert.equal(status.snapshot().visible, false);
  assert.equal(item.visible, false);
  status.setVisible(true);
  assert.equal(item.visible, true);
  status.dispose();
  assert.equal(item.disposed, true);
});
