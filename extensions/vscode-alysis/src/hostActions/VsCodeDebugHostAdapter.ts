import { createHash, randomUUID } from "node:crypto";
import path from "node:path";

import * as vscode from "vscode";

import type { HostActionName } from "../client/AlysisProtocol";
import {
  HostActionAdapter,
  HostActionError,
  HostActionExecutionContext,
  optionalBoundedString,
  requireNoExtraArguments,
  requiredBoundedString,
  throwIfAborted
} from "./HostActionAdapters";

const MAX_DEBUG_ROWS = 100;
// Injected into the configuration passed to startDebugging so the published session can be tied back
// to the exact debug.start request that asked for it.
const ALYSIS_DEBUG_REQUEST_KEY = "__alysisHostActionRequestId";
const ALYSIS_DEBUG_REQUEST_PREFIX = "alysis-debug-";

export interface VsCodeDebugApi {
  workspaceFolders(): readonly vscode.WorkspaceFolder[];
  configurations(folder: vscode.WorkspaceFolder): unknown;
  startDebugging(
    folder: vscode.WorkspaceFolder,
    configuration: vscode.DebugConfiguration
  ): Thenable<boolean>;
  stopDebugging(session: vscode.DebugSession): Thenable<void>;
  onDidStartDebugSession(listener: (session: vscode.DebugSession) => unknown): vscode.Disposable;
  onDidTerminateDebugSession(listener: (session: vscode.DebugSession) => unknown): vscode.Disposable;
}

interface DebugConfigurationEntry {
  id: string;
  folder: vscode.WorkspaceFolder;
  configuration: vscode.DebugConfiguration;
}

interface DebugSessionEntry {
  session: vscode.DebugSession;
  sequence: number;
}

interface OwnedDebugSession {
  session: vscode.DebugSession;
  ownerKey: string;
  configurationId: string;
}

interface StoppedDebugSession {
  id: string;
  name: string;
  type: string;
  workspaceKey: string;
  workspaceFolder?: string;
}

interface LateStartCleanup {
  requestId: string;
  expiresMs: number;
}

export class VsCodeDebugHostAdapter implements HostActionAdapter {
  public readonly actions = ["debug.list", "debug.start", "debug.stop", "debug.status"] as const;

  private readonly subscriptions: vscode.Disposable[];
  private readonly observedStarts = new Map<string, DebugSessionEntry>();
  private readonly owned = new Map<string, OwnedDebugSession>();
  private readonly stopped = new Map<string, StoppedDebugSession>();
  private readonly terminationWaiters = new Map<string, Set<() => void>>();
  private readonly startWaiters = new Set<() => void>();
  private readonly lateStartCleanups: LateStartCleanup[] = [];
  private sequence = 0;
  private disposed = false;

  public constructor(private readonly api: VsCodeDebugApi) {
    this.subscriptions = [
      api.onDidStartDebugSession((session) => this.noteStarted(session)),
      api.onDidTerminateDebugSession((session) => this.noteTerminated(session))
    ];
  }

  public async execute(
    action: HostActionName,
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>> {
    this.ensureUsable();
    throwIfAborted(context.signal);
    switch (action) {
      case "debug.list": return this.list(args, context);
      case "debug.start": return this.start(args, context);
      case "debug.stop": return this.stop(args, context);
      case "debug.status": return this.status(args, context);
      default:
        throw new HostActionError("unsupported_action", "This Debug host action is not supported.");
    }
  }

  public dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    for (const subscription of this.subscriptions.splice(0)) {
      subscription.dispose();
    }
    this.invalidateAll();
    this.observedStarts.clear();
    this.stopped.clear();
    this.terminationWaiters.clear();
    this.startWaiters.clear();
    this.lateStartCleanups.splice(0);
  }

  public invalidateSession(sessionId: string, workspaceFence: string): void {
    const ownerPrefix = `${sessionId}\u0000${workspaceFence}\u0000`;
    for (const [debugSessionId, owned] of this.owned) {
      if (!owned.ownerKey.startsWith(ownerPrefix)) {
        continue;
      }
      this.stopDetached(owned.session);
      this.owned.delete(debugSessionId);
      this.rememberStopped(owned);
    }
  }

