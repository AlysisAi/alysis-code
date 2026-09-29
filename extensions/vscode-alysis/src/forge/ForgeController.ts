import { randomUUID } from "node:crypto";

import * as vscode from "vscode";
import { activeWorkspaceRoot, workspaceScopeRequiredMessage } from "../workspace/activeWorkspace";
import {
  assertNoDirtyWorkspaceDocuments,
  isDirtyWorkspaceDocumentsError
} from "../workspace/DirtyWorkspaceGuard";

import { AlysisConfig, redactForDisplay } from "../client/CliDiscovery";
import { BridgeProfileStartResult, ProtocolClientError } from "../client/AlysisBridgeClient";
import {
  ArtifactReadResult,
  BridgeHealth,
  DiffGetResult,
  DiffSummary,
  ForgeAssetDetail,
  ForgeAssetEntry,
  ForgeAssetsListParams,
  ForgeAssetsListResult,
  ForgeAssetsShowParams,
  ForgeAssetsShowResult,
  ForgeExecutePreviewParams,
  ForgeExecutePreviewResult,
  ForgeListParams,
  ForgeListResult,
  ForgeOpenParams,
  ForgePlanParams,
  ForgePlanResult,
  ForgePlanStartResult,
  ForgePlanSummary,
  ForgePlanTask,
  ForgeJobProgress,
  ForgeReviewParams,
  ForgeReviewResult,
  ForgeSwarmApplyResult,
  ForgeSwarmDiscardResult,
  ForgeSwarmDurableStatus,
  ForgeSwarmJobResult,
  ForgeSwarmListResult,
  ForgeSwarmResumeParams,
  ForgeSwarmResumeResult,
  ForgeSwarmReviewResult,
  ForgeSwarmStartParams,
  ForgeSwarmStartResult,
  JobStatusResult,
  ProtocolEventEnvelope,
  AlysisMode
} from "../client/AlysisProtocol";
import {
  BridgeCompatibilitySnapshot,
  CompatibilityFeatureId,
  FeatureCompatibilityError,
  assertFeatureCompatible,
  cliHealthStatusForCompatibilityError,
  evaluateBridgeCompatibility
} from "../client/compatibility";
import {
  ForgeAssetDetailView,
  ForgeAssetSummary,
  ForgeCockpitApproval,
  ForgeCockpitState,
  ForgeReviewSummary,
  SwarmCockpitState,
  SwarmRecoveryJobView,
  SwarmTaskView,
  emptySwarmCockpitState
} from "../chat/CockpitState";
import { CockpitRuntimeState } from "../chat/CockpitRuntimeState";
import { AlysisSecretStore } from "../secrets/secretStorage";
import { evaluateProcessExecutionCommand } from "../security/commandGuards";
import { evaluateWorkspaceTrust } from "../security/workspaceTrust";
import { AlysisStatusBar } from "../status/statusBar";
import { ArtifactsViewProvider } from "../views/artifactsView";
import { ForgePlanViewProvider } from "../views/forgePlanView";
import { SessionsViewProvider } from "../views/sessionsView";
import { ForgeDiffContentProvider } from "./ForgeDiffContentProvider";

export interface ForgeBridgeLike {
  on(eventName: string | symbol, listener: (...args: any[]) => void): unknown;
  off?(eventName: string | symbol, listener: (...args: any[]) => void): unknown;
  health?(config: AlysisConfig): Promise<BridgeHealth>;
  ensureStarted(config: AlysisConfig, options?: { apiKey?: string; stripApiKey?: boolean }): Promise<void>;
  createSession?(params: {
    workspace: string;
    mode?: AlysisMode;
    session_id?: string;
    model?: string;
    base_url?: string;
  }): Promise<{ session_id: string; workspace_root: string; mode: AlysisMode }>;
  ensureStartedForProfile?(
    config: AlysisConfig,
    options?: { apiKey?: string; stripApiKey?: boolean },
    requiredProfile?: { credentialsRequired?: boolean }
  ): Promise<BridgeProfileStartResult | void>;
  supportsMethod?(method: string): boolean;
  supportsFeature?(path: readonly string[]): boolean;
  forgePlan(params: ForgePlanParams): Promise<ForgePlanResult>;
  forgePlanStart?(params: ForgePlanParams): Promise<ForgePlanStartResult>;
  forgePlanResult?(jobId: string): Promise<ForgePlanResult>;
  forgeList(params: ForgeListParams): Promise<ForgeListResult>;
  forgeOpen(params: ForgeOpenParams): Promise<ForgePlanResult>;
  forgeStatus(sessionId: string, planId: string): Promise<ForgePlanResult>;
  forgeExecute(params: {
    session_id: string;
    plan_id: string;
    task_ids?: string[];
    mode?: "review" | "auto";
    dry_run?: boolean;
    workspace_trusted?: boolean;
    sandbox_profile?: string;
    max_steps?: number;
    no_log?: boolean;
  }): Promise<{ session_id: string; plan_id: string; job_id: string | null; status: string }>;
  forgeExecutePreview(params: ForgeExecutePreviewParams): Promise<ForgeExecutePreviewResult>;
  // FE-8: real bridge methods (capability-gated; optional so older bridges still type-check).
  forgeReview?(params: ForgeReviewParams): Promise<ForgeReviewResult>;
  forgeAssetsList?(params: ForgeAssetsListParams): Promise<ForgeAssetsListResult>;
  forgeAssetsShow?(params: ForgeAssetsShowParams): Promise<ForgeAssetsShowResult>;
  forgeCancel?(params: { session_id: string; plan_id: string; reason?: string }): Promise<Record<string, unknown>>;
  cancelSession?(sessionId: string): Promise<Record<string, unknown>>;
  respondApproval?(params: {
    session_id: string;
    approval_id: string;
    allow: boolean;
    allow_for_session: boolean;
  }): Promise<{
    allow: boolean;
    allow_for_session: boolean;
    allow_for_session_warning: string | null;
  }>;
  diffList(sessionId: string, planId?: string): Promise<{
    diffs: Array<{
      diff_id: string;
      session_id: string;
      plan_id: string;
      job_id: string | null;
      file_path: string;
      status: string;
      old_label: string;
      new_label: string;
      size_bytes: number;
    }>;
  }>;
  // SW6: Forge swarm jobs + per-task review (optional; capability-gated).
  forgeSwarmStart?(params: ForgeSwarmStartParams): Promise<ForgeSwarmStartResult>;
  forgeSwarmResume?(params: ForgeSwarmResumeParams): Promise<ForgeSwarmResumeResult>;
  forgeSwarmList?(params: { session_id: string; limit?: number }): Promise<ForgeSwarmListResult>;
  forgeSwarmResult?(jobId: string): Promise<ForgeSwarmJobResult>;
  forgeSwarmCancel?(sessionId: string, jobId: string, reason?: string): Promise<Record<string, unknown>>;
  forgeSwarmReview?(sessionId: string, planId: string): Promise<ForgeSwarmReviewResult>;
  forgeSwarmApply?(sessionId: string, planId: string, taskIds: string[], options?: { base_branch?: string }): Promise<ForgeSwarmApplyResult>;
  forgeSwarmDiscard?(sessionId: string, planId: string, taskIds: string[]): Promise<ForgeSwarmDiscardResult>;
  forgePlanRegenerateStart?(params: { session_id: string; plan_id: string; workspace_trusted: boolean; instruction?: string; focus?: string }): Promise<{ job_id: string }>;
  diffGet(diffId: string, options: { sessionId: string; planId: string; maxBytes?: number }): Promise<DiffGetResult>;
  artifactList(sessionId: string): Promise<{
    session_id: string;
    artifacts: Array<{ artifact_id: string; root: string; path: string; size_bytes: number }>;
    truncated: boolean;
  }>;
  artifactRead(sessionId: string, artifactId: string): Promise<ArtifactReadResult>;
  sessionList(): Promise<{ sessions: any[] }>;
  getEvents(sessionId: string, afterSequence?: number, maxEvents?: number): Promise<{
    events: ProtocolEventEnvelope[];
    truncated: boolean;
  }>;
  jobStatus(jobId: string): Promise<JobStatusResult>;
}

export interface ForgeContextPersistence {
  get(): unknown;
  update(value: { sessionId: string; planId: string }): PromiseLike<void> | void;
}

export class ForgeController implements vscode.Disposable {
  private sessionId: string | undefined;
  private planId: string | undefined;
  private recoverablePlanId: string | undefined;
  private recoverableSessionId: string | undefined;
  private planRecoveryPromise: Promise<boolean> | undefined;
  private planStartPromise: Promise<boolean> | undefined;
  private planIntentGeneration = 0;
  private planStartCancellationRequested = false;
  private activeJobId: string | undefined;
  private readonly ownedSessionIds = new Set<string>();
  private readonly ownedJobIds = new Set<string>();
  private readonly pendingApprovals = new Map<
    string,
    { sessionId: string; approvalId: string; jobId: string | null; prompt: ForgeApprovalPrompt }
  >();
  private readonly pendingApprovalResponses = new Set<string>();
  private pendingPlanEvents: ProtocolEventEnvelope[] | undefined;
  private lastExecutePreview: ForgeExecutePreviewResult | null = null;
  private selectedTaskIds: string[] = [];
  private lastReview: ForgeReviewResult | null = null;
  private reviewBusy = false;
  private executeBusy = false;
  private assets: ForgeAssetEntry[] = [];
  private assetDetail: ForgeAssetDetail | null = null;
  private assetsBusy = false;
  private readonly unsupportedCancellationJobIds = new Set<string>();
  private readonly cancellationRequestedJobIds = new Set<string>();
  // SW6 swarm console run state.
  private swarmJobId: string | undefined;
  private swarmStatus = "idle";
  private swarmRunStatus: string | null = null;
  private swarmParallel = 2;
  private swarmBusy = false;
  private swarmReview: ForgeSwarmReviewResult | null = null;
  private swarmStartPromise: Promise<void> | undefined;
  private swarmIntentGeneration = 0;
  private swarmStartCancellationRequested = false;
  private readonly swarmCancellationRequestedJobIds = new Set<string>();
  private swarmCancellationPromise: Promise<void> | undefined;
  private swarmRecoveryStatus: "idle" | "loading" | "ready" | "resuming" | "error" = "idle";
  private swarmRecoveryReason: string | null = null;
  private swarmRecoveryActiveJobId: string | null = null;
  private swarmRecoveryJobs: ForgeSwarmDurableStatus[] = [];
  private swarmRecoveryGeneration = 0;
  private readonly dismissedSwarmRecoveryRevisions = new Map<string, number>();
  // Live render states keyed by task id, driven by swarm_worker_state_changed events.
  private readonly swarmTaskRenderStates = new Map<string, string>();
  private pendingForgeFailureRootCauseKey: string | undefined;
  private cockpitCallbacks:
    | {
        reveal?: () => Promise<void>;
        refresh?: () => void;
      }
    | undefined;
  private disposed = false;
  private shutdownPromise: Promise<void> | undefined;
  // Forge owns half of the "a run is in flight" signal that gates alysis.cancelCurrentRun. The
  // host has no other way to observe a Forge state transition, so publish an explicit event.
  private readonly activityChanged = new vscode.EventEmitter<boolean>();
  private lastPublishedActivity = false;
  public readonly onDidChangeActivity = this.activityChanged.event;
  private readonly disposables: vscode.Disposable[] = [];
  private readonly onBridgeEvent = (event: ProtocolEventEnvelope): void => {
    if (!this.disposed) {
      this.handleBridgeEvent(event);
    }
  };
  private readonly onBridgeExit = (code: number | null): void => {
    if (!this.disposed) {
      this.handleBridgeDisconnect(
        `Alysis Code IDE bridge exited with code ${code ?? "unknown"}. The persisted Forge plan can be recovered after reconnecting.`,
        "Bridge exited",
        "error"
      );
    }
  };
  private readonly onBridgeReset = (reason: string): void => {
    if (!this.disposed) {
      this.handleBridgeDisconnect(
        `Alysis Code IDE bridge is restarting (${reason}). The persisted Forge plan can be recovered after reconnecting.`,
        "Bridge restarting",
        "warning"
      );
    }
  };
  private readonly onBridgeError = (error: Error): void => {
    if (this.disposed) {
      return;
    }
    const message = redactForDisplay(error.message);
    this.runtime?.setBridgeProcess("error", message);
    this.runtime?.recordError("bridge", "Bridge error", message);
    this.forgeView.addEvent({
      type: "error_raised",
      label: "bridge_error",
      description: message,
      severity: "error",
      sessionId: this.sessionId
    });
  };

  public constructor(
    private readonly bridge: ForgeBridgeLike,
    private readonly getConfig: () => AlysisConfig,
    private readonly secrets: AlysisSecretStore,
    private readonly statusBar: AlysisStatusBar,
    private readonly sessionsView: SessionsViewProvider,
    private readonly forgeView: ForgePlanViewProvider,
    private readonly artifactsView: ArtifactsViewProvider,
    private readonly output: vscode.OutputChannel,
    private readonly diffContentProvider: ForgeDiffContentProvider,
    private readonly runtime?: CockpitRuntimeState,
    private readonly contextPersistence?: ForgeContextPersistence
  ) {
    const persisted = persistedForgeContext(contextPersistence?.get());
    this.recoverableSessionId = persisted?.sessionId;
    this.recoverablePlanId = persisted?.planId;
    this.bridge.on("event", this.onBridgeEvent);
    this.bridge.on("exit", this.onBridgeExit);
    this.bridge.on("reset", this.onBridgeReset);
    this.bridge.on("error", this.onBridgeError);
    // Restored `alysis-diff` editors re-request content with an empty cache after a window reload.
    diffContentProvider.setResolver((uri) => this.restoreDiffDocument(uri));
    this.disposables.push(
      { dispose: () => diffContentProvider.setResolver(undefined) },
      vscode.workspace.registerTextDocumentContentProvider("alysis-diff", diffContentProvider),
      vscode.commands.registerCommand("alysis.forge.showTaskDetails", (task: ForgePlanTask) =>
        this.showTaskDetails(task)
      ),
      vscode.commands.registerCommand("alysis.forgeListPlans", () => this.listPlans()),
      vscode.commands.registerCommand("alysis.forgeOpenPlan", () => this.openPlan()),
      vscode.commands.registerCommand("alysis.forge.openDiff", (diffId?: string) =>
        this.openDiff(diffId)
      ),
      vscode.commands.registerCommand("alysis.artifact.open", (sessionId: string, artifactId: string) =>
        this.openArtifact(sessionId, artifactId)
      )
    );
  }

  private handleBridgeDisconnect(
    message: string,
    title: "Bridge exited" | "Bridge restarting",
    severity: "error" | "warning"
  ): void {
    this.planIntentGeneration += 1;
    this.swarmIntentGeneration += 1;
    if (this.planId) {
      this.recoverablePlanId = this.planId;
    }
    if (this.sessionId) {
      this.recoverableSessionId = this.sessionId;
    }
    this.sessionId = undefined;
    this.planId = undefined;
    this.activeJobId = undefined;
    this.ownedSessionIds.clear();
    this.ownedJobIds.clear();
    this.unsupportedCancellationJobIds.clear();
    this.cancellationRequestedJobIds.clear();
    this.pendingApprovals.clear();
    this.pendingApprovalResponses.clear();
    this.lastExecutePreview = null;
    this.selectedTaskIds = [];
    this.pendingPlanEvents = undefined;
    this.planStartCancellationRequested = false;
    this.swarmJobId = undefined;
    this.swarmStatus = "idle";
    this.swarmRunStatus = null;
    this.swarmBusy = false;
    this.swarmStartCancellationRequested = false;
    this.swarmCancellationRequestedJobIds.clear();
    this.swarmCancellationPromise = undefined;
    this.swarmReview = null;
    this.swarmRecoveryGeneration += 1;
    this.swarmRecoveryStatus = "idle";
    this.swarmRecoveryReason = "Reconnect and reopen the Forge plan to check interrupted runs.";
    this.swarmRecoveryActiveJobId = null;
    this.swarmRecoveryJobs = [];
    this.swarmTaskRenderStates.clear();
    this.runtime?.setBridgeProcess("stopped", message);
    this.runtime?.setBridgeProtocol("unknown", null);
    this.runtime?.recordEvent({
      severity,
      source: "bridge",
      title,
      message
    });
    this.forgeView.markRuntimeStale(message);
    this.artifactsView.clear();
    this.refreshCockpit();
  }

