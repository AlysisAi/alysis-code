import * as vscode from "vscode";

import { SessionSummary } from "../client/AlysisProtocol";
import { redactContextSecrets } from "../context/secretRedaction";

export type SessionTreeState = "active" | "retained" | "closed" | "failed" | "historical";

export class SessionTreeItem extends vscode.TreeItem {
  public readonly canMutateLiveSession: boolean;
  public readonly canResumeIntoLiveSession: boolean;

  public constructor(
    public readonly sessionId: string,
    public readonly sessionState: SessionTreeState,
    description?: string
  ) {
    super(sessionId, vscode.TreeItemCollapsibleState.None);
    this.description = description;
    this.canMutateLiveSession = sessionState === "active";
    this.canResumeIntoLiveSession = sessionState !== "active";
  }
}

/** Persists the human titles of sessions (their first prompt) across window reloads. */
export interface SessionTitleStore {
  get(): Record<string, string> | undefined;
  update(titles: Record<string, string>): PromiseLike<void> | void;
  getSessions?(): unknown;
  updateSessions?(sessions: SessionSummary[]): PromiseLike<void> | void;
}

const MAX_SESSION_TITLE_CHARS = 120;
const MAX_REMEMBERED_TITLES = 200;

export class SessionsViewProvider implements vscode.TreeDataProvider<SessionTreeItem> {
  private readonly changed = new vscode.EventEmitter<SessionTreeItem | undefined | null | void>();
  private sessions: SessionSummary[] = [];
  // The live chat session id (from ChatController). Its tree row gets the active context value so
  // per-item menus can distinguish live-session mutations from retained-session read/resume actions.
  private activeSessionId: string | undefined;
  // Session id -> the first thing the user asked. The bridge's session summaries carry no title, and
  // "Task 3f2a9c1b…" tells nobody what a task was about.
  private readonly titles = new Map<string, string>();
  public readonly onDidChangeTreeData = this.changed.event;

  public constructor(private readonly titleStore?: SessionTitleStore) {
    const stored = titleStore?.get();
    if (stored && typeof stored === "object") {
      let sanitized = false;
      for (const [sessionId, title] of Object.entries(stored).slice(-MAX_REMEMBERED_TITLES)) {
        if (typeof sessionId === "string" && typeof title === "string" && title.trim()) {
          const safeTitle = sessionTitleFromPrompt(title);
          this.titles.set(sessionId, safeTitle);
          sanitized ||= safeTitle !== title;
        }
      }
      if (sanitized) this.persistTitles();
    }
    const retained = titleStore?.getSessions?.();
    if (Array.isArray(retained)) {
      this.sessions = retained.slice(-MAX_REMEMBERED_TITLES)
        .map(retainedSessionSummary).filter((session): session is SessionSummary => Boolean(session));
    }
  }

  /** Remember the first prompt of a session as its title. Later prompts never overwrite it. */
  public rememberTitle(sessionId: string, prompt: string): void {
    const title = sessionTitleFromPrompt(prompt);
    if (!sessionId || !title || this.titles.has(sessionId)) {
      return;
    }
    this.titles.set(sessionId, title);
    while (this.titles.size > MAX_REMEMBERED_TITLES) {
      const oldest = this.titles.keys().next().value as string | undefined;
      if (!oldest) {
        break;
      }
      this.titles.delete(oldest);
    }
    this.persistTitles();
    this.changed.fire();
  }

  private persistTitles(): void {
    try {
      void Promise.resolve(this.titleStore?.update(Object.fromEntries(this.titles))).catch(() => undefined);
    } catch {
      // Task submission must still work when VS Code's optional title storage is unavailable.
    }
  }

  public titleFor(sessionId: string): string | undefined {
    return this.titles.get(sessionId);
  }

  public getTreeItem(element: SessionTreeItem): vscode.TreeItem {
    return element;
  }

  public getChildren(): SessionTreeItem[] {
    if (this.sessions.length > 0) {
      return this.sessions.map((session) => {
        const isActive =
          !session.closed && this.activeSessionId !== undefined && session.session_id === this.activeSessionId;
        const sessionState = sessionTreeState(session, isActive);
        const job = session.active_job ?? session.last_job;
        const displayState = job?.status || sessionState;
        const description = `${modeLabel(session.mode)} · ${sessionStateLabel(sessionState, displayState)}`;
        const item = new SessionTreeItem(session.session_id, sessionState, description);
        const title = this.titles.get(session.session_id);
        item.label = title ?? (isActive ? "Current task" : `Task ${shortId(session.session_id)}`);
        item.tooltip = [
          `Session: ${session.session_id}`,
          `Workspace: ${session.workspace_root}`,
          `Mode: ${session.mode}`,
          `State: ${sessionState}`,
          job ? `Job: ${job.job_id} (${job.status})` : `State Detail: ${displayState}`
        ].join("\n");
        item.iconPath = new vscode.ThemeIcon(iconForSession(sessionState, job?.status));
        item.contextValue = contextValueForSessionState(sessionState);
        return item;
      });
    }
    // Return no children when idle so the contributed viewsWelcome (Open Alysis Code / Set up /
    // Locate CLI buttons) is shown instead of placeholder rows.
    return [];
  }

