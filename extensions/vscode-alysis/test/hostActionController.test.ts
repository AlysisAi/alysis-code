import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createHash } from "node:crypto";
import test from "node:test";

import {
  HostActionController,
  type HostActionBridge
} from "../src/hostActions/HostActionController";
import type {
  HostActionAdapter,
  HostActionExecutionContext
} from "../src/hostActions/HostActionAdapters";
import type {
  HostActionName,
  HostActionRespondParams,
  HostActionRespondResult,
  HostActionsNegotiation,
  ProtocolEventEnvelope
} from "../src/client/AlysisProtocol";

const ROOT = "C:\\workspace";
const FENCE = "wf_host-actions-test";
const FINGERPRINT = "a".repeat(64);

test("host capabilities fail closed in untrusted workspaces", () => {
  const fixture = createFixture({ trusted: false });
  assert.deepEqual(fixture.controller.advertisement(), {
    workspace_trusted: false,
    host_capabilities: { protocol_version: "1", actions: [] }
  });
  fixture.controller.dispose();
});

test("a negotiated request dispatches once and returns a bounded correlated result", async () => {
  const fixture = createFixture();
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("ha_list_once"));
  await settle();

  assert.equal(fixture.adapter.calls.length, 1);
  assert.equal(fixture.bridge.responses.length, 1);
  assert.deepEqual(fixture.bridge.responses[0], {
    session_id: "session-1",
    host_action_id: hostActionId("ha_list_once"),
    workspace_fence: FENCE,
    capability_fingerprint: FINGERPRINT,
    ok: true,
    result: { tasks: [], truncated: false }
  });
  assert.equal(fixture.controller.snapshot()[0].outcome, "result");
  fixture.controller.dispose();
});

test("live trust loss and workspace authority changes refuse actions before adapter side effects", async () => {
  const fixture = createFixture();
  negotiate(fixture);
  fixture.trusted.value = false;
  fixture.bridge.emit("event", requestedEvent("ha_untrusted"));
  await settle();
  assert.equal(fixture.adapter.calls.length, 0);
  assert.equal(fixture.bridge.responses[0].ok, false);
  assert.equal(fixture.bridge.responses[0].error?.code, "workspace_untrusted");

  fixture.trusted.value = true;
  fixture.workspaces[0] = { ...fixture.workspaces[0], authority: "different-remote" };
  fixture.bridge.emit("event", requestedEvent("ha_wrong_authority"));
  await settle();
  assert.equal(fixture.adapter.calls.length, 0);
  assert.equal(fixture.bridge.responses[1].error?.code, "workspace_unavailable");
  fixture.controller.dispose();
});

test("wrong root, fence, fingerprint, action, expiry, and oversized arguments all fail closed", async () => {
  const fixture = createFixture();
  negotiate(fixture);
  const cases: Array<[string, Partial<Record<string, unknown>>, string]> = [
    ["ha_wrong_root", { workspace_root: "C:\\other" }, "workspace_fence_mismatch"],
    ["ha_wrong_fence", { workspace_fence: "wf_wrong" }, "workspace_fence_mismatch"],
    ["ha_wrong_fingerprint", { capability_fingerprint: "b".repeat(64) }, "workspace_fence_mismatch"],
    ["ha_not_negotiated", { action: "debug.list" }, "unsupported_action"],
    ["ha_expired", { expires_at: new Date(0).toISOString() }, "request_expired"],
    ["ha_big_args", { arguments: { text: "x".repeat(9_000) } }, "arguments_too_large"]
  ];
  for (const [id, overrides, code] of cases) {
    fixture.bridge.emit("event", requestedEvent(id, overrides));
    await settle();
    assert.equal(fixture.bridge.responses.at(-1)?.error?.code, code, id);
  }
  assert.equal(fixture.adapter.calls.length, 0);
  fixture.controller.dispose();
});

test("duplicate requests are single-flight and completed duplicates stay stale", async () => {
  let release: (() => void) | undefined;
  const fixture = createFixture({
    execute: async () => new Promise<Record<string, unknown>>((resolve) => {
      release = () => resolve({ tasks: [], truncated: false });
    })
  });
  negotiate(fixture);
  const event = requestedEvent("ha_duplicate");
  fixture.bridge.emit("event", event);
  fixture.bridge.emit("event", event);
  await settle();
  assert.equal(fixture.adapter.calls.length, 1);
  release?.();
  await settle();
  fixture.bridge.emit("event", event);
  await settle();
  assert.equal(fixture.adapter.calls.length, 1);
  assert.equal(fixture.bridge.responses.length, 1);
  fixture.controller.dispose();
});