  public async plan(): Promise<void> {
    const instruction = await vscode.window.showInputBox({
      title: "Create a plan",
      prompt: "Describe the outcome you want. Alysis Code will prepare a reviewable plan before changing files.",
      ignoreFocusOut: true,
      validateInput: (value) => (value.trim().length === 0 ? "Describe the change you want to plan." : undefined)
    });
    if (instruction === undefined) {
      return;
    }
    await this.planWithInstruction(instruction);
  }

  public setCockpitCallbacks(callbacks: { reveal?: () => Promise<void>; refresh?: () => void }): void {
    this.cockpitCallbacks = callbacks;
  }

  public cockpitState(): ForgeCockpitState {
    const view = this.forgeView.state();
    return {
      sessionId: this.sessionId ?? null,
      planId: this.planId ?? null,
      activeJobId: this.activeJobId ?? null,
      plan: view.plan,
      planning: view.planning,
      events: view.events,
      diffs: view.diffs,
      artifacts: this.artifactsView.state(),
      executePreview: this.lastExecutePreview,
      selectedTaskIds: [...this.selectedTaskIds],
      approvals: Array.from(this.pendingApprovals.values()).map((pending): ForgeCockpitApproval => ({
        approvalId: pending.approvalId,
        sessionId: pending.sessionId,
        jobId: pending.jobId,
        kind: pending.prompt.kind,
        reason: pending.prompt.reason,
        preview: pending.prompt.preview,
        command: pending.prompt.command,
        files: [...pending.prompt.files],
        allowForSessionSupported: pending.prompt.allowForSessionSupported,
        allowForSessionScope: pending.prompt.allowForSessionScope ? { ...pending.prompt.allowForSessionScope } : null,
        warning: null,
        swarmTaskId: pending.prompt.swarmTaskId,
        swarmWorker: pending.prompt.swarmWorker
      })),
      review: this.lastReview ? reviewSummary(this.lastReview) : null,
      reviewBusy: this.reviewBusy,
      assets: this.assets.map(assetSummary),
      assetDetail: this.assetDetail ? assetDetailView(this.assetDetail) : null,
      assetsBusy: this.assetsBusy
    };
  }

  // ---- SW6: Forge swarm console ---------------------------------------------

  // Project the swarm run for the cockpit. `compatibility` comes from the live
  // runtime snapshot so the Run Swarm control is method-gated (dimmed + lock
  // reason when the bridge lacks swarm), never a working control on an old CLI.
  public swarmCockpitState(compatibility?: BridgeCompatibilitySnapshot): SwarmCockpitState {
    const feature = compatibility?.features?.forgeSwarm;
    const supported =
      (feature ? feature.supported : false) &&
      typeof this.bridge.forgeSwarmStart === "function";
    const reason = supported ? null : feature?.reason?.message ?? feature?.disabledReason ?? null;
    const recoverySupported = Boolean(
      (feature ? feature.supported : true)
      && this.bridge.forgeSwarmList
      && this.bridge.forgeSwarmResume
    );
    const tasks = this.swarmReview
      ? this.swarmReview.items.map((item): SwarmTaskView => {
          const live = this.swarmTaskRenderStates.get(item.task_id);
          return {
            taskId: item.task_id,
            title: item.title,
            status: item.status,
            state: live ?? item.state,
            writeScope: [],
            reviewable: item.reviewable,
            diffAvailable: item.diff_available,
            diffArtifactId: item.diff_artifact_id,
            untrackedFilesNotApplied: [...item.untracked_files],
            untrackedNote: item.untracked_files_note,
            applied: item.applied,
            discarded: item.discarded,
            recovery: recoveryOffer(item.recovery)
          };
        })
      : this.swarmTasksFromRenderStates();
    return {
      ...emptySwarmCockpitState(),
      supported,
      reason,
      sessionId: this.sessionId ?? null,
      planId: this.planId ?? null,
      jobId: this.swarmJobId ?? null,
      status: this.swarmStatus,
      runStatus: this.swarmRunStatus,
      parallel: this.swarmParallel,
      busy: this.swarmBusy,
      cancellable: this.swarmStatus === "running" || this.swarmStatus === "starting" || this.swarmStatus === "cancelling",
      tasks,
      pendingReviewTaskIds: this.swarmReview ? [...this.swarmReview.pending_review_task_ids] : [],
      workingTreeUntouchedUntilApply: true,
      recovery: {
        supported: recoverySupported,
        status: this.swarmRecoveryStatus,
        reason: recoverySupported
          ? this.swarmRecoveryReason
          : feature?.reason?.message ?? feature?.disabledReason ?? "This CLI cannot list and resume interrupted Forge swarms.",
        activeJobId: this.swarmRecoveryActiveJobId,
        jobs: this.visibleSwarmRecoveryJobs()
      }
    };
  }

  private visibleSwarmRecoveryJobs(): SwarmRecoveryJobView[] {
    return this.swarmRecoveryJobs
      .filter((job) => (
        job.resumable
        && (job.state === "interrupted" || job.state === "failed")
        && this.dismissedSwarmRecoveryRevisions.get(job.job_id) !== job.revision
      ))
      .map((job) => ({
        jobId: job.job_id,
        state: job.state as "interrupted" | "failed",
        revision: job.revision,
        attempts: job.attempts,
        resumeCount: job.resume_count,
        createdAt: job.created_at,
        updatedAt: job.updated_at,
        errorCode: job.error_code,
        errorSummary: job.error_summary ? redactForDisplay(job.error_summary).slice(0, 600) : null,
        calls: job.usage.calls,
        totalTokens: job.usage.total_tokens
      }));
  }

  /** Refresh the bounded, public recovery projection for the currently open Forge plan. */
  public async refreshSwarmRecovery(): Promise<void> {
    if (this.swarmRecoveryStatus === "resuming") {
      return;
    }
    const generation = ++this.swarmRecoveryGeneration;
    const config = this.getConfig();
    if (!config.enableForge) {
      this.setSwarmRecoveryUnavailable("Forge is disabled in Alysis Code settings.");
      return;
    }
    if (!vscode.workspace.isTrusted) {
      this.setSwarmRecoveryUnavailable("Trust this folder before checking or resuming interrupted runs.");
      return;
    }
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      this.setSwarmRecoveryUnavailable("Create or open a Forge plan to check its interrupted runs.");
      return;
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      this.setSwarmRecoveryUnavailable("Create or open a Forge plan to check its interrupted runs.");
      return;
    }
    if (!this.bridge.forgeSwarmList || !this.bridge.forgeSwarmResume) {
      this.setSwarmRecoveryUnavailable("Upgrade the Alysis Code CLI to list and resume interrupted Forge swarms.");
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      this.setSwarmRecoveryUnavailable(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    this.swarmRecoveryStatus = "loading";
    this.swarmRecoveryReason = null;
    this.refreshCockpit();
    try {
      await this.preflightFeature(config, "forgeSwarm", "Forge swarm recovery");
      await this.ensureBridgeWithoutSecrets(config);
      const result = await this.bridge.forgeSwarmList({ session_id: sessionId, limit: 50 });
      if (
        generation !== this.swarmRecoveryGeneration
        || this.disposed
        || this.sessionId !== sessionId
        || this.planId !== planId
      ) {
        return;
      }
      this.swarmRecoveryJobs = result.jobs.slice(0, 50);
      this.swarmRecoveryStatus = "ready";
      this.swarmRecoveryReason = null;
    } catch (error) {
      if (generation !== this.swarmRecoveryGeneration || this.disposed) {
        return;
      }
      this.swarmRecoveryJobs = [];
      this.swarmRecoveryStatus = "error";
      this.swarmRecoveryReason = boundedRecoveryError(error);
      this.output.appendLine(`forge swarm recovery list failed: ${protocolErrorMessage(error)}`);
    } finally {
      if (generation === this.swarmRecoveryGeneration) {
        this.refreshCockpit();
      }
    }
  }

  private setSwarmRecoveryUnavailable(reason: string): void {
    this.swarmRecoveryGeneration += 1;
    this.swarmRecoveryJobs = [];
    this.swarmRecoveryStatus = "idle";
    this.swarmRecoveryReason = redactForDisplay(reason).slice(0, 600);
    this.swarmRecoveryActiveJobId = null;
    this.refreshCockpit();
  }

  public dismissSwarmRecovery(jobId: string, revision: number): void {
    if (this.swarmRecoveryStatus === "resuming") {
      return;
    }
    const clean = jobId.trim();
    const visible = this.swarmRecoveryJobs.some((job) => (
      job.job_id === clean && job.revision === revision && job.resumable
    ));
    if (!visible) {
      return;
    }
    this.dismissedSwarmRecoveryRevisions.set(clean, revision);
    this.refreshCockpit();
  }

  public async resumeSwarm(jobId: string, revision: number): Promise<void> {
    if (this.swarmRecoveryStatus === "resuming" || this.swarmRecoveryActiveJobId) {
      void vscode.window.showInformationMessage("A Forge swarm recovery is already awaiting confirmation or starting.");
      return;
    }
    const clean = jobId.trim();
    const listed = this.swarmRecoveryJobs.find((job) => (
      job.job_id === clean && job.revision === revision && job.resumable
    ));
    if (!listed) {
      void vscode.window.showInformationMessage("That interrupted swarm changed or is no longer available. Refresh recovery jobs and try again.");
      return;
    }
    if (this.hasActiveJob()) {
      void vscode.window.showInformationMessage("Wait for the active Alysis Code operation to finish or stop it before resuming this swarm.");
      return;
    }
    const config = this.getConfig();
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      void vscode.window.showInformationMessage("Reopen the Forge plan before resuming its swarm.");
      return;
    }
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
    if (!trust.allowed) {
      void vscode.window.showWarningMessage(trust.reason ?? "Workspace Trust is required to resume a Forge swarm.");
      return;
    }
    if (!this.bridge.forgeSwarmList || !this.bridge.forgeSwarmResume || !this.bridge.forgeSwarmResult) {
      void vscode.window.showInformationMessage("This Alysis Code CLI cannot resume interrupted Forge swarms. Upgrade the CLI and reconnect.");
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    const mutationWorkspace = workspaceRoot();
    if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "resuming this Forge swarm")) {
      return;
    }

