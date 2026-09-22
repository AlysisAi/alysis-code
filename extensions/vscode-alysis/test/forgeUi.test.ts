import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import Module from "node:module";
import { resolve } from "node:path";
import test from "node:test";

import { CockpitRuntimeState } from "../src/chat/CockpitRuntimeState";
import { OPTIONAL_BRIDGE_METHODS, PROTOCOL_VERSION, REQUIRED_BRIDGE_METHODS } from "../src/client/AlysisProtocol";

let workspaceTrusted = true;
let inputText: string | undefined = "Plan the Forge UI";
let textDocuments: any[] = [];
const warnings: string[] = [];
const warningCalls: Array<{ message: string; items: unknown[] }> = [];
const infos: string[] = [];
const errors: string[] = [];
const executedCommands: Array<{ command: string; args: unknown[] }> = [];
const registeredCommands = new Map<string, (...args: unknown[]) => unknown>();
const openedDocuments: Array<{ content: string; language: string }> = [];
let warningResponse: string | undefined;
let quickPickOverride:
  | ((items: unknown[], options?: { canPickMany?: boolean }) => Promise<unknown> | unknown)
  | undefined;
const disposable = { dispose: () => undefined };

class EventEmitter<T = unknown> {
  private readonly listeners = new Set<(value: T) => void>();
  public event = (listener: (value: T) => void) => {
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  };
  public fire(value?: T): void {
    for (const listener of [...this.listeners]) {
      listener(value as T);
    }
  }
  public dispose(): void {
    this.listeners.clear();
  }
}

class TreeItem {
  public description?: string | boolean;
  public tooltip?: unknown;
  public contextValue?: string;
  public iconPath?: unknown;
  public command?: unknown;
  public constructor(public label: string, public collapsibleState?: number) {}
}

class ThemeIcon {
  public constructor(public readonly id: string) {}
}

const vscodeStub = {
  TreeItem,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  ThemeIcon,
  EventEmitter,
  ExtensionMode: { Test: 3 },
  workspace: {
    get isTrusted() {
      return workspaceTrusted;
    },
    workspaceFolders: [{ uri: { scheme: "file", authority: "", fsPath: "/workspace/project" } }],
    get textDocuments() {
      return textDocuments;
    },
    registerTextDocumentContentProvider: () => disposable,
    openTextDocument: async (document: { content: string; language: string }) => {
      openedDocuments.push(document);
      return document;
    }
  },
  window: {
    showInputBox: async () => inputText,
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    },
    showWarningMessage: async (message: string, ...items: unknown[]) => {
      warnings.push(message);
      warningCalls.push({ message, items });
      return warningResponse;
    },
    showErrorMessage: async (message: string) => {
      errors.push(message);
      return undefined;
    },
    showQuickPick: async (items: unknown[], options?: { canPickMany?: boolean }) => {
      if (quickPickOverride) {
        return quickPickOverride(items, options);
      }
      return options?.canPickMany ? [(items as any[])[0]] : (items as any[])[0];
    },
    showTextDocument: async () => undefined,
    withProgress: async (
      _options: unknown,
      task: (progress: unknown, token: unknown) => Promise<unknown>
    ) => task(
      { report: () => undefined },
      { isCancellationRequested: false, onCancellationRequested: () => disposable }
    )
  },
  ProgressLocation: { SourceControl: 1, Window: 10, Notification: 15 },
  commands: {
    registerCommand: (command: string, callback: (...args: unknown[]) => unknown) => {
      registeredCommands.set(command, callback);
      return disposable;
    },
    executeCommand: async (command: string, ...args: unknown[]) => {
      executedCommands.push({ command, args });
      return undefined;
    }
  },
  Uri: {
    from: (parts: { scheme: string; authority?: string; path: string; query?: string }) => ({
      ...parts,
      toString: () => `${parts.scheme}://${parts.authority ?? ""}${parts.path}?${parts.query ?? ""}`
    })
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

const { ForgeController } = require("../src/forge/ForgeController") as typeof import("../src/forge/ForgeController");
const { ProtocolClientError } = require("../src/client/AlysisBridgeClient") as typeof import("../src/client/AlysisBridgeClient");
const { ForgePlanViewProvider } = require("../src/views/forgePlanView") as typeof import("../src/views/forgePlanView");
const { ArtifactsViewProvider } = require("../src/views/artifactsView") as typeof import("../src/views/artifactsView");
const { ForgeDiffContentProvider } = require("../src/forge/ForgeDiffContentProvider") as typeof import("../src/forge/ForgeDiffContentProvider");
const { registerCancelCurrentRunCommand } = require("../src/commands/cancelCurrentRun") as typeof import("../src/commands/cancelCurrentRun");
moduleLoader._load = originalLoad;

test("empty Forge Plan offers a clear first action while Artifacts stays hidden", () => {
  const planItems = new ForgePlanViewProvider().getChildren();
  assert.equal(planItems.length, 1);
  assert.equal(planItems[0]?.kind, "start");
  assert.equal(planItems[0]?.command?.command, "alysis.forgePlan");
  assert.deepEqual(new ArtifactsViewProvider().getChildren(), []);
});

test("Forge Plan tree renders tasks and structured event details", () => {
  const view = new ForgePlanViewProvider();
  view.setPlan(planPayload());
  view.applyEvent(eventEnvelope("plan_node_updated", { node_id: "T01", state: "running", summary: "Working" }, 1));
  view.applyEvent(eventEnvelope("verify_gate_result", { command: "npm test", success: true, summary: "passed" }, 2));
  view.applyEvent(eventEnvelope("review_gate_decision", { decision: "approved", summary: "ready" }, 3));
  view.applyEvent(eventEnvelope("swarm_worker_state_changed", { worker_id: "worker-1", state: "running", role: "coder" }, 4));

  const roots = view.getChildren();
  assert.equal(roots[0].label, "Current plan");
  assert.match(String(roots[0].tooltip), /plan-1/);
  const task = view.getChildren(roots[0]).find((item) => String(item.label).includes("T01"));
  assert.ok(task);
  assert.equal(task.description, "running");
  const details = view.getChildren(task);
  assert.equal(details.some((item) => item.label === "Objective" && item.description === "Build UI"), true);
  assert.equal(details.some((item) => item.label === "Dependencies" && item.description === "T00"), true);
  assert.equal(details.some((item) => item.label === "Risk" && String(item.description).includes("Low risk")), true);
  assert.equal(details.some((item) => item.label === "Warnings" && String(item.description).includes("scope verified")), true);
  assert.equal(view.state().events.some((event) => event.type === "verify_gate_result"), true);
  assert.equal(view.state().events.some((event) => event.type === "review_gate_decision"), true);
});

test("Forge Plan tree marks failed verify and blocked review decisions as errors", () => {
  const view = new ForgePlanViewProvider();
  view.setPlan(planPayload());

  view.applyEvent(eventEnvelope("verify_gate_result", { command: "npm test", success: false, summary: "failed" }, 1));
  view.applyEvent(eventEnvelope("review_gate_decision", { decision: "blocked", summary: "verification failed" }, 2));

  const events = view.state().events;
  assert.equal(events.find((event) => event.type === "verify_gate_result")?.severity, "error");
  assert.equal(events.find((event) => event.type === "review_gate_decision")?.severity, "error");
});

test("Forge Plan tree tracks sequence by session", () => {
  const view = new ForgePlanViewProvider();
  view.setPlan(planPayload());
  view.applyEvent(eventEnvelope("warning_emitted", { message: "s1" }, 5, "session-1"));
  view.applyEvent(eventEnvelope("warning_emitted", { message: "s2" }, 1, "session-2"));
  view.applyEvent(eventEnvelope("warning_emitted", { message: "s2 duplicate" }, 1, "session-2"));

  assert.equal(view.lastSequence("session-1"), 5);
  assert.equal(view.lastSequence("session-2"), 1);
  assert.equal(view.state().events.filter((event) => event.type === "warning_emitted").length, 2);
});

test("Forge Plan tree clears plan-scoped events and diffs when plan changes", () => {
  const view = new ForgePlanViewProvider();
  view.setPlan(planPayload());
  view.applyEvent(eventEnvelope("warning_emitted", { message: "old plan" }, 5, "session-1"));
  view.setDiffs([diffSummary("diff-old", "plan-1")]);

  view.setPlan({ ...planPayload(), plan_id: "plan-2" });

  assert.equal(view.lastSequence("session-1"), 5);
  assert.equal(view.state().events.length, 0);
  assert.equal(view.state().diffs.length, 0);
});

test("ForgeController plan calls forge.plan through BridgeClient", async () => {
  workspaceTrusted = true;
  inputText = "Create a production Forge plan";
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);

  await controller.plan();

  assert.equal(bridge.ensureStartedCalls, 1);
  assert.equal(bridge.forgePlanCalls.length, 1);
  assert.equal(bridge.forgePlanCalls[0].instruction, inputText);
  assert.equal(views.forge.state().plan?.plan_id, "plan-1");
  assert.equal(views.artifacts.state()[0].artifacts[0].artifact_id, "forge_plan:plan/PLAN.md");
});

test("Forge Plan fails closed before bridge or plan state when an owned workspace file is dirty", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  textDocuments = [dirtyDocument("/workspace/project/src/dirty.ts")];
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);

  try {
    assert.equal(await controller.planWithInstruction("change the project"), false);
    assert.equal(bridge.ensureStartedCalls, 0);
    assert.deepEqual(bridge.forgePlanCalls, []);
    assert.equal(controller.testState().planId, null);
    assert.equal(warnings.some((message) => /Save or revert 1 unsaved workspace file/.test(message)), true);
  } finally {
    textDocuments = [];
  }
});

test("Forge Plan rechecks unsaved files at the last host boundary before the backend mutation", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  textDocuments = [];
  const bridge = bridgeMock();
  const originalEnsure = bridge.ensureStartedForProfile;
  bridge.ensureStartedForProfile = async (...args: any[]) => {
    const result = await originalEnsure(...args);
    textDocuments = [dirtyDocument("/workspace/project/src/became-dirty.ts")];
    return result;
  };
  const controller = controllerFor(bridge, viewSet());

  try {
    assert.equal(await controller.planWithInstruction("change after bridge startup"), false);
    assert.equal(bridge.ensureStartedCalls, 1);
    assert.deepEqual(bridge.forgePlanCalls, []);
    assert.equal(controller.testState().planId, null);
    assert.equal(warnings.some((message) => /Save or revert 1 unsaved workspace file/.test(message)), true);
  } finally {
    textDocuments = [];
  }
});

test("ForgeController single-flights rapid Forge Plan requests", async () => {
  workspaceTrusted = true;
  infos.length = 0;
  const bridge = bridgeMock();
  let releasePlan: (() => void) | undefined;
  const planGate = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  bridge.forgePlan = async (params: any) => {
    bridge.forgePlanCalls.push(params);
    await planGate;
    return planPayload();
  };
  const controller = controllerFor(bridge, viewSet());

  const first = controller.planWithInstruction("first plan");
  await waitFor(() => bridge.forgePlanCalls.length === 1);
  const second = controller.planWithInstruction("duplicate click");
  await delay(0);

  assert.equal(bridge.forgePlanCalls.length, 1);
  assert.equal(infos.includes("A Forge Plan is already being prepared."), true);
  releasePlan?.();
  assert.deepEqual(await Promise.all([first, second]), [true, false]);
  assert.equal(controller.testState().planId, "plan-1");
});

