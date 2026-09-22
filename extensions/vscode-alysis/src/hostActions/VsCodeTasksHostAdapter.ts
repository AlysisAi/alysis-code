import { createHash, randomUUID } from "node:crypto";
import path from "node:path";

import * as vscode from "vscode";

import type { HostActionName } from "../client/AlysisProtocol";
import { redactForDisplay } from "../client/CliDiscovery";
import { redactContextSecrets } from "../context/secretRedaction";
import {
  HostActionAdapter,
  HostActionError,
  HostActionExecutionContext,
  HostWorkspaceIdentity,
  optionalBoundedString,
  requireNoExtraArguments,
  requiredBoundedString,
  throwIfAborted
} from "./HostActionAdapters";

const MAX_TASKS = 100;
const MAX_DIAGNOSTIC_ITEMS = 50;
const MAX_DIAGNOSTIC_DELTA_BYTES = 32_000;

export interface VsCodeTasksApi {
  fetchTasks(filter?: vscode.TaskFilter): Thenable<vscode.Task[]>;
  executeTask(task: vscode.Task): Thenable<vscode.TaskExecution>;
  readonly taskExecutions: readonly vscode.TaskExecution[];
  onDidEndTask(listener: (event: { execution: vscode.TaskExecution }) => unknown): vscode.Disposable;
  onDidEndTaskProcess(listener: (event: vscode.TaskProcessEndEvent) => unknown): vscode.Disposable;
  diagnostics(): Array<[vscode.Uri, readonly vscode.Diagnostic[]]>;
  workspaceFolder(uri: vscode.Uri): vscode.WorkspaceFolder | undefined;
  workspaceFolderCount(): number;
}

interface ExecutionState {
  ended: boolean;
  exitCode?: number;
}

interface OwnedExecution {
  execution: vscode.TaskExecution;
  ownerKey: string;
  taskId: string;
  diagnosticsBefore: Map<string, DiagnosticEntry>;
  workspace: HostWorkspaceIdentity;
}

interface TerminalExecution {
  ownerKey: string;
  taskId: string;
  state: "completed" | "failed" | "terminated";
  exitCode?: number;
  diagnosticsDelta?: Record<string, unknown>;
}

interface DiagnosticEntry {
  identity: string;
  fingerprint: string;
  item: Record<string, unknown>;
}

export class VsCodeTasksHostAdapter implements HostActionAdapter {
  public readonly actions = ["tasks.list", "tasks.run", "tasks.terminate", "tasks.status"] as const;

  private readonly subscriptions: vscode.Disposable[];
  private readonly stateByExecution = new WeakMap<vscode.TaskExecution, ExecutionState>();
  private readonly waiters = new WeakMap<vscode.TaskExecution, Set<() => void>>();
  private readonly ownedExecutions = new Map<string, OwnedExecution>();
  private readonly terminalExecutions = new Map<string, TerminalExecution>();
  private disposed = false;

