import * as vscode from "vscode";

import { DiffSummary, ForgePlanResult, ForgePlanTask, ProtocolEventEnvelope } from "../client/AlysisProtocol";

export interface ForgePlanEventSummary {
  type: string;
  label: string;
  description?: string;
  severity: "info" | "warning" | "error";
  sessionId?: string;
  jobId?: string | null;
  sequence?: number;
  rootCauseKey?: string | null;
}

type ForgePlanItemKind =
  | "start"
  | "planning"
  | "plan"
  | "task"
  | "task-detail"
  | "events"
  | "event"
  | "diffs"
  | "diff";

export class ForgePlanTreeItem extends vscode.TreeItem {
  public constructor(
    public readonly kind: ForgePlanItemKind,
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState = vscode.TreeItemCollapsibleState.None,
    public readonly task?: ForgePlanTask,
    public readonly event?: ForgePlanEventSummary,
    public readonly diffId?: string
  ) {
    super(label, collapsibleState);
  }
}

export class ForgePlanViewProvider implements vscode.TreeDataProvider<ForgePlanTreeItem> {
  private readonly changed = new vscode.EventEmitter<ForgePlanTreeItem | undefined | null | void>();
  private plan: ForgePlanResult | undefined;
  private planning:
    | {
        sessionId: string;
        jobId: string;
        instruction: string;
      }
    | undefined;
  private readonly taskOverrides = new Map<string, Partial<ForgePlanTask>>();
  private events: ForgePlanEventSummary[] = [];
  private diffItems: DiffSummary[] = [];
  private readonly lastSequenceBySession = new Map<string, number>();
  public readonly onDidChangeTreeData = this.changed.event;

  public getTreeItem(element: ForgePlanTreeItem): vscode.TreeItem {
    return element;
  }