test("global cancellation covers Forge Plan startup and cleans up a late backend session once", async () => {
  workspaceTrusted = true;
  infos.length = 0;
  const bridge = bridgeMock();
  let releasePlan: (() => void) | undefined;
  const planGate = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  bridge.forgePlan = async (params: any) => {
    bridge.forgePlanCalls.push(params);
    await planGate;
    return planPayload();
  };
  const controller = controllerFor(bridge, viewSet());

  const planning = controller.planWithInstruction("cancel this plan startup");
  await waitFor(() => bridge.forgePlanCalls.length === 1);
  assert.equal(controller.hasActiveJob(), true);
  assert.equal(await controller.cancelCurrentRun(), true);
  assert.equal(await controller.cancelCurrentRun(), true);
  releasePlan?.();
  assert.equal(await planning, false);

  assert.equal(controller.testState().planId, null);
  assert.deepEqual(bridge.sessionCancelCalls, [{ sessionId: "session-1" }]);
  assert.equal(infos.includes("Forge Plan cancellation is already requested while startup finishes."), true);
});

test("ForgeController does not start a swarm while Forge Plan startup is active", async () => {
  workspaceTrusted = true;
  infos.length = 0;
  const bridge = bridgeMock();
  let releasePlan: (() => void) | undefined;
  const planGate = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  bridge.forgePlan = async (params: any) => {
    bridge.forgePlanCalls.push(params);
    await planGate;
    return planPayload();
  };
  const controller = controllerFor(bridge, viewSet());

  const planning = controller.planWithInstruction("hold plan startup");
  await waitFor(() => bridge.forgePlanCalls.length === 1);
  await controller.runSwarm(2);

  assert.equal(bridge.forgeSwarmStartCalls.length, 0);
  assert.equal(infos.includes("Wait for the active Forge operation to finish or stop it before starting a swarm."), true);
  releasePlan?.();
  assert.equal(await planning, true);
});

test("ForgeController ignores and cleans up a Forge Plan result returned after bridge reset", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let releasePlan: (() => void) | undefined;
  const planGate = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  bridge.forgePlan = async (params: any) => {
    bridge.forgePlanCalls.push(params);
    await planGate;
    return planPayload();
  };
  const controller = controllerFor(bridge, viewSet());

  const planning = controller.planWithInstruction("stale after reset");
  await waitFor(() => bridge.forgePlanCalls.length === 1);
  bridge.emitReset("test_reset");
  releasePlan?.();
  assert.equal(await planning, false);

  assert.equal(controller.testState().sessionId, null);
  assert.equal(controller.testState().planId, null);
  assert.deepEqual(bridge.sessionCancelCalls, [{ sessionId: "session-1" }]);
});

test("newer persisted-plan intent cannot be overwritten by an older Forge Plan completion", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  let releasePlan: (() => void) | undefined;
  const planGate = new Promise<void>((resolve) => {
    releasePlan = resolve;
  });
  bridge.forgePlan = async (params: any) => {
    bridge.forgePlanCalls.push(params);
    await planGate;
    return planPayload();
  };
  bridge.forgeOpen = async (params: any) => {
    bridge.forgeOpenCalls.push(params);
    return {
      ...planPayload(),
      plan_id: "plan-new",
      session_id: "session-new",
      source: "loaded_persisted",
      created_session: true
    };
  };
  const controller = controllerFor(bridge, viewSet());

  const planning = controller.planWithInstruction("old slow plan");
  await waitFor(() => bridge.forgePlanCalls.length === 1);
  await controller.openPlanById("plan-new");
  releasePlan?.();
  assert.equal(await planning, false);

  assert.equal(controller.testState().sessionId, "session-new");
  assert.equal(controller.testState().planId, "plan-new");
  assert.equal(bridge.forgePlanCalls.length, 1);
});

test("ForgeController records one canonical Forge Plan failure", async () => {
  workspaceTrusted = true;
  inputText = "Create a failing Forge plan";
  errors.length = 0;
  const bridge = bridgeMock();
  bridge.forgePlanError = new Error("Mock Forge Plan failed");
  const views = viewSet();
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(bridge, views, {}, { getApiKey: async () => "secret" }, runtime);

  assert.equal(await controller.planWithInstruction(inputText), false);

  const runtimeErrors = runtime.snapshot().events.filter((event) => event.severity === "error");
  assert.equal(runtimeErrors.length, 1);
  assert.equal(runtimeErrors[0].title, "Forge Plan failed");
  assert.match(runtimeErrors[0].message, /Mock Forge Plan failed/);
  assert.ok(runtimeErrors[0].rootCauseKey);
  assert.equal(runtimeErrors.some((event) => event.title === "Forge error"), false);
  assert.equal(views.forge.state().events.some((event) => event.label === "forge_ui_error"), false);
  assert.equal(errors.some((message) => message.includes("Mock Forge Plan failed")), true);
});

test("FE-8: ForgeController review surfaces an approved decision and lists assets", async () => {
  workspaceTrusted = true;
  inputText = "Plan it";
  const bridge = bridgeMock();
  const reviewCalls: any[] = [];
  (bridge as any).forgeReview = async (params: any) => {
    reviewCalls.push(params);
    return {
      session_id: params.session_id,
      plan_id: params.plan_id,
      task_id: params.task_id,
      approved: true,
      confidence: "high",
      summary: "Looks good.",
      blocking_issues_count: 0,
      non_blocking_issues_count: 2,
      review_json: null,
      review_markdown: "ok",
      json_artifact_id: "rev.json",
      markdown_artifact_id: "rev.md",
      requires_human_approval: false,
      action: null
    };
  };
  (bridge as any).forgeAssetsList = async (params: any) => ({
    session_id: params.session_id,
    plan_id: params.plan_id,
    run_id: "run-1",
    assets: [
      {
        record: {
          id: "asset-1", title: "Design doc", description: "", kind: "doc", mime: "text/markdown",
          original_filename: "d.md", size_bytes: 42, sha256: "x", stored_path: "p",
          extracted_text_path: null, thumbnail_path: null, pinned: false, added_at: "", added_by: {},
          deleted_at: null, comprehension_status: "ready", comprehension_current_version: 1
        },
        comprehension_status: "ready", comprehension_source: null, comprehension_summary_preview: "summary", detected_language: null
      }
    ],
    count: 1,
    include_deleted: false
  });
  const views = viewSet();
  const controller = controllerFor(bridge, views);

  await controller.plan();
  await controller.reviewChanges();
  await controller.refreshAssets();

  assert.equal(reviewCalls.length, 1);
  assert.equal(reviewCalls[0].plan_id, "plan-1");
  assert.equal(reviewCalls[0].task_id, "T01");
  const state = controller.cockpitState();
  assert.equal(state.review?.approved, true);
  assert.equal(state.review?.nonBlockingIssues, 2);
  assert.equal(state.assets.length, 1);
  assert.equal(state.assets[0].title, "Design doc");
});

test("FE-8: ForgeController review reports changes-requested with blocking issues", async () => {
  workspaceTrusted = true;
  inputText = "Plan it";
  const bridge = bridgeMock();
  (bridge as any).forgeReview = async (params: any) => ({
    session_id: params.session_id, plan_id: params.plan_id, task_id: params.task_id,
    approved: false, confidence: "medium", summary: "Needs work.", blocking_issues_count: 3,
    non_blocking_issues_count: 1, review_json: null, review_markdown: "x", json_artifact_id: "",
    markdown_artifact_id: "", requires_human_approval: true, action: null
  });
  const views = viewSet();
  const controller = controllerFor(bridge, views);

  await controller.plan();
  await controller.reviewChanges();

  const review = controller.cockpitState().review;
  assert.equal(review?.approved, false);
  assert.equal(review?.blockingIssues, 3);
  assert.equal(review?.requiresHumanApproval, true);
});

test("ForgeController restarts no-secret bridge safely before credentialed Forge Plan", async () => {
  workspaceTrusted = true;
  inputText = "Create a credentialed Forge plan";
  let apiKeyReads = 0;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(
    bridge,
    views,
    {},
    {
      getApiKey: async () => {
        apiKeyReads += 1;
        return "secret";
      }
    }
  );

  await controller.openPlan();
  bridge.ensureStartedCalls = 0;
  bridge.ensureStartedOptions.length = 0;
  bridge.ensureStartedRequirements.length = 0;
  bridge.restartNextEnsure = true;
  apiKeyReads = 0;

  await controller.plan();

  assert.equal(apiKeyReads, 1);
  assert.deepEqual(bridge.ensureStartedOptions[0], { apiKey: "secret" });
  assert.deepEqual(bridge.ensureStartedRequirements[0], { credentialsRequired: true });
  assert.equal(bridge.forgePlanCalls.length, 1);
  assert.equal("session_id" in bridge.forgePlanCalls[0], false);
  assert.equal(views.forge.state().plan?.plan_id, "plan-1");
});

test("ForgeController uses async Forge Plan start when bridge advertises it", async () => {
  workspaceTrusted = true;
  inputText = "Create an async Forge plan";
  const bridge = bridgeMock();
  bridge.supportsAsyncPlan = true;
  bridge.planEventDuringRequest = eventEnvelope(
    "plan_node_updated",
    { node_id: "T01", state: "running", summary: "Planning task" },
    1
  );
  const views = viewSet();
  const controller = controllerFor(bridge, views);

  await controller.plan();

  assert.equal(bridge.forgePlanCalls.length, 0);
  assert.equal(bridge.forgePlanStartCalls.length, 1);
  assert.equal(typeof bridge.forgePlanStartCalls[0].idempotency_key, "string");
  assert.equal(bridge.forgePlanStartCalls[0].idempotency_key.length > 0, true);
  assert.equal(bridge.forgePlanResultCalls, 1);
  assert.equal(bridge.jobStatusCalls.length > 0, true);
  assert.equal(views.forge.state().planning, null);
  assert.equal(views.forge.state().plan?.plan_id, "plan-1");
  assert.equal(views.forge.state().events.some((event) => event.description === "Planning task"), true);
});

test("ForgeController acknowledges a correlated Start View request after durable job acceptance", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.supportsAsyncPlan = true;
  let releaseStatus: (() => void) | undefined;
  const statusGate = new Promise<void>((resolve) => {
    releaseStatus = resolve;
  });
  bridge.jobStatus = async (jobId: string) => {
    bridge.jobStatusCalls.push(jobId);
    await statusGate;
    return { job_id: jobId, session_id: "session-1", status: "completed" };
  };
  const controller = controllerFor(bridge, viewSet());

  const accepted = await controller.submitPlanWithInstruction(
    "Create a durably accepted plan",
    "review",
    "start-view-forge-request-1"
  );

  assert.equal(accepted, true);
  assert.equal(bridge.forgePlanStartCalls.length, 1);
  assert.equal(
    bridge.forgePlanStartCalls[0].idempotency_key,
    "start-view-forge-request-1"
  );
  assert.equal(bridge.forgePlanResultCalls, 0);
  releaseStatus?.();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(bridge.forgePlanResultCalls, 1);
});

test("ForgeController correlated submission resolves false when startup rejects before acceptance", async () => {
  const controller = controllerFor(bridgeMock(), viewSet());
  (controller as any).planWithInstruction = async () => {
    throw new Error("preflight UI failure");
  };

  assert.equal(
    await controller.submitPlanWithInstruction(
      "Keep this draft",
      "review",
      "start-view-forge-rejected-1"
    ),
    false
  );
});

test("ForgeController surfaces async Forge Plan backend errors without detached-method TypeError", async () => {
  workspaceTrusted = true;
  inputText = "Create an async Forge plan";
  errors.length = 0;
  const bridge = bridgeMock();
  bridge.supportsAsyncPlan = true;
  bridge.forgePlanStartError = new Error(
    "planner unavailable; Authorization: Bearer abcdefgh1234567890"
  );
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(bridge, viewSet(), {}, undefined, runtime);

  await controller.plan();

  assert.equal(bridge.forgePlanStartCalls.length, 0);
  assert.equal(errors.some((message) => message.includes("planner unavailable")), true);
  assert.equal(errors.some((message) => message.includes("<redacted>")), true);
  assert.equal(errors.some((message) => message.includes("abcdefgh1234567890")), false);
  assert.doesNotMatch(JSON.stringify(runtime.snapshot()), /abcdefgh1234567890/);
  assert.equal(
    errors.some(
      (message) =>
        message.includes("Cannot read properties of undefined") ||
        message.includes("trackStartedJob")
    ),
    false
  );
});

