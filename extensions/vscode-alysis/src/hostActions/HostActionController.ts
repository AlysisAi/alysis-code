import path from "node:path";

import type {
  HostActionCancelledPayload,
  HostActionName,
  HostActionRequestedPayload,
  HostActionRespondParams,
  HostActionRespondResult,
  HostActionsNegotiation,
  HostCapabilitiesAdvertisement,
  ProtocolEventEnvelope
} from "../client/AlysisProtocol";
import { HOST_ACTION_NAMES, isRecord } from "../client/AlysisProtocol";
import { redactForDisplay } from "../client/CliDiscovery";
import {
  HostActionAdapter,
  HostActionError,
  HostWorkspaceIdentity
} from "./HostActionAdapters";

const MAX_ARGUMENT_BYTES = 8_192;
const MAX_RESULT_BYTES = 65_536;
const MAX_AUDIT_ENTRIES = 128;

export interface HostActionBridge {
  on(event: string, listener: (...args: any[]) => void): unknown;
  off?(event: string, listener: (...args: any[]) => void): unknown;
  supportsMethod?(method: string): boolean;
  hostActionRespond(params: HostActionRespondParams): Promise<HostActionRespondResult>;
}

export interface HostActionAuditEntry {
  sessionId: string;
  hostActionId: string;
  action: HostActionName;
  outcome: "result" | "error" | "cancelled" | "rejected";
  code?: string;
}

export interface HostActionControllerOptions {
  bridge: HostActionBridge;
  adapters: readonly HostActionAdapter[];
  isWorkspaceTrusted(): boolean;
  workspaceIdentities(): readonly HostWorkspaceIdentity[];
  report(message: string): void;
  now?(): number;
}

interface NegotiatedSession {
  sessionId: string;
  workspace: HostWorkspaceIdentity;
  actions: Set<HostActionName>;
  workspaceFence: string;
  capabilityFingerprint: string;
  requestTimeoutSeconds: number;
  maxArgumentBytes: number;
  maxResultBytes: number;
  completedIds: Set<string>;
}

interface PendingAction {
  abort: AbortController;
  token: symbol;
  action: HostActionName;
  sessionId: string;
  hostActionId: string;
  workspaceFence: string;
}

export class HostActionController {
  private readonly adaptersByAction = new Map<HostActionName, HostActionAdapter>();
  private readonly sessions = new Map<string, NegotiatedSession>();
  private readonly pending = new Map<string, PendingAction>();
  private readonly completedRequestKeys = new Set<string>();
  private readonly audit: HostActionAuditEntry[] = [];
  private readonly now: () => number;
  private disposed = false;

  private readonly onNegotiated = (
    sessionId: string,
    workspaceRoot: string,
    negotiation: HostActionsNegotiation
  ): void => this.registerSession(sessionId, workspaceRoot, negotiation);

  private readonly onEvent = (event: ProtocolEventEnvelope): void => {
    if (event.type === "host_action_requested") {
      void this.handleRequested(event);
    } else if (event.type === "host_action_cancelled") {
      this.handleCancelled(event);
    } else if (event.type === "session_closed") {
      this.handleSessionClosed(event);
    }
  };

  private readonly onBridgeReset = (): void => this.invalidateSessions();

  public constructor(private readonly options: HostActionControllerOptions) {
    this.now = options.now ?? Date.now;
    for (const adapter of options.adapters) {
      for (const action of adapter.actions) {
        if (this.adaptersByAction.has(action)) {
          throw new Error(`Duplicate host action adapter registration: ${action}`);
        }
        this.adaptersByAction.set(action, adapter);
      }
    }
    options.bridge.on("hostActionsNegotiated", this.onNegotiated);
    options.bridge.on("event", this.onEvent);
    options.bridge.on("reset", this.onBridgeReset);
    options.bridge.on("exit", this.onBridgeReset);
  }