  public getChildren(element?: ForgePlanTreeItem): ForgePlanTreeItem[] {
    if (!this.plan) {
      if (!element) {
        const roots: ForgePlanTreeItem[] = [];
        if (this.planning) {
          roots.push(
            this.decorate(
              new ForgePlanTreeItem("planning", "Creating plan", vscode.TreeItemCollapsibleState.Expanded),
              {
                description: "In progress",
                tooltip: this.planning.instruction,
                iconPath: new vscode.ThemeIcon("sync~spin")
              }
            )
          );
        }
        if (this.events.length > 0) {
          roots.push(
            this.decorate(
              new ForgePlanTreeItem("events", "Events", vscode.TreeItemCollapsibleState.Collapsed),
              { description: String(this.events.length), iconPath: new vscode.ThemeIcon("pulse") }
            )
          );
        }
        if (!this.planning && this.events.length === 0) {
          roots.push(
            this.decorate(new ForgePlanTreeItem("start", "Create a plan"), {
              description: "Describe the change you want to make",
              tooltip: "Turn an idea into a clear, reviewable Alysis Code plan.",
              iconPath: new vscode.ThemeIcon("checklist"),
              command: {
                command: "alysis.forgePlan",
                title: "Create Forge Plan"
              }
            })
          );
        }
        return roots;
      }
      if (element.kind === "planning" && this.planning) {
        return [
          this.decorate(new ForgePlanTreeItem("task-detail", "Request"), {
            description: this.planning.instruction
          }),
          this.decorate(new ForgePlanTreeItem("task-detail", "Status"), {
            description: "Working"
          })
        ];
      }
      return [];
    }
    const plan = this.plan;
    if (!plan) {
      return [];
    }
    if (!element) {
      const roots = [
        this.decorate(
          new ForgePlanTreeItem("plan", "Current plan", vscode.TreeItemCollapsibleState.Expanded),
          {
            description: `${friendlyStatus(plan.status)} · ${plan.tasks.length} ${plan.tasks.length === 1 ? "task" : "tasks"}${plan.incomplete ? " · incomplete" : ""}`,
            tooltip: [`Plan: ${plan.plan_id}`, plan.summary || plan.project_goal, `Source: ${plan.source}`].filter(Boolean).join("\n"),
            iconPath: new vscode.ThemeIcon("checklist")
          }
        )
      ];
      if (this.diffItems.length > 0) {
        roots.push(
          this.decorate(
            new ForgePlanTreeItem("diffs", "Diffs", vscode.TreeItemCollapsibleState.Collapsed),
            {
              description: String(this.diffItems.length),
              contextValue: "alysisForgeDiffs",
              iconPath: new vscode.ThemeIcon("diff")
            }
          )
        );
      }
      if (this.events.length > 0) {
        roots.push(
          this.decorate(
            new ForgePlanTreeItem("events", "Events", vscode.TreeItemCollapsibleState.Collapsed),
            { description: String(this.events.length), iconPath: new vscode.ThemeIcon("pulse") }
          )
        );
      }
      return roots;
    }
    if (element.kind === "plan") {
      const taskItems = plan.tasks.map((task) => {
        const merged = this.task(task.task_id) ?? task;
        return this.decorate(
          new ForgePlanTreeItem("task", `${merged.task_id} ${merged.title}`, vscode.TreeItemCollapsibleState.Collapsed, merged),
          {
            description: merged.status,
            iconPath: new vscode.ThemeIcon(taskIcon(merged.status)),
            contextValue: "alysisForgeTask",
            command: {
              command: "alysis.forge.showTaskDetails",
              title: "Show Forge Task Details",
              arguments: [merged]
            }
          }
        );
      });
      return [
        this.decorate(new ForgePlanTreeItem("task-detail", "Goal"), {
          description: plan.project_goal
        }),
        this.decorate(new ForgePlanTreeItem("task-detail", "Summary"), {
          description: plan.summary
        }),
        this.decorate(new ForgePlanTreeItem("task-detail", "Source"), {
          description: plan.source,
          tooltip: `Session: ${plan.session_id}`
        }),
        ...(plan.warnings.length > 0
          ? [
              this.decorate(new ForgePlanTreeItem("task-detail", "Warnings"), {
                description: plan.warnings.join("; "),
                iconPath: new vscode.ThemeIcon("warning")
              })
            ]
          : []),
        ...taskItems
      ];
    }
    if (element.kind === "task" && element.task) {
      return taskDetailItems(element.task).map(([label, description]) =>
        this.decorate(new ForgePlanTreeItem("task-detail", label), { description })
      );
    }
    if (element.kind === "events") {
      return this.events.map((event) =>
        this.decorate(new ForgePlanTreeItem("event", event.label, vscode.TreeItemCollapsibleState.None, undefined, event), {
          description: event.description,
          tooltip: event.description,
          iconPath: event.severity === "error" ? new vscode.ThemeIcon("error") : event.severity === "warning" ? new vscode.ThemeIcon("warning") : new vscode.ThemeIcon("pulse")
        })
      );
    }
    if (element.kind === "diffs") {
      return this.diffItems.map((diff) =>
        this.decorate(new ForgePlanTreeItem("diff", diff.file_path, vscode.TreeItemCollapsibleState.None, undefined, undefined, diff.diff_id), {
          description: diffDescription(diff),
          tooltip: `${diff.old_label || "Alysis Code original"} -> ${diff.new_label || "Alysis Code proposed"} | ${diff.file_path}`,
          iconPath: new vscode.ThemeIcon("diff"),
          contextValue: "alysisForgeDiff",
          command: {
            command: "alysis.forge.openDiff",
            title: "Open Forge Diff",
            arguments: [diff.diff_id]
          }
        })
      );
    }
    return [];
  }

  public setPlan(plan: ForgePlanResult): void {
    const planChanged = !this.plan || this.plan.plan_id !== plan.plan_id || this.plan.session_id !== plan.session_id;
    const preservePlanningEvents = this.planning?.sessionId === plan.session_id;
    this.plan = plan;
    this.planning = undefined;
    this.taskOverrides.clear();
    for (const task of plan.tasks) {
      this.taskOverrides.set(task.task_id, task);
    }
    if (planChanged) {
      if (!preservePlanningEvents) {
        this.events = [];
      }
      this.diffItems = [];
    }
    this.changed.fire();
  }