test("ForgeController handles approval prompts buffered during async Forge Plan start", async () => {
  workspaceTrusted = true;
  warningCalls.length = 0;
  warningResponse = "Allow once";
  const bridge = bridgeMock();
  bridge.supportsAsyncPlan = true;
  bridge.planEventDuringRequest = eventEnvelope(
    "prompt_for_input",
    approvalPayload({ approvalId: "approval-buffered", allowForSessionSupported: true }),
    2,
    "session-1",
    "job-plan-1"
  );
  const controller = controllerFor(bridge, viewSet());

  try {
    await controller.plan();
    await waitFor(() => bridge.respondApprovalCalls.length === 1);
  } finally {
    warningResponse = undefined;
  }

  assert.deepEqual(bridge.respondApprovalCalls[0], {
    session_id: "session-1",
    approval_id: "approval-buffered",
    allow: true,
    allow_for_session: false
  });
  assert.match(warningCalls.at(-1)?.message ?? "", /fs_write/);
});

test("ForgeController lists and opens persisted Forge plans without reading secrets", async () => {
  workspaceTrusted = true;
  let apiKeyReads = 0;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(
    bridge,
    views,
    {},
    {
      getApiKey: async () => {
        apiKeyReads += 1;
        return "secret";
      }
    }
  );

  await controller.openPlan();

  assert.equal(bridge.ensureStartedCalls, 1);
  assert.deepEqual(bridge.ensureStartedOptions[0], { stripApiKey: true });
  assert.equal(apiKeyReads, 0);
  assert.deepEqual(bridge.forgeListCalls, [{ workspace: "/workspace/project", max_items: 50 }]);
  assert.deepEqual(bridge.forgeOpenCalls, [{ workspace: "/workspace/project", plan_id: "plan-1" }]);
  assert.equal(views.forge.state().plan?.source, "loaded_persisted");
  assert.deepEqual(controller.activePlanContext(), { sessionId: "session-1", planId: "plan-1" });
  assert.deepEqual(bridge.diffListCalls, [{ sessionId: "session-1", planId: "plan-1" }]);
  assert.deepEqual(bridge.artifactListCalls, ["session-1"]);
});

test("ForgeController recovers persisted plan artifacts and diffs after credential bridge restart", async () => {
  workspaceTrusted = true;
  warningResponse = "Start Execute";
  const bridge = bridgeMock();
  bridge.previewRealExecutionSupported = true;
  bridge.previewSandboxAvailable = true;
  const views = viewSet();
  const controller = controllerFor(bridge, views, { defaultMode: "review" });
  await controller.plan();
  bridge.forgeOpenCalls.length = 0;
  bridge.diffListCalls.length = 0;
  bridge.artifactListCalls.length = 0;
  bridge.restartNextCredentialEnsure = true;

  try {
    await controller.execute();
  } finally {
    warningResponse = undefined;
  }

  assert.deepEqual(bridge.forgeOpenCalls, [{ workspace: "/workspace/project", plan_id: "plan-1" }]);
  assert.deepEqual(bridge.diffListCalls, [{ sessionId: "session-1", planId: "plan-1" }]);
  assert.deepEqual(bridge.artifactListCalls, ["session-1"]);
  assert.equal(views.forge.state().plan?.source, "loaded_persisted");
  assert.equal(bridge.forgeExecuteCalls.length, 1);
});

test("ForgeController buffers plan events and does not replay old plan events into a new plan", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  bridge.replayedEvents = [eventEnvelope("warning_emitted", { message: "old plan" }, 3)];
  bridge.planEventDuringRequest = eventEnvelope("plan_node_updated", { node_id: "T01", state: "planned", summary: "new plan" }, 4);

  await controller.plan();

  assert.equal(bridge.getEventsCalls[0].afterSequence, 4);
  assert.equal(views.forge.state().events.some((event) => event.description === "old plan"), false);
  assert.equal(views.forge.state().events.some((event) => event.description === "new plan"), true);
});

test("ForgeController clears runtime ids and actionable diff/artifact state after bridge exit", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();

  bridge.emitExit(1);

  assert.equal(controller.testState().sessionId, null);
  assert.equal(controller.testState().planId, null);
  assert.equal(controller.testState().activeJobId, null);
  assert.equal(views.forge.state().plan?.status, "stale");
  assert.equal(views.forge.state().diffs.length, 0);
  assert.equal(views.artifacts.state().length, 0);

  await controller.refreshStatus();

  assert.equal(controller.testState().sessionId, "session-1");
  assert.equal(controller.testState().planId, "plan-1");
  assert.deepEqual(bridge.forgeOpenCalls.slice(-1), [{ session_id: "session-1", plan_id: "plan-1" }]);
});

test("ForgeController invalidates stale runtime ids on intentional bridge reset", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();

  bridge.emitReset("bridge_profile_upgrade");

  assert.equal(controller.testState().sessionId, null);
  assert.equal(controller.testState().planId, null);
  assert.equal(views.forge.state().plan?.status, "stale");
});

test("ForgeController redacts bridge process errors before publishing them", () => {
  const bridge = bridgeMock();
  const views = viewSet();
  const runtime = new CockpitRuntimeState();
  controllerFor(bridge, views, {}, undefined, runtime);

  bridge.emitError(new Error("bridge failed with Authorization: Bearer abcdefgh1234567890"));

  const published = `${JSON.stringify(views.forge.state())}\n${JSON.stringify(runtime.snapshot())}`;
  assert.match(published, /<redacted>/);
  assert.doesNotMatch(published, /abcdefgh1234567890/);
});

test("ForgeController serializes concurrent persisted-plan recovery", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();
  bridge.emitReset("bridge_profile_upgrade");
  bridge.forgeOpenCalls.length = 0;
  let releaseOpen: (() => void) | undefined;
  const openGate = new Promise<void>((resolve) => {
    releaseOpen = resolve;
  });
  bridge.forgeOpen = async (params: any) => {
    bridge.forgeOpenCalls.push(params);
    await openGate;
    return { ...planPayload(), source: "loaded_persisted", created_session: true };
  };

  const first = controller.refreshStatus();
  const second = controller.refreshAssets();
  for (let attempt = 0; attempt < 20 && bridge.forgeOpenCalls.length === 0; attempt += 1) {
    await delay(0);
  }
  assert.equal(bridge.forgeOpenCalls.length, 1);

  releaseOpen?.();
  await Promise.all([first, second]);

  assert.equal(bridge.forgeOpenCalls.length, 1);
  assert.deepEqual(controller.activePlanContext(), { sessionId: "session-1", planId: "plan-1" });
});

test("ForgeController single-flights rapid swarm startup", async () => {
  workspaceTrusted = true;
  infos.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.planWithInstruction("prepare swarm plan");
  let releaseStart: (() => void) | undefined;
  const startGate = new Promise<void>((resolve) => {
    releaseStart = resolve;
  });
  bridge.forgeSwarmStart = async (params: any) => {
    bridge.forgeSwarmStartCalls.push(params);
    await startGate;
    return { session_id: params.session_id, plan_id: params.plan_id, job_id: "swarm-job-1", status: "started" };
  };

  const first = controller.runSwarm(4);
  await waitFor(() => bridge.forgeSwarmStartCalls.length === 1);
  const openCallsBefore = bridge.forgeOpenCalls.length;
  await controller.openPlanById("different-plan");
  const second = controller.runSwarm(8);
  await delay(0);

  assert.equal(controller.hasActiveJob(), true);
  assert.equal(bridge.forgeOpenCalls.length, openCallsBefore);
  assert.equal(bridge.forgeSwarmStartCalls.length, 1);
  assert.equal(infos.includes("A Forge swarm is already starting or running."), true);
  assert.equal(infos.includes("Wait for the active Forge swarm to finish or stop it before opening another plan."), true);
  releaseStart?.();
  await Promise.all([first, second]);
  assert.deepEqual(bridge.forgeSwarmStartCalls[0].approval_scope_grants, []);
  assert.equal(controller.testState().swarm.jobId, "swarm-job-1");
  await controller.cancelSwarm();
  await controller.shutdown();
});

test("ForgeController lists only bounded public resumable swarm state and dismisses one revision", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.forgeSwarmJobs = [durableSwarmJob({
    error_summary: "worker failed with Authorization: Bearer abcdefgh1234567890"
  })];
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(bridge, viewSet(), {}, undefined, runtime);
  await controller.planWithInstruction("prepare recoverable swarm");

  await controller.refreshSwarmRecovery();

  const recovery = controller.testState().swarm.recovery;
  assert.equal(recovery.status, "ready");
  assert.equal(recovery.jobs.length, 1);
  assert.match(recovery.jobs[0]?.errorSummary ?? "", /<redacted>/);
  assert.doesNotMatch(JSON.stringify(recovery), /abcdefgh1234567890/);
  assert.deepEqual(bridge.forgeSwarmListCalls.at(-1), { session_id: "session-1", limit: 50 });

  controller.dismissSwarmRecovery("durable-job-1", 7);
  assert.equal(controller.testState().swarm.recovery.jobs.length, 0);
  bridge.forgeSwarmJobs = [durableSwarmJob({ revision: 8 })];
  await controller.refreshSwarmRecovery();
  assert.equal(controller.testState().swarm.recovery.jobs.length, 1, "a newer backend revision must reappear");
});

test("ForgeController reload recovery recreates the persisted session identity before listing jobs", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  bridge.forgeSwarmJobs = [durableSwarmJob()];
  let missingSession = true;
  bridge.forgeOpen = async (params: any) => {
    bridge.forgeOpenCalls.push(params);
    if (missingSession && params.session_id === "retained-session") {
      missingSession = false;
      throw new ProtocolClientError("session_not_found", "Session is not active after reload.");
    }
    return {
      ...planPayload(),
      session_id: params.session_id ?? "new-session",
      plan_id: params.plan_id,
      source: "loaded_persisted",
      created_session: false
    };
  };
  const persisted: any[] = [];
  const controller = controllerFor(bridge, viewSet(), {}, undefined, undefined, {
    get: () => ({ sessionId: "retained-session", planId: "retained-plan" }),
    update: (value: unknown) => {
      persisted.push(value);
    }
  });

  await controller.refreshSwarmRecovery();

  assert.deepEqual(bridge.createSessionCalls.map((params: any) => ({
    workspace: params.workspace,
    session_id: params.session_id,
    mode: params.mode
  })), [{ workspace: "/workspace/project", session_id: "retained-session", mode: "readonly" }]);
  assert.deepEqual(bridge.forgeOpenCalls, [
    { session_id: "retained-session", plan_id: "retained-plan" },
    { session_id: "retained-session", plan_id: "retained-plan" }
  ]);
  assert.deepEqual(bridge.forgeSwarmListCalls.at(-1), { session_id: "retained-session", limit: 50 });
  assert.deepEqual(persisted.at(-1), { sessionId: "retained-session", planId: "retained-plan" });
  assert.equal(controller.testState().swarm.recovery.jobs.length, 1);
});

test("ForgeController resume revalidates revision and issues a fresh job-scoped recovery grant", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  warningCalls.length = 0;
  warningResponse = "Resume swarm";
  const bridge = bridgeMock();
  bridge.forgeSwarmJobs = [durableSwarmJob()];
  const controller = controllerFor(bridge, viewSet());
  await controller.planWithInstruction("prepare resumable swarm");
  await controller.refreshSwarmRecovery();

  try {
    await controller.resumeSwarm("durable-job-1", 7);
  } finally {
    warningResponse = undefined;
  }

  assert.equal(bridge.forgeSwarmListCalls.length, 2, "resume must re-list immediately before confirmation");
  assert.deepEqual(bridge.forgeSwarmResumeCalls, [{
    session_id: "session-1",
    plan_id: "plan-1",
    job_id: "durable-job-1",
    workspace_trusted: true,
    approval_scope_grants: [{
      kind: "forge_swarm_resume",
      scope: {
        type: "forge_swarm_resume_v1",
        session_id: "session-1",
        plan_id: "plan-1",
        job_id: "durable-job-1",
        revision: 7
      }
    }],
    expected_revision: 7,
    parallel: 2
  }]);
  assert.equal(controller.testState().swarm.jobId, "durable-job-1");
  assert.equal(controller.testState().swarm.status, "running");
  assert.match(String((warningCalls.at(-1)?.items[0] as any)?.detail), /Previous permission grants are not reused/);
  await controller.shutdown();
});