  public setSessions(sessions: SessionSummary[]): void {
    if (this.titleStore?.updateSessions) {
      // The bridge lists only sessions in its current process. Keep the bounded, workspace-local
      // index across process restarts, without retaining stale jobs or arbitrary protocol payloads.
      const currentIds = new Set(sessions.map((session) => session.session_id));
      this.sessions = [...sessions, ...this.sessions
        .filter((session) => !currentIds.has(session.session_id))
        .map(retainedSessionSummary).filter((session): session is SessionSummary => Boolean(session))]
        .slice(0, MAX_REMEMBERED_TITLES);
      this.persistSessions();
    } else {
      this.sessions = sessions;
    }
    this.changed.fire();
  }

  public rememberSession(session: SessionSummary): void {
    if (this.sessions.some((known) => known.session_id === session.session_id)) return;
    const retained = retainedSessionSummary(session);
    if (!retained) return;
    this.sessions = [retained, ...this.sessions].slice(0, MAX_REMEMBERED_TITLES);
    this.persistSessions();
    this.changed.fire();
  }

  private persistSessions(): void {
    try {
      const retained = this.sessions.map(retainedSessionSummary)
        .filter((session): session is SessionSummary => Boolean(session));
      void Promise.resolve(this.titleStore?.updateSessions?.(retained)).catch(() => undefined);
    } catch {
      // An unavailable optional history index must not prevent a chat from starting.
    }
  }

  public setActiveSession(sessionId: string | undefined): void {
    this.activeSessionId = sessionId;
    this.changed.fire();
  }

  public snapshot(): { sessions: SessionSummary[]; activeSessionId: string | undefined; titles: Record<string, string> } {
    return {
      sessions: [...this.sessions],
      activeSessionId: this.activeSessionId,
      titles: Object.fromEntries(this.titles)
    };
  }
}

function retainedSessionSummary(value: unknown): SessionSummary | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const row = value as Record<string, unknown>;
  if (typeof row.session_id !== "string" || !/^[A-Za-z0-9_-]{1,200}$/.test(row.session_id)
      || typeof row.workspace_root !== "string" || !row.workspace_root.trim()
      || row.workspace_root.length > 4096
      || !["readonly", "review", "auto"].includes(String(row.mode))) return undefined;
  return {
    session_id: row.session_id, workspace_root: row.workspace_root,
    mode: row.mode as SessionSummary["mode"], closed: true
  };
}

/** First line of the prompt, without a leading slash command's noise, clipped for a list row. */
export function sessionTitleFromPrompt(prompt: string): string {
  const firstLine = redactContextSecrets(String(prompt || ""))
    .split(/\r?\n/)
    .map((line) => line.trim())
    .find((line) => line.length > 0) ?? "";
  const cleaned = firstLine.replace(/\s+/g, " ").trim();
  if (!cleaned) {
    return "";
  }
  return cleaned.length > MAX_SESSION_TITLE_CHARS ? `${cleaned.slice(0, MAX_SESSION_TITLE_CHARS - 1)}…` : cleaned;
}

function modeLabel(mode: string): string {
  switch (mode) {
    case "readonly":
      return "Read-only";
    case "auto":
      return "Auto";
    default:
      return "Review";
  }
}

function sessionStateLabel(state: SessionTreeState, detail: string): string {
  const normalized = detail.trim().toLowerCase();
  if (["running", "starting", "queued"].includes(normalized)) {
    return "Working";
  }
  if (["completed", "done", "passed"].includes(normalized)) {
    return "Completed";
  }
  switch (state) {
    case "active":
      return "Active";
    case "closed":
      return "Closed";
    case "failed":
      return "Needs attention";
    case "historical":
      return "Previous run";
    case "retained":
      return "Ready to resume";
  }
}

function shortId(value: string): string {
  return value.length <= 12 ? value : `${value.slice(0, 8)}…`;
}

function sessionTreeState(session: SessionSummary, isActive: boolean): SessionTreeState {
  if (isActive) {
    return "active";
  }
  if (session.closed) {
    return "closed";
  }
  const job = session.active_job ?? session.last_job;
  if (job?.status === "failed") {
    return "failed";
  }
  if (job) {
    return "historical";
  }
  return "retained";
}

function contextValueForSessionState(state: SessionTreeState): string {
  switch (state) {
    case "active":
      return "alysisSessionActive";
    case "closed":
      return "alysisSessionClosed";
    case "failed":
      return "alysisSessionFailed";
    case "historical":
      return "alysisSessionHistorical";
    case "retained":
      return "alysisSessionRetained";
  }
}

function iconForSession(state: SessionTreeState, jobStatus?: string): string {
  if (jobStatus === "running") {
    return "sync~spin";
  }
  switch (state) {
    case "active":
      return "debug-console";
    case "closed":
      return "circle-slash";
    case "failed":
      return "error";
    case "historical":
      return "history";
    case "retained":
      return "archive";
  }
}