    const generation = ++this.swarmRecoveryGeneration;
    this.swarmRecoveryStatus = "resuming";
    this.swarmRecoveryReason = null;
    this.swarmRecoveryActiveJobId = clean;
    this.refreshCockpit();
    let started = false;
    try {
      await this.preflightFeature(config, "forgeSwarm", "Forge swarm recovery");
      await this.ensureBridgeWithoutSecrets(config);
      const fresh = await this.bridge.forgeSwarmList({ session_id: sessionId, limit: 50 });
      const current = fresh.jobs.find((job) => job.job_id === clean);
      if (!current || current.revision !== revision || !current.resumable) {
        throw new Error("The interrupted swarm changed after it was shown. Refresh the recovery list before resuming.");
      }
      if (
        generation !== this.swarmRecoveryGeneration
        || this.disposed
        || this.sessionId !== sessionId
        || this.planId !== planId
      ) {
        return;
      }
      const confirmation = await vscode.window.showWarningMessage(
        `Resume interrupted Forge swarm ${shortJobId(clean)}?`,
        {
          modal: true,
          detail: "Alysis Code will continue only unfinished work. Previous permission grants are not reused; dangerous actions will ask again. Existing task worktrees remain isolated until you explicitly apply changes."
        },
        "Resume swarm"
      );
      if (confirmation !== "Resume swarm") {
        return;
      }
      if (
        generation !== this.swarmRecoveryGeneration
        || this.disposed
        || this.sessionId !== sessionId
        || this.planId !== planId
      ) {
        return;
      }
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "resuming this Forge swarm")) {
        return;
      }
      const resumed = await this.bridge.forgeSwarmResume({
        session_id: sessionId,
        plan_id: planId,
        job_id: clean,
        workspace_trusted: true,
        approval_scope_grants: [freshSwarmRecoveryGrant(sessionId, planId, clean, revision)],
        expected_revision: revision,
        parallel: this.swarmParallel
      });
      if (this.sessionId !== sessionId || this.planId !== planId || this.disposed) {
        await this.cancelSupersededSwarm(sessionId, resumed.job_id);
        return;
      }
      started = true;
      this.swarmJobId = resumed.job_id;
      this.swarmStatus = "running";
      this.swarmRunStatus = null;
      this.swarmBusy = false;
      this.swarmRecoveryJobs = this.swarmRecoveryJobs.filter((job) => job.job_id !== resumed.job_id);
      this.swarmRecoveryStatus = "ready";
      this.swarmRecoveryActiveJobId = null;
      this.ownSession(sessionId);
      this.ownJob(resumed.job_id);
      this.statusBar.setActiveRun(resumed.job_id);
      this.recordRuntime("info", "Forge swarm resumed", `job=${shortJobId(resumed.job_id)}; prior grants were not reused`);
      this.refreshCockpit();
      void this.pollSwarmJob(resumed.job_id);
    } catch (error) {
      if (generation === this.swarmRecoveryGeneration && !this.disposed) {
        this.swarmRecoveryStatus = "error";
        this.swarmRecoveryReason = boundedRecoveryError(error);
        this.showError(`Forge swarm could not resume: ${protocolErrorMessage(error)}`);
      }
    } finally {
      if (!started && generation === this.swarmRecoveryGeneration) {
        this.swarmRecoveryActiveJobId = null;
        if (this.swarmRecoveryStatus === "resuming") {
          this.swarmRecoveryStatus = "ready";
        }
        this.refreshCockpit();
      }
    }
  }

  // Before any review payload exists (mid-run) the task grid is driven purely by
  // swarm_worker_state_changed render states.
  private swarmTasksFromRenderStates(): SwarmTaskView[] {
    const tasks: SwarmTaskView[] = [];
    for (const [taskId, state] of this.swarmTaskRenderStates.entries()) {
      tasks.push({
        taskId,
        title: taskId,
        status: state,
        state,
        writeScope: [],
        reviewable: false,
        diffAvailable: false,
        diffArtifactId: null,
        untrackedFilesNotApplied: [],
        untrackedNote: null,
        applied: false,
        discarded: false,
        recovery: null
      });
    }
    return tasks.sort((a, b) => a.taskId.localeCompare(b.taskId));
  }

  public async runSwarm(parallel?: number): Promise<void> {
    if (this.disposed) {
      return;
    }
    if (this.planStartPromise || this.activeJobId || this.executeBusy || this.reviewBusy) {
      void vscode.window.showInformationMessage("Wait for the active Forge operation to finish or stop it before starting a swarm.");
      return;
    }
    if (this.swarmStartPromise || this.swarmJobId || this.swarmStatus === "running") {
      void vscode.window.showInformationMessage("A Forge swarm is already starting or running.");
      return;
    }
    const generation = ++this.swarmIntentGeneration;
    this.swarmStartCancellationRequested = false;
    this.swarmBusy = true;
    this.swarmStatus = "starting";
    this.swarmReview = null;
    this.swarmTaskRenderStates.clear();
    this.refreshCockpit();
    const startPromise = this.withForgeProgress(
      "Alysis Code is starting a Forge swarm",
      () => this.runSwarmOnce(parallel, generation)
    );
    this.swarmStartPromise = startPromise;
    try {
      await startPromise;
    } finally {
      if (this.swarmStartPromise === startPromise) {
        this.swarmStartPromise = undefined;
      }
      if (!this.swarmJobId && this.swarmStatus === "starting") {
        this.swarmStatus = "idle";
      }
      this.swarmBusy = false;
      this.refreshCockpit();
    }
  }

  private async runSwarmOnce(parallel: number | undefined, generation: number): Promise<void> {
    const config = this.getConfig();
    if (!config.enableForge) {
      void vscode.window.showInformationMessage("Alysis Code Forge commands are disabled in settings.");
      return;
    }
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      void vscode.window.showInformationMessage("Create or open a Forge plan before running a swarm.");
      return;
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      return;
    }
    if (!this.bridge.forgeSwarmStart || !this.bridge.forgeSwarmResult) {
      void vscode.window.showInformationMessage("This Alysis Code CLI does not support forge.swarm. Upgrade the CLI.");
      return;
    }
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
    if (!trust.allowed) {
      void vscode.window.showWarningMessage(trust.reason ?? "Workspace Trust is required to run a Forge swarm.");
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    const mutationWorkspace = workspaceRoot();
    if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "starting this Forge swarm")) {
      return;
    }
    if (typeof parallel === "number" && Number.isInteger(parallel)) {
      this.swarmParallel = Math.min(8, Math.max(1, parallel));
    }
    try {
      await this.preflightFeature(config, "forgeSwarm", "Forge swarm");
      this.assertSwarmIntentCurrent(generation, sessionId, planId);
      await this.ensureBridgeWithoutSecrets(config);
      this.assertSwarmIntentCurrent(generation, sessionId, planId);
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "starting this Forge swarm")) {
        return;
      }
      const started = await this.bridge.forgeSwarmStart({
        session_id: sessionId,
        plan_id: planId,
        workspace_trusted: vscode.workspace.isTrusted,
        parallel: this.swarmParallel,
        approval_scope_grants: []
      });
      if (!this.isSwarmIntentCurrent(generation, sessionId, planId)) {
        await this.cancelSupersededSwarm(sessionId, started.job_id);
        if (this.swarmStartCancellationRequested) {
          this.swarmStatus = "cancelled";
        }
        return;
      }
      const jobId = started.job_id;
      this.swarmJobId = jobId;
      this.swarmStatus = "running";
      this.swarmRunStatus = null;
      this.ownSession(sessionId);
      this.ownJob(jobId);
      this.statusBar.setActiveRun(jobId);
      this.recordRuntime("info", "Forge swarm started", `parallel=${this.swarmParallel}`);
      this.refreshCockpit();
      void this.pollSwarmJob(jobId);
    } catch (error) {
      if (isForgeOperationSupersededError(error)) {
        if (this.swarmStartCancellationRequested) {
          this.swarmStatus = "cancelled";
        }
        return;
      }
      if (generation === this.swarmIntentGeneration) {
        this.swarmStatus = "idle";
      }
      this.showError(`Forge swarm failed to start: ${protocolErrorMessage(error)}`);
    }
  }

  private async pollSwarmJob(jobId: string): Promise<void> {
    while (!this.disposed && this.swarmJobId === jobId) {
      await sleep(1500);
      if (this.disposed || this.swarmJobId !== jobId) {
        return;
      }
      try {
        const result = await this.bridge.forgeSwarmResult!(jobId);
        if (this.disposed || this.swarmJobId !== jobId) {
          return;
        }
        if (isSwarmProgress(result)) {
          this.refreshCockpit();
          continue;
        }
        // Terminal: a finished run is a data outcome (failure dominates the run
        // status, but the per-task review still lists pending items).
        this.swarmStatus = "review_pending";
        this.swarmRunStatus = typeof result.run_status === "string" ? result.run_status : null;
        if (result.interrupted) {
          this.swarmStatus = "interrupted";
        } else if (this.swarmRunStatus && this.swarmRunStatus !== "review_pending" && this.swarmRunStatus !== "clean") {
          this.swarmStatus = this.swarmRunStatus; // failed / incomplete
        }
        this.clearActiveJob(jobId);
        this.swarmCancellationRequestedJobIds.delete(jobId);
        this.recordRuntime("info", "Forge swarm finished", `status=${this.swarmRunStatus ?? "unknown"}`);
        await this.refreshSwarmReview();
        await this.refreshSwarmRecovery();
        this.refreshCockpit();
        return;
      } catch (error) {
        if (this.disposed || this.swarmJobId !== jobId) {
          return;
        }
        this.output.appendLine(`forge swarm status failed: ${protocolErrorMessage(error)}`);
        this.swarmStatus = "failed";
        this.refreshCockpit();
        return;
      } finally {
        if (this.swarmJobId === jobId && this.swarmStatus !== "running") {
          this.swarmJobId = undefined;
          this.swarmCancellationRequestedJobIds.delete(jobId);
        }
      }
    }
  }

  public async cancelSwarm(): Promise<void> {
    if (this.swarmStartPromise && !this.swarmJobId) {
      if (this.swarmStartCancellationRequested) {
        void vscode.window.showInformationMessage("Swarm cancellation is already requested while startup finishes.");
        return;
      }
      this.swarmStartCancellationRequested = true;
      this.swarmIntentGeneration += 1;
      this.swarmStatus = "cancelling";
      this.recordRuntime(
        "warning",
        "Swarm cancellation requested",
        "Startup will stop before launch, or the backend job will be cancelled as soon as its identifier is returned."
      );
      this.refreshCockpit();
      return;
    }
    const jobId = this.swarmJobId;
    if (!jobId || !this.sessionId || !this.bridge.forgeSwarmCancel) {
      void vscode.window.showInformationMessage("No active swarm run to cancel.");
      return;
    }
    if (this.swarmCancellationRequestedJobIds.has(jobId) || this.swarmCancellationPromise) {
      void vscode.window.showInformationMessage("Swarm cancellation is already requested.");
      return;
    }
    this.swarmCancellationRequestedJobIds.add(jobId);
    const cancellation = this.cancelSwarmJob(this.sessionId, jobId);
    this.swarmCancellationPromise = cancellation;
    try {
      await cancellation;
    } finally {
      if (this.swarmCancellationPromise === cancellation) {
        this.swarmCancellationPromise = undefined;
      }
    }
  }

  private async cancelSwarmJob(sessionId: string, jobId: string): Promise<void> {
    try {
      // Cooperative cancel: running workers stop at their next checkpoint,
      // queued tasks never start, and worktrees are preserved for review.
      // It is not a hard kill.
      await this.bridge.forgeSwarmCancel!(sessionId, jobId, "cancelled_by_user");
      this.recordRuntime(
        "warning",
        "Swarm cancellation requested",
        "Cooperative cancel: workers stop at their next checkpoint; queued tasks won't start; worktrees are preserved."
      );
      this.refreshCockpit();
    } catch (error) {
      this.swarmCancellationRequestedJobIds.delete(jobId);
      this.showError(`Swarm cancel failed: ${protocolErrorMessage(error)}`);
    }
  }

  public async refreshSwarmReview(): Promise<void> {
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId || !this.bridge.forgeSwarmReview) {
      return;
    }
    try {
      const review = await this.bridge.forgeSwarmReview(sessionId, planId);
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.swarmReview = review;
      this.refreshCockpit();
    } catch (error) {
      this.output.appendLine(`forge swarm review failed: ${protocolErrorMessage(error)}`);
    }
  }

  public async applySwarmTask(taskId: string): Promise<void> {
    const clean = (taskId ?? "").trim();
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!clean || !sessionId || !planId || !this.bridge.forgeSwarmApply) {
      return;
    }
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
    if (!trust.allowed) {
      void vscode.window.showWarningMessage(trust.reason ?? "Workspace Trust is required to apply a swarm task.");
      return;
    }
    const mutationWorkspace = workspaceRoot();
    try {
      // Apply writes the task's diff to the working tree only — it never commits.
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "applying this Forge swarm task")) {
        return;
      }
      const confirmed = await vscode.window.showWarningMessage(
        applySwarmTaskConfirmation(clean, this.swarmReviewItem(clean)),
        { modal: true },
        "Apply to working tree"
      );
      if (confirmed !== "Apply to working tree") {
        return;
      }
      // The plan, the selection, and the working tree can all move while the modal is open, so
      // re-check the same boundaries the modal was shown for before anything is written.
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      const confirmedTrust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
      if (!confirmedTrust.allowed) {
        void vscode.window.showWarningMessage(
          confirmedTrust.reason ?? "Workspace Trust is required to apply a swarm task."
        );
        return;
      }
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "applying this Forge swarm task")) {
        return;
      }
      const result = await this.bridge.forgeSwarmApply(sessionId, planId, [clean]);
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      const applied = result.applied.find((entry) => entry.task_id === clean) as
        | Record<string, unknown>
        | undefined;
      const untracked = Array.isArray(applied?.untracked_files_not_applied)
        ? (applied!.untracked_files_not_applied as unknown[]).filter((x): x is string => typeof x === "string")
        : [];
      const note = untracked.length > 0 ? ` Untracked files created (not applied): ${untracked.join(", ")}.` : "";
      this.recordRuntime("info", `Applied ${clean} to working tree`, `Not committed.${note}`);
      await this.refreshSwarmReview();
    } catch (error) {
      this.showError(`Apply ${clean} failed: ${protocolErrorMessage(error)}`);
    }
  }

  private swarmReviewItem(taskId: string): ForgeSwarmReviewResult["items"][number] | undefined {
    return this.swarmReview?.items.find((item) => item.task_id === taskId);
  }

  public async discardSwarmTask(taskId: string): Promise<void> {
    const clean = (taskId ?? "").trim();
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!clean || !sessionId || !planId || !this.bridge.forgeSwarmDiscard) {
      return;
    }
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
    if (!trust.allowed) {
      void vscode.window.showWarningMessage(trust.reason ?? "Workspace Trust is required to discard a swarm task.");
      return;
    }
    const choice = await vscode.window.showWarningMessage(
      `Discard swarm task ${clean}? Its preserved worktree is dropped; no changes land in your working tree.`,
      { modal: true },
      "Discard"
    );
    if (choice !== "Discard") {
      return;
    }
    try {
      await this.bridge.forgeSwarmDiscard(sessionId, planId, [clean]);
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.recordRuntime("info", `Discarded ${clean}`, "Worktree dropped; nothing landed.");
      await this.refreshSwarmReview();
    } catch (error) {
      this.showError(`Discard ${clean} failed: ${protocolErrorMessage(error)}`);
    }
  }

  // Failure-recovery: regenerate a task's subtree. The instruction is the
  // user-edited (pre-filled, editable) text from the recovery card — sent only
  // on an explicit click, never silently auto-run.
  public async regenerateSwarmTask(taskId: string, instruction: string): Promise<void> {
    const clean = (taskId ?? "").trim();
    const text = (instruction ?? "").trim();
    if (!clean || !text || !this.sessionId || !this.planId || !this.bridge.forgePlanRegenerateStart) {
      void vscode.window.showInformationMessage("Regenerate is unavailable for this task.");
      return;
    }
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
    if (!trust.allowed) {
      void vscode.window.showWarningMessage(trust.reason ?? "Workspace Trust is required to regenerate a plan subtree.");
      return;
    }
    const mutationWorkspace = workspaceRoot();
    try {
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "regenerating this Forge plan")) {
        return;
      }
      await this.bridge.forgePlanRegenerateStart({
        session_id: this.sessionId,
        plan_id: this.planId,
        workspace_trusted: vscode.workspace.isTrusted,
        instruction: text
      });
      this.recordRuntime("info", `Regenerating subtree for ${clean}`, "Plan regeneration requested.");
      this.refreshCockpit();
    } catch (error) {
      this.showError(`Regenerate ${clean} failed: ${protocolErrorMessage(error)}`);
    }
  }

  public async reviewChanges(taskId?: string): Promise<void> {
    if (this.hasSwarmOperation()) {
      void vscode.window.showInformationMessage("Wait for the active Forge swarm to finish or stop it before reviewing changes.");
      return;
    }
    if (this.planStartPromise || this.activeJobId || this.executeBusy || this.reviewBusy) {
      void vscode.window.showInformationMessage("Another Forge operation is already active.");
      return;
    }
    this.reviewBusy = true;
    this.refreshCockpit();
    try {
      await this.withForgeProgress(
        "Alysis Code is reviewing Forge changes",
        () => this.reviewChangesOnce(taskId)
      );
    } finally {
      this.reviewBusy = false;
      this.refreshCockpit();
    }
  }

  private async reviewChangesOnce(taskId?: string): Promise<void> {
    const config = this.getConfig();
    if (!config.enableForge) {
      void vscode.window.showInformationMessage("Alysis Code Forge commands are disabled in settings.");
      return;
    }
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      void vscode.window.showInformationMessage("Create or open a Forge plan before requesting a review.");
      return;
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      return;
    }
    if (!this.bridge.forgeReview) {
      void vscode.window.showInformationMessage("This Alysis Code CLI does not support forge.review. Upgrade the CLI.");
      return;
    }
    const task = (taskId ?? this.firstReviewableTaskId() ?? "").trim();
    if (!task) {
      void vscode.window.showInformationMessage("No Forge task is available to review.");
      return;
    }
    // forge.review reads/annotates the working tree (running the review model); it never commits or
    // merges. Gate it like other Forge execution: Workspace Trust + executable-origin.
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
    if (!trust.allowed) {
      void vscode.window.showWarningMessage(trust.reason ?? "Workspace Trust is required to run a Forge review.");
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    try {
      await this.preflightFeature(config, "forgeReviewGate", "Forge review");
      await this.ensureBridgeWithoutSecrets(config);
      const result = await this.bridge.forgeReview({
        session_id: sessionId,
        plan_id: planId,
        workspace_trusted: vscode.workspace.isTrusted,
        task_id: task
      });
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.lastReview = result;
      this.recordRuntime(
        "info",
        "Forge review complete",
        `${result.approved ? "Approved" : "Changes requested"} — ${result.blocking_issues_count} blocking, ${result.non_blocking_issues_count} non-blocking.`
      );
      await this.refreshArtifactsAndDiffs();
    } catch (error) {
      this.showError(`Forge review failed: ${protocolErrorMessage(error)}`);
    }
  }

  public async refreshAssets(): Promise<void> {
    const config = this.getConfig();
    if (!config.enableForge) {
      void vscode.window.showInformationMessage("Alysis Code Forge commands are disabled in settings.");
      return;
    }
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      void vscode.window.showInformationMessage("Open a Forge plan to list its assets.");
      return;
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      return;
    }
    if (!this.bridge.forgeAssetsList) {
      void vscode.window.showInformationMessage("This Alysis Code CLI does not support forge.assets. Upgrade the CLI.");
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    this.assetsBusy = true;
    this.refreshCockpit();
    try {
      await this.preflightFeature(config, "assets", "Forge assets");
      await this.ensureBridgeWithoutSecrets(config);
      const result = await this.bridge.forgeAssetsList({ session_id: sessionId, plan_id: planId });
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.assets = result.assets;
    } catch (error) {
      this.showError(`Could not list Forge assets: ${protocolErrorMessage(error)}`);
    } finally {
      this.assetsBusy = false;
      this.refreshCockpit();
    }
  }

  public async openAsset(assetId: string): Promise<void> {
    const config = this.getConfig();
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!config.enableForge || !sessionId || !planId || !this.bridge.forgeAssetsShow || !assetId) {
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    this.assetsBusy = true;
    this.refreshCockpit();
    try {
      await this.preflightFeature(config, "assets", "Forge assets");
      await this.ensureBridgeWithoutSecrets(config);
      const result = await this.bridge.forgeAssetsShow({ session_id: sessionId, plan_id: planId, asset_id: assetId });
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.assetDetail = result.asset;
    } catch (error) {
      this.showError(`Could not open Forge asset: ${protocolErrorMessage(error)}`);
    } finally {
      this.assetsBusy = false;
      this.refreshCockpit();
    }
  }

  private firstReviewableTaskId(): string | undefined {
    if (this.selectedTaskIds.length > 0) {
      return this.selectedTaskIds[0];
    }
    const plan = this.forgeView.state().plan;
    return plan && plan.tasks.length > 0 ? plan.tasks[0].task_id : undefined;
  }

  public async refreshStatus(): Promise<void> {
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      return;
    }
    await this.replayEvents();
    await this.refreshArtifactsAndDiffs();
    this.refreshCockpit();
  }

  public async respondToCockpitApproval(
    sessionId: string,
    approvalId: string,
    decision: "allow_once" | "allow_for_session" | "deny"
  ): Promise<void> {
    const pending = this.pendingApprovals.get(approvalKey(sessionId, approvalId));
    if (!pending) {
      void vscode.window.showInformationMessage("That Forge approval is no longer pending.");
      return;
    }
    if (!this.bridge.respondApproval) {
      await this.denyApproval(
        pending.sessionId,
        pending.approvalId,
        "Forge approval could not be answered from cockpit; denied fail-closed."
      );
      return;
    }
    const key = approvalKey(pending.sessionId, pending.approvalId);
    if (this.pendingApprovalResponses.has(key)) {
      return;
    }
    // Acquire the per-approval lock before any asynchronous validation. An
    // allow and deny click can otherwise both pass the first await and race
    // contradictory responses to the backend.
    this.pendingApprovalResponses.add(key);
    try {
      if (decision !== "deny") {
        const mutationWorkspace = workspaceRoot();
        if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "approving this Forge workspace change")) {
          return;
        }
      }
      const result = await this.bridge.respondApproval({
        session_id: pending.sessionId,
        approval_id: pending.approvalId,
        allow: decision !== "deny",
        allow_for_session: decision === "allow_for_session"
      });
      this.pendingApprovals.delete(key);
      if (result?.allow_for_session_warning) {
        this.forgeView.addEvent({
          type: "warning_emitted",
          label: "Warning",
          description: result.allow_for_session_warning,
          severity: "warning",
          sessionId: pending.sessionId,
          jobId: pending.jobId
        });
      }
      this.statusBar.setActiveRun(this.activeJobId);
      this.runtime?.setBridgeProcess(
        this.activeJobId ? "active" : "ready",
        this.activeJobId ? `Forge job ${this.activeJobId} is running.` : "Forge approval was answered from cockpit."
      );
      this.refreshCockpit();
    } catch (error) {
      // Keep the inbox item until the backend confirms an outcome. Retrying the opposite decision
      // here could race a response that reached the bridge but whose acknowledgement was lost.
      this.showError(`Forge approval response failed; the approval remains pending: ${protocolErrorMessage(error)}`);
    } finally {
      this.pendingApprovalResponses.delete(key);
      this.refreshCockpit();
    }
  }

  /** Returns true only when this instruction produced a new Forge plan. */
  public async planWithInstruction(
    instruction: string,
    modeOverride?: AlysisMode,
    requestId?: string,
    onDurablyAccepted?: () => void
  ): Promise<boolean> {
    const normalizedInstruction = instruction.trim();
    if (!normalizedInstruction) {
      await this.warnAndShow("Forge Plan instruction is required.", "Forge Plan validation");
      return false;
    }
    if (this.disposed) {
      return false;
    }
    if (this.hasSwarmOperation()) {
      void vscode.window.showInformationMessage("Wait for the active Forge swarm to finish or stop it before creating another plan.");
      return false;
    }
    if (this.activeJobId || this.executeBusy || this.reviewBusy) {
      void vscode.window.showInformationMessage("Wait for the active Forge operation to finish or stop it before creating another plan.");
      return false;
    }
    if (this.planStartPromise) {
      void vscode.window.showInformationMessage("A Forge Plan is already being prepared.");
      await this.planStartPromise;
      return false;
    }
    this.pendingPlanEvents = undefined;
    this.planStartCancellationRequested = false;
    const generation = ++this.planIntentGeneration;
    const idempotencyKey = requestId?.trim() || randomUUID();
    const startPromise = this.withForgeProgress(
      "Alysis Code is preparing a Forge plan",
      () => this.planWithInstructionOnce(
        normalizedInstruction,
        modeOverride,
        generation,
        idempotencyKey,
        onDurablyAccepted
      )
    );
    this.planStartPromise = startPromise;
    this.notifyActivityChanged();
    try {
      return await startPromise;
    } finally {
      if (this.planStartPromise === startPromise) {
        this.planStartPromise = undefined;
        this.planStartCancellationRequested = false;
        this.notifyActivityChanged();
      }
    }
  }

  /**
   * Start a Forge plan from a correlated webview submission and resolve once
   * the backend has durably accepted the job, while the controller continues
   * owning progress, result polling, and error presentation.
   */
  public submitPlanWithInstruction(
    instruction: string,
    modeOverride: AlysisMode,
    requestId: string
  ): Promise<boolean> {
    return new Promise<boolean>((resolve) => {
      let acknowledged = false;
      const acknowledge = (): void => {
        if (acknowledged) {
          return;
        }
        acknowledged = true;
        resolve(true);
      };
      void this.planWithInstruction(
        instruction,
        modeOverride,
        requestId,
        acknowledge
      ).then(
        (completed) => {
          if (!acknowledged) {
            resolve(completed);
          }
        },
        () => {
          if (!acknowledged) {
            resolve(false);
          }
        }
      );
    });
  }

  private async planWithInstructionOnce(
    normalizedInstruction: string,
    modeOverride: AlysisMode | undefined,
    initialGeneration: number,
    idempotencyKey: string,
    onDurablyAccepted: (() => void) | undefined
  ): Promise<boolean> {
    let generation = initialGeneration;
    const config = this.getConfig();
    this.runtime?.applyConfig(config);
    this.recordRuntime(
      "info",
      "Forge Plan validating workspace",
      "Validating a correlated Forge Plan request.",
      "Checking Forge enablement, workspace root, and Workspace Trust."
    );
    if (!config.enableForge) {
      await this.infoAndShow("Alysis Code Forge commands are disabled in settings.", "Forge Plan disabled");
      return false;
    }
    const workspace = workspaceRoot();
    if (!workspace) {
      await this.warnAndShow(workspaceScopeRequiredMessage("starting Forge Plan"), "Forge Plan validation");
      return false;
    }
    if (!await this.canMutateWorkspace(workspace, "creating this Forge plan")) {
      return false;
    }
    const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgePlan");
    if (!trust.allowed) {
      await this.warnAndShow(
        trust.reason ??
          "Forge Plan is blocked in untrusted workspaces because the current backend records Forge artifacts in the workspace.",
        "Forge Plan blocked"
      );
      return false;
    }
    this.recordRuntime("info", "Forge Plan checking executable origin", "Checking Alysis Code CLI executable-origin guards.");
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      const message = processGate.reason ?? "Alysis Code CLI execution is blocked.";
      this.runtime?.setCliOrigin("blocked", message);
      await this.warnAndShow(message, "Forge Plan blocked");
      return false;
    }
    this.pendingPlanEvents = [];
    try {
      await this.preflightFeature(config, "forgePlan", "Forge Plan");
      this.assertPlanIntentCurrent(generation);
      this.runtime?.setBridgeProcess("starting", "Starting or reusing Alysis Code IDE bridge for Forge Plan.");
      this.recordRuntime("info", "Forge Plan starting bridge", "Starting or reusing the Alysis Code IDE bridge.");
      const bridgeStart = await this.ensureBridge(config);
      if (bridgeStart?.restarted) {
        // A credential-profile restart emits a bridge reset by design. The same
        // user intent owns the freshly started bridge, so rebase its generation.
        generation = this.planIntentGeneration;
      }
      this.assertPlanIntentCurrent(generation);
      this.runtime?.setCliHealth("ok", "CLI started the IDE bridge.");
      this.runtime?.setBridgeProtocol("ok", "IDE bridge initialized.");
      this.runtime?.setBridgeProcess("ready", "Alysis Code IDE bridge is ready for Forge Plan.");
      if (
        onDurablyAccepted &&
        ![
          "durable_acceptance",
          "idempotency_key_and_payload_hashed",
          "acknowledgement_after_durable_acceptance",
          "stable_job_id_across_bridge_restart",
          "job_status_restart_recovery",
          "result_restart_recovery",
          "fenced_worker_leases"
        ].every((flag) => this.bridge.supportsFeature?.(["forge", "plan", flag]) === true)
      ) {
        throw new Error(
          "This Alysis Code CLI cannot durably accept correlated Forge Plan requests. Upgrade the CLI before starting Forge from the Start view."
        );
      }
      const previousSessionId = this.sessionId;
      if (bridgeStart?.restarted) {
        this.sessionId = undefined;
        this.planId = undefined;
        this.activeJobId = undefined;
        this.ownedSessionIds.clear();
        this.ownedJobIds.clear();
        this.forgeView.addEvent({
          type: "status_update",
          label: "Bridge restarted",
          description: "Alysis Code bridge restarted with a credential-capable launch profile for Forge Plan.",
          severity: "info",
          sessionId: previousSessionId
        });
      }
      const params: ForgePlanParams = {
        workspace,
        instruction: normalizedInstruction,
        mode: modeOverride ?? config.defaultMode,
        idempotency_key: idempotencyKey
      };
      if (this.sessionId) {
        params.session_id = this.sessionId;
      }
      if (config.defaultModel) {
        params.model = config.defaultModel;
      }
      if (config.baseUrl) {
        params.base_url = config.baseUrl;
      }
      if (!await this.canMutateWorkspace(workspace, "creating this Forge plan")) {
        return false;
      }
      const plan = await this.runForgePlan(
        params,
        normalizedInstruction,
        generation,
        onDurablyAccepted
      );
      this.assertPlanIntentCurrent(generation);
      const pendingEvents = this.pendingPlanEvents ?? [];
      this.pendingPlanEvents = undefined;
      this.applyPlan(plan);
      this.handleBufferedPlanEvents(pendingEvents, plan.session_id);
      await this.refreshSessionsView();
      await this.refreshArtifactsAndDiffs();
      await this.replayEvents();
      await vscode.commands.executeCommand("workbench.view.extension.alysis");
      await this.revealCockpit();
      this.recordRuntime("info", "Forge Plan completed", `Forge plan ready: ${plan.plan_id}.`);
      void vscode.window.showInformationMessage(`Alysis Code Forge plan ready: ${plan.plan_id}`);
      return true;
    } catch (error) {
      if (isForgeOperationSupersededError(error) || generation !== this.planIntentGeneration || this.disposed) {
        return false;
      }
      const message = protocolErrorMessage(error);
      const rootCauseKey =
        this.pendingForgeFailureRootCauseKey ?? forgeErrorRootCauseKey(message, this.activeJobId, this.sessionId);
      this.pendingForgeFailureRootCauseKey = undefined;
      this.pendingPlanEvents = undefined;
      this.activeJobId = undefined;
      this.statusBar.setIdle();
      this.runtime?.setBridgeProcess("error", message);
      this.showError(message, {
        title: "Forge Plan failed",
        details: protocolErrorDetails(error),
        rootCauseKey
      });
      return false;
    }
  }

  public async listPlans(): Promise<void> {
    await this.openPersistedPlanFromPicker("List Alysis Code Forge Plans");
  }

  public async openPlan(): Promise<void> {
    await this.openPersistedPlanFromPicker("Open Alysis Code Forge Plan");
  }

  public async execute(): Promise<void> {
    await this.executeWithPreview({ startExecution: true });
  }

  public async executePreview(auto?: boolean): Promise<void> {
    await this.executeWithPreview({ startExecution: false, auto });
  }

  public async openPlanById(planId?: string): Promise<void> {
    const normalizedPlanId = planId?.trim();
    if (!normalizedPlanId) {
      await this.openPlan();
      return;
    }
    if (this.hasSwarmOperation()) {
      void vscode.window.showInformationMessage("Wait for the active Forge swarm to finish or stop it before opening another plan.");
      return;
    }
    if (this.activeJobId || this.executeBusy || this.reviewBusy) {
      void vscode.window.showInformationMessage("Wait for the active Forge operation to finish or stop it before opening another plan.");
      return;
    }
    const config = this.getConfig();
    if (!config.enableForge) {
      void vscode.window.showInformationMessage("Alysis Code Forge commands are disabled in settings.");
      return;
    }
    const workspace = workspaceRoot();
    if (!workspace) {
      void vscode.window.showWarningMessage(workspaceScopeRequiredMessage("opening a Forge plan"));
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    this.pendingPlanEvents = undefined;
    const generation = ++this.planIntentGeneration;
    try {
      await this.preflightFeature(config, "forgePlanPersistence", "Forge Plan open/list");
      await this.ensureBridgeWithoutSecrets(config);
      const plan = await this.openPersistedPlanWithRecoveryIdentity(workspace, normalizedPlanId);
      if (!this.isPlanIntentCurrent(generation)) {
        await this.cancelSupersededPlan(plan.session_id, plan.plan_id, plan.created_session === true);
        return;
      }
      this.applyPlan(plan);
      await this.refreshSessionsView();
      await this.refreshArtifactsAndDiffs();
      await this.replayEvents();
      await vscode.commands.executeCommand("workbench.view.extension.alysis");
      await this.revealCockpit();
      void vscode.window.showInformationMessage(`Alysis Code Forge plan opened: ${plan.plan_id}`);
    } catch (error) {
      if (generation === this.planIntentGeneration && !this.disposed) {
        this.showError(protocolErrorMessage(error));
      }
    }
  }

  public async refreshArtifacts(): Promise<void> {
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      void vscode.window.showInformationMessage("Create or open a Forge plan before refreshing artifacts.");
      return;
    }
    await this.refreshArtifactsAndDiffs();
    await vscode.commands.executeCommand("workbench.view.extension.alysis");
    this.refreshCockpit();
  }

  private async executeWithPreview(options: { startExecution: boolean; auto?: boolean }): Promise<void> {
    if (this.hasSwarmOperation()) {
      void vscode.window.showInformationMessage("Wait for the active Forge swarm to finish or stop it before running Forge Execute.");
      return;
    }
    if (this.planStartPromise || this.activeJobId || this.executeBusy || this.reviewBusy) {
      void vscode.window.showInformationMessage("Another Forge operation is already active.");
      return;
    }
    this.executeBusy = true;
    try {
      await this.withForgeProgress(
        options.startExecution ? "Alysis Code is running Forge Execute" : "Alysis Code is preparing a Forge Execute preview",
        () => this.executeWithPreviewOnce(options)
      );
    } finally {
      this.executeBusy = false;
      this.refreshCockpit();
    }
  }

  private async executeWithPreviewOnce(options: { startExecution: boolean; auto?: boolean }): Promise<void> {
    const config = this.getConfig();
    if (!config.enableForge) {
      void vscode.window.showInformationMessage("Alysis Code Forge commands are disabled in settings.");
      return;
    }
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      void vscode.window.showInformationMessage("Create or open a Forge plan before running Forge Execute Preview.");
      return;
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      return;
    }
    if (config.defaultMode !== "readonly") {
      const trust = evaluateWorkspaceTrust(vscode.workspace.isTrusted, "forgeExecute");
      if (!trust.allowed) {
        void vscode.window.showWarningMessage(
          trust.reason ??
            "Workspace Trust is required for mutating Forge Execute modes. Switch Alysis Code to readonly mode to preview without execution."
        );
        return;
      }
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    try {
      await this.preflightFeature(config, "forgeExecutePreview", "Forge Execute Preview");
      let taskIds: string[] | undefined;
      if (options.auto) {
        // Non-interactive auto-preview (no task picker): cover the whole plan.
        taskIds = undefined;
      } else {
        taskIds = await this.pickPreviewTaskIds();
        if (taskIds === undefined) {
          return;
        }
      }
      await this.ensureBridgeWithoutSecrets(config);
      const preview = await this.bridge.forgeExecutePreview({
        session_id: sessionId,
        plan_id: planId,
        task_ids: taskIds && taskIds.length > 0 ? taskIds : undefined,
        mode: config.defaultMode,
        workspace_trusted: vscode.workspace.isTrusted,
        sandbox_profile: config.sandboxProfile,
        ...forgeExecutePolicyParams(config)
      });
      await this.showExecutePreview(preview);
      this.selectedTaskIds = [...preview.selected_task_ids];
      this.forgeView.addEvent({
        type: "status_update",
        label: "Forge execute review preview",
        description: executePreviewEventDescription(preview),
        severity: preview.preview_ready ? "info" : "warning",
        sessionId: this.sessionId,
        jobId: null
      });
      if (!preview.real_execution_supported) {
        void vscode.window.showWarningMessage("Forge Execute is not available in this build. Preview completed.");
        return;
      }
      if (!options.startExecution) {
        return;
      }
      if (!preview.preview_ready) {
        void vscode.window.showWarningMessage("Forge Execute Preview found blockers. Resolve them before running real execution.");
        return;
      }
      if (config.defaultMode !== "review") {
        await this.warnAndShow(
          `Forge Execute runs in review mode only, but Alysis Code is set to ${config.defaultMode} mode. `
            + "Review mode still lands every change, it just routes each one through the Forge diff and approval flow first. "
            + "Set alysis.defaultMode to review in VS Code settings and start Execute again. The chat Permissions picker changes only the active chat session.",
          "Forge Execute mode unsupported"
        );
        return;
      }
      const confirmed = await vscode.window.showWarningMessage(
        "Start Alysis Code Forge Execute in review mode for the selected task(s)?",
        { modal: true },
        "Start Execute"
      );
      if (confirmed !== "Start Execute") {
        return;
      }
      const mutationWorkspace = workspaceRoot();
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "starting Forge Execute")) {
        return;
      }
      await this.preflightFeature(config, "forgeExecuteReview", "Forge Execute Review");
      const previousSessionId = this.sessionId;
      const bridgeStart = await this.ensureBridge(config);
      if (bridgeStart?.restarted) {
        const workspace = workspaceRoot();
        const currentPlanId = this.planId;
        this.ownedSessionIds.clear();
        this.ownedJobIds.clear();
        if (!workspace) {
          void vscode.window.showWarningMessage(workspaceScopeRequiredMessage("running Forge Execute"));
          return;
        }
        if (!currentPlanId) {
          void vscode.window.showWarningMessage("Forge plan was not available after bridge restart.");
          return;
        }
        const reopened = await this.bridge.forgeOpen({ workspace, plan_id: currentPlanId });
        this.applyPlan(reopened);
        await this.refreshArtifactsAndDiffs();
        this.forgeView.addEvent({
          type: "status_update",
          label: "Bridge restarted",
          description: "Alysis Code bridge restarted with credentials before Forge Execute.",
          severity: "info",
          sessionId: previousSessionId
        });
      }
      if (!this.sessionId || !this.planId) {
        void vscode.window.showWarningMessage("Forge plan was not available after bridge restart.");
        return;
      }
      if (mutationWorkspace && !await this.canMutateWorkspace(mutationWorkspace, "starting Forge Execute")) {
        return;
      }
      const execute = await this.bridge.forgeExecute({
        session_id: this.sessionId,
        plan_id: this.planId,
        task_ids: taskIds,
        mode: "review",
        workspace_trusted: vscode.workspace.isTrusted,
        sandbox_profile: config.sandboxProfile,
        ...forgeExecutePolicyParams(config)
      });
      this.ownSession(execute.session_id);
      if (execute.job_id) {
        this.activeJobId = execute.job_id;
        this.ownJob(execute.job_id);
        this.statusBar.setActiveRun(execute.job_id);
        this.forgeView.addEvent({
          type: "status_update",
          label: "Forge execute review",
          description: execute.status,
          severity: "info",
          sessionId: execute.session_id,
          jobId: execute.job_id
        });
        this.refreshCockpit();
        void this.pollJob(execute.job_id);
      }
    } catch (error) {
      this.showError(protocolErrorMessage(error));
    }
  }

  public async openDiff(diffId?: string): Promise<void> {
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      void vscode.window.showInformationMessage("Create or select a Forge plan before opening diffs.");
      return;
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      return;
    }
    try {
      await this.preflightFeature(this.getConfig(), "diffs", "Forge diffs");
      let selectedDiffId = diffId;
      if (!selectedDiffId) {
        const list = await this.bridge.diffList(sessionId, planId);
        this.forgeView.setDiffs(list.diffs);
        if (list.diffs.length === 0) {
          void vscode.window.showInformationMessage("No Forge diffs are available for this plan.");
          return;
        }
        const picked = await vscode.window.showQuickPick(
          list.diffs.map((diff) => ({
            label: diff.file_path,
            description: diffPickerDescription(diff),
            detail: `${diff.old_label || "Alysis Code original"} -> ${diff.new_label || "Alysis Code proposed"}`,
            diffId: diff.diff_id
          })),
          { title: "Open Alysis Code Forge Diff" }
        );
        selectedDiffId = picked?.diffId;
      }
      if (!selectedDiffId) {
        return;
      }
      const summary = this.forgeView.diff(selectedDiffId);
      const diff = await this.bridge.diffGet(selectedDiffId, {
        sessionId,
        planId
      });
      await this.openDiffPayload(diff, summary);
    } catch (error) {
      this.showError(protocolErrorMessage(error));
    }
  }

  public async openArtifact(sessionId: string, artifactId: string): Promise<void> {
    try {
      const artifact = await this.bridge.artifactRead(sessionId, artifactId);
      const document = await vscode.workspace.openTextDocument({
        content: artifact.content,
        language: "text"
      });
      await vscode.window.showTextDocument(document, { preview: true });
      if (artifact.truncated) {
        void vscode.window.showWarningMessage(`Alysis Code artifact ${artifact.path} was truncated at ${artifact.max_bytes} bytes.`);
      }
    } catch (error) {
      this.showError(protocolErrorMessage(error));
    }
  }

  public testState(): {
    sessionId: string | null;
    planId: string | null;
    activeJobId: string | null;
    view: ReturnType<ForgePlanViewProvider["state"]>;
    artifacts: ReturnType<ArtifactsViewProvider["state"]>;
    cockpit: ForgeCockpitState;
    swarm: SwarmCockpitState;
  } {
    return {
      sessionId: this.sessionId ?? null,
      planId: this.planId ?? null,
      activeJobId: this.activeJobId ?? null,
      view: this.forgeView.state(),
      artifacts: this.artifactsView.state(),
      cockpit: this.cockpitState(),
      swarm: this.swarmCockpitState(this.runtime?.snapshot().compatibility)
    };
  }

  public hasActiveJob(): boolean {
    return (
      this.activeJobId !== undefined ||
      this.planStartPromise !== undefined ||
      this.swarmJobId !== undefined ||
      this.swarmStartPromise !== undefined ||
      this.swarmBusy ||
      this.swarmRecoveryStatus === "resuming"
    );
  }

  private hasSwarmOperation(): boolean {
    return Boolean(
      this.swarmStartPromise ||
      this.swarmJobId ||
      this.swarmBusy ||
      this.swarmStatus === "starting" ||
      this.swarmStatus === "running" ||
      this.swarmStatus === "cancelling" ||
      this.swarmRecoveryStatus === "resuming"
    );
  }

  public activePlanContext(): { sessionId: string; planId: string } | undefined {
    return this.sessionId && this.planId
      ? { sessionId: this.sessionId, planId: this.planId }
      : undefined;
  }

  public async cancelCurrentRun(): Promise<boolean> {
    if (this.swarmStartPromise || this.swarmJobId || this.swarmBusy) {
      await this.cancelSwarm();
      return true;
    }
    if (this.planStartPromise && !this.activeJobId) {
      if (this.planStartCancellationRequested) {
        void vscode.window.showInformationMessage("Forge Plan cancellation is already requested while startup finishes.");
        return true;
      }
      this.planStartCancellationRequested = true;
      this.planIntentGeneration += 1;
      this.pendingPlanEvents = undefined;
      this.recordRuntime(
        "warning",
        "Forge Plan cancellation requested",
        "Planning will stop before launch, or the late backend session will be cancelled when it is returned."
      );
      this.refreshCockpit();
      return true;
    }
    if (!this.sessionId || !this.activeJobId) {
      return false;
    }
    const sessionId = this.sessionId;
    const jobId = this.activeJobId;
    const planId = this.planId;
    if (this.cancellationRequestedJobIds.has(jobId)) {
      void vscode.window.showInformationMessage("Cancellation is already requested for the active Forge job.");
      return true;
    }
    if (this.unsupportedCancellationJobIds.has(jobId)) {
      void vscode.window.showWarningMessage(
        "Forge job is still running; active cancellation was already reported as unsupported by this bridge."
      );
      return true;
    }
    try {
      const result = await this.requestForgeCancellation(sessionId, planId);
      const status = stringField(result.status) || stringField(result.state) || "cancellation_requested";
      if (status === "cancellation_requested") {
        this.cancellationRequestedJobIds.add(jobId);
        this.unsupportedCancellationJobIds.delete(jobId);
        this.statusBar.setActiveRun(jobId);
        this.runtime?.setBridgeProcess("ready", "Forge cancellation requested.");
        this.forgeView.addEvent({
          type: "status_update",
          label: "Forge cancellation",
          description: "Cancellation requested. Waiting for the backend to stop at the next checkpoint.",
          severity: "info",
          sessionId,
          jobId
        });
        await this.refreshSessionsView();
        this.refreshCockpit();
        return true;
      }
      if (status === "non_cancellable") {
        const message = "The active Forge job cannot be cancelled by the backend.";
        this.forgeView.addEvent({
          type: "warning_emitted",
          label: "Warning",
          description: message,
          severity: "warning",
          sessionId,
          jobId
        });
        void vscode.window.showWarningMessage(message);
        return true;
      }
      if (status === "no_active_job" || status === "completed" || status === "failed" || status === "cancelled") {
        this.clearActiveJob(jobId);
      }
      const description =
        status === "no_active_job"
          ? "No active Forge job remains on the backend."
          : `Forge job reached ${status}.`;
      this.forgeView.addEvent({
        type: "status_update",
        label: "Forge cancellation",
        description,
        severity: "info",
        sessionId,
        jobId
      });
      await this.refreshSessionsView();
      this.refreshCockpit();
      return true;
    } catch (error) {
      const message = forgeCancelMessage(error);
      if (isCancellationUnsupportedError(error)) {
        this.unsupportedCancellationJobIds.add(jobId);
      }
      this.runtime?.recordEvent({
        severity: "warning",
        source: "forge",
        title: isCancellationUnsupportedError(error) ? "Forge cancellation unsupported" : "Forge cancellation failed",
        message
      });
      this.forgeView.addEvent({
        type: "warning_emitted",
        label: "Warning",
        description: message,
        severity: "warning",
        sessionId,
        jobId
      });
      this.statusBar.setActiveRun(jobId);
      void vscode.window.showWarningMessage(message);
      return true;
    }
  }

  public shutdown(): Promise<void> {
    if (!this.shutdownPromise) {
      this.shutdownPromise = this.performShutdown();
    }
    return this.shutdownPromise;
  }

  private async performShutdown(): Promise<void> {
    this.disposed = true;
    this.planIntentGeneration += 1;
    this.swarmIntentGeneration += 1;
    this.swarmStartCancellationRequested = true;
    this.detachBridgeListeners();
    await this.denyPendingApprovals("Forge controller disposed before approval was answered.");
    for (const disposable of this.disposables.splice(0)) {
      disposable.dispose();
    }
    this.activityChanged.dispose();
  }

  public dispose(): void {
    void this.shutdown();
  }

  private detachBridgeListeners(): void {
    if (!this.bridge.off) {
      return;
    }
    this.bridge.off("event", this.onBridgeEvent);
    this.bridge.off("exit", this.onBridgeExit);
    this.bridge.off("reset", this.onBridgeReset);
    this.bridge.off("error", this.onBridgeError);
  }

  private async ensureBridge(config: AlysisConfig): Promise<BridgeProfileStartResult | void> {
    const options = await this.bridgeStartOptions(config);
    if (this.bridge.ensureStartedForProfile) {
      return this.bridge.ensureStartedForProfile(config, options, { credentialsRequired: true });
    }
    await this.bridge.ensureStarted(config, options);
    return undefined;
  }

  private async ensureBridgeWithoutSecrets(config: AlysisConfig): Promise<void> {
    if (this.bridge.ensureStartedForProfile) {
      await this.bridge.ensureStartedForProfile(config, { stripApiKey: true }, { credentialsRequired: false });
      return;
    }
    await this.bridge.ensureStarted(config, { stripApiKey: true });
  }

  private async preflightFeature(
    config: AlysisConfig,
    feature: CompatibilityFeatureId,
    label: string
  ): Promise<void> {
    if (!this.bridge.health) {
      return;
    }
    this.runtime?.setCliHealth("checking", `Checking Alysis Code CLI compatibility for ${label}.`);
    try {
      const health = await this.bridge.health(config);
      const compatibility = this.runtime?.applyBridgeHealth(health) ?? evaluateBridgeCompatibility(health);
      assertFeatureCompatible(compatibility, feature);
      this.runtime?.setCliHealth("ok", `Alysis Code CLI supports ${label}.`);
    } catch (error) {
      const message = compatibilityErrorMessage(error);
      this.runtime?.setCliHealth(cliHealthStatusForCompatibilityError(error), message);
      this.runtime?.setBridgeProcess("error", message);
      this.runtime?.recordEvent({
        severity: "error",
        source: "compatibility",
        title: `${label} unavailable`,
        message,
        details: "Upgrade or install a compatible Alysis Code CLI before starting this workflow.",
        rootCauseKey: forgeErrorRootCauseKey(message, this.activeJobId, this.sessionId)
      });
      throw error;
    }
  }

  private async bridgeStartOptions(config: AlysisConfig): Promise<{ apiKey?: string }> {
    if (config.security && !config.security.cliPath.apiKeyForwardingAllowed) {
      return {};
    }
    const apiKey = await this.secrets.getApiKey();
    return apiKey && apiKey.trim().length > 0 ? { apiKey } : {};
  }

  private async runForgePlan(
    params: ForgePlanParams,
    instruction: string,
    generation: number,
    onDurablyAccepted?: () => void
  ): Promise<ForgePlanResult> {
    this.recordRuntime(
      "info",
      "Forge planning started",
      "The Forge Plan request was durably submitted without logging its instruction or idempotency key.",
      "Forge Plan request was sent to the backend."
    );
    const supportsAsync =
      this.bridge.supportsMethod?.("forge.plan.start") === true &&
      this.bridge.supportsMethod?.("forge.plan.result") === true;
    if (!supportsAsync) {
      // The legacy synchronous contract predates idempotency_key. Keep that
      // compatibility call byte-for-byte within its advertised parameter set.
      const compatibilityParams = { ...params };
      delete compatibilityParams.idempotency_key;
      const plan = await this.bridge.forgePlan(compatibilityParams);
      onDurablyAccepted?.();
      if (!this.isPlanIntentCurrent(generation)) {
        await this.cancelSupersededPlan(plan.session_id, plan.plan_id, plan.created_session === true);
        throw forgeOperationSupersededError();
      }
      return plan;
    }
    if (typeof this.bridge.forgePlanStart !== "function" || typeof this.bridge.forgePlanResult !== "function") {
      throw new Error("Alysis Code bridge advertises async Forge Plan support, but the extension bridge client cannot start it.");
    }
    const started = await this.bridge.forgePlanStart(params);
    // Current bridges explicitly attest the durable commit. Older compatible
    // bridges omit the field, so they retain the safer completion-time ack.
    if (started.durably_accepted === true) {
      onDurablyAccepted?.();
    }
    if (!this.isPlanIntentCurrent(generation)) {
      await this.cancelSupersededPlan(started.session_id, undefined, true);
      throw forgeOperationSupersededError();
    }
    this.sessionId = started.session_id;
    this.activeJobId = started.job_id;
    this.ownSession(started.session_id);
    this.ownJob(started.job_id);
    this.forgeView.setPlanning(started.session_id, started.job_id, instruction);
    this.statusBar.setActiveRun(started.job_id);
    this.runtime?.setBridgeProcess("active", `Forge planning job ${started.job_id} is running.`);
    const pendingEvents = this.pendingPlanEvents ?? [];
    this.pendingPlanEvents = undefined;
    this.handleBufferedPlanEvents(pendingEvents, started.session_id);
    await this.refreshSessionsView();
    await vscode.commands.executeCommand("workbench.view.extension.alysis");
    const plan = await this.waitForForgePlan(started, generation);
    if (started.durably_accepted !== true) {
      onDurablyAccepted?.();
    }
    return plan;
  }

  private async waitForForgePlan(started: ForgePlanStartResult, generation: number): Promise<ForgePlanResult> {
    while (this.isPlanIntentCurrent(generation) && this.activeJobId === started.job_id) {
      const status = await this.bridge.jobStatus(started.job_id);
      if (!this.isPlanIntentCurrent(generation) || this.activeJobId !== started.job_id) {
        await this.cancelSupersededPlan(started.session_id, undefined, this.sessionId !== started.session_id);
        throw forgeOperationSupersededError();
      }
      const failedMessage = status.status === "failed" ? status.error || "Alysis Code Forge Plan failed." : "";
      const rootCauseKey =
        status.status === "failed"
          ? forgeErrorRootCauseKey(failedMessage, started.job_id, status.session_id)
          : undefined;
      this.forgeView.addEvent({
        type: "status_update",
        label: "Forge planning",
        description: status.status,
        severity: status.status === "failed" ? "error" : "info",
        sessionId: status.session_id,
        jobId: started.job_id,
        rootCauseKey
      });
      await this.replayEvents();
      if (status.status === "completed") {
        if (typeof this.bridge.forgePlanResult !== "function") {
          throw new Error("Alysis Code bridge advertises async Forge Plan support, but the extension bridge client cannot read its result.");
        }
        const plan = await this.bridge.forgePlanResult(started.job_id);
        if (!this.isPlanIntentCurrent(generation) || this.activeJobId !== started.job_id) {
          await this.cancelSupersededPlan(plan.session_id, plan.plan_id, plan.created_session === true);
          throw forgeOperationSupersededError();
        }
        this.activeJobId = undefined;
        this.ownedJobIds.delete(started.job_id);
        this.statusBar.setIdle();
        this.runtime?.setBridgeProcess("ready", "Forge planning completed.");
        return plan;
      }
      if (status.status === "failed") {
        const message = status.error || "Alysis Code Forge Plan failed.";
        this.activeJobId = undefined;
        this.ownedJobIds.delete(started.job_id);
        this.statusBar.setIdle();
        this.pendingForgeFailureRootCauseKey = rootCauseKey;
        this.runtime?.setBridgeProcess("error", message);
        throw new Error(message);
      }
      if (status.status === "cancelled") {
        this.activeJobId = undefined;
        this.ownedJobIds.delete(started.job_id);
        this.statusBar.setIdle();
        this.runtime?.setBridgeProcess("ready", "Forge planning was cancelled by the backend.");
        this.runtime?.recordEvent({
          severity: "warning",
          source: "forge",
          title: "Forge planning cancelled",
          message: "Alysis Code Forge Plan job was cancelled by the backend."
        });
        throw new Error("Alysis Code Forge Plan job was cancelled by the backend.");
      }
      await sleep(1000);
    }
    throw forgeOperationSupersededError();
  }

  private isPlanIntentCurrent(generation: number): boolean {
    return !this.disposed && generation === this.planIntentGeneration;
  }

  private assertPlanIntentCurrent(generation: number): void {
    if (!this.isPlanIntentCurrent(generation)) {
      throw forgeOperationSupersededError();
    }
  }

  private isSwarmIntentCurrent(generation: number, sessionId: string, planId: string): boolean {
    return (
      !this.disposed &&
      generation === this.swarmIntentGeneration &&
      this.sessionId === sessionId &&
      this.planId === planId
    );
  }

  private assertSwarmIntentCurrent(generation: number, sessionId: string, planId: string): void {
    if (!this.isSwarmIntentCurrent(generation, sessionId, planId)) {
      throw forgeOperationSupersededError();
    }
  }

  private async cancelSupersededPlan(
    sessionId: string,
    planId: string | undefined,
    sessionOwnedBySupersededRequest: boolean
  ): Promise<void> {
    if (planId && this.bridge.forgeCancel) {
      try {
        await this.bridge.forgeCancel({ session_id: sessionId, plan_id: planId, reason: "superseded" });
        return;
      } catch {
        // Fall through to session cancellation only when the stale request owns
        // that session. Never cancel a session now used by a newer plan.
      }
    }
    if (
      sessionOwnedBySupersededRequest &&
      sessionId !== this.sessionId &&
      this.bridge.cancelSession
    ) {
      try {
        await this.bridge.cancelSession(sessionId);
      } catch {
        // Best effort: the bridge may already have reset or completed the job.
      }
    }
  }

  private async cancelSupersededSwarm(sessionId: string, jobId: string): Promise<void> {
    if (!this.bridge.forgeSwarmCancel) {
      return;
    }
    try {
      await this.bridge.forgeSwarmCancel(sessionId, jobId, "superseded");
      this.swarmCancellationRequestedJobIds.add(jobId);
    } catch {
      // Best effort: startup can race a bridge reset, in which case the old
      // process already owns (or has already terminated) the returned job.
    }
  }

  private applyPlan(plan: ForgePlanResult): void {
    this.sessionId = plan.session_id;
    this.planId = plan.plan_id;
    this.recoverableSessionId = plan.session_id;
    this.recoverablePlanId = plan.plan_id;
    void Promise.resolve(this.contextPersistence?.update({
      sessionId: plan.session_id,
      planId: plan.plan_id
    })).catch((error) => {
      this.output.appendLine(`forge context persistence failed: ${protocolErrorMessage(error)}`);
    });
    this.activeJobId = undefined;
    this.lastExecutePreview = null;
    this.selectedTaskIds = [];
    this.ownSession(plan.session_id);
    if (plan.job_id) {
      this.ownJob(plan.job_id);
    }
    this.forgeView.setPlan(plan);
    this.statusBar.setBridgeOk();
    this.runtime?.setBridgeProcess("ready", `Forge plan ${plan.plan_id} is loaded.`);
    this.refreshCockpit();
    if (
      this.runtime?.snapshot().compatibility.features.forgeSwarm.supported
      && this.bridge.forgeSwarmList
      && this.bridge.forgeSwarmResume
    ) {
      void this.refreshSwarmRecovery();
    }
  }

  private async openPersistedPlanFromPicker(title: string): Promise<void> {
    const config = this.getConfig();
    if (!config.enableForge) {
      void vscode.window.showInformationMessage("Alysis Code Forge commands are disabled in settings.");
      return;
    }
    const workspace = workspaceRoot();
    if (!workspace) {
      void vscode.window.showWarningMessage(workspaceScopeRequiredMessage("listing Forge plans"));
      return;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return;
    }
    try {
      await this.preflightFeature(config, "forgePlanPersistence", "Forge Plan open/list");
      await this.ensureBridgeWithoutSecrets(config);
      const result = await this.bridge.forgeList({ workspace, max_items: 50 });
      if (result.plans.length === 0) {
        void vscode.window.showInformationMessage("No persisted Alysis Code Forge plans were found for this workspace.");
        return;
      }
      const picked = await vscode.window.showQuickPick(
        result.plans.map((plan) => ({
          label: plan.plan_id,
          description: `${plan.status} | ${plan.task_count} tasks | ${plan.source}`,
          detail: plan.summary || plan.project_goal || plan.updated_at,
          plan
        })),
        { title, ignoreFocusOut: true }
      );
      if (!picked) {
        return;
      }
      await this.openPersistedPlan(workspace, picked.plan);
    } catch (error) {
      this.showError(protocolErrorMessage(error));
    }
  }

  private async openPersistedPlan(workspace: string, summary: ForgePlanSummary): Promise<void> {
    if (this.hasSwarmOperation()) {
      void vscode.window.showInformationMessage("Wait for the active Forge swarm to finish or stop it before opening another plan.");
      return;
    }
    if (this.activeJobId || this.executeBusy || this.reviewBusy) {
      void vscode.window.showInformationMessage("Wait for the active Forge operation to finish or stop it before opening another plan.");
      return;
    }
    this.pendingPlanEvents = undefined;
    const generation = ++this.planIntentGeneration;
    const plan = await this.openPersistedPlanWithRecoveryIdentity(workspace, summary.plan_id);
    if (!this.isPlanIntentCurrent(generation)) {
      await this.cancelSupersededPlan(plan.session_id, plan.plan_id, plan.created_session === true);
      return;
    }
    this.applyPlan(plan);
    await this.refreshSessionsView();
    await this.refreshArtifactsAndDiffs();
    await this.replayEvents();
    await vscode.commands.executeCommand("workbench.view.extension.alysis");
    await this.revealCockpit();
    void vscode.window.showInformationMessage(`Alysis Code Forge plan opened: ${plan.plan_id}`);
  }

  private async recoverPersistedPlanIfNeeded(): Promise<boolean> {
    if (this.sessionId && this.planId) {
      return true;
    }
    if (this.planRecoveryPromise) {
      return this.planRecoveryPromise;
    }
    const recovery = this.recoverPersistedPlanIfNeededOnce();
    this.planRecoveryPromise = recovery;
    try {
      return await recovery;
    } finally {
      if (this.planRecoveryPromise === recovery) {
        this.planRecoveryPromise = undefined;
      }
    }
  }

  private async recoverPersistedPlanIfNeededOnce(): Promise<boolean> {
    const planId = this.recoverablePlanId;
    if (!planId) {
      return false;
    }
    const config = this.getConfig();
    const workspace = workspaceRoot();
    if (!config.enableForge || !workspace) {
      return false;
    }
    const processGate = evaluateProcessExecutionCommand(config, "bridgeStart");
    if (!processGate.allowed) {
      void vscode.window.showWarningMessage(processGate.reason ?? "Alysis Code CLI execution is blocked.");
      return false;
    }
    try {
      await this.preflightFeature(config, "forgePlanPersistence", "Forge Plan recovery");
      await this.ensureBridgeWithoutSecrets(config);
      const plan = await this.openPersistedPlanWithRecoveryIdentity(workspace, planId);
      this.recoverablePlanId = undefined;
      this.recoverableSessionId = undefined;
      this.applyPlan(plan);
      await this.refreshSessionsView();
      await this.refreshArtifactsAndDiffs();
      await this.replayEvents();
      this.recordRuntime("info", "Forge plan recovered", `Recovered persisted Forge plan ${plan.plan_id} after reconnecting.`);
      return true;
    } catch (error) {
      this.showError(`Forge plan recovery failed: ${protocolErrorMessage(error)}`);
      return false;
    }
  }

  private async openPersistedPlanWithRecoveryIdentity(
    workspace: string,
    planId: string
  ): Promise<ForgePlanResult> {
    const sessionId = this.recoverablePlanId === planId ? this.recoverableSessionId : undefined;
    if (!sessionId) {
      return this.bridge.forgeOpen({ workspace, plan_id: planId });
    }
    try {
      return await this.bridge.forgeOpen({ session_id: sessionId, plan_id: planId });
    } catch (error) {
      if (!isSessionNotFoundError(error) || !this.bridge.createSession) {
        throw error;
      }
    }
    const config = this.getConfig();
    const created = await this.bridge.createSession({
      workspace,
      mode: config.defaultMode,
      session_id: sessionId,
      ...(config.defaultModel ? { model: config.defaultModel } : {}),
      ...(config.baseUrl ? { base_url: config.baseUrl } : {})
    });
    if (created.session_id !== sessionId) {
      throw new Error("The bridge did not restore the requested Forge session identity.");
    }
    return this.bridge.forgeOpen({ session_id: sessionId, plan_id: planId });
  }

  private async pickPreviewTaskIds(): Promise<string[] | undefined> {
    const tasks = this.forgeView.state().plan?.tasks ?? [];
    if (tasks.length === 0) {
      return [];
    }
    const picked = await vscode.window.showQuickPick(
      tasks.map((task) => ({
        label: `${task.task_id} ${task.title}`,
        description: task.status,
        detail: task.objective,
        taskId: task.task_id
      })),
      {
        title: "Preview Forge Execute Review Tasks",
        canPickMany: true,
        ignoreFocusOut: true,
        placeHolder: "Select tasks to include in the non-mutating execution preview."
      }
    );
    if (!picked) {
      return undefined;
    }
    const selected = Array.isArray(picked) ? picked : [picked];
    const taskIds = selected
      .map((item) => item.taskId)
      .filter((taskId): taskId is string => Boolean(taskId));
    if (taskIds.length === 0) {
      void vscode.window.showWarningMessage("Select at least one Forge task to preview execution readiness.");
      return undefined;
    }
    return taskIds;
  }

  private async showExecutePreview(preview: ForgeExecutePreviewResult): Promise<void> {
    this.lastExecutePreview = preview;
    this.selectedTaskIds = [...preview.selected_task_ids];
    await this.revealCockpit();
    this.refreshCockpit();
  }

  private handleBridgeEvent(event: ProtocolEventEnvelope): void {
    if (this.pendingPlanEvents) {
      this.pendingPlanEvents.push(event);
      return;
    }
    if (!this.ownsEvent(event)) {
      return;
    }
    this.forgeView.applyEvent(event);
    if (event.type === "swarm_worker_state_changed") {
      this.applySwarmWorkerEvent(event);
    }
    this.refreshCockpit();
    if (event.type === "prompt_for_input" && event.payload.kind === "approval") {
      void this.handleApprovalPrompt(event);
      return;
    }
    if (event.type === "prompt_for_input" && event.payload.kind === "approval_result") {
      const approvalId = stringField(event.payload.approval_id) || stringField(event.payload.prompt_id);
      if (approvalId) {
        this.pendingApprovals.delete(approvalKey(event.session_id, approvalId));
        this.refreshCockpit();
      }
    }
  }

  // swarm_worker_state_changed carries worker_id=taskId + a render state
  // (scheduled/started/progress/approval_pending/interrupted/failed/merged).
  // Map it to the cockpit task-grid render state.
  private applySwarmWorkerEvent(event: ProtocolEventEnvelope): void {
    const taskId = stringField(event.payload.worker_id);
    const state = stringField(event.payload.state);
    if (!taskId || !state) {
      return;
    }
    const RENDER: Record<string, string> = {
      scheduled: "scheduled",
      started: "running",
      progress: "running",
      approval_pending: "approval",
      interrupted: "interrupted",
      failed: "failed",
      merged: "merged"
    };
    const mapped = RENDER[state];
    if (mapped) {
      this.swarmTaskRenderStates.set(taskId, mapped);
    }
  }

  private handleBufferedPlanEvents(events: ProtocolEventEnvelope[], sessionId: string): void {
    for (const event of events) {
      if (event.session_id !== sessionId) {
        continue;
      }
      this.handleBridgeEvent(event);
    }
  }

  private ownsEvent(event: ProtocolEventEnvelope): boolean {
    if (this.ownedSessionIds.has(event.session_id)) {
      return true;
    }
    return typeof event.job_id === "string" && this.ownedJobIds.has(event.job_id);
  }

  private ownSession(sessionId: string): void {
    if (sessionId) {
      this.ownedSessionIds.add(sessionId);
    }
  }

  private ownJob(jobId: string): void {
    if (jobId) {
      this.ownedJobIds.add(jobId);
    }
  }

  private async handleApprovalPrompt(event: ProtocolEventEnvelope): Promise<void> {
    const approval = approvalPromptFromEvent(event);
    if (!approval || !this.bridge.respondApproval) {
      await this.denyApproval(event.session_id, approval?.approvalId, "Forge approval could not be shown; denied fail-closed.");
      return;
    }
    const key = approvalKey(event.session_id, approval.approvalId);
    if (this.pendingApprovals.has(key)) {
      return;
    }
    this.pendingApprovals.set(key, {
      sessionId: event.session_id,
      approvalId: approval.approvalId,
      jobId: event.job_id ?? null,
      prompt: approval
    });
    this.forgeView.addEvent({
      type: "prompt_for_input",
      label: `Approval required: ${approval.kind}`,
      description: approval.reason || approval.preview,
      severity: "warning",
      sessionId: event.session_id,
      jobId: event.job_id ?? null
    });
    this.refreshCockpit();
    this.statusBar.setApprovalNeeded();
    this.runtime?.setBridgeProcess("approval_needed", "Forge is waiting for an approval decision.");
    // Swarm approvals are non-modal: a modal per worker would be unworkable
    // under parallelism. The cockpit's task-attributed inbox renders them and
    // resolves via respondToCockpitApproval. The requesting worker stays paused
    // (never auto-denied) until the user answers in the inbox.
    if (approval.swarmTaskId) {
      return;
    }
    try {
      if (this.disposed) {
        await this.denyApproval(event.session_id, approval.approvalId, "Forge controller disposed before approval was answered.");
        return;
      }
      const actions = approval.allowForSessionSupported
        ? (["Allow once", "Allow for session", "Deny"] as const)
        : (["Allow once", "Deny"] as const);
      const selected = await vscode.window.showWarningMessage(
        forgeApprovalMessage(approval),
        { modal: true, detail: forgeApprovalDetail(approval) },
        ...actions
      );
      if (!this.pendingApprovals.has(key)) {
        return;
      }
      if (this.pendingApprovalResponses.has(key)) {
        return;
      }
      this.pendingApprovalResponses.add(key);
      try {
        const allow = selected === "Allow once" || selected === "Allow for session";
        const result = await this.bridge.respondApproval({
          session_id: event.session_id,
          approval_id: approval.approvalId,
          allow,
          allow_for_session: selected === "Allow for session"
        });
        this.pendingApprovals.delete(key);
        if (!allow) {
          this.forgeView.addEvent({
            type: "warning_emitted",
            label: "Warning",
            description: "Forge approval denied.",
            severity: "warning",
            sessionId: event.session_id,
            jobId: event.job_id ?? null
          });
        }
        if (result.allow_for_session_warning) {
          this.forgeView.addEvent({
            type: "warning_emitted",
            label: "Warning",
            description: result.allow_for_session_warning,
            severity: "warning",
            sessionId: event.session_id,
            jobId: event.job_id ?? null
          });
        }
        this.statusBar.setActiveRun(this.activeJobId);
        this.runtime?.setBridgeProcess(this.activeJobId ? "active" : "ready", this.activeJobId ? `Forge job ${this.activeJobId} is running.` : "Forge approval was answered.");
        this.refreshCockpit();
        return;
      } catch (error) {
        // Keep the approval visible on an ambiguous transport failure. A second fail-closed denial
        // could contradict an allow that the bridge already received before the response was lost.
        this.showError(`Forge approval response failed; the approval remains pending: ${protocolErrorMessage(error)}`);
        return;
      } finally {
        this.pendingApprovalResponses.delete(key);
      }
    } catch (error) {
      await this.denyApproval(
        event.session_id,
        approval.approvalId,
        `Forge approval failed closed: ${protocolErrorMessage(error)}`
      );
    }
  }

  private async denyPendingApprovals(reason: string): Promise<void> {
    for (const pending of Array.from(this.pendingApprovals.values())) {
      await this.denyApproval(pending.sessionId, pending.approvalId, reason);
    }
  }

  private async denyApproval(
    sessionId: string,
    approvalId: string | undefined,
    reason: string
  ): Promise<void> {
    if (!approvalId) {
      this.output.appendLine(reason);
      return;
    }
    const key = approvalKey(sessionId, approvalId);
    if (this.pendingApprovalResponses.has(key)) {
      return;
    }
    this.pendingApprovalResponses.add(key);
    try {
      await this.bridge.respondApproval?.({
        session_id: sessionId,
        approval_id: approvalId,
        allow: false,
        allow_for_session: false
      });
    } catch {
      // Fail closed: unresolved backend approvals time out as denial.
    } finally {
      this.pendingApprovalResponses.delete(key);
    }
    this.pendingApprovals.delete(key);
    this.forgeView.addEvent({
      type: "warning_emitted",
      label: "Warning",
      description: reason,
      severity: "warning",
      sessionId,
      jobId: this.activeJobId ?? null
    });
    this.statusBar.setActiveRun(this.activeJobId);
    this.runtime?.setBridgeProcess(this.activeJobId ? "active" : "ready", this.activeJobId ? `Forge job ${this.activeJobId} is running.` : reason);
    this.refreshCockpit();
  }

  private async replayEvents(): Promise<void> {
    const sessionId = this.sessionId;
    if (!sessionId) {
      return;
    }
    try {
      const replay = await this.bridge.getEvents(sessionId, this.forgeView.lastSequence(sessionId));
      if (this.sessionId !== sessionId) {
        return;
      }
      for (const event of replay.events) {
        this.forgeView.applyEvent(event);
      }
      if (replay.truncated) {
        this.forgeView.addEvent({
          type: "warning_emitted",
          label: "Warning",
          description: "Forge event replay was truncated.",
          severity: "warning",
          sessionId
        });
      }
      this.refreshCockpit();
    } catch (error) {
      this.output.appendLine(`forge replay failed: ${protocolErrorMessage(error)}`);
    }
  }

  private async refreshArtifactsAndDiffs(): Promise<void> {
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId) {
      return;
    }
    try {
      const artifacts = await this.bridge.artifactList(sessionId);
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.artifactsView.setGroup({
        id: `${sessionId}:${planId}`,
        label: `Plan ${planId}`,
        sessionId,
        planId,
        artifacts: artifacts.artifacts,
        truncated: artifacts.truncated
      });
      this.refreshCockpit();
    } catch (error) {
      this.output.appendLine(`artifact refresh failed: ${protocolErrorMessage(error)}`);
    }
    try {
      const diffs = await this.bridge.diffList(sessionId, planId);
      if (this.sessionId !== sessionId || this.planId !== planId) {
        return;
      }
      this.forgeView.setDiffs(diffs.diffs);
      this.refreshCockpit();
    } catch (error) {
      this.output.appendLine(`diff refresh failed: ${protocolErrorMessage(error)}`);
    }
  }

  private async openDiffPayload(diff: DiffGetResult, summary?: DiffSummary): Promise<void> {
    if (diff.truncated) {
      void vscode.window.showWarningMessage(`Alysis Code diff for ${diff.file_path} was truncated at ${diff.max_bytes} bytes.`);
    }
    const oldText = await this.resolveDiffSide(diff, "old");
    const newText = await this.resolveDiffSide(diff, "new");
    if (oldText !== undefined && newText !== undefined) {
      const original = this.diffUri(diff, "original");
      const proposed = this.diffUri(diff, "proposed");
      this.diffContentProvider.setDocument(original, oldText);
      this.diffContentProvider.setDocument(proposed, newText);
      await vscode.commands.executeCommand(
        "vscode.diff",
        original,
        proposed,
        diffTitle(diff, summary)
      );
      return;
    }
    const document = await vscode.workspace.openTextDocument({
      content: diff.unified_diff,
      language: "diff"
    });
    await vscode.window.showTextDocument(document, { preview: true });
    void vscode.window.showInformationMessage(
      "Alysis Code provided a unified diff preview, but not old/new text snapshots for native side-by-side diff."
    );
  }

  private async resolveDiffSide(diff: DiffGetResult, side: "old" | "new"): Promise<string | undefined> {
    const text = side === "old" ? diff.old_text : diff.new_text;
    if (typeof text === "string") {
      return text;
    }
    const artifactId = side === "old" ? diff.old_artifact_id : diff.new_artifact_id;
    if (!artifactId || !diff.session_id) {
      return undefined;
    }
    const artifact = await this.bridge.artifactRead(diff.session_id, artifactId);
    return artifact.content;
  }

  private diffUri(diff: DiffGetResult, side: "original" | "proposed"): vscode.Uri {
    // Deterministic and re-resolvable: reopening the same diff reuses the same document key instead of
    // minting a timestamped one per click, and a restored editor carries enough identity to refetch.
    const key = encodeURIComponent(`${diff.plan_id}:${diff.diff_id}:${side}`);
    const path = `/${side}/${diff.file_path.replace(/\\/g, "/")}`;
    return vscode.Uri.from({
      scheme: "alysis-diff",
      authority: "forge",
      path,
      query: key
    });
  }

  /**
   * Refill a diff document VS Code restored after a window reload. The in-memory cache is gone by
   * then, so the content is fetched again through the same bounded, redacted protocol path.
   */
  private async restoreDiffDocument(uri: vscode.Uri): Promise<string> {
    const identity = parseDiffDocumentKey(uri);
    if (!identity) {
      throw new Error("Alysis Code cannot restore this diff. Reopen it from the Alysis Code plan view.");
    }
    if ((!this.sessionId || !this.planId) && !await this.recoverPersistedPlanIfNeeded()) {
      throw new Error("Alysis Code cannot restore this diff until its Forge plan is open again.");
    }
    const sessionId = this.sessionId;
    const planId = this.planId;
    if (!sessionId || !planId || planId !== identity.planId) {
      throw new Error("Alysis Code cannot restore this diff: its Forge plan is no longer open.");
    }
    try {
      const diff = await this.bridge.diffGet(identity.diffId, { sessionId, planId });
      const text = await this.resolveDiffSide(diff, identity.side === "original" ? "old" : "new");
      if (text === undefined) {
        throw new Error("Alysis Code did not return snapshot text for this diff side.");
      }
      return text;
    } catch (error) {
      throw new Error(
        `Alysis Code could not reload this diff: ${redactForDisplay(protocolErrorMessage(error))}`
      );
    }
  }

  private async showTaskDetails(task: ForgePlanTask): Promise<void> {
    const document = await vscode.workspace.openTextDocument({
      content: renderTaskDetails(task),
      language: "markdown"
    });
    await vscode.window.showTextDocument(document, { preview: true });
  }

  private async refreshSessionsView(): Promise<void> {
    try {
      const list = await this.bridge.sessionList();
      this.sessionsView.setSessions(list.sessions);
    } catch {
      this.sessionsView.setSessions([]);
    }
  }

  private async pollJob(jobId: string): Promise<void> {
    let consecutiveStatusFailures = 0;
    let abandonedMessage: string | undefined;
    while (!this.disposed && this.activeJobId === jobId) {
      await sleep(1500);
      if (this.disposed || this.activeJobId !== jobId) {
        return;
      }
      try {
        const status = await this.bridge.jobStatus(jobId);
        if (
          this.disposed ||
          this.activeJobId !== jobId ||
          this.sessionId !== status.session_id
        ) {
          return;
        }
        this.forgeView.addEvent({
          type: "status_update",
          label: "Forge job",
          description: status.status,
          severity: forgeJobSeverity(status.status),
          sessionId: status.session_id,
          jobId
        });
        this.refreshCockpit();
        if (status.status === "completed" || status.status === "failed" || status.status === "cancelled") {
          if (this.disposed || this.activeJobId !== jobId) {
            return;
          }
          this.clearActiveJob(jobId);
          this.runtime?.setBridgeProcess("ready", `Forge job ${jobId} reached ${status.status}.`);
          await this.refreshArtifactsAndDiffs();
          this.refreshCockpit();
          return;
        }
      } catch (error) {
        if (this.disposed || this.activeJobId !== jobId) {
          return;
        }
        consecutiveStatusFailures += 1;
        if (consecutiveStatusFailures <= MAX_TRANSIENT_JOB_STATUS_FAILURES) {
          // One bridge hiccup must never abandon a live run: keep observing the same job.
          this.output.appendLine(
            `forge job status failed (attempt ${consecutiveStatusFailures} of ${MAX_TRANSIENT_JOB_STATUS_FAILURES + 1}): ${protocolErrorMessage(error)}`
          );
          continue;
        }
        abandonedMessage =
          `Could not refresh Forge job status after ${consecutiveStatusFailures} attempts: ${protocolErrorMessage(error)}`;
        return;
      } finally {
        // Giving up still has to release the shared active-job lease. Without this every Forge entry
        // point stays blocked on "Another Forge operation is already active" and the status bar spins
        // forever. Clear first (clearActiveJob resets the status bar), then report the failure.
        if (abandonedMessage !== undefined) {
          this.output.appendLine(abandonedMessage);
          this.forgeView.addEvent({
            type: "error_raised",
            label: "Forge job",
            description: "status_unknown",
            severity: "error",
            sessionId: this.sessionId,
            jobId
          });
          this.clearActiveJob(jobId);
          this.showError(abandonedMessage, { title: "Forge job status lost" });
        }
      }
    }
  }

  private async requestForgeCancellation(sessionId: string, planId: string | undefined): Promise<Record<string, unknown>> {
    const reason = "cancelled_by_user";
    if (planId) {
      const activeForgeCancellationSupported =
        this.bridge.supportsFeature?.(["forge", "cancel", "supported"]) === true;
      if (
        activeForgeCancellationSupported &&
        this.bridge.supportsMethod?.("forge.cancel") === true &&
        this.bridge.forgeCancel
      ) {
        return this.bridge.forgeCancel({ session_id: sessionId, plan_id: planId, reason });
      }
      throw new ProtocolClientError(
        "forge_cancel_unsupported",
        "Forge job is running; active cancellation is not advertised by this bridge."
      );
    }

    const activeSessionCancellationSupported =
      this.bridge.supportsFeature?.(["cancellation", "active_jobs"]) === true;
    if (
      activeSessionCancellationSupported &&
      this.bridge.supportsMethod?.("session.cancel") === true &&
      this.bridge.cancelSession
    ) {
      return this.bridge.cancelSession(sessionId);
    }
    throw new ProtocolClientError(
      "forge_cancel_unsupported",
      "Forge job is running before a plan is attached; active cancellation is unavailable from this bridge."
    );
  }

  private clearActiveJob(jobId: string): void {
    if (this.activeJobId === jobId) {
      this.activeJobId = undefined;
    }
    this.unsupportedCancellationJobIds.delete(jobId);
    this.cancellationRequestedJobIds.delete(jobId);
    this.ownedJobIds.delete(jobId);
    this.statusBar.setIdle();
    this.notifyActivityChanged();
  }

  private showError(
    text: string,
    options: {
      title?: string;
      details?: string | null;
      rootCauseKey?: string | null;
      record?: boolean;
    } = {}
  ): void {
    const safeText = redactForDisplay(text);
    const safeDetails = options.details ? redactForDisplay(options.details) : null;
    this.statusBar.setError(safeText);
    this.runtime?.setBridgeProcess("error", safeText);
    if (options.record !== false) {
      this.runtime?.recordError(
        "forge",
        options.title ?? "Forge error",
        safeText,
        safeDetails,
        options.rootCauseKey ?? forgeErrorRootCauseKey(safeText, this.activeJobId, this.sessionId)
      );
    }
    this.refreshCockpit();
    void vscode.window.showErrorMessage(safeText);
  }

  private async canMutateWorkspace(workspace: string, action: string): Promise<boolean> {
    try {
      assertNoDirtyWorkspaceDocuments(vscode, workspace, action);
      return true;
    } catch (error) {
      if (!isDirtyWorkspaceDocumentsError(error)) {
        throw error;
      }
      this.output.appendLine(`workspace mutation blocked: ${error.message}`);
      void vscode.window.showWarningMessage(error.message);
      return false;
    }
  }

  /**
   * Long Forge operations block on the backend for many seconds. Show the standard VS Code progress
   * notification so the window never looks idle; cancellation stays with alysis.cancelCurrentRun,
   * which is the only path that can stop the backend job cooperatively.
   */
  private withForgeProgress<T>(title: string, task: () => Promise<T>): Promise<T> {
    return Promise.resolve(
      vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title, cancellable: false },
        () => task()
      )
    );
  }

  private async revealCockpit(): Promise<void> {
    await this.cockpitCallbacks?.reveal?.();
  }

  private refreshCockpit(): void {
    this.notifyActivityChanged();
    this.cockpitCallbacks?.refresh?.();
  }

  private notifyActivityChanged(): void {
    const active = this.hasActiveJob();
    if (active === this.lastPublishedActivity) {
      return;
    }
    this.lastPublishedActivity = active;
    this.activityChanged.fire(active);
  }

  private recordRuntime(
    severity: "info" | "warning" | "error",
    title: string,
    message: string,
    details?: string | null
  ): void {
    this.runtime?.recordEvent({ severity, source: "forge", title, message, details });
  }

  private async warnAndShow(message: string, title: string): Promise<void> {
    this.recordRuntime("warning", title, message);
    void vscode.window.showWarningMessage(message);
  }

  private async infoAndShow(message: string, title: string): Promise<void> {
    this.recordRuntime("info", title, message);
    void vscode.window.showInformationMessage(message);
  }
}