test("ForgeController never issues recovery authority when fresh confirmation is dismissed", async () => {
  workspaceTrusted = true;
  warningResponse = undefined;
  const bridge = bridgeMock();
  bridge.forgeSwarmJobs = [durableSwarmJob()];
  const controller = controllerFor(bridge, viewSet());
  await controller.planWithInstruction("prepare denied recovery");
  await controller.refreshSwarmRecovery();

  await controller.resumeSwarm("durable-job-1", 7);

  assert.equal(bridge.forgeSwarmResumeCalls.length, 0);
  assert.equal(controller.testState().swarm.recovery.activeJobId, null);
  await controller.shutdown();
});

test("ForgeController fails closed when a recovery revision changes or the workspace is untrusted", async () => {
  workspaceTrusted = true;
  errors.length = 0;
  const bridge = bridgeMock();
  bridge.forgeSwarmJobs = [durableSwarmJob()];
  const controller = controllerFor(bridge, viewSet());
  await controller.planWithInstruction("prepare stale recovery");
  await controller.refreshSwarmRecovery();
  bridge.forgeSwarmJobs = [durableSwarmJob({ revision: 8 })];

  await controller.resumeSwarm("durable-job-1", 7);

  assert.equal(bridge.forgeSwarmResumeCalls.length, 0);
  assert.equal(controller.testState().swarm.recovery.status, "error");
  assert.equal(errors.some((message) => message.includes("changed after it was shown")), true);

  const listCount = bridge.forgeSwarmListCalls.length;
  workspaceTrusted = false;
  await controller.refreshSwarmRecovery();
  assert.equal(bridge.forgeSwarmListCalls.length, listCount);
  assert.match(controller.testState().swarm.recovery.reason ?? "", /Trust this folder/);
  workspaceTrusted = true;
});

test("ForgeController cancellation covers a starting swarm and remains idempotent", async () => {
  workspaceTrusted = true;
  infos.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.planWithInstruction("prepare cancellable swarm");
  let releaseStart: (() => void) | undefined;
  const startGate = new Promise<void>((resolve) => {
    releaseStart = resolve;
  });
  bridge.forgeSwarmStart = async (params: any) => {
    bridge.forgeSwarmStartCalls.push(params);
    await startGate;
    return { session_id: params.session_id, plan_id: params.plan_id, job_id: "swarm-job-stale", status: "started" };
  };

  const starting = controller.runSwarm(2);
  await waitFor(() => bridge.forgeSwarmStartCalls.length === 1);
  assert.equal(controller.hasActiveJob(), true);
  assert.equal(await controller.cancelCurrentRun(), true);
  assert.equal(await controller.cancelCurrentRun(), true);
  assert.equal(bridge.forgeSwarmCancelCalls.length, 0);

  releaseStart?.();
  await starting;

  assert.deepEqual(bridge.forgeSwarmCancelCalls, [
    { sessionId: "session-1", jobId: "swarm-job-stale", reason: "superseded" }
  ]);
  assert.equal(controller.testState().swarm.jobId, null);
  assert.equal(controller.testState().swarm.status, "cancelled");
  assert.equal(infos.includes("Swarm cancellation is already requested while startup finishes."), true);
});

test("ForgeController bridge listeners detach exactly once during shutdown", async () => {
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  for (const eventName of ["event", "exit", "reset", "error"]) {
    assert.equal(bridge.listenerCount(eventName), 1);
  }

  await Promise.all([controller.shutdown(), controller.shutdown()]);

  for (const eventName of ["event", "exit", "reset", "error"]) {
    assert.equal(bridge.listenerCount(eventName), 0);
  }
  bridge.emitError(new Error("ignored after disposal"));
  assert.equal(controller.testState().view.events.some((event) => event.description?.includes("ignored after disposal")), false);
});

test("Forge Plan is blocked in untrusted workspace before bridge start or SecretStorage", async () => {
  workspaceTrusted = false;
  warnings.length = 0;
  let apiKeyReads = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(
    bridge,
    viewSet(),
    { defaultMode: "readonly" },
    {
      getApiKey: async () => {
        apiKeyReads += 1;
        return "secret";
      }
    }
  );

  await controller.plan();

  assert.equal(bridge.ensureStartedCalls, 0);
  assert.equal(bridge.forgePlanCalls.length, 0);
  assert.equal(apiKeyReads, 0);
  assert.equal(warnings.some((message) => /Workspace Trust|untrusted/.test(message)), true);
  workspaceTrusted = true;
});

test("Forge Plan missing CLI methods fails closed before bridge start or SecretStorage", async () => {
  workspaceTrusted = true;
  errors.length = 0;
  let apiKeyReads = 0;
  const bridge = bridgeMock();
  bridge.healthMethods = [...REQUIRED_BRIDGE_METHODS];
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(
    bridge,
    viewSet(),
    {},
    {
      getApiKey: async () => {
        apiKeyReads += 1;
        return "secret";
      }
    },
    runtime
  );

  await controller.plan();

  assert.equal(bridge.ensureStartedCalls, 0);
  assert.equal(bridge.forgePlanCalls.length, 0);
  assert.equal(apiKeyReads, 0);
  assert.equal(runtime.snapshot().cliHealth.status, "incompatible");
  assert.equal(runtime.snapshot().compatibility.features.forgePlan.supported, false);
  assert.equal(errors.some((message) => message.includes("Forge Plan requires")), true);
});

test("ForgeController updates task state from plan_node_updated events", async () => {
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();

  bridge.emitEvent(eventEnvelope("plan_node_updated", { node_id: "T01", state: "completed", summary: "done" }, 8));

  const planRoot = views.forge.getChildren()[0];
  const task = views.forge.getChildren(planRoot).find((item) => String(item.label).includes("T01"));
  assert.equal(task?.description, "completed");
});

test("Forge Execute is blocked in untrusted workspace before bridge start", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  await controller.plan();
  bridge.ensureStartedCalls = 0;
  workspaceTrusted = false;

  await controller.execute();

  assert.equal(bridge.ensureStartedCalls, 0);
  assert.equal(bridge.forgeExecutePreviewCalls.length, 0);
  assert.equal(warnings.some((message) => /Workspace Trust/.test(message)), true);
  workspaceTrusted = true;
});

test("Forge Execute requires an active Forge plan before preview or execute", async () => {
  workspaceTrusted = true;
  infos.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });

  await controller.execute();

  assert.equal(bridge.ensureStartedCalls, 0);
  assert.equal(bridge.forgeExecutePreviewCalls.length, 0);
  assert.equal(bridge.forgeExecuteCalls.length, 0);
  assert.equal(
    infos.some((message) => message.includes("Create or open a Forge plan before running Forge Execute Preview")),
    true
  );
});

test("Forge Execute command calls preview and renders readiness summary", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  warningResponse = undefined;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  await controller.plan();

  await controller.execute();

  assert.deepEqual(bridge.forgeExecutePreviewCalls, [
    {
      session_id: "session-1",
      plan_id: "plan-1",
      task_ids: ["T01"],
      mode: "review",
      workspace_trusted: true,
      sandbox_profile: "default",
      no_log: false
    }
  ]);
  assert.equal(bridge.forgeExecuteCalls.length, 0);
  assert.equal(controller.testState().cockpit.executePreview?.plan_id, "plan-1");
  assert.equal(controller.testState().cockpit.executePreview?.real_execution_supported, false);
  assert.equal(controller.testState().cockpit.executePreview?.runtime_approval_requirements[0].scope_requirement?.type, "exact_command_hash");
  assert.equal(warnings.some((message) => message.includes("Forge Execute is not available in this build")), true);
});

test("Forge Execute requires confirmation then starts review job with explicit execution policy", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  openedDocuments.length = 0;
  warningResponse = "Start Execute";
  const bridge = bridgeMock();
  bridge.previewRealExecutionSupported = true;
  bridge.previewSandboxAvailable = true;
  const views = viewSet();
  const controller = controllerFor(bridge, views, {
    defaultMode: "review",
    forgeExecuteMaxSteps: 9,
    forgeExecuteNoLog: true
  });
  await controller.plan();

  try {
    await controller.execute();
  } finally {
    warningResponse = undefined;
  }

  assert.equal(
    warnings.some((message) => message.includes("Start Alysis Code Forge Execute in review mode")),
    true
  );
  assert.deepEqual(bridge.ensureStartedRequirements.slice(-1)[0], { credentialsRequired: true });
  assert.equal(bridge.forgeExecutePreviewCalls.at(-1)?.max_steps, 9);
  assert.equal(bridge.forgeExecutePreviewCalls.at(-1)?.no_log, true);
  assert.deepEqual(bridge.forgeExecuteCalls, [
    {
      session_id: "session-1",
      plan_id: "plan-1",
      task_ids: ["T01"],
      mode: "review",
      workspace_trusted: true,
      sandbox_profile: "default",
      max_steps: 9,
      no_log: true
    }
  ]);
  assert.equal(views.forge.state().events.some((event) => event.label === "Forge execute review"), true);
});

test("Forge Execute v1 does not start real execution outside review mode", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  const bridge = bridgeMock();
  bridge.previewRealExecutionSupported = true;
  bridge.previewSandboxAvailable = true;
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "auto" });
  await controller.plan();

  await controller.execute();

  assert.equal(bridge.forgeExecutePreviewCalls[0].mode, "auto");
  assert.equal(bridge.forgeExecuteCalls.length, 0);
  const modeWarning = warnings.find((message) => message.includes("Forge Execute runs in review mode only"));
  assert.ok(modeWarning, "the refusal must explain the limit, not just state it");
  assert.match(modeWarning, /Alysis Code is set to auto mode/);
  assert.match(modeWarning, /alysis.defaultMode to review/);
  assert.equal(controller.testState().cockpit.executePreview?.execution_mode_requested, "auto");
  assert.equal(controller.testState().cockpit.executePreview?.preview_ready, true);
});

test("ForgeController handles owned Forge approvals with allow once", async () => {
  workspaceTrusted = true;
  warningCalls.length = 0;
  warningResponse = "Allow once";
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-1", allowForSessionSupported: true }),
        9,
        "session-1",
        "job-1"
      )
    );
    await waitFor(() => bridge.respondApprovalCalls.length === 1);
  } finally {
    warningResponse = undefined;
  }

  assert.deepEqual(bridge.respondApprovalCalls[0], {
    session_id: "session-1",
    approval_id: "approval-1",
    allow: true,
    allow_for_session: false
  });
  assert.match(warningCalls.at(-1)?.message ?? "", /fs_write/);
  assert.equal(warningCalls.at(-1)?.items.includes("Allow for session"), true);
});

test("ForgeController exposes pending approval cards and cockpit denial fails closed", async () => {
  workspaceTrusted = true;
  warningCalls.length = 0;
  let releaseModal: (value: undefined) => void = () => undefined;
  warningResponse = new Promise<undefined>((resolve) => {
    releaseModal = resolve;
  }) as any;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-card", allowForSessionSupported: true }),
        9,
        "session-1",
        "job-1"
      )
    );
    await waitFor(() => controller.testState().cockpit.approvals.length === 1);

    const approval = controller.testState().cockpit.approvals[0];
    assert.equal(approval.kind, "fs_write");
    assert.equal(approval.allowForSessionSupported, true);

    await controller.respondToCockpitApproval("session-1", "approval-card", "deny");
    assert.equal(
      bridge.respondApprovalCalls.some((call) => call.approval_id === "approval-card" && call.allow === false),
      true
    );
  } finally {
    releaseModal(undefined);
    warningResponse = undefined;
  }
});

