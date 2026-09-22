import path from "node:path";

import * as vscode from "vscode";

import { redactForDisplay } from "../client/CliDiscovery";
import {
  CodeReviewFinding,
  CodeReviewResult,
  CodeReviewStartParams,
  CodeReviewStartResult
} from "../client/AlysisProtocol";
import {
  WorkspaceBoundary,
  isSafePath,
  isSensitivePath
} from "../context/IdeContextCollector";
import {
  ContextDocument,
  ContextSkip,
  IdeContextSource
} from "../context/IdeContextTypes";

const DEFAULT_MAX_POLL_ATTEMPTS = 120;
const DEFAULT_MAX_CONSECUTIVE_ERRORS = 3;
const DEFAULT_POLL_INTERVAL_MS = 1_000;
const MAX_FINDINGS = 100;
const MAX_FINDINGS_PER_FILE = 50;
const MAX_REVIEW_FILES = 100;
const MAX_RANGE_SPAN = 200;
const MAX_TITLE_CHARS = 240;
const MAX_DETAIL_CHARS = 2_000;
const MAX_DIAGNOSTIC_MESSAGE_CHARS = 4_000;
const MAX_WARNING_CHARS = 500;
const MAX_WARNINGS = 20;
const MAX_DIFF_PATHS = 200;

export interface CodeReviewBridge {
  codeReviewStart(params: CodeReviewStartParams): Promise<CodeReviewStartResult>;
  codeReviewResult(jobId: string): Promise<CodeReviewResult>;
  cancelSession(sessionId: string): Promise<Record<string, unknown>>;
}

export interface ReviewCancellationToken {
  readonly isCancellationRequested: boolean;
  onCancellationRequested(listener: () => void): vscode.Disposable;
}

export interface ReviewProgress {
  report(value: { message?: string; increment?: number }): void;
}

export interface CodeReviewPresenterOptions {
  maxPollAttempts?: number;
  maxConsecutiveErrors?: number;
  pollIntervalMs?: number;
  sleep?(milliseconds: number): Promise<void>;
  runWithProgress?<T>(
    task: (progress: ReviewProgress, token: ReviewCancellationToken) => Promise<T>
  ): Promise<T>;
  diagnostics?: vscode.DiagnosticCollection;
}

/**
 * Owns the complete VS Code code-review presentation lifecycle. Only findings
 * that survive the same realpath, sensitive-path, configured-exclusion, and
 * Git-ignore policy as IDE context can enter Problems.
 */
export class CodeReviewPresenter implements vscode.Disposable {
  private readonly diagnostics: vscode.DiagnosticCollection;
  private readonly maxPollAttempts: number;
  private readonly maxConsecutiveErrors: number;
  private readonly pollIntervalMs: number;
  private readonly sleep: (milliseconds: number) => Promise<void>;
  private readonly runWithProgress: NonNullable<CodeReviewPresenterOptions["runWithProgress"]>;
  private generation = 0;
  private running = false;
  private disposed = false;
  private activeReview: { generation: number; sessionId: string; cancellation?: Promise<void> } | undefined;