  public setDiffs(diffs: DiffSummary[]): void {
    this.diffItems = diffs;
    this.changed.fire();
  }

  public diff(diffId: string): DiffSummary | undefined {
    return this.diffItems.find((diff) => diff.diff_id === diffId);
  }

  public clear(message = "No Forge plan loaded"): void {
    this.plan = undefined;
    this.planning = undefined;
    this.taskOverrides.clear();
    this.events = [{ type: "info", label: message, severity: "info" }];
    this.diffItems = [];
    this.lastSequenceBySession.clear();
    this.changed.fire();
  }

  public markRuntimeStale(message: string): void {
    this.planning = undefined;
    if (this.plan) {
      this.plan = { ...this.plan, status: "stale" };
    }
    this.diffItems = [];
    const event: ForgePlanEventSummary = {
      type: "warning_emitted",
      label: "Bridge exited",
      description: message,
      severity: "warning",
      sessionId: this.plan?.session_id
    };
    this.events = [event, ...this.events].slice(0, 100);
    this.changed.fire();
  }

  public applyEvent(event: ProtocolEventEnvelope): void {
    const previous = this.lastSequenceBySession.get(event.session_id) ?? 0;
    if (event.sequence <= previous) {
      return;
    }
    this.lastSequenceBySession.set(event.session_id, event.sequence);
    const summary = summarizeEvent(event);
    if (summary) {
      this.events = [summary, ...this.events].slice(0, 100);
    }
    if (event.type === "plan_node_updated") {
      const taskId = stringField(event.payload.node_id);
      if (taskId) {
        const current = this.task(taskId);
        this.taskOverrides.set(taskId, {
          ...(current ?? {
            task_id: taskId,
            title: taskId,
            objective: "",
            file_scope: { estimated_files: [], write_scope: [] },
            acceptance_criteria: [],
            verification_commands: [],
            risk_notes: [],
            dependencies: [],
            order: null,
            scope_unknown_reason: "",
            warnings: [],
            status: ""
          }),
          status: stringField(event.payload.state) || current?.status || "updated",
          title: current?.title || stringField(event.payload.summary) || taskId
        });
      }
    }
    this.changed.fire();
  }

  public addEvent(event: ForgePlanEventSummary): void {
    this.events = [event, ...this.events].slice(0, 100);
    this.changed.fire();
  }

  public setPlanning(sessionId: string, jobId: string, instruction: string): void {
    const event: ForgePlanEventSummary = {
      type: "status_update",
      label: "Forge planning",
      description: "running",
      severity: "info",
      sessionId,
      jobId
    };
    this.plan = undefined;
    this.planning = { sessionId, jobId, instruction };
    this.taskOverrides.clear();
    this.diffItems = [];
    this.events = [event, ...this.events].slice(0, 100);
    this.changed.fire();
  }

  public clearPlanning(): void {
    this.planning = undefined;
    this.changed.fire();
  }

  public state(): {
    plan: ForgePlanResult | null;
    planning: { sessionId: string; jobId: string; instruction: string } | null;
    events: ForgePlanEventSummary[];
    diffs: DiffSummary[];
  } {
    return {
      plan: this.plan ?? null,
      planning: this.planning ?? null,
      events: [...this.events],
      diffs: [...this.diffItems]
    };
  }

  public lastSequence(sessionId: string): number {
    return this.lastSequenceBySession.get(sessionId) ?? 0;
  }

  private task(taskId: string): ForgePlanTask | undefined {
    const override = this.taskOverrides.get(taskId);
    if (!override) {
      return this.plan?.tasks.find((task) => task.task_id === taskId);
    }
    return override as ForgePlanTask;
  }

  private decorate(
    item: ForgePlanTreeItem,
    fields: Partial<Pick<vscode.TreeItem, "description" | "tooltip" | "contextValue" | "iconPath" | "command">>
  ): ForgePlanTreeItem {
    Object.assign(item, fields);
    return item;
  }
}