test("Forge cockpit approval allow updates central runtime bridge status", async () => {
  workspaceTrusted = true;
  let releaseModal: (value: undefined) => void = () => undefined;
  warningResponse = new Promise<undefined>((resolve) => {
    releaseModal = resolve;
  }) as any;
  const bridge = bridgeMock();
  const runtime = new CockpitRuntimeState();
  const controller = controllerFor(bridge, viewSet(), {}, { getApiKey: async () => "secret" }, runtime);
  await controller.plan();

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-allow", allowForSessionSupported: true }),
        10,
        "session-1",
        "job-1"
      )
    );
    await waitFor(() => controller.testState().cockpit.approvals.length === 1);
    assert.equal(runtime.snapshot().bridgeProcess.status, "approval_needed");

    await controller.respondToCockpitApproval("session-1", "approval-allow", "allow_once");

    assert.equal(runtime.snapshot().bridgeProcess.status, "ready");
    assert.equal(bridge.respondApprovalCalls.at(-1)?.allow, true);
  } finally {
    releaseModal(undefined);
    warningResponse = undefined;
  }
});

test("Forge cockpit approval is single-flight and keeps the card until the backend answers", async () => {
  workspaceTrusted = true;
  let releaseModal: (value: undefined) => void = () => undefined;
  warningResponse = new Promise<undefined>((resolve) => {
    releaseModal = resolve;
  }) as any;
  let releaseResponse: (() => void) | undefined;
  const responseGate = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  const bridge = bridgeMock();
  bridge.respondApproval = async (params: any) => {
    bridge.respondApprovalCalls.push(params);
    await responseGate;
    return { allow: params.allow, allow_for_session: params.allow_for_session, allow_for_session_warning: null };
  };
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  try {
    bridge.emitEvent(eventEnvelope(
      "prompt_for_input",
      approvalPayload({ approvalId: "approval-single-flight", allowForSessionSupported: true }),
      11,
      "session-1",
      "job-1"
    ));
    await waitFor(() => controller.testState().cockpit.approvals.length === 1);

    const allow = controller.respondToCockpitApproval("session-1", "approval-single-flight", "allow_once");
    const deny = controller.respondToCockpitApproval("session-1", "approval-single-flight", "deny");
    await delay(0);
    assert.equal(bridge.respondApprovalCalls.length, 1);
    assert.equal(controller.testState().cockpit.approvals.length, 1, "pending remains visible while response is in flight");

    releaseResponse?.();
    await Promise.all([allow, deny]);
    assert.equal(controller.testState().cockpit.approvals.length, 0);
    assert.equal(bridge.respondApprovalCalls[0].allow, true, "a contradictory second click never reaches the backend");
  } finally {
    releaseModal(undefined);
    warningResponse = undefined;
  }
});

test("Forge cockpit keeps approval pending after an ambiguous response failure", async () => {
  workspaceTrusted = true;
  errors.length = 0;
  let releaseModal: (value: undefined) => void = () => undefined;
  warningResponse = new Promise<undefined>((resolve) => {
    releaseModal = resolve;
  }) as any;
  const bridge = bridgeMock();
  bridge.respondApproval = async (params: any) => {
    bridge.respondApprovalCalls.push(params);
    throw new Error("connection reset");
  };
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  try {
    bridge.emitEvent(eventEnvelope(
      "prompt_for_input",
      approvalPayload({ approvalId: "approval-retry", allowForSessionSupported: true }),
      12,
      "session-1",
      "job-1"
    ));
    await waitFor(() => controller.testState().cockpit.approvals.length === 1);
    await controller.respondToCockpitApproval("session-1", "approval-retry", "allow_once");

    assert.equal(bridge.respondApprovalCalls.length, 1);
    assert.equal(controller.testState().cockpit.approvals.length, 1);
    assert.equal(errors.some((message) => message.includes("remains pending")), true);
  } finally {
    releaseModal(undefined);
    warningResponse = undefined;
  }
});

test("Forge cockpit approval responses are scoped by session id", async () => {
  workspaceTrusted = true;
  let releaseModal: (value: undefined) => void = () => undefined;
  warningResponse = new Promise<undefined>((resolve) => {
    releaseModal = resolve;
  }) as any;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();
  (controller as any).ownedSessionIds.add("session-2");

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "shared-approval", allowForSessionSupported: true }),
        20,
        "session-1",
        "job-1"
      )
    );
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "shared-approval", allowForSessionSupported: true }),
        1,
        "session-2",
        "job-2"
      )
    );
    await waitFor(() => controller.testState().cockpit.approvals.length === 2);

    await controller.respondToCockpitApproval("session-2", "shared-approval", "deny");

    assert.equal(bridge.respondApprovalCalls.at(-1)?.session_id, "session-2");
    assert.equal(bridge.respondApprovalCalls.at(-1)?.approval_id, "shared-approval");
    assert.equal(bridge.respondApprovalCalls.at(-1)?.allow, false);
    assert.deepEqual(
      controller.testState().cockpit.approvals.map((approval) => approval.sessionId),
      ["session-1"]
    );
  } finally {
    releaseModal(undefined);
    warningResponse = undefined;
  }
});

test("Forge cockpit refreshes when backend approval result clears a pending approval", async () => {
  workspaceTrusted = true;
  let releaseModal: (value: undefined) => void = () => undefined;
  warningResponse = new Promise<undefined>((resolve) => {
    releaseModal = resolve;
  }) as any;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();
  let refreshes = 0;
  controller.setCockpitCallbacks({ refresh: () => { refreshes += 1; } });

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-result", allowForSessionSupported: true }),
        21,
        "session-1",
        "job-1"
      )
    );
    await waitFor(() => controller.testState().cockpit.approvals.length === 1);
    refreshes = 0;

    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        { kind: "approval_result", approval_id: "approval-result", decision: "deny" },
        22,
        "session-1",
        "job-1"
      )
    );

    assert.equal(controller.testState().cockpit.approvals.length, 0);
    assert.equal(refreshes > 0, true);
  } finally {
    releaseModal(undefined);
    warningResponse = undefined;
  }
});

test("Forge approval modal only shows allow-for-session when backend supports the scope", async () => {
  workspaceTrusted = true;
  warningCalls.length = 0;
  warningResponse = "Deny";
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-no-session", allowForSessionSupported: false }),
        10,
        "session-1",
        "job-1"
      )
    );
    await waitFor(() => bridge.respondApprovalCalls.length === 1);
  } finally {
    warningResponse = undefined;
  }

  assert.equal(warningCalls.at(-1)?.items.includes("Allow for session"), false);
  assert.equal(bridge.respondApprovalCalls[0].allow, false);
});

test("Forge approval denial fails closed when the modal is dismissed", async () => {
  workspaceTrusted = true;
  warningResponse = undefined;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();

  bridge.emitEvent(
    eventEnvelope(
      "prompt_for_input",
      approvalPayload({ approvalId: "approval-dismissed", allowForSessionSupported: true }),
      11,
      "session-1",
      "job-1"
    )
  );
  await waitFor(() => bridge.respondApprovalCalls.length === 1);

  assert.deepEqual(bridge.respondApprovalCalls[0], {
    session_id: "session-1",
    approval_id: "approval-dismissed",
    allow: false,
    allow_for_session: false
  });
  assert.equal(
    views.forge.state().events.some((event) => event.description === "Forge approval denied."),
    true
  );
});

test("Unknown Forge approval events are ignored and not auto-approved", async () => {
  warningResponse = "Allow once";
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-unowned", allowForSessionSupported: true }),
        12,
        "unowned-session",
        "unowned-job"
      )
    );
    await delay(20);
  } finally {
    warningResponse = undefined;
    controller.dispose();
  }

  assert.equal(bridge.respondApprovalCalls.length, 0);
});

test("ForgeController ignores chat-owned events", async () => {
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();

  bridge.emitEvent(eventEnvelope("message_delta", { text: "chat token" }, 13, "chat-session", "chat-job"));

  assert.equal(views.forge.state().events.some((event) => event.description === "chat token"), false);
});

test("ForgeController disposal denies pending Forge approvals", async () => {
  workspaceTrusted = true;
  let resolvePrompt: ((value: string | undefined) => void) | undefined;
  quickPickOverride = undefined;
  const originalWarningResponse = warningResponse;
  warningResponse = undefined;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();
  const previousShowWarning = vscodeStub.window.showWarningMessage;
  vscodeStub.window.showWarningMessage = async (message: string, ...items: unknown[]) => {
    warnings.push(message);
    warningCalls.push({ message, items });
    return new Promise<string | undefined>((resolve) => {
      resolvePrompt = resolve;
    });
  };

  try {
    bridge.emitEvent(
      eventEnvelope(
        "prompt_for_input",
        approvalPayload({ approvalId: "approval-dispose", allowForSessionSupported: true }),
        14,
        "session-1",
        "job-1"
      )
    );
    await waitFor(() => warningCalls.some((call) => call.message.includes("Alysis Code Forge approval required")));
    await controller.shutdown();
    assert.equal(
      bridge.respondApprovalCalls.some(
        (call) => call.approval_id === "approval-dispose" && call.allow === false
      ),
      true
    );
  } finally {
    resolvePrompt?.(undefined);
    vscodeStub.window.showWarningMessage = previousShowWarning;
    warningResponse = originalWarningResponse;
  }
});

test("Cancel Current Run routes to Forge when Forge has the active job", async () => {
  registeredCommands.clear();
  let chatCancelCalls = 0;
  let forgeCancelCalls = 0;
  registerCancelCurrentRunCommand(
    { subscriptions: [] } as any,
    {
      hasActiveJob: () => false,
      hasSession: () => false,
      cancelCurrentRun: async () => {
        chatCancelCalls += 1;
      }
    } as any,
    {
      hasActiveJob: () => true,
      cancelCurrentRun: async () => {
        forgeCancelCalls += 1;
        return true;
      }
    } as any
  );

  await registeredCommands.get("alysis.cancelCurrentRun")?.();

  assert.equal(chatCancelCalls, 0);
  assert.equal(forgeCancelCalls, 1);
});

test("Cancel Current Run asks before resolving simultaneous chat and Forge active jobs", async () => {
  registeredCommands.clear();
  warningCalls.length = 0;
  warningResponse = "Cancel Forge";
  let chatCancelCalls = 0;
  let forgeCancelCalls = 0;
  registerCancelCurrentRunCommand(
    { subscriptions: [] } as any,
    {
      hasActiveJob: () => true,
      hasSession: () => true,
      cancelCurrentRun: async () => {
        chatCancelCalls += 1;
      }
    } as any,
    {
      hasActiveJob: () => true,
      cancelCurrentRun: async () => {
        forgeCancelCalls += 1;
        return true;
      }
    } as any
  );

  try {
    await registeredCommands.get("alysis.cancelCurrentRun")?.();
  } finally {
    warningResponse = undefined;
  }

  assert.equal(chatCancelCalls, 0);
  assert.equal(forgeCancelCalls, 1);
  assert.equal(
    warningCalls.some((call) => call.message.includes("Both Alysis Code Chat and Forge report active jobs")),
    true
  );
});

test("Forge cancel reports unsupported active cancellation honestly", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  warningResponse = undefined;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  bridge.previewRealExecutionSupported = true;
  bridge.previewSandboxAvailable = true;
  bridge.supportsForgeCancel = true;
  bridge.supportsForgeCancelFeature = false;
  warningResponse = "Start Execute";
  await controller.plan();
  await controller.execute();
  warningResponse = undefined;

  await controller.cancelCurrentRun();
  const warningEventsAfterFirstCancel = controller.testState().view.events.filter(
    (event) => event.label === "Warning" && event.jobId === "job-1"
  ).length;
  await controller.cancelCurrentRun();

  assert.equal(
    warnings.some((message) => message === "Forge job is running; active cancellation is not advertised by this bridge."),
    true
  );
  assert.equal(
    warnings.some((message) => message === "Forge job is still running; active cancellation was already reported as unsupported by this bridge."),
    true
  );
  assert.equal(controller.testState().activeJobId, "job-1");
  assert.equal(bridge.forgeCancelCalls.length, 0);
  assert.equal(
    controller.testState().view.events.filter((event) => event.label === "Warning" && event.jobId === "job-1").length,
    warningEventsAfterFirstCancel
  );
});