  public constructor(
    private readonly bridge: CodeReviewBridge,
    private readonly source: IdeContextSource,
    private readonly output: vscode.OutputChannel,
    options: CodeReviewPresenterOptions = {}
  ) {
    this.maxPollAttempts = boundedInteger(
      options.maxPollAttempts ?? DEFAULT_MAX_POLL_ATTEMPTS,
      1,
      600,
      "poll attempt"
    );
    this.maxConsecutiveErrors = boundedInteger(
      options.maxConsecutiveErrors ?? DEFAULT_MAX_CONSECUTIVE_ERRORS,
      1,
      10,
      "poll retry"
    );
    this.pollIntervalMs = boundedInteger(
      options.pollIntervalMs ?? DEFAULT_POLL_INTERVAL_MS,
      0,
      10_000,
      "poll interval"
    );
    this.sleep = options.sleep ?? ((milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)));
    this.diagnostics = options.diagnostics ?? vscode.languages.createDiagnosticCollection("alysis-code-review");
    this.runWithProgress = options.runWithProgress ?? ((task) => Promise.resolve(vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "Alysis Code code review",
        cancellable: true
      },
      task
    )));
  }

  public async run(params: CodeReviewStartParams, workspaceRoot?: string): Promise<CodeReviewResult> {
    this.assertUsable();
    if (this.running) {
      throw new Error("An Alysis Code code review is already running.");
    }
    this.running = true;
    const generation = ++this.generation;
    this.diagnostics.clear();
    try {
      return await this.runWithProgress((progress, token) =>
        this.startAndPoll(params, generation, progress, token, workspaceRoot)
      );
    } catch (error) {
      if (this.isCurrent(generation)) {
        this.diagnostics.clear();
      }
      // Raw bridge failures can carry paths and provider detail; the caller renders this text.
      throw safeReviewError(error);
    } finally {
      if (this.isCurrent(generation)) {
        this.running = false;
      }
    }
  }

  /** Publishes a manually fetched completed result through the same safety boundary. */
  public async present(
    result: CodeReviewResult,
    expectedSessionId: string,
    workspaceRoot?: string
  ): Promise<CodeReviewResult> {
    this.assertUsable();
    void this.cancelActiveReview();
    const generation = ++this.generation;
    this.running = false;
    this.diagnostics.clear();
    if (!expectedSessionId || result.session_id !== expectedSessionId) {
      throw new Error("Alysis Code code review response session verification failed.");
    }
    if (!isCompleted(result)) {
      return boundedReviewResult(result, [], 0, false);
    }
    return this.publishCompleted(result, generation, workspaceRoot);
  }

  public clear(): void {
    void this.cancelActiveReview();
    this.generation += 1;
    this.running = false;
    this.diagnostics.clear();
  }

  public dispose(): void {
    if (this.disposed) {
      return;
    }
    void this.cancelActiveReview();
    this.disposed = true;
    this.generation += 1;
    this.running = false;
    this.diagnostics.clear();
    this.diagnostics.dispose();
  }

  private async startAndPoll(
    params: CodeReviewStartParams,
    generation: number,
    progress: ReviewProgress,
    token: ReviewCancellationToken,
    workspaceRoot?: string
  ): Promise<CodeReviewResult> {
    let cancellationRequested = token.isCancellationRequested;
    let started: CodeReviewStartResult | undefined;
    let cancelPromise: Promise<void> | undefined;
    let terminalResultObserved = false;
    const requestCancellation = (): void => {
      cancellationRequested = true;
      if (started && !cancelPromise) {
        cancelPromise = this.cancelActiveReview() ?? this.cancelOnce(params.session_id);
      }
    };
    const cancellationDisposable = token.onCancellationRequested(requestCancellation);
    try {
      progress.report({ message: "Starting bounded review…" });
      if (cancellationRequested) {
        return cancelledReview(params.session_id, "cancelled-before-start");
      }
      started = await this.bridge.codeReviewStart(params);
      this.activeReview = {
        generation,
        sessionId: started.session_id
      };
      if (!this.isCurrent(generation)) {
        await this.cancelActiveReview();
        return cancelledReview(params.session_id, started.job_id);
      }
      if (cancellationRequested) {
        requestCancellation();
        await cancelPromise;
        return cancelledReview(started.session_id, started.job_id, started.scope);
      }

      let consecutiveErrors = 0;
      for (let attempt = 1; attempt <= this.maxPollAttempts; attempt += 1) {
        if (cancellationRequested || token.isCancellationRequested) {
          requestCancellation();
          await cancelPromise;
          this.diagnostics.clear();
          return cancelledReview(started.session_id, started.job_id, started.scope);
        }
        progress.report({ message: `Reviewing changes… (${attempt}/${this.maxPollAttempts})` });
        let result: CodeReviewResult;
        try {
          result = await this.bridge.codeReviewResult(started.job_id);
          consecutiveErrors = 0;
        } catch {
          consecutiveErrors += 1;
          if (consecutiveErrors >= this.maxConsecutiveErrors) {
            throw new Error(
              `Alysis Code could not retrieve code review status after ${this.maxConsecutiveErrors} bounded attempts.`
            );
          }
          await this.waitForNextPoll();
          continue;
        }
        assertReviewScope(result, started);
        if (isCancelled(result)) {
          terminalResultObserved = true;
          this.diagnostics.clear();
          return boundedReviewResult(result, [], 0, false);
        }
        if (isFailed(result)) {
          terminalResultObserved = true;
          throw new Error("Alysis Code code review failed before producing safe findings.");
        }
        if (isCompleted(result)) {
          terminalResultObserved = true;
          if (cancellationRequested || token.isCancellationRequested || !this.isCurrent(generation)) {
            this.diagnostics.clear();
            return cancelledReview(started.session_id, started.job_id, started.scope);
          }
          return this.publishCompleted(result, generation, workspaceRoot);
        }
        if (attempt < this.maxPollAttempts) {
          await this.waitForNextPoll();
        }
      }
      throw new Error(
        `Alysis Code code review did not complete within ${this.maxPollAttempts} bounded status checks.`
      );
    } catch (error) {
      if (started && !terminalResultObserved) {
        await this.cancelActiveReview();
      }
      throw error;
    } finally {
      cancellationDisposable.dispose();
      if (this.activeReview?.generation === generation) {
        this.activeReview = undefined;
      }
    }
  }

  private async publishCompleted(
    result: CodeReviewResult,
    generation: number,
    workspaceRoot?: string
  ): Promise<CodeReviewResult> {
    const projection = await projectFindings(result.findings ?? [], this.source, workspaceRoot);
    const safeResult = boundedReviewResult(
      result,
      projection.findings,
      (result.findings ?? []).length,
      projection.truncated
    );
    if (!this.isCurrent(generation)) {
      return safeResult;
    }
    this.diagnostics.set(projection.entries);
    this.output.appendLine(
      `Alysis Code code review published ${projection.findings.length} safe finding${projection.findings.length === 1 ? "" : "s"} to Problems.`
    );
    return safeResult;
  }

  private async cancelOnce(sessionId: string): Promise<void> {
    try {
      await this.bridge.cancelSession(sessionId);
    } catch {
      this.output.appendLine("Alysis Code code review cancellation could not be confirmed by the bridge.");
    }
  }

  private cancelActiveReview(): Promise<void> | undefined {
    const active = this.activeReview;
    if (!active) {
      return undefined;
    }
    active.cancellation ??= this.cancelOnce(active.sessionId);
    return active.cancellation;
  }

  private async waitForNextPoll(): Promise<void> {
    if (this.pollIntervalMs > 0) {
      await this.sleep(this.pollIntervalMs);
    }
  }

  private isCurrent(generation: number): boolean {
    return !this.disposed && generation === this.generation;
  }

  private assertUsable(): void {
    if (this.disposed) {
      throw new Error("Alysis Code code review presenter is disposed.");
    }
  }
}