  public advertisement(): {
    workspace_trusted: boolean;
    host_capabilities: HostCapabilitiesAdvertisement;
  } {
    const trusted = this.options.isWorkspaceTrusted();
    const actions = trusted
      ? HOST_ACTION_NAMES.filter((action) => this.adaptersByAction.has(action))
      : [];
    return {
      workspace_trusted: trusted,
      host_capabilities: { protocol_version: "1", actions }
    };
  }

  public snapshot(): readonly HostActionAuditEntry[] {
    return this.audit.map((entry) => ({ ...entry }));
  }

  public dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.options.bridge.off?.("hostActionsNegotiated", this.onNegotiated);
    this.options.bridge.off?.("event", this.onEvent);
    this.options.bridge.off?.("reset", this.onBridgeReset);
    this.options.bridge.off?.("exit", this.onBridgeReset);
    this.invalidateSessions();
    this.completedRequestKeys.clear();
    for (const adapter of new Set(this.adaptersByAction.values())) {
      adapter.dispose();
    }
    this.adaptersByAction.clear();
  }

  private registerSession(
    sessionId: string,
    workspaceRoot: string,
    negotiation: HostActionsNegotiation
  ): void {
    if (this.disposed) {
      return;
    }
    const matchingWorkspaces = this.options.workspaceIdentities().filter(
      (workspace) => normalizedRoot(workspace.root) === normalizedRoot(workspaceRoot)
    );
    if (matchingWorkspaces.length !== 1) {
      this.options.report("Host actions were not enabled because the negotiated workspace is unavailable or ambiguous.");
      return;
    }
    const actions = negotiation.actions.filter((action) => this.adaptersByAction.has(action));
    if (actions.length !== negotiation.actions.length) {
      this.options.report("Host actions were not enabled because the negotiated action set is unsupported.");
      return;
    }
    if (this.options.bridge.supportsMethod?.("host.action.respond") === false) {
      this.options.report("Host actions were not enabled because the bridge cannot accept host responses.");
      return;
    }
    this.sessions.set(sessionId, {
      sessionId,
      workspace: { ...matchingWorkspaces[0] },
      actions: new Set(actions),
      workspaceFence: negotiation.workspace_fence,
      capabilityFingerprint: negotiation.capability_fingerprint,
      requestTimeoutSeconds: negotiation.request_timeout_seconds,
      maxArgumentBytes: Math.min(negotiation.max_argument_bytes, MAX_ARGUMENT_BYTES),
      maxResultBytes: Math.min(negotiation.max_result_bytes, MAX_RESULT_BYTES),
      completedIds: new Set()
    });
  }

  private async handleRequested(event: ProtocolEventEnvelope): Promise<void> {
    const session = this.sessions.get(event.session_id);
    if (!session || this.disposed) {
      return;
    }
    let request: HostActionRequestedPayload;
    try {
      request = parseRequestedPayload(event.payload);
    } catch (error) {
      const id = safeHostActionId(event.payload.host_action_id);
      if (id) {
        await this.respondError(session, id, error);
      }
      return;
    }
    const key = actionKey(session.sessionId, request.host_action_id);
    if (
      session.completedIds.has(request.host_action_id)
      || this.completedRequestKeys.has(completedRequestKey(session, request.host_action_id))
      || this.pending.has(key)
    ) {
      return;
    }
    const validationError = this.validateRequest(session, request);
    if (validationError) {
      this.rememberCompleted(session, request.host_action_id);
      this.noteAudit(session, request, "rejected", validationError.code);
      await this.respondError(session, request.host_action_id, validationError);
      return;
    }
    const adapter = this.adaptersByAction.get(request.action);
    if (!adapter) {
      this.rememberCompleted(session, request.host_action_id);
      await this.respondError(
        session,
        request.host_action_id,
        new HostActionError("unsupported_action", "The negotiated host action is unavailable.")
      );
      return;
    }
    const abort = new AbortController();
    const token = Symbol(request.host_action_id);
    this.pending.set(key, {
      abort,
      token,
      action: request.action,
      sessionId: session.sessionId,
      hostActionId: request.host_action_id,
      workspaceFence: session.workspaceFence
    });
    try {
      const result = await adapter.execute(request.action, request.arguments, {
        sessionId: session.sessionId,
        workspaceFence: session.workspaceFence,
        workspace: { ...session.workspace },
        signal: abort.signal,
        deadlineMs: Date.parse(request.expires_at),
        maxResultBytes: Math.min(request.max_result_bytes, session.maxResultBytes)
      });
      if (!this.isCurrentPending(key, token)) {
        return;
      }
      if (this.now() >= Date.parse(request.expires_at)) {
        // The adapter finished after the bridge's deadline. The bridge has already
        // given up on this id, so no response is sent -- but the id must still be
        // fenced. Without this, a re-delivery carrying a refreshed expires_at passes
        // the dedupe checks above and runs the side effect (tasks.run, debug.start)
        // a second time.
        this.rememberCompleted(session, request.host_action_id);
        this.noteAudit(session, request, "error", "request_expired");
        return;
      }
      const resultBytes = serializedBytes(result);
      if (resultBytes > Math.min(request.max_result_bytes, session.maxResultBytes)) {
        throw new HostActionError("result_too_large", "The VS Code host action result exceeded its negotiated byte limit.");
      }
      this.rememberCompleted(session, request.host_action_id);
      this.pending.delete(key);
      try {
        await this.options.bridge.hostActionRespond({
          session_id: session.sessionId,
          host_action_id: request.host_action_id,
          workspace_fence: session.workspaceFence,
          capability_fingerprint: session.capabilityFingerprint,
          ok: true,
          result
        });
        this.noteAudit(session, request, "result");
      } catch (responseError) {
        this.options.report(`Host action response failed: ${redactForDisplay(errorMessage(responseError))}`);
      }
    } catch (error) {
      if (!this.isCurrentPending(key, token)) {
        return;
      }
      this.rememberCompleted(session, request.host_action_id);
      await this.respondError(session, request.host_action_id, error);
      this.noteAudit(session, request, "error", hostActionError(error).code);
    } finally {
      if (this.isCurrentPending(key, token)) {
        this.pending.delete(key);
      }
    }
  }

  private handleCancelled(event: ProtocolEventEnvelope): void {
    const session = this.sessions.get(event.session_id);
    if (!session || this.disposed) {
      return;
    }
    let cancellation: HostActionCancelledPayload;
    try {
      cancellation = parseCancelledPayload(event.payload);
    } catch {
      return;
    }
    if (
      cancellation.workspace_fence !== session.workspaceFence
      || cancellation.capability_fingerprint !== session.capabilityFingerprint
      || !session.actions.has(cancellation.action)
    ) {
      return;
    }
    const key = actionKey(session.sessionId, cancellation.host_action_id);
    const pending = this.pending.get(key);
    if (!pending || pending.action !== cancellation.action) {
      this.rememberCompleted(session, cancellation.host_action_id);
      return;
    }
    this.pending.delete(key);
    this.rememberCompleted(session, cancellation.host_action_id);
    pending.abort.abort();
    this.pushAudit({
      sessionId: session.sessionId,
      hostActionId: cancellation.host_action_id,
      action: cancellation.action,
      outcome: "cancelled"
    });
  }

  private handleSessionClosed(event: ProtocolEventEnvelope): void {
    const session = this.sessions.get(event.session_id);
    if (
      !session
      || event.payload.protocol_version !== "1"
      || event.payload.workspace_fence !== session.workspaceFence
      || event.payload.capability_fingerprint !== session.capabilityFingerprint
    ) {
      return;
    }
    for (const [key, pending] of this.pending) {
      if (!key.startsWith(`${session.sessionId}\u0000`)) {
        continue;
      }
      this.pending.delete(key);
      this.rememberCompleted(session, pending.hostActionId);
      pending.abort.abort();
    }
    for (const adapter of new Set(this.adaptersByAction.values())) {
      adapter.invalidateSession?.(session.sessionId, session.workspaceFence);
    }
    this.sessions.delete(session.sessionId);
  }

  private validateRequest(
    session: NegotiatedSession,
    request: HostActionRequestedPayload
  ): HostActionError | undefined {
    if (request.protocol_version !== "1") {
      return new HostActionError("protocol_mismatch", "The host action protocol version is unsupported.");
    }
    if (request.workspace_fence !== session.workspaceFence
      || request.capability_fingerprint !== session.capabilityFingerprint) {
      return new HostActionError("workspace_fence_mismatch", "The host action workspace fence is invalid.");
    }
    if (normalizedRoot(request.workspace_root) !== normalizedRoot(session.workspace.root)) {
      return new HostActionError("workspace_fence_mismatch", "The host action workspace root is invalid.");
    }
    if (!session.actions.has(request.action)) {
      return new HostActionError("unsupported_action", "The host action was not negotiated for this session.");
    }
    if (!this.options.isWorkspaceTrusted()) {
      return new HostActionError("workspace_untrusted", "VS Code host actions require Workspace Trust.");
    }
    const current = this.options.workspaceIdentities().filter(
      (workspace) => workspaceIdentityKey(workspace) === workspaceIdentityKey(session.workspace)
    );
    if (current.length !== 1) {
      return new HostActionError("workspace_unavailable", "The negotiated workspace folder is no longer open.");
    }
    if (serializedBytes(request.arguments) > session.maxArgumentBytes) {
      return new HostActionError("arguments_too_large", "The host action arguments exceeded their negotiated byte limit.");
    }
    if (request.max_result_bytes < 1 || request.max_result_bytes > session.maxResultBytes) {
      return new HostActionError("result_limit_mismatch", "The host action result limit is invalid.");
    }
    const expiresAt = Date.parse(request.expires_at);
    const now = this.now();
    if (!Number.isFinite(expiresAt) || expiresAt <= now) {
      return new HostActionError("request_expired", "The host action request has expired.");
    }
    if (expiresAt > now + (session.requestTimeoutSeconds * 1_000) + 5_000) {
      return new HostActionError("invalid_expiry", "The host action request expiry exceeds the negotiated timeout.");
    }
    return undefined;
  }

  private async respondError(
    session: NegotiatedSession,
    hostActionId: string,
    error: unknown
  ): Promise<void> {
    const normalized = hostActionError(error);
    try {
      await this.options.bridge.hostActionRespond({
        session_id: session.sessionId,
        host_action_id: hostActionId,
        workspace_fence: session.workspaceFence,
        capability_fingerprint: session.capabilityFingerprint,
        ok: false,
        error: {
          code: boundedCode(normalized.code),
          message: redactForDisplay(normalized.message).slice(0, 512),
          retryable: normalized.retryable
        }
      });
    } catch (responseError) {
      this.options.report(`Host action response failed: ${redactForDisplay(errorMessage(responseError))}`);
    }
  }

  private noteAudit(
    session: NegotiatedSession,
    request: Pick<HostActionRequestedPayload, "host_action_id" | "action">,
    outcome: HostActionAuditEntry["outcome"],
    code?: string
  ): void {
    this.pushAudit({
      sessionId: session.sessionId,
      hostActionId: request.host_action_id,
      action: request.action,
      outcome,
      ...(code ? { code } : {})
    });
  }

  private pushAudit(entry: HostActionAuditEntry): void {
    this.audit.push(entry);
    if (this.audit.length > MAX_AUDIT_ENTRIES) {
      this.audit.splice(0, this.audit.length - MAX_AUDIT_ENTRIES);
    }
  }

  private rememberCompleted(session: NegotiatedSession, hostActionId: string): void {
    session.completedIds.add(hostActionId);
    this.completedRequestKeys.add(completedRequestKey(session, hostActionId));
    while (session.completedIds.size > 512) {
      const oldest = session.completedIds.values().next().value as string | undefined;
      if (!oldest) {
        break;
      }
      session.completedIds.delete(oldest);
    }
    while (this.completedRequestKeys.size > 2_048) {
      const oldest = this.completedRequestKeys.values().next().value as string | undefined;
      if (!oldest) {
        break;
      }
      this.completedRequestKeys.delete(oldest);
    }
  }

  private isCurrentPending(key: string, token: symbol): boolean {
    return this.pending.get(key)?.token === token;
  }

  private invalidateSessions(): void {
    for (const pending of this.pending.values()) {
      const session = this.sessions.get(pending.sessionId);
      if (session && session.workspaceFence === pending.workspaceFence) {
        this.rememberCompleted(session, pending.hostActionId);
      }
      pending.abort.abort();
    }
    this.pending.clear();
    this.sessions.clear();
    for (const adapter of new Set(this.adaptersByAction.values())) {
      adapter.invalidateAll?.();
    }
  }
}