test("Forge pre-plan cancel requires active cancellation feature, not session.cancel method presence", async () => {
  workspaceTrusted = true;
  warningResponse = undefined;
  warnings.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  bridge.supportsSessionCancelMethod = true;
  bridge.supportsActiveJobCancellationFeature = false;
  (controller as any).sessionId = "session-1";
  (controller as any).activeJobId = "job-pre-plan";

  await controller.cancelCurrentRun();
  await controller.cancelCurrentRun();

  assert.equal(bridge.sessionCancelCalls.length, 0);
  assert.equal(
    warnings.some((message) => message === "Forge job is running; active cancellation is not advertised by this bridge."),
    true
  );
  assert.equal(
    warnings.some((message) => message === "Forge job is still running; active cancellation was already reported as unsupported by this bridge."),
    true
  );
});

test("Forge cancel request keeps active job until backend reports terminal state", async () => {
  workspaceTrusted = true;
  warningResponse = undefined;
  infos.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  bridge.previewRealExecutionSupported = true;
  bridge.previewSandboxAvailable = true;
  bridge.supportsForgeCancel = true;
  bridge.supportsForgeCancelFeature = true;
  bridge.forgeCancelShouldSucceed = true;
  bridge.forgeCancelResult = { status: "cancellation_requested" };
  warningResponse = "Start Execute";
  await controller.plan();
  await controller.execute();
  warningResponse = undefined;
  const refreshedActiveJobIds: Array<string | null> = [];
  controller.setCockpitCallbacks({
    refresh: () => {
      refreshedActiveJobIds.push(controller.testState().cockpit.activeJobId);
    }
  });

  await controller.cancelCurrentRun();

  assert.equal(controller.testState().activeJobId, "job-1");
  assert.equal(controller.testState().cockpit.activeJobId, "job-1");
  assert.equal((controller as any).ownedJobIds.has("job-1"), true);
  assert.equal(refreshedActiveJobIds.at(-1), "job-1");
  assert.equal(bridge.forgeCancelCalls.length, 1);
  assert.equal(
    controller.testState().view.events.some(
      (event) =>
        event.label === "Forge cancellation" &&
        event.jobId === "job-1" &&
        typeof event.description === "string" &&
        event.description.includes("Cancellation requested")
    ),
    true
  );
  await controller.cancelCurrentRun();
  assert.equal(bridge.forgeCancelCalls.length, 1);
  assert.equal(
    infos.some((message) => message === "Cancellation is already requested for the active Forge job."),
    true
  );
});

test("Forge cancel terminal result clears active job before cockpit refresh", async () => {
  workspaceTrusted = true;
  warningResponse = undefined;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  bridge.previewRealExecutionSupported = true;
  bridge.previewSandboxAvailable = true;
  bridge.supportsForgeCancel = true;
  bridge.supportsForgeCancelFeature = true;
  bridge.forgeCancelShouldSucceed = true;
  bridge.forgeCancelResult = { status: "cancelled" };
  warningResponse = "Start Execute";
  await controller.plan();
  await controller.execute();
  warningResponse = undefined;
  const refreshedActiveJobIds: Array<string | null> = [];
  controller.setCockpitCallbacks({
    refresh: () => {
      refreshedActiveJobIds.push(controller.testState().cockpit.activeJobId);
    }
  });

  await controller.cancelCurrentRun();

  assert.equal(controller.testState().activeJobId, null);
  assert.equal(controller.testState().cockpit.activeJobId, null);
  assert.equal((controller as any).ownedJobIds.has("job-1"), false);
  assert.equal(refreshedActiveJobIds.at(-1), null);
  assert.equal(bridge.forgeCancelCalls.length, 1);
});

test("Forge Execute Preview renders unsupported blockers", async () => {
  workspaceTrusted = true;
  warningResponse = undefined;
  const bridge = bridgeMock();
  bridge.previewMissingPrerequisites = ["T01: verification commands are required before execution."];
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  await controller.plan();

  await controller.execute();

  const preview = controller.testState().cockpit.executePreview;
  assert.ok(preview);
  assert.equal(
    preview.missing_prerequisites.includes("T01: verification commands are required before execution."),
    true
  );
  assert.equal(preview.preview_ready, false);
});

test("Forge Execute Preview works in readonly mode in untrusted workspace without secrets", async () => {
  workspaceTrusted = true;
  let apiKeyReads = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(
    bridge,
    viewSet(),
    { defaultMode: "readonly" },
    {
      getApiKey: async () => {
        apiKeyReads += 1;
        return "secret";
      }
    }
  );
  await controller.plan();
  bridge.ensureStartedCalls = 0;
  bridge.ensureStartedOptions.length = 0;
  apiKeyReads = 0;
  workspaceTrusted = false;

  await controller.execute();

  assert.equal(bridge.ensureStartedCalls, 1);
  assert.deepEqual(bridge.ensureStartedOptions[0], { stripApiKey: true });
  assert.equal(apiKeyReads, 0);
  assert.equal(bridge.forgeExecutePreviewCalls[0].mode, "readonly");
  assert.equal(bridge.forgeExecutePreviewCalls[0].workspace_trusted, false);
  assert.equal(bridge.forgeExecutePreviewCalls[0].no_log, false);
  assert.equal(controller.testState().cockpit.executePreview?.workspace_trust_required, false);
  workspaceTrusted = true;
});

test("Forge Execute Preview rejects empty task selection before bridge start", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet(), { defaultMode: "review" });
  await controller.plan();
  bridge.ensureStartedCalls = 0;
  bridge.ensureStartedOptions.length = 0;
  bridge.forgeExecutePreviewCalls.length = 0;
  quickPickOverride = (_items, options) => (options?.canPickMany ? [] : undefined);

  try {
    await controller.execute();
  } finally {
    quickPickOverride = undefined;
  }

  assert.equal(bridge.ensureStartedCalls, 0);
  assert.equal(bridge.forgeExecutePreviewCalls.length, 0);
  assert.equal(warnings.some((message) => message.includes("Select at least one Forge task")), true);
});

test("ForgeController opens native diff documents from backend-provided old/new text", async () => {
  workspaceTrusted = true;
  executedCommands.length = 0;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  await controller.openDiff("diff-1");

  const diffCommand = executedCommands.find((entry) => entry.command === "vscode.diff");
  assert.ok(diffCommand);
  assert.equal((diffCommand.args[0] as any).scheme, "alysis-diff");
  assert.equal((diffCommand.args[1] as any).scheme, "alysis-diff");
  assert.match(String(diffCommand.args[2]), /Plan label -> Proposed label/);
  assert.match(String(diffCommand.args[2]), /plan plan-1/);
  assert.match(String(diffCommand.args[2]), /job job-1/);
  assert.deepEqual(bridge.diffGetCalls, [
    { diffId: "diff-1", options: { sessionId: "session-1", planId: "plan-1" } }
  ]);
});

test("ForgeController warns for truncated diffs", async () => {
  warnings.length = 0;
  const bridge = bridgeMock();
  bridge.diffTruncated = true;
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();

  await controller.openDiff("diff-1");

  assert.equal(warnings.some((message) => message.includes("truncated")), true);
});

test("Forge job polling tolerates transient status failures and always releases the active job", async () => {
  workspaceTrusted = true;
  errors.length = 0;
  const bridge = bridgeMock();
  let statusCalls = 0;
  bridge.jobStatus = async (jobId: string) => {
    bridge.jobStatusCalls.push(jobId);
    statusCalls += 1;
    if (statusCalls <= 3) {
      throw new Error("bridge hiccup");
    }
    return { job_id: jobId, session_id: "session-1", status: "completed" };
  };
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();
  (controller as any).activeJobId = "job-transient-1";

  await (controller as any).pollJob("job-transient-1");

  assert.equal(statusCalls, 4, "three transient failures are retried before the fourth call succeeds");
  assert.equal(controller.testState().activeJobId, null);
  assert.equal(controller.hasActiveJob(), false);
  assert.deepEqual(errors, []);
});

test("Forge job polling clears the active job when status never recovers", async () => {
  workspaceTrusted = true;
  errors.length = 0;
  const bridge = bridgeMock();
  bridge.jobStatus = async (jobId: string) => {
    bridge.jobStatusCalls.push(jobId);
    throw new Error("bridge is gone");
  };
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();
  (controller as any).activeJobId = "job-lost-1";

  await (controller as any).pollJob("job-lost-1");

  assert.equal(bridge.jobStatusCalls.filter((id: string) => id === "job-lost-1").length, 4);
  assert.equal(controller.testState().activeJobId, null);
  assert.equal(controller.hasActiveJob(), false, "a wedged poll must never block later Forge operations");
  assert.equal(
    errors.some((message) => /Could not refresh Forge job status after 4 attempts/.test(message)),
    true
  );
  assert.equal(
    views.forge.state().events.some((event) => event.description === "status_unknown"),
    true
  );
});

test("Forge publishes activity transitions so the cancel command can follow them", async () => {
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const controller = controllerFor(bridge, viewSet());
  const observed: boolean[] = [];
  controller.onDidChangeActivity((active: boolean) => observed.push(active));

  await controller.plan();
  (controller as any).activeJobId = "job-activity-1";
  (controller as any).refreshCockpit();
  assert.equal(controller.hasActiveJob(), true);
  (controller as any).clearActiveJob("job-activity-1");

  assert.deepEqual(observed.slice(-2), [true, false]);
  assert.equal(controller.hasActiveJob(), false);
});

test("Swarm Apply confirms before writing agent output and names untracked files", async () => {
  workspaceTrusted = true;
  warnings.length = 0;
  const bridge = bridgeMock();
  const applyCalls: any[] = [];
  (bridge as any).forgeSwarmApply = async (sessionId: string, planId: string, taskIds: string[]) => {
    applyCalls.push({ sessionId, planId, taskIds });
    return { session_id: sessionId, plan_id: planId, applied: [{ task_id: taskIds[0] }] };
  };
  (bridge as any).forgeSwarmReview = async () => swarmReviewPayload();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();
  (controller as any).swarmReview = swarmReviewPayload();

  await controller.applySwarmTask("T01");
  assert.deepEqual(applyCalls, [], "a dismissed modal never writes to the working tree");

  warningResponse = "Apply to working tree";
  try {
    await controller.applySwarmTask("T01");
  } finally {
    warningResponse = undefined;
  }

  assert.deepEqual(applyCalls, [{ sessionId: "session-1", planId: "plan-1", taskIds: ["T01"] }]);
  const modal = warnings.find((message) => message.includes("Apply Forge swarm task T01"));
  assert.ok(modal);
  assert.match(modal, /working tree/);
  assert.match(modal, /NOT applied/);
  assert.match(modal, /docs\/new-file\.md/);
});

test("Swarm Apply fails closed when trust is revoked while the confirmation is open", async () => {
  warnings.length = 0;
  workspaceTrusted = true;
  const bridge = bridgeMock();
  const applyCalls: any[] = [];
  (bridge as any).forgeSwarmApply = async (...args: unknown[]) => {
    applyCalls.push(args);
    return { applied: [] };
  };
  (bridge as any).forgeSwarmReview = async () => swarmReviewPayload();
  const controller = controllerFor(bridge, viewSet());
  await controller.plan();
  (controller as any).swarmReview = swarmReviewPayload();
  warningResponse = "Apply to working tree";
  const originalShowWarning = vscodeStub.window.showWarningMessage;
  vscodeStub.window.showWarningMessage = async (message: string, ...items: unknown[]) => {
    warnings.push(message);
    warningCalls.push({ message, items });
    workspaceTrusted = false;
    return warningResponse;
  };

  try {
    await controller.applySwarmTask("T01");
  } finally {
    vscodeStub.window.showWarningMessage = originalShowWarning;
    warningResponse = undefined;
    workspaceTrusted = true;
  }

  assert.deepEqual(applyCalls, []);
});