interface ProjectedFindings {
  findings: CodeReviewFinding[];
  entries: Array<[vscode.Uri, vscode.Diagnostic[]]>;
  truncated: boolean;
}

async function projectFindings(
  rawFindings: readonly CodeReviewFinding[],
  source: IdeContextSource,
  workspaceRoot?: string
): Promise<ProjectedFindings> {
  const skips: ContextSkip[] = [];
  const availableFolders = source.getWorkspaceFolders();
  const folders = workspaceRoot
    ? availableFolders.filter((folder) => normalizedFsPath(folder.uri.fsPath) === normalizedFsPath(workspaceRoot))
    : availableFolders.length === 1 ? availableFolders : [];
  // A multi-root review must carry an explicit, uniquely resolved session
  // root. Never fall back to folder zero or let a relative finding cross roots.
  if (folders.length !== 1) {
    return { findings: [], entries: [], truncated: rawFindings.length > 0 };
  }
  const boundary = await WorkspaceBoundary.create(source, folders, skips);
  if (!source.isWorkspaceTrusted || !boundary.hasRoots) {
    return { findings: [], entries: [], truncated: rawFindings.length > 0 };
  }
  const byUri = new Map<string, { uri: vscode.Uri; diagnostics: vscode.Diagnostic[] }>();
  const accepted: CodeReviewFinding[] = [];
  const perFile = new Map<string, number>();
  const candidateFindings = rawFindings.slice(0, MAX_FINDINGS);
  for (const finding of candidateFindings) {
    const relativePath = safeRelativeReviewPath(finding.path);
    if (!relativePath) {
      continue;
    }
    const safe = await findSafeReviewPath(relativePath, folders, boundary);
    if (!safe) {
      continue;
    }
    const uriKey = normalizedFsPath(safe.fsPath);
    if (!byUri.has(uriKey) && byUri.size >= MAX_REVIEW_FILES) {
      continue;
    }
    const fileCount = perFile.get(uriKey) ?? 0;
    if (fileCount >= MAX_FINDINGS_PER_FILE) {
      continue;
    }
    const document = await source.openDocument(safe.uri);
    if (!document || document.lineCount < 1) {
      continue;
    }
    const range = boundedReviewRange(finding, document);
    const boundedFinding = boundedFindingForOutput(finding, safe.relativePath, range);
    const diagnostic = new vscode.Diagnostic(
      new vscode.Range(
        range.startLine,
        0,
        range.endLine,
        range.endCharacter
      ),
      diagnosticMessage(boundedFinding),
      diagnosticSeverity(boundedFinding.severity)
    );
    diagnostic.source = "Alysis Code code review";
    diagnostic.code = `alysis-review/${boundedFinding.severity}`;
    const bucket = byUri.get(uriKey) ?? {
      uri: vscode.Uri.file(safe.fsPath),
      diagnostics: []
    };
    bucket.diagnostics.push(diagnostic);
    byUri.set(uriKey, bucket);
    perFile.set(uriKey, fileCount + 1);
    accepted.push(boundedFinding);
  }
  const acceptedAll = accepted.length === rawFindings.length && skips.length === 0;
  return {
    findings: accepted,
    entries: [...byUri.values()].map((item) => [item.uri, item.diagnostics]),
    truncated: !acceptedAll
  };
}