function taskDetailItems(task: ForgePlanTask): Array<[string, string]> {
  return [
    ["Objective", task.objective],
    ["File Scope", scopeText(task)],
    ["Acceptance", task.acceptance_criteria.length > 0 ? task.acceptance_criteria.join("; ") : "(none)"],
    ["Verification", task.verification_commands.length > 0 ? task.verification_commands.join("; ") : "(none)"],
    ["Dependencies", task.dependencies.length > 0 ? task.dependencies.join(", ") : "(none)"],
    ["Risk", task.risk_notes.length > 0 ? task.risk_notes.join("; ") : "(none)"],
    ["Scope Note", task.scope_unknown_reason || "(none)"],
    ["Warnings", task.warnings.length > 0 ? task.warnings.join("; ") : "(none)"],
    ["Status", task.status]
  ];
}

function taskIcon(status: string): string {
  switch (status.trim().toLowerCase()) {
    case "running":
      return "sync~spin";
    case "completed":
    case "done":
    case "passed":
      return "pass";
    case "failed":
    case "error":
      return "error";
    case "blocked":
    case "cancelled":
      return "circle-slash";
    default:
      return "circle-outline";
  }
}

function scopeText(task: ForgePlanTask): string {
  const estimated = task.file_scope.estimated_files;
  const write = task.file_scope.write_scope;
  const parts = [];
  if (estimated.length > 0) {
    parts.push(`estimated: ${estimated.join(", ")}`);
  }
  if (write.length > 0) {
    parts.push(`write: ${write.join(", ")}`);
  }
  return parts.join(" | ") || "(none)";
}

function diffDescription(diff: DiffSummary): string {
  return `${friendlyStatus(diff.status)} · ${formatBytes(diff.size_bytes)}`;
}

function friendlyStatus(value: string): string {
  const normalized = value.trim().replace(/[_-]+/g, " ");
  return normalized ? normalized[0].toUpperCase() + normalized.slice(1) : "Ready";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  return `${(bytes / 1024).toFixed(bytes < 10_240 ? 1 : 0)} KB`;
}

function summarizeEvent(event: ProtocolEventEnvelope): ForgePlanEventSummary | undefined {
  switch (event.type) {
    case "plan_node_updated":
      return {
        type: event.type,
        label: `${stringField(event.payload.node_id) || "plan"} ${stringField(event.payload.state) || "updated"}`,
        description: stringField(event.payload.summary),
        severity: "info",
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    case "status_update":
      return {
        type: event.type,
        label: "Status update",
        description: [stringField(event.payload.mode), stringField(event.payload.model)].filter(Boolean).join(" | "),
        severity: "info",
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    case "warning_emitted":
      return {
        type: event.type,
        label: "Warning",
        description: stringField(event.payload.message),
        severity: "warning",
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    case "error_raised":
      return {
        type: event.type,
        label: stringField(event.payload.code) || "Error",
        description: stringField(event.payload.message),
        severity: "error",
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    case "verify_gate_result":
      return {
        type: event.type,
        label: `Verify ${event.payload.success === true ? "passed" : "failed"}`,
        description: `${stringField(event.payload.command)} ${stringField(event.payload.summary)}`.trim(),
        severity: event.payload.success === true ? "info" : "error",
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    case "review_gate_decision": {
      const decision = stringField(event.payload.decision);
      return {
        type: event.type,
        label: `Review ${decision || "decision"}`,
        description: stringField(event.payload.summary),
        severity: reviewDecisionSeverity(decision),
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    }
    case "swarm_worker_state_changed":
      return {
        type: event.type,
        label: `${stringField(event.payload.worker_id) || "worker"} ${stringField(event.payload.state) || "updated"}`,
        description: stringField(event.payload.role),
        severity: "info",
        sessionId: event.session_id,
        jobId: event.job_id,
        sequence: event.sequence
      };
    default:
      return undefined;
  }
}

function stringField(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function reviewDecisionSeverity(decision: string): "info" | "warning" | "error" {
  const normalized = decision.trim().toLowerCase();
  if (["blocked", "failed", "rejected", "denied"].includes(normalized)) {
    return "error";
  }
  if (["needs_review", "pending"].includes(normalized)) {
    return "warning";
  }
  return "info";
}