function workspaceRoot(): string | undefined {
  return activeWorkspaceRoot();
}

function parseDiffDocumentKey(
  uri: vscode.Uri
): { planId: string; diffId: string; side: "original" | "proposed" } | undefined {
  let decoded: string;
  try {
    decoded = decodeURIComponent(uri.query ?? "");
  } catch {
    return undefined;
  }
  const separator = decoded.lastIndexOf(":");
  const side = decoded.slice(separator + 1);
  const rest = decoded.slice(0, separator);
  const planSeparator = rest.indexOf(":");
  const planId = rest.slice(0, planSeparator);
  const diffId = rest.slice(planSeparator + 1);
  if (!planId || !diffId || (side !== "original" && side !== "proposed")) {
    return undefined;
  }
  return { planId, diffId, side };
}

// Apply is the only swarm control that writes agent output into the user's files, so it names exactly
// what lands and where before a single byte is written.
function applySwarmTaskConfirmation(
  taskId: string,
  item: ForgeSwarmReviewResult["items"][number] | undefined
): string {
  const title = item?.title ? `: ${redactForDisplay(item.title).slice(0, 120)}` : "";
  const lines = [
    `Apply Forge swarm task ${taskId}${title} to your working tree?`,
    "Alysis Code writes this task's agent-generated diff into your workspace files now. Nothing is committed, so review or revert it with Git afterwards."
  ];
  const untracked = item?.untracked_files ?? [];
  if (untracked.length > 0) {
    const shown = untracked.slice(0, 5).map((file) => redactForDisplay(file)).join(", ");
    lines.push(
      `${untracked.length} new file(s) the agent created are NOT applied and stay in the task worktree: ${shown}${untracked.length > 5 ? ", …" : ""}.`
    );
  }
  return lines.join("\n\n");
}