async function findSafeReviewPath(
  relativePath: string,
  folders: readonly ReturnType<IdeContextSource["getWorkspaceFolders"]>[number][],
  boundary: WorkspaceBoundary
) {
  for (const folder of folders) {
    const candidate = path.resolve(folder.uri.fsPath, ...relativePath.split("/"));
    const assessment = await boundary.assessFsPath(candidate);
    if (isSafePath(assessment)) {
      return assessment;
    }
  }
  return undefined;
}

function boundedReviewRange(
  finding: CodeReviewFinding,
  document: ContextDocument
): { startLine: number; endLine: number; endCharacter: number } {
  const maxLine = Math.max(0, Math.min(document.lineCount - 1, 999_999));
  const startLine = clampReviewLine(finding.line_start, maxLine);
  const requestedEnd = clampReviewLine(finding.line_end ?? finding.line_start, maxLine);
  const endLine = Math.max(startLine, Math.min(requestedEnd, startLine + MAX_RANGE_SPAN - 1));
  let endCharacter = 0;
  try {
    endCharacter = Math.max(0, Math.min(document.getLineRange(endLine).end.character, 100_000));
  } catch {
    endCharacter = 0;
  }
  return { startLine, endLine, endCharacter };
}

function clampReviewLine(value: number | null | undefined, maxLine: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    return 0;
  }
  return Math.max(0, Math.min(value - 1, maxLine));
}