  public invalidateAll(): void {
    for (const [debugSessionId, owned] of this.owned) {
      this.stopDetached(owned.session);
      this.owned.delete(debugSessionId);
      this.rememberStopped(owned);
    }
  }

  /**
   * Best-effort stop for a session nobody is awaiting. VS Code rejects stopDebugging when the session
   * already ended, and an unhandled rejection in the extension host is never acceptable.
   */
  private stopDetached(session: vscode.DebugSession): void {
    void Promise.resolve(this.api.stopDebugging(session)).catch(() => undefined);
  }

  private list(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Record<string, unknown> {
    requireNoExtraArguments(args, []);
    const configurations = this.configurationEntries(context);
    const candidates = configurations.slice(0, MAX_DEBUG_ROWS).map((entry) => ({
      id: entry.id,
      name: boundedText(entry.configuration.name, 512),
      type: boundedText(entry.configuration.type, 256),
      ...(boundedOptionalText(entry.configuration.request, 64)
        ? { request: boundedOptionalText(entry.configuration.request, 64) }
        : {}),
      workspace_folder: boundedText(entry.folder.name, 256)
    }));
    const rows = boundedRows("configurations", candidates, context.maxResultBytes);
    return { configurations: rows, truncated: configurations.length > rows.length };
  }

  private async start(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>> {
    requireNoExtraArguments(args, ["configuration_id"]);
    const configurationId = requiredBoundedString(args, "configuration_id");
    const entry = this.configurationEntries(context).find((candidate) => candidate.id === configurationId);
    if (!entry) {
      throw new HostActionError("debug_configuration_not_found", "The selected debug configuration is unavailable.");
    }
    const baselineSequence = this.sequence;
    throwIfAborted(context.signal);
    // Correlate this exact request with the session VS Code publishes for it. Without the marker a
    // timed-out start could later adopt — or stop — an unrelated session with the same name and type.
    const requestId = `${ALYSIS_DEBUG_REQUEST_PREFIX}${randomUUID()}`;
    const started = await this.api.startDebugging(entry.folder, {
      ...entry.configuration,
      [ALYSIS_DEBUG_REQUEST_KEY]: requestId
    });
    if (!started) {
      throw new HostActionError("debug_start_failed", "VS Code did not start the selected debug configuration.", true);
    }
    const session = await this.waitForStartedSession(entry, context, baselineSequence, requestId);
    this.observedStarts.delete(session.id);
    const owned: OwnedDebugSession = {
      session,
      ownerKey: debugOwnerKey(context),
      configurationId
    };
    this.stopped.delete(session.id);
    this.owned.set(session.id, owned);
    if (context.signal.aborted) {
      await this.api.stopDebugging(session);
      this.owned.delete(session.id);
      this.rememberStopped(owned);
      throw new HostActionError("host_action_cancelled", "The debug start action was cancelled.");
    }
    return {
      debug_session_id: boundedText(session.id, 256),
      configuration_id: configurationId,
      state: "started"
    };
  }

  private async stop(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>> {
    requireNoExtraArguments(args, ["debug_session_id"]);
    const debugSessionId = requiredBoundedString(args, "debug_session_id");
    const running = this.owned.get(debugSessionId);
    const ownerKey = debugOwnerKey(context);
    if (!running || running.ownerKey !== ownerKey) {
      if (this.stopped.get(debugSessionId)?.workspaceKey === ownerKey) {
        return { debug_session_id: debugSessionId, stopped: false, state: "already_ended" };
      }
      return { debug_session_id: debugSessionId, stopped: false, state: "not_found" };
    }
    throwIfAborted(context.signal);
    await this.api.stopDebugging(running.session);
    await this.waitForTermination(debugSessionId, context);
    return { debug_session_id: debugSessionId, stopped: true, state: "stopped" };
  }

  private status(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Record<string, unknown> {
    requireNoExtraArguments(args, ["debug_session_id"]);
    const requestedId = optionalBoundedString(args, "debug_session_id");
    const workspaceKey = debugOwnerKey(context);
    const rows: Record<string, unknown>[] = [];
    for (const entry of this.owned.values()) {
      if (entry.ownerKey !== workspaceKey || (requestedId && entry.session.id !== requestedId)) {
        continue;
      }
      rows.push(debugSessionRow(entry.session, "running"));
    }
    for (const entry of this.stopped.values()) {
      if (entry.workspaceKey !== workspaceKey || (requestedId && entry.id !== requestedId)) {
        continue;
      }
      rows.push({
        id: entry.id,
        name: entry.name,
        type: entry.type,
        state: "stopped",
        ...(entry.workspaceFolder ? { workspace_folder: entry.workspaceFolder } : {})
      });
    }
    const bounded = boundedRows("sessions", rows.slice(0, MAX_DEBUG_ROWS), context.maxResultBytes);
    return { sessions: bounded, truncated: rows.length > bounded.length };
  }

  private configurationEntries(context: HostActionExecutionContext): DebugConfigurationEntry[] {
    const folder = this.api.workspaceFolders().find((candidate) => workspaceUriMatches(candidate.uri, context));
    if (!folder) {
      throw new HostActionError("workspace_unavailable", "The negotiated workspace folder is no longer open.");
    }
    const raw = this.api.configurations(folder);
    if (!Array.isArray(raw)) {
      return [];
    }
    const entries: DebugConfigurationEntry[] = [];
    for (const candidate of raw) {
      if (!isDebugConfiguration(candidate)) {
        continue;
      }
      entries.push({
        id: configurationIdentity(candidate, folder, context),
        folder,
        configuration: candidate
      });
    }
    return entries;
  }

  private sessionMatchesConfiguration(
    session: vscode.DebugSession,
    entry: DebugConfigurationEntry,
    context: HostActionExecutionContext,
    requestId: string
  ): boolean {
    if (debugSessionRequestId(session) === requestId) {
      return true;
    }
    // Fallback for debug adapters that drop unknown configuration keys: identity plus the sequence
    // fence still scopes the match to sessions published after this request started.
    return debugSessionRequestId(session) === undefined
      && session.name === entry.configuration.name
      && session.type === entry.configuration.type
      && Boolean(session.workspaceFolder && workspaceUriMatches(session.workspaceFolder.uri, context));
  }

  private async waitForStartedSession(
    entry: DebugConfigurationEntry,
    context: HostActionExecutionContext,
    baselineSequence: number,
    requestId: string
  ): Promise<vscode.DebugSession> {
    const findMatch = (): vscode.DebugSession | undefined => [...this.observedStarts.values()]
      .filter((candidate) => candidate.sequence > baselineSequence)
      .map((candidate) => candidate.session)
      .find((candidate) => this.sessionMatchesConfiguration(candidate, entry, context, requestId));
    const existing = findMatch();
    if (existing) {
      return existing;
    }
    const waitMs = Math.max(0, context.deadlineMs - Date.now() - 100);
    if (waitMs === 0) {
      this.rememberLateStartCleanup(context, requestId);
      throw new HostActionError("debug_start_timeout", "VS Code did not publish the started debug session before the host action deadline.", true);
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (session?: vscode.DebugSession, error?: HostActionError): void => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timeout);
        context.signal.removeEventListener("abort", onAbort);
        this.startWaiters.delete(onStart);
        if (session) {
          resolve(session);
        } else {
          this.rememberLateStartCleanup(context, requestId);
          reject(error ?? new HostActionError("debug_start_timeout", "VS Code did not publish the started debug session before the host action deadline.", true));
        }
      };
      const onStart = (): void => {
        const session = findMatch();
        if (session) {
          finish(session);
        }
      };
      const onAbort = (): void => finish(
        undefined,
        new HostActionError("host_action_cancelled", "The debug start action was cancelled.")
      );
      const timeout = setTimeout(() => finish(), waitMs);
      this.startWaiters.add(onStart);
      context.signal.addEventListener("abort", onAbort, { once: true });
      onStart();
      if (context.signal.aborted) {
        onAbort();
      }
    });
  }

  private rememberLateStartCleanup(
    context: HostActionExecutionContext,
    requestId: string
  ): void {
    this.lateStartCleanups.push({
      requestId,
      expiresMs: context.deadlineMs + 5_000
    });
    if (this.lateStartCleanups.length > 32) {
      this.lateStartCleanups.splice(0, this.lateStartCleanups.length - 32);
    }
  }

  private waitForTermination(
    sessionId: string,
    context: HostActionExecutionContext
  ): Promise<void> {
    if (!this.owned.has(sessionId)) {
      return Promise.resolve();
    }
    const waitMs = Math.max(0, context.deadlineMs - Date.now() - 100);
    if (waitMs === 0) {
      return Promise.reject(new HostActionError("debug_stop_timeout", "VS Code did not confirm debug termination before the host action deadline.", true));
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      let waiters = this.terminationWaiters.get(sessionId);
      if (!waiters) {
        waiters = new Set();
        this.terminationWaiters.set(sessionId, waiters);
      }
      const finish = (error?: Error): void => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timeout);
        context.signal.removeEventListener("abort", onAbort);
        waiters?.delete(onTerminate);
        if (error) {
          reject(error);
        } else {
          resolve();
        }
      };
      const onTerminate = (): void => finish();
      const onAbort = (): void => finish(new HostActionError("host_action_cancelled", "The debug stop action was cancelled."));
      const timeout = setTimeout(
        () => finish(new HostActionError("debug_stop_timeout", "VS Code did not confirm debug termination before the host action deadline.", true)),
        waitMs
      );
      waiters.add(onTerminate);
      context.signal.addEventListener("abort", onAbort, { once: true });
      if (!this.owned.has(sessionId)) {
        onTerminate();
      } else if (context.signal.aborted) {
        onAbort();
      }
    });
  }

  private noteStarted(session: vscode.DebugSession): void {
    const now = Date.now();
    for (let index = this.lateStartCleanups.length - 1; index >= 0; index -= 1) {
      const cleanup = this.lateStartCleanups[index];
      if (cleanup.expiresMs < now) {
        this.lateStartCleanups.splice(index, 1);
        continue;
      }
      // Correlation id only: matching on (name, type, workspace) alone would stop an unrelated
      // same-named session the user started inside the cleanup window.
      if (cleanup.requestId === debugSessionRequestId(session)) {
        this.lateStartCleanups.splice(index, 1);
        this.stopDetached(session);
        return;
      }
    }
    this.sequence += 1;
    this.observedStarts.set(session.id, { session, sequence: this.sequence });
    while (this.observedStarts.size > 256) {
      const oldest = this.observedStarts.keys().next().value as string | undefined;
      if (!oldest) {
        break;
      }
      this.observedStarts.delete(oldest);
    }
    for (const waiter of [...this.startWaiters]) {
      waiter();
    }
  }

  private noteTerminated(session: vscode.DebugSession): void {
    this.observedStarts.delete(session.id);
    const owned = this.owned.get(session.id);
    if (owned) {
      this.owned.delete(session.id);
      this.rememberStopped(owned);
    }
    for (const waiter of this.terminationWaiters.get(session.id) ?? []) {
      waiter();
    }
    this.terminationWaiters.delete(session.id);
  }

  private rememberStopped(owned: OwnedDebugSession): void {
    this.stopped.set(owned.session.id, {
      id: boundedText(owned.session.id, 256),
      name: boundedText(owned.session.name, 512),
      type: boundedText(owned.session.type, 256),
      workspaceKey: owned.ownerKey,
      workspaceFolder: boundedOptionalText(owned.session.workspaceFolder?.name, 256)
    });
    while (this.stopped.size > 256) {
      const oldest = this.stopped.keys().next().value as string | undefined;
      if (!oldest) {
        break;
      }
      this.stopped.delete(oldest);
    }
  }

  private ensureUsable(): void {
    if (this.disposed) {
      throw new HostActionError("host_adapter_disposed", "The VS Code Debug adapter is unavailable.");
    }
  }
}