function parseRequestedPayload(payload: Record<string, unknown>): HostActionRequestedPayload {
  const hostActionId = safeHostActionId(payload.host_action_id);
  const action = safeHostActionName(payload.action);
  if (
    !hostActionId
    || !isRecord(payload.arguments)
    || typeof payload.workspace_root !== "string"
    || payload.workspace_root.length < 1
    || payload.workspace_root.length > 4_096
    || typeof payload.workspace_fence !== "string"
    || typeof payload.capability_fingerprint !== "string"
    || typeof payload.expires_at !== "string"
    || payload.protocol_version !== "1"
    || typeof payload.max_result_bytes !== "number"
    || !Number.isInteger(payload.max_result_bytes)
  ) {
    throw new HostActionError("invalid_request", "The host action request payload is invalid.");
  }
  return {
    host_action_id: hostActionId,
    action,
    arguments: payload.arguments,
    workspace_root: payload.workspace_root,
    workspace_fence: payload.workspace_fence,
    capability_fingerprint: payload.capability_fingerprint,
    expires_at: payload.expires_at,
    protocol_version: "1",
    max_result_bytes: payload.max_result_bytes
  };
}

function parseCancelledPayload(payload: Record<string, unknown>): HostActionCancelledPayload {
  const hostActionId = safeHostActionId(payload.host_action_id);
  const action = safeHostActionName(payload.action);
  if (
    !hostActionId
    || typeof payload.workspace_fence !== "string"
    || typeof payload.capability_fingerprint !== "string"
    || typeof payload.reason !== "string"
    || payload.reason.length > 512
    || payload.protocol_version !== "1"
  ) {
    throw new HostActionError("invalid_request", "The host action cancellation payload is invalid.");
  }
  return {
    host_action_id: hostActionId,
    action,
    workspace_fence: payload.workspace_fence,
    capability_fingerprint: payload.capability_fingerprint,
    reason: payload.reason,
    protocol_version: "1"
  };
}