function boundedFindingForOutput(
  finding: CodeReviewFinding,
  relativePath: string,
  range: { startLine: number; endLine: number }
): CodeReviewFinding {
  return {
    severity: normalizeSeverity(finding.severity),
    title: boundedRedactedText(finding.title, MAX_TITLE_CHARS, "Review finding"),
    explanation: boundedRedactedText(finding.explanation, MAX_DETAIL_CHARS),
    path: relativePath,
    line_start: range.startLine + 1,
    line_end: range.endLine + 1,
    evidence: boundedRedactedText(finding.evidence, MAX_DETAIL_CHARS),
    suggested_fix: boundedRedactedText(finding.suggested_fix, MAX_DETAIL_CHARS),
    confidence: finding.confidence === "high" || finding.confidence === "low"
      ? finding.confidence
      : "medium"
  };
}

function diagnosticMessage(finding: CodeReviewFinding): string {
  return boundedRedactedText([
    finding.title,
    finding.explanation,
    finding.evidence ? `Evidence: ${finding.evidence}` : "",
    finding.suggested_fix ? `Suggested fix: ${finding.suggested_fix}` : ""
  ].filter(Boolean).join("\n\n"), MAX_DIAGNOSTIC_MESSAGE_CHARS, "Alysis Code code review finding");
}

function diagnosticSeverity(severity: CodeReviewFinding["severity"]): vscode.DiagnosticSeverity {
  switch (severity) {
    case "critical":
    case "high":
      return vscode.DiagnosticSeverity.Error;
    case "medium":
      return vscode.DiagnosticSeverity.Warning;
    case "low":
      return vscode.DiagnosticSeverity.Information;
  }
}

function boundedReviewResult(
  result: CodeReviewResult,
  findings: CodeReviewFinding[],
  rawFindingCount: number,
  ideTruncated: boolean
): CodeReviewResult {
  const warnings = (result.summary?.warnings ?? [])
    .slice(0, MAX_WARNINGS)
    .map((warning) => boundedRedactedText(warning, MAX_WARNING_CHARS))
    .filter(Boolean);
  if (ideTruncated) {
    warnings.push("Some findings were omitted by IDE path, range, or presentation limits.");
  }
  const truncated = result.summary?.truncated === true
    || ideTruncated
    || rawFindingCount > findings.length;
  return {
    session_id: boundedIdentifier(result.session_id),
    job_id: boundedIdentifier(result.job_id),
    status: boundedRedactedText(result.status, 60, "unknown"),
    ...(result.scope ? { scope: result.scope } : {}),
    complete: result.complete === true || isCompleted(result),
    cancelled: result.cancelled === true || isCancelled(result),
    findings,
    ...(result.summary ? {
      summary: {
        verdict: result.summary.verdict,
        overview: boundedRedactedText(result.summary.overview, MAX_DETAIL_CHARS),
        finding_counts: boundedFindingCounts(findings),
        changed_file_count: boundedCount(result.summary.changed_file_count),
        reviewed_file_count: boundedCount(result.summary.reviewed_file_count),
        omitted_file_count: boundedCount(result.summary.omitted_file_count) + Math.max(0, rawFindingCount - findings.length),
        truncated,
        warnings: warnings.slice(0, MAX_WARNINGS)
      }
    } : {}),
    ...(result.diff ? { diff: boundedDiff(result.diff) } : {})
  };
}

function boundedDiff(diff: NonNullable<CodeReviewResult["diff"]>): NonNullable<CodeReviewResult["diff"]> {
  const safePaths = (values: readonly string[]) => values
    .map(safeRelativeReviewPath)
    .filter((value): value is string => Boolean(value))
    .slice(0, MAX_DIFF_PATHS);
  return {
    scope: diff.scope,
    changed_files: safePaths(diff.changed_files),
    included_files: safePaths(diff.included_files),
    omitted_files: diff.omitted_files.slice(0, MAX_DIFF_PATHS).map((item) => ({
      path: safeRelativeReviewPath(item.path) ?? "[redacted path]",
      reason: boundedRedactedText(item.reason, MAX_WARNING_CHARS, "omitted")
    })),
    truncated: diff.truncated === true
      || diff.changed_files.length > MAX_DIFF_PATHS
      || diff.included_files.length > MAX_DIFF_PATHS
      || diff.omitted_files.length > MAX_DIFF_PATHS,
    warnings: diff.warnings.slice(0, MAX_WARNINGS).map((warning) => boundedRedactedText(warning, MAX_WARNING_CHARS)),
    metadata: Object.fromEntries(
      Object.entries(diff.metadata).slice(0, 50).map(([key, value]) => [
        boundedRedactedText(key, 120),
        boundedRedactedText(value, MAX_WARNING_CHARS)
      ])
    ),
    byte_count: boundedCount(diff.byte_count)
  };
}