export function createVsCodeDebugApi(vscodeApi: typeof vscode): VsCodeDebugApi {
  return {
    workspaceFolders: () => vscodeApi.workspace.workspaceFolders ?? [],
    configurations: (folder) =>
      vscodeApi.workspace.getConfiguration("launch", folder.uri).get<unknown>("configurations", []),
    startDebugging: (folder, configuration) => vscodeApi.debug.startDebugging(folder, configuration),
    stopDebugging: (session) => vscodeApi.debug.stopDebugging(session),
    onDidStartDebugSession: (listener) => vscodeApi.debug.onDidStartDebugSession(listener),
    onDidTerminateDebugSession: (listener) => vscodeApi.debug.onDidTerminateDebugSession(listener)
  };
}

function isDebugConfiguration(value: unknown): value is vscode.DebugConfiguration {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return typeof record.name === "string"
    && record.name.length > 0
    && record.name.length <= 512
    && typeof record.type === "string"
    && record.type.length > 0
    && record.type.length <= 256
    && typeof record.request === "string"
    && (record.request === "launch" || record.request === "attach");
}

function configurationIdentity(
  configuration: vscode.DebugConfiguration,
  folder: vscode.WorkspaceFolder,
  context: HostActionExecutionContext
): string {
  const serialized = stableJson({
    workspace: workspaceIdentityKey(context),
    folder: folder.name,
    configuration
  });
  return `debug_${createHash("sha256").update(serialized, "utf8").digest("hex").slice(0, 40)}`;
}