test("an action that completes after its deadline is fenced against re-delivery", async () => {
  // Regression: the post-execute expiry check returned without rememberCompleted(),
  // so a bridge that re-delivered the same host_action_id with a fresh expires_at
  // passed every dedupe check and ran the adapter (tasks.run, debug.start) twice.
  let release: (() => void) | undefined;
  const clock = { value: 0 };
  const fixture = createFixture({
    now: () => clock.value,
    execute: async () => new Promise<Record<string, unknown>>((resolve) => {
      release = () => resolve({ tasks: [], truncated: false });
    })
  });
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("ha_late"));
  await settle();
  assert.equal(fixture.adapter.calls.length, 1);

  // The deadline passes while the adapter is still running, then it finishes.
  clock.value = 30_000;
  release?.();
  await settle();
  assert.equal(fixture.bridge.responses.length, 0, "a late result must not be sent");
  assert.deepEqual(
    fixture.controller.snapshot().at(-1),
    {
      sessionId: "session-1",
      hostActionId: hostActionId("ha_late"),
      action: "tasks.list",
      outcome: "error",
      code: "request_expired"
    }
  );

  // Same id comes back with a refreshed expiry: it must be treated as already done.
  fixture.bridge.emit("event", requestedEvent("ha_late", { expires_at: new Date(60_000).toISOString() }));
  await settle();
  assert.equal(fixture.adapter.calls.length, 1, "the same host_action_id must never execute twice");
  assert.equal(fixture.bridge.responses.length, 0);
  fixture.controller.dispose();
});

test("explicit cancellation aborts the adapter and fences every late completion", async () => {
  let release: (() => void) | undefined;
  let observedSignal: AbortSignal | undefined;
  const fixture = createFixture({
    execute: async (_action, _args, context) => {
      observedSignal = context.signal;
      return new Promise<Record<string, unknown>>((resolve) => {
        release = () => resolve({ tasks: [], truncated: false });
      });
    }
  });
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("ha_cancelled"));
  await settle();
  fixture.bridge.emit("event", cancelledEvent("ha_cancelled"));
  assert.equal(observedSignal?.aborted, true);
  release?.();
  await settle();
  assert.equal(fixture.bridge.responses.length, 0);
  assert.equal(fixture.controller.snapshot().at(-1)?.outcome, "cancelled");
  fixture.controller.dispose();
});

test("bridge reset and disposal abort pending actions without sending stale responses", async () => {
  const signals: AbortSignal[] = [];
  const fixture = createFixture({
    execute: async (_action, _args, context) => {
      signals.push(context.signal);
      return new Promise<Record<string, unknown>>(() => undefined);
    }
  });
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("ha_reset"));
  await settle();
  fixture.bridge.emit("reset", "bridge_restarted");
  assert.equal(signals[0].aborted, true);

  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("ha_dispose"));
  await settle();
  fixture.controller.dispose();
  assert.equal(signals[1].aborted, true);
  assert.equal(fixture.bridge.responses.length, 0);
});

test("serialized results that exceed the negotiated limit return an error instead of leaking partial data", async () => {
  const fixture = createFixture({ execute: async () => ({ value: "x".repeat(70_000) }) });
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("ha_oversize_result"));
  await settle();
  assert.equal(fixture.bridge.responses.length, 1);
  assert.equal(fixture.bridge.responses[0].ok, false);
  assert.equal(fixture.bridge.responses[0].error?.code, "result_too_large");
  assert.equal("result" in fixture.bridge.responses[0], false);
  fixture.controller.dispose();
});

test("malformed host action ids are ignored before dispatch or response", async () => {
  const fixture = createFixture();
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("valid", { host_action_id: "ha_not-32-hex" }));
  await settle();
  assert.equal(fixture.adapter.calls.length, 0);
  assert.equal(fixture.bridge.responses.length, 0);
  fixture.controller.dispose();
});

test("completed and reset-aborted request ids stay replay-fenced across session re-adoption", async () => {
  let release: (() => void) | undefined;
  const fixture = createFixture({
    execute: async () => new Promise<Record<string, unknown>>((resolve) => {
      release = () => resolve({ tasks: [], truncated: false });
    })
  });
  negotiate(fixture);
  const event = requestedEvent("replay-after-reset");
  fixture.bridge.emit("event", event);
  await settle();
  assert.equal(fixture.adapter.calls.length, 1);
  fixture.bridge.emit("reset", "bridge_restarted");
  release?.();
  await settle();
  negotiate(fixture);
  fixture.bridge.emit("event", event);
  await settle();
  assert.equal(fixture.adapter.calls.length, 1, "replayed mutation must stay stale after re-adoption");
  assert.equal(fixture.bridge.responses.length, 0);
  fixture.controller.dispose();
});

test("session_closed invalidates exact-fence owned resources and fences late work", async () => {
  const fixture = createFixture({
    execute: async () => new Promise<Record<string, unknown>>(() => undefined)
  });
  negotiate(fixture);
  fixture.bridge.emit("event", requestedEvent("session-close"));
  await settle();
  fixture.bridge.emit("event", {
    ...requestedEvent("close-envelope"),
    type: "session_closed",
    payload: {
      protocol_version: "1",
      workspace_fence: FENCE,
      capability_fingerprint: FINGERPRINT
    }
  });
  assert.deepEqual(fixture.adapter.invalidatedSessions, [["session-1", FENCE]]);
  assert.equal(fixture.bridge.responses.length, 0);
  fixture.controller.dispose();
});