function safeRelativeReviewPath(value: unknown): string | undefined {
  if (typeof value !== "string" || !value || value.length > 4_096 || value.includes("\0")) {
    return undefined;
  }
  const normalized = value.replaceAll("\\", "/").replace(/^\.\//, "");
  if (
    !normalized
    || normalized.startsWith("/")
    || /^[A-Za-z]:\//.test(normalized)
    || normalized.includes("://")
    || normalized.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    return undefined;
  }
  const platformPath = normalized.split("/").join(path.sep);
  if (isSensitivePath(platformPath)) {
    return undefined;
  }
  return normalized;
}

function normalizedFsPath(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function boundedFindingCounts(findings: readonly CodeReviewFinding[]): Record<string, number> {
  const counts: Record<string, number> = { critical: 0, high: 0, medium: 0, low: 0 };
  for (const finding of findings) {
    counts[finding.severity] += 1;
  }
  return counts;
}

function normalizeSeverity(value: unknown): CodeReviewFinding["severity"] {
  return value === "critical" || value === "high" || value === "low" ? value : "medium";
}

function boundedIdentifier(value: unknown): string {
  return boundedRedactedText(value, 160, "unknown");
}

function boundedRedactedText(value: unknown, maxChars: number, fallback = ""): string {
  const clean = redactForDisplay(typeof value === "string" ? value.replaceAll("\0", "") : "").trim();
  if (!clean) {
    return fallback;
  }
  return clean.length <= maxChars ? clean : `${clean.slice(0, Math.max(0, maxChars - 1))}…`;
}

function boundedCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value)
    ? Math.max(0, Math.min(Math.trunc(value), 1_000_000))
    : 0;
}

function boundedInteger(value: number, min: number, max: number, label: string): number {
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    throw new Error(`Code review ${label} limit must be between ${min} and ${max}.`);
  }
  return value;
}

/**
 * Bound and redact anything that leaves this presenter. Bridge rejections carry backend-formatted
 * text (paths, provider detail) and the caller renders the message directly to the user.
 */
function safeReviewError(error: unknown): Error {
  const message = boundedRedactedText(
    error instanceof Error ? error.message : "",
    MAX_WARNING_CHARS,
    "Alysis Code code review failed safely."
  );
  const code = error instanceof Error && "code" in error && typeof (error as { code?: unknown }).code === "string"
    ? boundedRedactedText((error as { code: string }).code, 80)
    : "";
  return new Error(code ? `Alysis Code code review failed (${code}). ${message}` : message);
}

function assertReviewScope(result: CodeReviewResult, started: CodeReviewStartResult): void {
  if (result.job_id !== started.job_id || result.session_id !== started.session_id) {
    throw new Error("Alysis Code code review response scope verification failed.");
  }
}

function isCompleted(result: CodeReviewResult): boolean {
  return result.complete === true || ["completed", "succeeded", "success"].includes(result.status.toLowerCase());
}

function isCancelled(result: CodeReviewResult): boolean {
  return result.cancelled === true || ["cancelled", "canceled"].includes(result.status.toLowerCase());
}

function isFailed(result: CodeReviewResult): boolean {
  return ["failed", "error"].includes(result.status.toLowerCase());
}

function cancelledReview(
  sessionId: string,
  jobId: string,
  scope?: CodeReviewStartResult["scope"]
): CodeReviewResult {
  return {
    session_id: boundedIdentifier(sessionId),
    job_id: boundedIdentifier(jobId),
    status: "cancelled",
    ...(scope ? { scope } : {}),
    complete: true,
    cancelled: true,
    findings: []
  };
}