function diffPickerDescription(diff: DiffSummary): string {
  const parts = [`plan ${diff.plan_id}`];
  if (diff.job_id) {
    parts.push(`job ${diff.job_id}`);
  }
  parts.push(diff.status, `${diff.size_bytes} bytes`);
  return parts.join(" | ");
}

function diffTitle(diff: DiffGetResult, summary?: DiffSummary): string {
  const oldLabel = summary?.old_label || "Alysis Code original";
  const newLabel = summary?.new_label || "Alysis Code proposed";
  const context = [`plan ${diff.plan_id}`];
  if (diff.job_id) {
    context.push(`job ${diff.job_id}`);
  }
  return `${oldLabel} -> ${newLabel}: ${diff.file_path} (${context.join(" | ")})`;
}

function executePreviewEventDescription(preview: ForgeExecutePreviewResult): string {
  const blockers = preview.missing_prerequisites.length;
  if (!preview.real_execution_supported) {
    return blockers > 0
      ? `Preview completed with ${blockers} blocker(s); real execution is unavailable.`
      : "Preview completed; real execution is unavailable in this build.";
  }
  return blockers > 0
    ? `Preview completed with ${blockers} blocker(s).`
    : "Preview completed and prerequisites passed.";
}

function forgeExecutePolicyParams(config: AlysisConfig): { max_steps?: number; no_log: boolean } {
  return {
    ...(config.forgeExecuteMaxSteps ? { max_steps: config.forgeExecuteMaxSteps } : {}),
    no_log: config.forgeExecuteNoLog
  };
}