  public constructor(private readonly api: VsCodeTasksApi) {
    this.subscriptions = [
      api.onDidEndTask((event) => this.noteEnded(event.execution)),
      api.onDidEndTaskProcess((event) => this.noteEnded(event.execution, event.exitCode))
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
      case "tasks.list":
        return this.list(args, context);
      case "tasks.run":
        return this.run(args, context);
      case "tasks.terminate":
        return this.terminate(args, context);
      case "tasks.status":
        return this.status(args, context);
      default:
        throw new HostActionError("unsupported_action", "This Tasks host action is not supported.");
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
    this.terminalExecutions.clear();
  }

  public invalidateSession(sessionId: string, workspaceFence: string): void {
    const ownerPrefix = `${sessionId}\u0000${workspaceFence}\u0000`;
    for (const [executionId, owned] of this.ownedExecutions) {
      if (!owned.ownerKey.startsWith(ownerPrefix)) {
        continue;
      }
      owned.execution.terminate();
      this.ownedExecutions.delete(executionId);
      this.rememberTerminal(executionId, {
        ownerKey: owned.ownerKey,
        taskId: owned.taskId,
        state: "terminated",
        diagnosticsDelta: diagnosticDelta(
          owned.diagnosticsBefore,
          this.captureDiagnostics({ workspace: owned.workspace })
        )
      });
    }
  }

  public invalidateAll(): void {
    for (const [executionId, owned] of this.ownedExecutions) {
      owned.execution.terminate();
      this.rememberTerminal(executionId, {
        ownerKey: owned.ownerKey,
        taskId: owned.taskId,
        state: "terminated",
        diagnosticsDelta: diagnosticDelta(
          owned.diagnosticsBefore,
          this.captureDiagnostics({ workspace: owned.workspace })
        )
      });
    }
    this.ownedExecutions.clear();
  }

  private async list(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>> {
    requireNoExtraArguments(args, ["task_type"]);
    const taskType = optionalBoundedString(args, "task_type", 128);
    const tasks = await this.scopedTasks(context, taskType);
    throwIfAborted(context.signal);
    const candidates = tasks.slice(0, MAX_TASKS).map(({ task, id }) => ({
      id,
      label: boundedText(task.name, 512),
      ...(boundedOptionalText(task.definition?.type, 128) ? { type: boundedOptionalText(task.definition.type, 128) } : {}),
      ...(boundedOptionalText(task.source, 256) ? { source: boundedOptionalText(task.source, 256) } : {}),
      ...(boundedOptionalText(task.detail, 512) ? { detail: boundedOptionalText(task.detail, 512) } : {})
    }));
    const rows = boundedRows("tasks", candidates, context.maxResultBytes);
    return { tasks: rows, truncated: tasks.length > rows.length };
  }

  private async run(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>> {
    requireNoExtraArguments(args, ["task_id"]);
    const taskId = requiredBoundedString(args, "task_id");
    const tasks = await this.scopedTasks(context);
    const selected = tasks.find((candidate) => candidate.id === taskId);
    if (!selected) {
      throw new HostActionError("task_not_found", "The selected VS Code task is unavailable.");
    }
    throwIfAborted(context.signal);
    const before = this.captureDiagnostics(context);
    const execution = await this.api.executeTask(selected.task);
    throwIfAbortedOrTerminate(context.signal, execution);
    const executionId = `te_${randomUUID()}`;
    const ownerKey = taskOwnerKey(context);
    this.ownedExecutions.set(executionId, {
      execution,
      ownerKey,
      taskId,
      diagnosticsBefore: before,
      workspace: { ...context.workspace }
    });
    const terminal = await this.waitForCompletion(execution, context);
    const after = this.captureDiagnostics(context);
    const diagnosticsDelta = diagnosticDelta(before, after);
    if (terminal.ended) {
      this.ownedExecutions.delete(executionId);
      this.rememberTerminal(executionId, {
        ownerKey,
        taskId,
        state: terminal.exitCode === undefined || terminal.exitCode === 0 ? "completed" : "failed",
        ...(terminal.exitCode !== undefined ? { exitCode: terminal.exitCode } : {}),
        diagnosticsDelta
      });
    }
    return {
      execution_id: executionId,
      task_id: taskId,
      state: terminal.ended
        ? terminal.exitCode === undefined || terminal.exitCode === 0 ? "completed" : "failed"
        : "started",
      ...(terminal.exitCode !== undefined ? { exit_code: terminal.exitCode } : {}),
      diagnostics_delta: diagnosticsDelta
    };
  }

  private terminate(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Record<string, unknown> {
    requireNoExtraArguments(args, ["execution_id"]);
    const executionId = requiredBoundedString(args, "execution_id");
    const ownerKey = taskOwnerKey(context);
    const owned = this.ownedExecutions.get(executionId);
    if (owned && owned.ownerKey === ownerKey) {
      owned.execution.terminate();
      this.ownedExecutions.delete(executionId);
      this.rememberTerminal(executionId, {
        ownerKey,
        taskId: owned.taskId,
        state: "terminated",
        diagnosticsDelta: diagnosticDelta(
          owned.diagnosticsBefore,
          this.captureDiagnostics({ workspace: owned.workspace })
        )
      });
      return { execution_id: executionId, terminated: true, state: "terminated" };
    }
    if (this.terminalExecutions.get(executionId)?.ownerKey === ownerKey) {
      return { execution_id: executionId, terminated: false, state: "already_ended" };
    }
    return { execution_id: executionId, terminated: false, state: "not_found" };
  }

  private status(
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Record<string, unknown> {
    requireNoExtraArguments(args, ["execution_id"]);
    const requestedId = optionalBoundedString(args, "execution_id");
    const ownerKey = taskOwnerKey(context);
    const rows: Record<string, unknown>[] = [];
    for (const [executionId, owned] of this.ownedExecutions) {
      if (owned.ownerKey !== ownerKey || (requestedId && executionId !== requestedId)) {
        continue;
      }
      rows.push({ execution_id: executionId, task_id: owned.taskId, state: "running" });
    }
    for (const [executionId, terminal] of this.terminalExecutions) {
      if (terminal.ownerKey !== ownerKey || (requestedId && executionId !== requestedId)) {
        continue;
      }
      rows.push({
        execution_id: executionId,
        task_id: terminal.taskId,
        state: terminal.state,
        ...(terminal.exitCode !== undefined ? { exit_code: terminal.exitCode } : {}),
        ...(terminal.diagnosticsDelta ? { diagnostics_delta: terminal.diagnosticsDelta } : {})
      });
    }
    const bounded = boundedRows("executions", rows.slice(0, MAX_TASKS), context.maxResultBytes);
    return { executions: bounded, truncated: rows.length > bounded.length };
  }

  private async scopedTasks(
    context: HostActionExecutionContext,
    taskType?: string
  ): Promise<Array<{ task: vscode.Task; id: string }>> {
    const fetched = await this.api.fetchTasks(taskType ? { type: taskType } : undefined);
    throwIfAborted(context.signal);
    const seen = new Set<string>();
    const tasks: Array<{ task: vscode.Task; id: string }> = [];
    for (const task of fetched) {
      if (!this.taskBelongsToWorkspace(task, context)) {
        continue;
      }
      const id = taskIdentity(task, context);
      if (seen.has(id)) {
        continue;
      }
      seen.add(id);
      tasks.push({ task, id });
    }
    return tasks;
  }

  private taskBelongsToWorkspace(task: vscode.Task, context: HostActionExecutionContext): boolean {
    const scope = task.scope;
    if (typeof scope === "object" && scope !== null && "uri" in scope) {
      const folder = scope as vscode.WorkspaceFolder;
      return workspaceUriMatches(folder.uri, context);
    }
    return scope === vscode.TaskScope.Workspace && this.api.workspaceFolderCount() === 1;
  }

  private captureDiagnostics(
    context: Pick<HostActionExecutionContext, "workspace">
  ): Map<string, DiagnosticEntry> {
    const entries = new Map<string, DiagnosticEntry>();
    for (const [uri, diagnostics] of this.api.diagnostics()) {
      if (!uriIsWithinWorkspace(uri, context)) {
        continue;
      }
      const folder = this.api.workspaceFolder(uri);
      if (!folder || !workspaceUriMatches(folder.uri, context)) {
        continue;
      }
      for (const diagnostic of diagnostics) {
        const identity = diagnosticIdentity(uri, diagnostic);
        const message = boundedText(redactHostText(diagnostic.message), 1_024);
        entries.set(identity, {
          identity,
          fingerprint: createHash("sha256").update(`${identity}\u0000${message}`, "utf8").digest("hex"),
          item: {
            uri: boundedText(redactHostText(uri.toString(true)), 1_024),
            line: diagnostic.range.start.line + 1,
            severity: diagnosticSeverity(diagnostic.severity),
            message,
            ...(boundedOptionalText(redactHostText(diagnostic.source ?? ""), 128)
              ? { source: boundedOptionalText(redactHostText(diagnostic.source ?? ""), 128) }
              : {})
          }
        });
      }
    }
    return entries;
  }

  private waitForCompletion(
    execution: vscode.TaskExecution,
    context: HostActionExecutionContext
  ): Promise<ExecutionState> {
    const current = this.stateByExecution.get(execution);
    if (current?.ended) {
      return Promise.resolve({ ...current });
    }
    const waitMs = Math.max(0, context.deadlineMs - Date.now() - 100);
    if (waitMs === 0) {
      return Promise.resolve({ ended: false });
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      let listeners = this.waiters.get(execution);
      if (!listeners) {
        listeners = new Set();
        this.waiters.set(execution, listeners);
      }
      const finish = (value?: ExecutionState, error?: Error): void => {
        if (settled) {
          return;
        }
        settled = true;
        clearTimeout(timeout);
        context.signal.removeEventListener("abort", onAbort);
        listeners?.delete(onEnd);
        if (error) {
          reject(error);
        } else {
          resolve(value ?? { ended: false });
        }
      };
      const onEnd = (): void => finish({ ...(this.stateByExecution.get(execution) ?? { ended: true }) });
      const onAbort = (): void => {
        execution.terminate();
        finish(undefined, new HostActionError("host_action_cancelled", "The VS Code task action was cancelled."));
      };
      const timeout = setTimeout(() => finish({ ended: false }), waitMs);
      listeners.add(onEnd);
      context.signal.addEventListener("abort", onAbort, { once: true });
      const raced = this.stateByExecution.get(execution);
      if (raced?.ended) {
        onEnd();
      } else if (context.signal.aborted) {
        onAbort();
      }
    });
  }

  private noteEnded(execution: vscode.TaskExecution, exitCode?: number): void {
    const previous = this.stateByExecution.get(execution);
    this.stateByExecution.set(execution, {
      ended: true,
      exitCode: exitCode ?? previous?.exitCode
    });
    for (const waiter of this.waiters.get(execution) ?? []) {
      waiter();
    }
    this.waiters.delete(execution);
    for (const [executionId, owned] of this.ownedExecutions) {
      if (owned.execution !== execution) {
        continue;
      }
      this.ownedExecutions.delete(executionId);
      const state = this.stateByExecution.get(execution);
      this.rememberTerminal(executionId, {
        ownerKey: owned.ownerKey,
        taskId: owned.taskId,
        state: state?.exitCode === undefined || state.exitCode === 0 ? "completed" : "failed",
        ...(state?.exitCode !== undefined ? { exitCode: state.exitCode } : {}),
        diagnosticsDelta: diagnosticDelta(
          owned.diagnosticsBefore,
          this.captureDiagnostics({ workspace: owned.workspace })
        )
      });
      break;
    }
  }

  private rememberTerminal(executionId: string, terminal: TerminalExecution): void {
    this.terminalExecutions.set(executionId, terminal);
    while (this.terminalExecutions.size > 256) {
      const oldest = this.terminalExecutions.keys().next().value as string | undefined;
      if (!oldest) {
        break;
      }
      this.terminalExecutions.delete(oldest);
    }
  }

  private ensureUsable(): void {
    if (this.disposed) {
      throw new HostActionError("host_adapter_disposed", "The VS Code Tasks adapter is unavailable.");
    }
  }
}

export function createVsCodeTasksApi(vscodeApi: typeof vscode): VsCodeTasksApi {
  return {
    fetchTasks: (filter) => vscodeApi.tasks.fetchTasks(filter),
    executeTask: (task) => vscodeApi.tasks.executeTask(task),
    get taskExecutions() { return vscodeApi.tasks.taskExecutions; },
    onDidEndTask: (listener) => vscodeApi.tasks.onDidEndTask(listener),
    onDidEndTaskProcess: (listener) => vscodeApi.tasks.onDidEndTaskProcess(listener),
    diagnostics: () => [...vscodeApi.languages.getDiagnostics()],
    workspaceFolder: (uri) => vscodeApi.workspace.getWorkspaceFolder(uri),
    workspaceFolderCount: () => vscodeApi.workspace.workspaceFolders?.length ?? 0
  };
}

function taskIdentity(task: vscode.Task, context: HostActionExecutionContext): string {
  const identity = stableJson({
    workspace: workspaceIdentityKey(context),
    definition: task.definition,
    name: task.name,
    source: task.source
  });
  return `task_${createHash("sha256").update(identity, "utf8").digest("hex").slice(0, 40)}`;
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

function diagnosticIdentity(uri: vscode.Uri, diagnostic: vscode.Diagnostic): string {
  const code = typeof diagnostic.code === "object" && diagnostic.code !== null
    ? String(diagnostic.code.value)
    : String(diagnostic.code ?? "");
  return [
    uri.toString(true),
    diagnostic.range.start.line,
    diagnostic.range.start.character,
    diagnostic.range.end.line,
    diagnostic.range.end.character,
    diagnostic.severity,
    diagnostic.source ?? "",
    code
  ].join("\u0000");
}

function diagnosticDelta(
  before: Map<string, DiagnosticEntry>,
  after: Map<string, DiagnosticEntry>
): Record<string, unknown> {
  let added = 0;
  let removed = 0;
  let changed = 0;
  const candidates: Record<string, unknown>[] = [];
  const items: Record<string, unknown>[] = [];
  for (const [identity, entry] of after) {
    const previous = before.get(identity);
    if (!previous) {
      added += 1;
      candidates.push(entry.item);
    } else if (previous.fingerprint !== entry.fingerprint) {
      changed += 1;
      candidates.push(entry.item);
    }
  }
  for (const identity of before.keys()) {
    if (!after.has(identity)) {
      removed += 1;
    }
  }
  let itemBytes = 2;
  for (const item of candidates) {
    if (items.length >= MAX_DIAGNOSTIC_ITEMS) {
      break;
    }
    const bytes = Buffer.byteLength(JSON.stringify(item), "utf8") + (items.length > 0 ? 1 : 0);
    if (itemBytes + bytes > MAX_DIAGNOSTIC_DELTA_BYTES) {
      break;
    }
    items.push(item);
    itemBytes += bytes;
  }
  return {
    added,
    removed,
    changed,
    total: after.size,
    truncated: candidates.length > items.length,
    items
  };
}

function diagnosticSeverity(value: vscode.DiagnosticSeverity): string {
  switch (value) {
    case vscode.DiagnosticSeverity.Error: return "error";
    case vscode.DiagnosticSeverity.Warning: return "warning";
    case vscode.DiagnosticSeverity.Information: return "information";
    default: return "hint";
  }
}

function boundedText(value: unknown, maxLength: number): string {
  return String(value ?? "").replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, maxLength);
}

function redactHostText(value: string): string {
  return redactForDisplay(redactContextSecrets(value));
}

function boundedOptionalText(value: unknown, maxLength: number): string | undefined {
  const text = boundedText(value, maxLength).trim();
  return text || undefined;
}

function normalizedRoot(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function workspaceUriMatches(
  uri: vscode.Uri,
  context: Pick<HostActionExecutionContext, "workspace">
): boolean {
  return uri.scheme === context.workspace.scheme
    && uri.authority === context.workspace.authority
    && normalizedRoot(uri.fsPath) === normalizedRoot(context.workspace.root);
}

function uriIsWithinWorkspace(
  uri: vscode.Uri,
  context: Pick<HostActionExecutionContext, "workspace">
): boolean {
  if (uri.scheme !== context.workspace.scheme || uri.authority !== context.workspace.authority) {
    return false;
  }
  const relative = path.relative(normalizedRoot(context.workspace.root), normalizedRoot(uri.fsPath));
  return relative === "" || (!relative.startsWith("..") && !path.isAbsolute(relative));
}

function workspaceIdentityKey(context: HostActionExecutionContext): string {
  return `${context.workspace.scheme}\u0000${context.workspace.authority}\u0000${normalizedRoot(context.workspace.root)}`;
}

function taskOwnerKey(context: HostActionExecutionContext): string {
  return `${context.sessionId}\u0000${context.workspaceFence}\u0000${workspaceIdentityKey(context)}`;
}

function throwIfAbortedOrTerminate(signal: AbortSignal, execution: vscode.TaskExecution): void {
  if (!signal.aborted) {
    return;
  }
  execution.terminate();
  throw new HostActionError("host_action_cancelled", "The VS Code task action was cancelled.");
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