function stableJson(value: unknown): string {
  if (value === null || typeof value === "boolean" || typeof value === "number" || typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(",")}]`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(String(value));
}

function debugSessionRow(session: vscode.DebugSession, state: "running" | "stopped"): Record<string, unknown> {
  return {
    id: boundedText(session.id, 256),
    name: boundedText(session.name, 512),
    type: boundedText(session.type, 256),
    state,
    ...(session.workspaceFolder?.name
      ? { workspace_folder: boundedText(session.workspaceFolder.name, 256) }
      : {})
  };
}

function boundedText(value: unknown, maxLength: number): string {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, maxLength);
}

function boundedOptionalText(value: unknown, maxLength: number): string | undefined {
  const text = boundedText(value, maxLength).trim();
  return text || undefined;
}

function normalizedRoot(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function workspaceUriMatches(uri: vscode.Uri, context: HostActionExecutionContext): boolean {
  return uri.scheme === context.workspace.scheme
    && uri.authority === context.workspace.authority
    && normalizedRoot(uri.fsPath) === normalizedRoot(context.workspace.root);
}

function workspaceIdentityKey(context: HostActionExecutionContext): string {
  return `${context.workspace.scheme}\u0000${context.workspace.authority}\u0000${normalizedRoot(context.workspace.root)}`;
}

function debugOwnerKey(context: HostActionExecutionContext): string {
  return `${context.sessionId}\u0000${context.workspaceFence}\u0000${workspaceIdentityKey(context)}`;
}

function debugSessionRequestId(session: vscode.DebugSession): string | undefined {
  const value = (session.configuration as Record<string, unknown> | undefined)?.[ALYSIS_DEBUG_REQUEST_KEY];
  return typeof value === "string" && value.startsWith(ALYSIS_DEBUG_REQUEST_PREFIX) ? value : undefined;
}

function boundedRows<T extends Record<string, unknown>>(
  field: string,
  candidates: readonly T[],
  maxBytes: number
): T[] {
  const rows: T[] = [];
  for (const candidate of candidates) {
    rows.push(candidate);
    if (Buffer.byteLength(JSON.stringify({ [field]: rows, truncated: true }), "utf8") > maxBytes) {
      rows.pop();
      break;
    }
  }
  return rows;
}