function createFixture(options: {
  trusted?: boolean;
  now?: () => number;
  execute?: (
    action: HostActionName,
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ) => Promise<Record<string, unknown>>;
} = {}) {
  const bridge = new FakeBridge();
  const trusted = { value: options.trusted ?? true };
  const workspaces = [{ root: ROOT, scheme: "file", authority: "", name: "workspace" }];
  const adapter = new FakeAdapter(options.execute);
  const controller = new HostActionController({
    bridge,
    adapters: [adapter],
    isWorkspaceTrusted: () => trusted.value,
    workspaceIdentities: () => workspaces,
    report: () => undefined,
    now: options.now ?? (() => 0)
  });
  return { bridge, trusted, workspaces, adapter, controller };
}

class FakeBridge extends EventEmitter implements HostActionBridge {
  public readonly responses: HostActionRespondParams[] = [];

  public supportsMethod(method: string): boolean {
    return method === "host.action.respond";
  }

  public async hostActionRespond(params: HostActionRespondParams): Promise<HostActionRespondResult> {
    this.responses.push(params);
    return {
      status: "applied",
      session_id: params.session_id,
      host_action_id: params.host_action_id,
      action: "tasks.list",
      outcome: params.ok ? "result" : "error"
    };
  }
}

class FakeAdapter implements HostActionAdapter {
  public readonly actions = ["tasks.list"] as const;
  public readonly calls: Array<{ action: HostActionName; args: Record<string, unknown> }> = [];
  public disposed = false;
  public readonly invalidatedSessions: Array<[string, string]> = [];
  public invalidatedAll = 0;

  public constructor(
    private readonly implementation: (
      action: HostActionName,
      args: Record<string, unknown>,
      context: HostActionExecutionContext
    ) => Promise<Record<string, unknown>> = async () => ({ tasks: [], truncated: false })
  ) {}

  public async execute(
    action: HostActionName,
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>> {
    this.calls.push({ action, args });
    return this.implementation(action, args, context);
  }

  public dispose(): void {
    this.disposed = true;
  }

  public invalidateSession(sessionId: string, workspaceFence: string): void {
    this.invalidatedSessions.push([sessionId, workspaceFence]);
  }

  public invalidateAll(): void {
    this.invalidatedAll += 1;
  }
}

function negotiate(fixture: ReturnType<typeof createFixture>): void {
  fixture.bridge.emit("hostActionsNegotiated", "session-1", ROOT, negotiation());
}

function negotiation(): HostActionsNegotiation {
  return {
    protocol_version: "1",
    actions: ["tasks.list"],
    workspace_fence: FENCE,
    capability_fingerprint: FINGERPRINT,
    request_event: "host_action_requested",
    cancellation_event: "host_action_cancelled",
    session_closed_event: "session_closed",
    response_method: "host.action.respond",
    request_timeout_seconds: 30,
    max_argument_bytes: 8_192,
    max_result_bytes: 65_536
  };
}

function requestedEvent(
  hostActionId: string,
  overrides: Partial<Record<string, unknown>> = {}
): ProtocolEventEnvelope {
  return {
    protocol_version: "1",
    session_id: "session-1",
    run_id: null,
    job_id: "job-1",
    sequence: 1,
    timestamp: new Date(0).toISOString(),
    type: "host_action_requested",
    payload: {
      host_action_id: exactHostActionId(hostActionId),
      action: "tasks.list",
      arguments: {},
      workspace_root: ROOT,
      workspace_fence: FENCE,
      capability_fingerprint: FINGERPRINT,
      expires_at: new Date(30_000).toISOString(),
      protocol_version: "1",
      max_result_bytes: 65_536,
      ...overrides
    }
  };
}

function cancelledEvent(hostActionId: string): ProtocolEventEnvelope {
  return {
    ...requestedEvent(hostActionId),
    type: "host_action_cancelled",
    payload: {
      host_action_id: exactHostActionId(hostActionId),
      action: "tasks.list",
      workspace_fence: FENCE,
      capability_fingerprint: FINGERPRINT,
      reason: "session_cancelled",
      protocol_version: "1"
    }
  };
}

async function settle(): Promise<void> {
  await new Promise<void>((resolve) => setImmediate(resolve));
  await new Promise<void>((resolve) => setImmediate(resolve));
}

function exactHostActionId(label: string): string {
  if (/^ha_[a-f0-9]{32}$/.test(label)) {
    return label;
  }
  return hostActionId(label);
}

function hostActionId(label: string): string {
  return `ha_${createHash("sha256").update(label, "utf8").digest("hex").slice(0, 32)}`;
}
