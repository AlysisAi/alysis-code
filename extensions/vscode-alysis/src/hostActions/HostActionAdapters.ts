import type { HostActionName } from "../client/AlysisProtocol";

export interface HostWorkspaceIdentity {
  root: string;
  scheme: string;
  authority: string;
  name: string;
}

export interface HostActionExecutionContext {
  sessionId: string;
  workspaceFence: string;
  workspace: HostWorkspaceIdentity;
  signal: AbortSignal;
  deadlineMs: number;
  maxResultBytes: number;
}

export interface HostActionAdapter {
  readonly actions: readonly HostActionName[];
  execute(
    action: HostActionName,
    args: Record<string, unknown>,
    context: HostActionExecutionContext
  ): Promise<Record<string, unknown>>;
  invalidateSession?(sessionId: string, workspaceFence: string): void;
  invalidateAll?(): void;
  dispose(): void;
}

export class HostActionError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
    public readonly retryable = false
  ) {
    super(message);
    this.name = "HostActionError";
  }
}

export function throwIfAborted(signal: AbortSignal): void {
  if (signal.aborted) {
    throw new HostActionError("host_action_cancelled", "The host action was cancelled.");
  }
}

export function requireNoExtraArguments(
  args: Record<string, unknown>,
  allowed: readonly string[]
): void {
  const allowedSet = new Set(allowed);
  if (Object.keys(args).some((key) => !allowedSet.has(key))) {
    throw new HostActionError("invalid_arguments", "The host action arguments are invalid.");
  }
}

export function optionalBoundedString(
  args: Record<string, unknown>,
  field: string,
  maxLength = 256
): string | undefined {
  const value = args[field];
  if (value === undefined) {
    return undefined;
  }
  if (typeof value !== "string" || value.length < 1 || value.length > maxLength) {
    throw new HostActionError("invalid_arguments", `Host action field ${field} is invalid.`);
  }
  return value;
}

export function requiredBoundedString(
  args: Record<string, unknown>,
  field: string,
  maxLength = 256
): string {
  const value = optionalBoundedString(args, field, maxLength);
  if (!value) {
    throw new HostActionError("invalid_arguments", `Host action field ${field} is required.`);
  }
  return value;
}