function forgeJobSeverity(status: string): "info" | "warning" | "error" {
  const normalized = status.trim().toLowerCase();
  if (normalized === "failed") {
    return "error";
  }
  if (normalized === "cancelled") {
    return "warning";
  }
  return "info";
}

function renderTaskDetails(task: ForgePlanTask): string {
  return [
    `# ${task.task_id} ${task.title}`,
    "",
    `Status: ${task.status}`,
    "",
    "## Objective",
    "",
    task.objective || "(none)",
    "",
    "## File Scope",
    "",
    `- Estimated: ${task.file_scope.estimated_files.join(", ") || "(none)"}`,
    `- Write: ${task.file_scope.write_scope.join(", ") || "(none)"}`,
    `- Scope note: ${task.scope_unknown_reason || "(none)"}`,
    "",
    "## Dependencies",
    "",
    ...(task.dependencies.length > 0 ? task.dependencies.map((item) => `- ${item}`) : ["- (none)"]),
    "",
    "## Risk Notes",
    "",
    ...(task.risk_notes.length > 0 ? task.risk_notes.map((item) => `- ${item}`) : ["- (none)"]),
    "",
    "## Acceptance Criteria",
    "",
    ...(task.acceptance_criteria.length > 0 ? task.acceptance_criteria.map((item) => `- ${item}`) : ["- (none)"]),
    "",
    "## Verification Commands",
    "",
    ...(task.verification_commands.length > 0 ? task.verification_commands.map((item) => `- \`${item}\``) : ["- (none)"]),
    "",
    "## Warnings",
    "",
    ...(task.warnings.length > 0 ? task.warnings.map((item) => `- ${item}`) : ["- (none)"])
  ].join("\n");
}