function safeHostActionId(value: unknown): string | undefined {
  return typeof value === "string" && /^ha_[a-f0-9]{32}$/.test(value) ? value : undefined;
}

function safeHostActionName(value: unknown): HostActionName {
  if (typeof value === "string" && (HOST_ACTION_NAMES as readonly string[]).includes(value)) {
    return value as HostActionName;
  }
  throw new HostActionError("unsupported_action", "The host action is unsupported.");
}

function hostActionError(error: unknown): HostActionError {
  if (error instanceof HostActionError) {
    return error;
  }
  return new HostActionError("host_action_failed", errorMessage(error));
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function boundedCode(value: string): string {
  const normalized = value.replace(/[^a-z0-9_]/gi, "_").slice(0, 64);
  return normalized || "host_action_failed";
}

function serializedBytes(value: unknown): number {
  try {
    return Buffer.byteLength(JSON.stringify(value), "utf8");
  } catch {
    throw new HostActionError("invalid_payload", "The host action payload could not be serialized.");
  }
}

function normalizedRoot(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function workspaceIdentityKey(workspace: HostWorkspaceIdentity): string {
  return `${workspace.scheme}\u0000${workspace.authority}\u0000${normalizedRoot(workspace.root)}`;
}

function actionKey(sessionId: string, hostActionId: string): string {
  return `${sessionId}\u0000${hostActionId}`;
}

function completedRequestKey(session: NegotiatedSession, hostActionId: string): string {
  return `${session.sessionId}\u0000${session.workspaceFence}\u0000${hostActionId}`;
}