test("Forge diff documents use deterministic keys and reload after the host cache is lost", async () => {
  workspaceTrusted = true;
  executedCommands.length = 0;
  const bridge = bridgeMock();
  const views = viewSet();
  const controller = controllerFor(bridge, views);
  await controller.plan();

  await controller.openDiff("diff-1");
  await controller.openDiff("diff-1");

  const diffCommands = executedCommands.filter((entry) => entry.command === "vscode.diff");
  assert.equal(diffCommands.length, 2);
  assert.equal((diffCommands[0].args[0] as any).query, (diffCommands[1].args[0] as any).query);
  assert.equal((diffCommands[0].args[1] as any).query, (diffCommands[1].args[1] as any).query);
  assert.equal((diffCommands[0].args[0] as any).query.includes("plan-1"), true);

  // Window reload: VS Code restores the editor, the in-memory cache is gone.
  (views.diffs as any).documents.clear();
  const restored = await views.diffs.provideTextDocumentContent(diffCommands[0].args[0] as any);
  assert.equal(restored, "old\n");
  assert.equal(bridge.diffGetCalls.length, 3, "the restored editor re-fetches instead of rendering empty");
});

test("Forge diff documents surface a real error instead of a false identical diff", async () => {
  const provider = new ForgeDiffContentProvider();
  const uri = {
    scheme: "alysis-diff",
    authority: "forge",
    path: "/original/README.md",
    query: encodeURIComponent("plan-1:diff-1:original"),
    toString: () => "alysis-diff://forge/original/README.md?plan-1%3Adiff-1%3Aoriginal"
  } as any;

  await assert.rejects(() => Promise.resolve(provider.provideTextDocumentContent(uri)), /cannot restore this diff/);

  provider.setResolver(async () => "restored\n");
  assert.equal(await provider.provideTextDocumentContent(uri), "restored\n");
});

test("Forge UI source does not parse terminal output", () => {
  const source = readFileSync(resolve(__dirname, "../src/forge/ForgeController.js"), "utf8");
  assert.equal(source.includes("terminal"), false);
  assert.equal(source.includes("stdout"), false);
});

function viewSet() {
  return {
    forge: new ForgePlanViewProvider(),
    artifacts: new ArtifactsViewProvider(),
    diffs: new ForgeDiffContentProvider()
  };
}

function swarmReviewPayload(): any {
  return {
    session_id: "session-1",
    plan_id: "plan-1",
    items: [
      {
        task_id: "T01",
        title: "Build UI",
        status: "review_pending",
        state: "completed",
        reviewable: true,
        diff_available: true,
        diff_artifact_id: "forge_swarm:T01.diff",
        untracked_files: ["docs/new-file.md"],
        untracked_files_note: "Created but not applied.",
        applied: false,
        discarded: false,
        recovery: null
      }
    ],
    pending_review_task_ids: ["T01"]
  };
}

function dirtyDocument(fsPath: string): any {
  return {
    isDirty: true,
    uri: { fsPath, scheme: "file", authority: "", toString: () => `file://${fsPath}` }
  };
}

function controllerFor(
  bridge: ReturnType<typeof bridgeMock>,
  views: ReturnType<typeof viewSet>,
  overrides: Record<string, unknown> = {},
  secretStore: { getApiKey(): Promise<string | undefined> } = { getApiKey: async () => "secret" },
  runtime?: CockpitRuntimeState,
  persistence?: { get(): unknown; update(value: unknown): PromiseLike<void> | void }
) {
  return new ForgeController(
    bridge as any,
    () => ({ ...config(), ...overrides }),
    secretStore as any,
    {
      setBridgeOk: () => undefined,
      setActiveRun: () => undefined,
      setApprovalNeeded: () => undefined,
      setIdle: () => undefined,
      setError: () => undefined
    } as any,
    { setSessions: () => undefined } as any,
    views.forge,
    views.artifacts,
    { appendLine: () => undefined } as any,
    views.diffs,
    runtime,
    persistence as any
  );
}

function bridgeMock() {
  const listeners = new Map<string | symbol, Array<(...args: any[]) => void>>();
  const emit = (eventName: string | symbol, ...args: unknown[]) => {
    for (const listener of listeners.get(eventName) ?? []) {
      listener(...args);
    }
  };
  const bridge = {
    ensureStartedCalls: 0,
    ensureStartedOptions: [] as any[],
    ensureStartedRequirements: [] as any[],
    createSessionCalls: [] as any[],
    restartNextEnsure: false,
    restartNextCredentialEnsure: false,
    supportsAsyncPlan: false,
    forgePlanCalls: [] as any[],
    forgePlanStartCalls: [] as any[],
    forgePlanResultCalls: 0,
    forgePlanError: undefined as Error | undefined,
    forgePlanStartError: undefined as Error | undefined,
    forgePlanResultError: undefined as Error | undefined,
    forgeListCalls: [] as any[],
    forgeOpenCalls: [] as any[],
    forgeExecuteCalls: [] as any[],
    forgeExecutePreviewCalls: [] as any[],
    forgeCancelCalls: [] as any[],
    forgeSwarmStartCalls: [] as any[],
    forgeSwarmListCalls: [] as any[],
    forgeSwarmResumeCalls: [] as any[],
    forgeSwarmJobs: [] as any[],
    forgeSwarmCancelCalls: [] as any[],
    sessionCancelCalls: [] as any[],
    respondApprovalCalls: [] as any[],
    diffListCalls: [] as any[],
    diffGetCalls: [] as any[],
    artifactListCalls: [] as any[],
    getEventsCalls: [] as any[],
    jobStatusCalls: [] as any[],
    replayedEvents: [] as any[],
    planEventDuringRequest: undefined as any,
    previewMissingPrerequisites: [] as string[],
    previewRealExecutionSupported: false,
    previewSandboxAvailable: false,
    supportsForgeCancel: false,
    supportsForgeCancelFeature: false,
    supportsActiveJobCancellationFeature: false,
    supportsSessionCancelMethod: false,
    forgeCancelShouldSucceed: false,
    forgeCancelResult: { status: "cancelled" } as Record<string, unknown>,
    diffTruncated: false,
    healthMethods: [...REQUIRED_BRIDGE_METHODS, ...OPTIONAL_BRIDGE_METHODS] as string[],
    on: (_event: string | symbol, listener: (event: unknown) => void) => {
      const current = listeners.get(_event) ?? [];
      current.push(listener);
      listeners.set(_event, current as any);
    },
    off: (_event: string | symbol, listener: (...args: any[]) => void) => {
      const current = listeners.get(_event) ?? [];
      listeners.set(_event, current.filter((candidate) => candidate !== listener));
    },
    listenerCount: (_event: string | symbol) => (listeners.get(_event) ?? []).length,
    emitEvent: (event: unknown) => {
      emit("event", event);
    },
    emitExit: (code: number | null) => {
      emit("exit", code);
    },
    emitReset: (reason: string) => {
      emit("reset", reason);
    },
    emitError: (error: Error) => {
      emit("error", error);
    },
    health: async () => healthPayload(bridge.healthMethods),
    ensureStarted: async (_config?: unknown, options?: unknown) => {
      bridge.ensureStartedCalls += 1;
      bridge.ensureStartedOptions.push(options ?? {});
    },
    ensureStartedForProfile: async (_config?: unknown, options?: unknown, requiredProfile?: unknown) => {
      bridge.ensureStartedCalls += 1;
      bridge.ensureStartedOptions.push(options ?? {});
      bridge.ensureStartedRequirements.push(requiredProfile ?? {});
      const credentialsRequired = Boolean((requiredProfile as any)?.credentialsRequired);
      const restarted = credentialsRequired
        ? bridge.restartNextCredentialEnsure || bridge.restartNextEnsure
        : bridge.restartNextEnsure;
      if (credentialsRequired) {
        bridge.restartNextCredentialEnsure = false;
        bridge.restartNextEnsure = false;
      } else {
        bridge.restartNextEnsure = false;
      }
      return {
        restarted,
        profile: {
          resolvedExecutablePath: "alysis",
          apiKeyForwardingAllowed: !Boolean((options as any)?.stripApiKey),
          stripApiKey: Boolean((options as any)?.stripApiKey),
          alysisApiKeyForwarded: typeof (options as any)?.apiKey === "string",
          credentialCapable: !Boolean((options as any)?.stripApiKey),
          model: "",
          baseUrl: "",
          workspaceTrusted,
          executableTrusted: true,
          executableOrigin: "default"
        }
      };
    },
    createSession: async (params: any) => {
      bridge.createSessionCalls.push(params);
      return { session_id: params.session_id, workspace_root: params.workspace, mode: params.mode };
    },
    supportsMethod: (method: string) =>
      ((method === "forge.plan.start" || method === "forge.plan.result") && bridge.supportsAsyncPlan) ||
      (method === "forge.cancel" && bridge.supportsForgeCancel) ||
      (method === "session.cancel" && bridge.supportsSessionCancelMethod),
    supportsFeature: (path: readonly string[]) =>
      (path[0] === "forge" &&
        path[1] === "plan" &&
        [
          "durable_acceptance",
          "idempotency_key_and_payload_hashed",
          "acknowledgement_after_durable_acceptance",
          "stable_job_id_across_bridge_restart",
          "job_status_restart_recovery",
          "result_restart_recovery",
          "fenced_worker_leases"
        ].includes(path[2] ?? "") &&
        bridge.supportsAsyncPlan) ||
      (path.join(".") === "forge.cancel.supported" && bridge.supportsForgeCancelFeature) ||
      (path.join(".") === "cancellation.active_jobs" && bridge.supportsActiveJobCancellationFeature),
    forgePlan: async (params: any) => {
      bridge.forgePlanCalls.push(params);
      if (bridge.forgePlanError) {
        throw bridge.forgePlanError;
      }
      if (bridge.planEventDuringRequest) {
        emit("event", bridge.planEventDuringRequest);
      }
      return planPayload();
    },
    forgePlanStart: async function (this: any, params: any) {
      if (this.forgePlanStartError) {
        throw this.forgePlanStartError;
      }
      this.forgePlanStartCalls.push(params);
      if (this.planEventDuringRequest) {
        emit("event", this.planEventDuringRequest);
      }
      return {
        session_id: "session-1",
        job_id: "job-plan-1",
        status: "started",
        durably_accepted: true,
        duplicate: false
      };
    },
    forgePlanResult: async function (this: any) {
      if (this.forgePlanResultError) {
        throw this.forgePlanResultError;
      }
      this.forgePlanResultCalls += 1;
      return { ...planPayload(), job_id: "job-plan-1" };
    },
    forgeList: async (params: any) => {
      bridge.forgeListCalls.push(params);
      return {
        workspace_root: "/workspace/project",
        plans: [
          {
            plan_id: "plan-1",
            session_id: null,
            workspace_root: "/workspace/project",
            status: "planned",
            source: "persisted",
            project_goal: "Forge UI",
            summary: "Build UI",
            task_count: 1,
            created_at: "2026-01-01T00:00:00Z",
            updated_at: "2026-01-01T00:00:00Z",
            plan_artifact_id: "forge_plan:plan/plan.json",
            plan_markdown_artifact_id: "forge_plan:plan/PLAN.md"
          }
        ],
        truncated: false,
        max_items: 50
      };
    },
    forgeOpen: async (params: any) => {
      bridge.forgeOpenCalls.push(params);
      return { ...planPayload(), source: "loaded_persisted", created_session: true };
    },
    forgeStatus: async () => planPayload(),
    forgeExecute: async (params: any) => {
      bridge.forgeExecuteCalls.push(params);
      return { session_id: "session-1", plan_id: "plan-1", job_id: "job-1", status: "started" };
    },
    forgeExecutePreview: async (params: any) => {
      bridge.forgeExecutePreviewCalls.push(params);
      return executePreviewPayload({
        missingPrerequisites: bridge.previewMissingPrerequisites,
        realExecutionSupported: bridge.previewRealExecutionSupported,
        mode: params.mode,
        workspaceTrusted: params.workspace_trusted,
        sandboxAvailable: bridge.previewSandboxAvailable
      });
    },
    forgeCancel: async (params: any) => {
      bridge.forgeCancelCalls.push(params);
      if (!bridge.forgeCancelShouldSucceed) {
        throw new Error("forge.cancel unsupported");
      }
      return bridge.forgeCancelResult;
    },
    forgeSwarmStart: async (params: any) => {
      bridge.forgeSwarmStartCalls.push(params);
      return { session_id: params.session_id, plan_id: params.plan_id, job_id: "swarm-job-1", status: "started" };
    },
    forgeSwarmList: async (params: any) => {
      bridge.forgeSwarmListCalls.push(params);
      return { session_id: params.session_id, jobs: bridge.forgeSwarmJobs, count: bridge.forgeSwarmJobs.length };
    },
    forgeSwarmResume: async (params: any) => {
      bridge.forgeSwarmResumeCalls.push(params);
      return {
        ...durableSwarmJob({ state: "queued", resumable: false, revision: params.expected_revision + 1 }),
        session_id: params.session_id,
        plan_id: params.plan_id,
        status: "resumed"
      };
    },
    forgeSwarmResult: async (jobId: string) => ({
      job_id: jobId,
      session_id: "session-1",
      plan_id: "plan-1",
      complete: false,
      status: "running"
    }),
    forgeSwarmCancel: async (sessionId: string, jobId: string, reason?: string) => {
      bridge.forgeSwarmCancelCalls.push({ sessionId, jobId, reason });
      return { status: "cancellation_requested" };
    },
    cancelSession: async (sessionId: string) => {
      bridge.sessionCancelCalls.push({ sessionId });
      if (!bridge.supportsActiveJobCancellationFeature) {
        throw new Error("session.cancel unsupported");
      }
      return { status: "cancellation_requested" };
    },
    respondApproval: async (params: any) => {
      bridge.respondApprovalCalls.push(params);
      return {
        allow: Boolean(params.allow),
        allow_for_session: Boolean(params.allow_for_session),
        allow_for_session_warning: null
      };
    },
    diffList: async (sessionId: string, planId?: string) => {
      bridge.diffListCalls.push({ sessionId, planId });
      return {
        diffs: [
          {
            diff_id: "diff-1",
            session_id: "session-1",
            plan_id: "plan-1",
            job_id: "job-1",
            file_path: "README.md",
            status: "available",
            old_label: "Plan label",
            new_label: "Proposed label",
            size_bytes: 8
          }
        ]
      };
    },
    diffGet: async (diffId: string, options: { sessionId: string; planId: string }) => {
      bridge.diffGetCalls.push({ diffId, options });
      return {
        diff_id: "diff-1",
        session_id: "session-1",
        plan_id: "plan-1",
        job_id: "job-1",
        file_path: "README.md",
        old_text: "old\n",
        new_text: "new\n",
        old_artifact_id: null,
        new_artifact_id: null,
        unified_diff: "--- a/README.md\n+++ b/README.md\n",
        truncated: bridge.diffTruncated,
        size_bytes: 8,
        max_bytes: 65536,
        redaction: "protocol_preview"
      };
    },
    artifactList: async (sessionId: string) => {
      bridge.artifactListCalls.push(sessionId);
      return {
        session_id: "session-1",
        artifacts: [{ artifact_id: "forge_plan:plan/PLAN.md", root: "forge_plan", path: "plan/PLAN.md", size_bytes: 10 }],
        truncated: false
      };
    },
    artifactRead: async () => ({
      session_id: "session-1",
      artifact_id: "forge_plan:plan/PLAN.md",
      path: "plan/PLAN.md",
      size_bytes: 10,
      truncated: false,
      max_bytes: 65536,
      encoding: "utf-8",
      content: "artifact"
    }),
    sessionList: async () => ({ sessions: [] }),
    getEvents: async (_sessionId: string, afterSequence?: number) => {
      bridge.getEventsCalls.push({ sessionId: _sessionId, afterSequence });
      return {
        events: bridge.replayedEvents.filter((event) => afterSequence === undefined || event.sequence > afterSequence),
        truncated: false
      };
    },
    jobStatus: async (jobId: string) => {
      bridge.jobStatusCalls.push(jobId);
      return { job_id: jobId, session_id: "session-1", status: "completed" };
    }
  };
  return bridge;
}