interface ForgeApprovalPrompt {
  approvalId: string;
  kind: string;
  reason: string;
  preview: string;
  command: string;
  files: string[];
  allowForSessionSupported: boolean;
  allowForSessionScope: Record<string, unknown> | null;
  // Set when the approval was raised by a swarm worker (from prompt metadata):
  // task id + worker label for the non-modal attributed inbox.
  swarmTaskId: string | null;
  swarmWorker: string | null;
}

function approvalPromptFromEvent(event: ProtocolEventEnvelope): ForgeApprovalPrompt | undefined {
  const approvalId =
    typeof event.payload.approval_id === "string"
      ? event.payload.approval_id
      : typeof event.payload.prompt_id === "string"
        ? event.payload.prompt_id
        : "";
  if (!approvalId) {
    return undefined;
  }
  const files = Array.isArray(event.payload.files)
    ? event.payload.files.filter((item): item is string => typeof item === "string")
    : [];
  const metadata = isRecord(event.payload.metadata) ? event.payload.metadata : {};
  return {
    approvalId,
    kind: approvalKindFromPayload(event.payload),
    reason: stringField(event.payload.reason),
    preview: stringField(event.payload.preview) || stringField(event.payload.prompt_text),
    command: stringField(event.payload.command),
    files,
    allowForSessionSupported: event.payload.allow_for_session_supported === true,
    allowForSessionScope: isRecord(event.payload.allow_for_session_scope)
      ? event.payload.allow_for_session_scope
      : null,
    swarmTaskId: stringField(metadata.forge_swarm_task_id) || null,
    swarmWorker: stringField(metadata.worker) || null
  };
}

function approvalKindFromPayload(payload: Record<string, unknown>): string {
  const metadata = isRecord(payload.metadata) ? payload.metadata : {};
  return (
    stringField(payload.approval_kind) ||
    stringField(metadata.approval_kind) ||
    stringField(metadata.kind) ||
    (stringField(payload.kind) !== "approval" ? stringField(payload.kind) : "") ||
    "approval"
  );
}

function forgeApprovalMessage(approval: ForgeApprovalPrompt): string {
  return `Alysis Code Forge approval required: ${approval.kind}`;
}

function forgeApprovalDetail(approval: ForgeApprovalPrompt): string {
  const lines = [
    `Kind: ${approval.kind}`,
    `Reason: ${approval.reason || "(none)"}`,
    `Command: ${approval.command || "(none)"}`,
    `Preview: ${approval.preview || "(none)"}`,
    `Files: ${approval.files.length > 0 ? approval.files.join(", ") : "(none)"}`,
    `Allow for session: ${approval.allowForSessionSupported ? "supported" : "unsupported"}`,
    `Scope: ${approval.allowForSessionScope ? JSON.stringify(approval.allowForSessionScope) : "(none)"}`
  ];
  return lines.join("\n");
}

function forgeCancelMessage(error: unknown): string {
  if (isCancellationUnsupportedError(error)) {
    return "Forge job is running; active cancellation is not advertised by this bridge.";
  }
  return protocolErrorMessage(error);
}

function isCancellationUnsupportedError(error: unknown): boolean {
  return (
    error instanceof ProtocolClientError &&
    (error.code === "forge_cancel_unsupported" || error.code === "cancel_not_supported")
  );
}

function approvalKey(sessionId: string, approvalId: string): string {
  return `${sessionId}\u0000${approvalId}`;
}

function stringField(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function reviewSummary(result: ForgeReviewResult): ForgeReviewSummary {
  return {
    taskId: result.task_id,
    approved: result.approved,
    confidence: result.confidence,
    summary: redactForDisplay(result.summary || ""),
    blockingIssues: result.blocking_issues_count,
    nonBlockingIssues: result.non_blocking_issues_count,
    requiresHumanApproval: result.requires_human_approval,
    jsonArtifactId: result.json_artifact_id,
    markdownArtifactId: result.markdown_artifact_id
  };
}

function assetSummary(entry: ForgeAssetEntry): ForgeAssetSummary {
  return {
    id: entry.record.id,
    title: entry.record.title || entry.record.original_filename || entry.record.id,
    kind: entry.record.kind,
    mime: entry.record.mime,
    sizeBytes: entry.record.size_bytes,
    pinned: entry.record.pinned,
    comprehensionStatus: entry.comprehension_status || entry.record.comprehension_status,
    summaryPreview: redactForDisplay(entry.comprehension_summary_preview || "")
  };
}

function assetDetailView(detail: ForgeAssetDetail): ForgeAssetDetailView {
  return {
    id: detail.record.id,
    title: detail.record.title || detail.record.original_filename || detail.record.id,
    description: redactForDisplay(detail.record.description || ""),
    kind: detail.record.kind,
    mime: detail.record.mime,
    sizeBytes: detail.record.size_bytes,
    comprehensionStatus: detail.comprehension_status || detail.record.comprehension_status,
    extractedTextPreview: redactForDisplay(detail.extracted_text_preview || "")
  };
}

function protocolErrorMessage(error: unknown): string {
  let message: string;
  if (error instanceof FeatureCompatibilityError) {
    message = error.message;
  } else if (error instanceof ProtocolClientError) {
    message = `${error.message} (${error.code})`;
  } else if (error instanceof Error) {
    message = error.message;
  } else {
    message = String(error);
  }
  return redactForDisplay(message);
}

function protocolErrorDetails(error: unknown): string | null {
  if (error instanceof Error && error.stack && error.stack !== error.message) {
    return redactForDisplay(error.stack);
  }
  return null;
}

function forgeOperationSupersededError(): Error {
  const error = new Error("Alysis Code Forge operation was superseded by a newer intent.");
  error.name = "ForgeOperationSupersededError";
  return error;
}

function isForgeOperationSupersededError(error: unknown): boolean {
  return error instanceof Error && error.name === "ForgeOperationSupersededError";
}

function forgeErrorRootCauseKey(message: string, jobId?: string | null, sessionId?: string | null): string {
  const scope = jobId || sessionId || "no-job";
  return `forge:${scope}:${normalizeRootCause(message)}`;
}

function normalizeRootCause(value: string): string {
  return value
    .toLowerCase()
    .replace(/\([^)]*\)/g, "")
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .slice(0, 160);
}

function compatibilityErrorMessage(error: unknown): string {
  if (error instanceof FeatureCompatibilityError) {
    return error.message;
  }
  return protocolErrorMessage(error);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Transient bridge failures are tolerated before a job is abandoned, mirroring the chat-side poller.
const MAX_TRANSIENT_JOB_STATUS_FAILURES = 3;

function shortJobId(jobId: string): string {
  return jobId.length <= 12 ? jobId : `${jobId.slice(0, 8)}…${jobId.slice(-4)}`;
}

function boundedRecoveryError(error: unknown): string {
  return protocolErrorMessage(error).slice(0, 600);
}

function isSessionNotFoundError(error: unknown): boolean {
  return error instanceof ProtocolClientError && error.code === "session_not_found";
}

function persistedForgeContext(value: unknown): { sessionId: string; planId: string } | undefined {
  if (!isRecord(value)) {
    return undefined;
  }
  const sessionId = typeof value.sessionId === "string" ? value.sessionId.trim() : "";
  const planId = typeof value.planId === "string" ? value.planId.trim() : "";
  if (!sessionId || sessionId.length > 256 || !planId || planId.length > 256) {
    return undefined;
  }
  return { sessionId, planId };
}

function freshSwarmRecoveryGrant(
  sessionId: string,
  planId: string,
  jobId: string,
  revision: number
): {
  kind: string;
  scope: Record<string, unknown>;
} {
  return {
    kind: "forge_swarm_resume",
    scope: {
      type: "forge_swarm_resume_v1",
      session_id: sessionId,
      plan_id: planId,
      job_id: jobId,
      revision
    }
  };
}

// A swarm result payload is still in-progress when it carries complete:false.
function isSwarmProgress(result: ForgeSwarmJobResult): result is ForgeJobProgress {
  return (result as ForgeJobProgress).complete === false;
}

// Map a bridge swarm-review recovery offer into the cockpit's editable
// regenerate-subtree affordance (pre-filled instruction + optional focus).
function recoveryOffer(raw: Record<string, unknown> | null): { instruction: string; focus: string | null } | null {
  if (!raw || typeof raw !== "object") {
    return null;
  }
  const params = isRecord(raw.suggested_params) ? raw.suggested_params : {};
  const instruction = typeof params.instruction === "string" ? params.instruction : "";
  if (!instruction) {
    return null;
  }
  return {
    instruction,
    focus: typeof params.focus === "string" ? params.focus : null
  };
}