function config() {
  return {
    cliPath: "alysis",
    defaultMode: "readonly" as const,
    defaultModel: "",
    baseUrl: "",
    provider: "",
    transport: "stdio" as const,
    sandboxProfile: "default" as const,
    forgeExecuteMaxSteps: undefined,
    forgeExecuteNoLog: false,
    showStatusBar: true,
    autoStartBridge: false,
    enableForge: true,
    security: {
      isWorkspaceTrusted: workspaceTrusted,
      ignoredWorkspaceSettings: [],
      workspaceRoots: ["/workspace/project"],
      cliPath: {
        value: "alysis",
        source: "default" as const,
        trusted: true,
        executionAllowed: true,
        apiKeyForwardingAllowed: true
      }
    }
  };
}

function healthPayload(methods: string[]) {
  return {
    ok: true,
    name: "alysis-ide-bridge",
    alysis_version: "0.1.4",
    protocol_version: PROTOCOL_VERSION,
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods,
      events: [],
      modes: ["readonly", "review", "auto"],
      transport: "stdio-jsonl"
    }
  };
}

function durableSwarmJob(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "durable-job-1",
    state: "interrupted",
    revision: 7,
    attempts: 1,
    resume_count: 0,
    created_at: 1_785_000_000,
    updated_at: 1_785_000_100,
    started_at: 1_785_000_001,
    terminal_at: 1_785_000_100,
    lease_expires_at: null,
    result_available: false,
    error_code: "worker_interrupted",
    error_summary: "The worker stopped before completion.",
    usage: {
      calls: 2,
      input_tokens: 100,
      output_tokens: 50,
      cached_input_tokens: 0,
      total_tokens: 150
    },
    resumable: true,
    ...overrides
  };
}

function planPayload() {
  return {
    plan_id: "plan-1",
    session_id: "session-1",
    job_id: null,
    status: "planned",
    source: "active_memory",
    created_session: true,
    project_goal: "Forge UI",
    summary: "Build UI",
    warnings: [],
    incomplete: false,
    tasks: [
      {
        task_id: "T01",
        title: "Render plan",
        objective: "Build UI",
        file_scope: { estimated_files: ["src/forge"], write_scope: ["src/forge"] },
        acceptance_criteria: ["Tree renders"],
        verification_commands: ["npm test"],
        risk_notes: ["Low risk when rendered from structured fields."],
        dependencies: ["T00"],
        order: 1,
        scope_unknown_reason: "",
        warnings: ["scope verified"],
        status: "planned"
      }
    ],
    artifacts: [],
    plan_artifact_id: "forge_plan:plan/plan.json",
    plan_markdown_artifact_id: "forge_plan:plan/PLAN.md"
  };
}

function executePreviewPayload(options: {
  missingPrerequisites: string[];
  realExecutionSupported: boolean;
  mode: "readonly" | "review" | "auto";
  workspaceTrusted: boolean;
  sandboxAvailable: boolean;
}) {
  const sandboxDiagnostic = options.sandboxAvailable
    ? "Sandbox ready using test (strict)."
    : "Run `alysis doctor sandbox`.";
  const missingPrerequisites =
    options.mode === "readonly" || options.sandboxAvailable
      ? options.missingPrerequisites
      : [...options.missingPrerequisites, sandboxDiagnostic];
  return {
    session_id: "session-1",
    plan_id: "plan-1",
    selected_task_ids: ["T01"],
    execution_mode_requested: options.mode,
    workspace_trust_required: options.mode !== "readonly",
    workspace_trusted: options.workspaceTrusted,
    estimated_file_scopes: [
      {
        task_id: "T01",
        title: "Render plan",
        estimated_files: ["src/forge"],
        write_scope: ["src/forge"],
        scope_unknown_reason: ""
      }
    ],
    verification_commands: [
      {
        task_id: "T01",
        commands: ["npm test"],
        source: "task",
        missing_reason: ""
      }
    ],
    required_approvals:
      options.mode === "readonly"
        ? []
        : [
            {
              kind: "fs_write",
              task_id: "T01",
              reason: "Forge execution may write the scoped task files.",
              scope: { type: "exact_file_set" },
              allow_for_session_scope: { type: "exact_file_set" },
              allow_for_session_supported: true
            },
            {
              kind: "verify_run",
              task_id: "T01",
              reason: "Forge execution may run the exact verification command set.",
              scope: { type: "exact_verify_command_set" },
              allow_for_session_scope: { type: "exact_verify_command_set" },
              allow_for_session_supported: true
            }
          ],
    runtime_approval_requirements:
      options.mode === "readonly"
        ? []
        : [
            {
              kind: "shell_run",
              reason: "Shell commands require exact command approval.",
              scope_requirement: { type: "exact_command_hash" },
              allow_for_session_supported: true,
              warning: null
            },
            {
              kind: "custom_tool_run",
              reason: "Custom tools require host approval.",
              scope_requirement: null,
              allow_for_session_supported: false,
              warning: "Allow for session is disabled without an explicit safe scope."
            }
          ],
    approval_scopes_safe: true,
    sandbox_profile: {
      requested: "default",
      supported: true,
      available: options.sandboxAvailable,
      diagnostic: sandboxDiagnostic
    },
    known_risks: ["Forge runtime cancellation uses cooperative checkpoints; hard interrupt is unavailable."],
    missing_prerequisites: missingPrerequisites,
    preview_ready: missingPrerequisites.length === 0,
    real_execution_supported: options.realExecutionSupported,
    unsupported_reason: options.realExecutionSupported ? "" : "Forge runtime execution is not wired.",
    active_cancellation_supported: true,
    cancellation: { supported: true, kind: "cooperative_checkpoint", hard_interrupt: false },
    next_recommended_action: "Review the preview.",
    status: "preview"
  };
}

function approvalPayload(options: { approvalId: string; allowForSessionSupported: boolean }) {
  return {
    kind: "approval",
    approval_kind: "fs_write",
    approval_id: options.approvalId,
    prompt_id: options.approvalId,
    reason: "Forge needs to write scoped files.",
    preview: "write src/forge/file.ts",
    command: "npm test",
    files: ["src/forge/file.ts"],
    metadata: { approval_kind: "fs_write" },
    allow_for_session_supported: options.allowForSessionSupported,
    allow_for_session_scope: options.allowForSessionSupported ? { type: "exact_file_set" } : null
  };
}

function diffSummary(diffId: string, planId: string) {
  return {
    diff_id: diffId,
    session_id: "session-1",
    plan_id: planId,
    job_id: null,
    file_path: "README.md",
    status: "available",
    old_label: "before",
    new_label: "after",
    size_bytes: 12
  };
}

async function waitFor(predicate: () => boolean, timeoutMs = 500): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) {
      return;
    }
    await delay(5);
  }
  assert.equal(predicate(), true);
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function eventEnvelope(
  type: string,
  payload: Record<string, unknown>,
  sequence: number,
  sessionId = "session-1",
  jobId: string | null = null
) {
  return {
    protocol_version: "1",
    session_id: sessionId,
    run_id: null,
    job_id: jobId,
    sequence,
    timestamp: "2026-05-21T00:00:00.000Z",
    type,
    payload
  };
}
