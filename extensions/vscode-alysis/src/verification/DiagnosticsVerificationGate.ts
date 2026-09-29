import { createHash } from "node:crypto";

import { redactContextSecrets } from "../context/secretRedaction";
import { diagnosticVerificationFingerprint } from "./diagnosticFingerprint";

export type VerificationDiagnosticSeverity = "error" | "warning" | "information" | "hint";

export interface VerificationPosition {
  readonly line: number;
  readonly character: number;
}

export interface VerificationRange {
  readonly start: VerificationPosition;
  readonly end: VerificationPosition;
}

export interface VerificationDiagnostic {
  readonly uri: string;
  readonly range: VerificationRange;
  readonly severity: VerificationDiagnosticSeverity;
  readonly message: string;
  readonly source?: string;
  readonly code?: string | number;
}

export interface DiagnosticReadResult {
  /** Diagnostics retained by the source, up to the requested limit. */
  readonly diagnostics: readonly VerificationDiagnostic[];
  /** Exact number observed before retention limits were applied. */
  readonly totalCount: number;
  readonly truncated: boolean;
}

export interface DiagnosticsVerificationScope {
  /** Exact workspace root owned by the chat session being verified. */
  readonly workspaceRoot: string;
}

export interface VerificationDisposable {
  dispose(): void;
}

/** Runtime-independent seam implemented with public VS Code diagnostic APIs. */
export interface DiagnosticsVerificationSource {
  readDiagnostics(maxItems: number, scope?: DiagnosticsVerificationScope): DiagnosticReadResult;
  onDidChangeDiagnostics(listener: () => void): VerificationDisposable;
}

export interface DiagnosticCounts {
  readonly error: number;
  readonly warning: number;
  readonly information: number;
  readonly hint: number;
  readonly total: number;
}

export interface DiagnosticBaseline {
  readonly schemaVersion: 1;
  readonly capturedAt: string;
  readonly counts: DiagnosticCounts;
  readonly totalCount: number;
  readonly truncated: boolean;
  readonly sourceError: boolean;
  /** Opaque hashes form a multiset; raw baseline messages are never retained. */
  readonly fingerprints: readonly string[];
  /** Opaque host-local identities for diagnostics actually attached to this job. */
  readonly requiredFingerprints: readonly string[];
  readonly scope?: DiagnosticsVerificationScope;
  readonly snapshotHash: string;
}

export interface VerificationTestCommandMetadata {
  readonly label?: string;
  readonly command?: string;
  readonly cwd?: string;
  /** Names only. Environment values must never be supplied to this module. */
  readonly environmentKeys?: readonly string[];
}

export interface ReportedTestCommandMetadata {
  readonly configured: boolean;
  /** This gate records metadata and deliberately never invokes a shell. */
  readonly execution: "not-run";
  readonly label?: string;
  readonly command?: string;
  readonly cwd?: string;
  readonly environmentKeys?: readonly string[];
}

export interface ReportedDiagnostic extends VerificationDiagnostic {
  readonly id: string;
  readonly code?: string;
}

export type VerificationStatus =
  | "passed"
  | "failed"
  | "timed-out"
  | "cancelled"
  | "incomplete"
  | "unavailable";

export interface DiagnosticsVerificationReport {
  readonly schemaVersion: 1;
  readonly status: VerificationStatus;
  readonly blocksCompletion: boolean;
  readonly reason: string;
  readonly startedAt: string;
  readonly completedAt: string;
  readonly settled: boolean;
  readonly settleDurationMs: number;
  readonly baseline: {
    readonly capturedAt: string;
    readonly counts: DiagnosticCounts;
    readonly totalCount: number;
    readonly truncated: boolean;
  };
  readonly current: {
    readonly counts: DiagnosticCounts;
    readonly totalCount: number;
    readonly truncated: boolean;
  };
  readonly introducedCounts: DiagnosticCounts;
  readonly preExistingCounts: DiagnosticCounts;
  readonly resolvedCount: number;
  readonly requiredAttachedCount: number;
  readonly clearedAttachedCount: number;
  readonly remainingAttachedCount: number;
  readonly introduced: readonly ReportedDiagnostic[];
  readonly preExisting: readonly ReportedDiagnostic[];
  readonly omittedDiagnostics: number;
  readonly reportTruncated: boolean;
  readonly testCommand: ReportedTestCommandMetadata;
}

export interface DiagnosticsVerificationGateLimits {
  readonly maxDiagnosticsRead: number;
  readonly maxReportedDiagnostics: number;
  readonly maxMessageBytes: number;
  readonly maxUriBytes: number;
  readonly maxMetadataFieldBytes: number;
  readonly maxEnvironmentKeys: number;
  readonly maxReportBytes: number;
}

export interface DiagnosticsVerificationGateOptions {
  readonly limits?: Partial<DiagnosticsVerificationGateLimits>;
  readonly redact?: (value: string) => string;
  readonly now?: () => Date;
}

export interface VerifyAfterChangesRequest {
  readonly settleMs?: number;
  readonly timeoutMs?: number;
  readonly signal?: AbortSignal;
  readonly testCommand?: VerificationTestCommandMetadata;
}

const DEFAULT_LIMITS: DiagnosticsVerificationGateLimits = {
  maxDiagnosticsRead: 2_000,
  maxReportedDiagnostics: 100,
  maxMessageBytes: 2_048,
  maxUriBytes: 1_024,
  maxMetadataFieldBytes: 2_048,
  maxEnvironmentKeys: 32,
  maxReportBytes: 128 * 1_024
};

const DEFAULT_SETTLE_MS = 350;
const DEFAULT_TIMEOUT_MS = 15_000;

interface Snapshot {
  readonly diagnostics: readonly VerificationDiagnostic[];
  readonly fingerprints: readonly string[];
  readonly counts: DiagnosticCounts;
  readonly totalCount: number;
  readonly truncated: boolean;
}

type SettleOutcome = "settled" | "timed-out" | "cancelled" | "unavailable";

interface SettledSnapshot {
  readonly outcome: SettleOutcome;
  readonly snapshot: Snapshot;
  readonly durationMs: number;
}

/**
 * Compares post-change diagnostics with a pre-change baseline. The gate is
 * deliberately fail-closed: a timeout, cancellation, provider failure, or a
 * bounded/truncated read can never be presented as a successful verification.
 */
export class DiagnosticsVerificationGate {
  private readonly limits: DiagnosticsVerificationGateLimits;
  private readonly redact: (value: string) => string;
  private readonly now: () => Date;

  public constructor(
    private readonly source: DiagnosticsVerificationSource,
    options: DiagnosticsVerificationGateOptions = {}
  ) {
    this.limits = normalizeLimits(options.limits);
    this.redact = options.redact ?? redactContextSecrets;
    this.now = options.now ?? (() => new Date());
  }

  public captureBaseline(
    requiredFingerprints: readonly string[] = [],
    scope?: DiagnosticsVerificationScope
  ): DiagnosticBaseline {
    const capturedAt = this.now().toISOString();
    const required = normalizeRequiredFingerprints(requiredFingerprints);
    const normalizedScope = normalizeScope(scope);
    try {
      const snapshot = this.readSnapshot(normalizedScope);
      return {
        schemaVersion: 1,
        capturedAt,
        counts: snapshot.counts,
        totalCount: snapshot.totalCount,
        truncated: snapshot.truncated,
        sourceError: false,
        fingerprints: snapshot.fingerprints,
        requiredFingerprints: required,
        ...(normalizedScope ? { scope: normalizedScope } : {}),
        snapshotHash: hashBaseline(snapshot.fingerprints, required, normalizedScope)
      };
    } catch {
      return {
        schemaVersion: 1,
        capturedAt,
        counts: emptyCounts(),
        totalCount: 0,
        truncated: false,
        sourceError: true,
        fingerprints: [],
        requiredFingerprints: required,
        ...(normalizedScope ? { scope: normalizedScope } : {}),
        snapshotHash: hashBaseline([], required, normalizedScope)
      };
    }
  }

  public async verifyAfterChanges(
    baseline: DiagnosticBaseline,
    request: VerifyAfterChangesRequest = {}
  ): Promise<DiagnosticsVerificationReport> {
    const startedAtDate = this.now();
    const startedAt = startedAtDate.toISOString();
    const settleMs = clampInteger(request.settleMs ?? DEFAULT_SETTLE_MS, 0, 60_000);
    const timeoutMs = Math.max(
      settleMs,
      clampInteger(request.timeoutMs ?? DEFAULT_TIMEOUT_MS, 1, 5 * 60_000)
    );

    let settled: SettledSnapshot;
    if (!isValidBaseline(baseline)) {
      settled = {
        outcome: "unavailable",
        snapshot: emptySnapshot(),
        durationMs: 0
      };
    } else {
      settled = await this.waitForSettledSnapshot(
        settleMs,
        timeoutMs,
        request.signal,
        startedAtDate,
        baseline.scope
      );
    }

    const comparison = compareSnapshots(baseline, settled.snapshot);
    const disposition = dispositionFor(baseline, settled, comparison);
    const completedAt = this.now().toISOString();
    return this.buildBoundedReport({
      baseline,
      settled,
      comparison,
      disposition,
      startedAt,
      completedAt,
      testCommand: request.testCommand
    });
  }

  private readSnapshot(scope?: DiagnosticsVerificationScope): Snapshot {
    const raw = this.source.readDiagnostics(this.limits.maxDiagnosticsRead, scope);
    const retained = raw.diagnostics.slice(0, this.limits.maxDiagnosticsRead).map(normalizeDiagnostic);
    const totalCount = Math.max(retained.length, nonNegativeInteger(raw.totalCount));
    const truncated = raw.truncated || raw.diagnostics.length > retained.length || totalCount > retained.length;
    const fingerprints = retained.map(diagnosticVerificationFingerprint);
    return {
      diagnostics: retained,
      fingerprints,
      counts: countDiagnostics(retained),
      totalCount,
      truncated
    };
  }

  private waitForSettledSnapshot(
    settleMs: number,
    timeoutMs: number,
    signal: AbortSignal | undefined,
    startedAt: Date,
    scope?: DiagnosticsVerificationScope
  ): Promise<SettledSnapshot> {
    return new Promise((resolve) => {
      let current = emptySnapshot();
      let finished = false;
      let quietTimer: ReturnType<typeof setTimeout> | undefined;
      let timeoutTimer: ReturnType<typeof setTimeout> | undefined;
      let subscription: VerificationDisposable | undefined;

      const duration = (): number => Math.max(0, this.now().getTime() - startedAt.getTime());
      const cleanup = (): void => {
        if (quietTimer !== undefined) {
          clearTimeout(quietTimer);
        }
        if (timeoutTimer !== undefined) {
          clearTimeout(timeoutTimer);
        }
        subscription?.dispose();
        signal?.removeEventListener("abort", onAbort);
      };
      const finish = (outcome: SettleOutcome): void => {
        if (finished) {
          return;
        }
        finished = true;
        cleanup();
        resolve({ outcome, snapshot: current, durationMs: duration() });
      };
      const sample = (): boolean => {
        try {
          current = this.readSnapshot(scope);
          return true;
        } catch {
          finish("unavailable");
          return false;
        }
      };
      const armQuietTimer = (): void => {
        if (quietTimer !== undefined) {
          clearTimeout(quietTimer);
        }
        quietTimer = setTimeout(() => {
          if (sample()) {
            finish("settled");
          }
        }, settleMs);
      };
      const onChange = (): void => {
        if (!finished && sample()) {
          armQuietTimer();
        }
      };
      const onAbort = (): void => finish("cancelled");

      try {
        subscription = this.source.onDidChangeDiagnostics(onChange);
      } catch {
        finish("unavailable");
        return;
      }
      if (!sample()) {
        return;
      }
      if (signal?.aborted) {
        finish("cancelled");
        return;
      }
      signal?.addEventListener("abort", onAbort, { once: true });
      timeoutTimer = setTimeout(() => {
        if (sample()) {
          finish("timed-out");
        }
      }, timeoutMs);
      armQuietTimer();
    });
  }

  private buildBoundedReport(input: {
    readonly baseline: DiagnosticBaseline;
    readonly settled: SettledSnapshot;
    readonly comparison: DiagnosticComparison;
    readonly disposition: VerificationDisposition;
    readonly startedAt: string;
    readonly completedAt: string;
    readonly testCommand?: VerificationTestCommandMetadata;
  }): DiagnosticsVerificationReport {
    let testCommand = this.reportTestCommand(input.testCommand);
    const allCandidates: Array<{ bucket: "introduced" | "preExisting"; diagnostic: VerificationDiagnostic }> = [
      ...sortDiagnostics(input.comparison.introduced).map((diagnostic) => ({ bucket: "introduced" as const, diagnostic })),
      ...sortDiagnostics(input.comparison.preExisting).map((diagnostic) => ({ bucket: "preExisting" as const, diagnostic }))
    ];
    const totalCandidates = input.settled.snapshot.totalCount;
    const introduced: ReportedDiagnostic[] = [];
    const preExisting: ReportedDiagnostic[] = [];
    let omittedDiagnostics = totalCandidates;
    let reportTruncated = input.settled.snapshot.truncated || input.baseline.truncated;

    const createReport = (): DiagnosticsVerificationReport => ({
      schemaVersion: 1,
      status: input.disposition.status,
      blocksCompletion: input.disposition.blocksCompletion,
      reason: input.disposition.reason,
      startedAt: input.startedAt,
      completedAt: input.completedAt,
      settled: input.settled.outcome === "settled",
      settleDurationMs: input.settled.durationMs,
      baseline: {
        capturedAt: input.baseline.capturedAt,
        counts: input.baseline.counts,
        totalCount: input.baseline.totalCount,
        truncated: input.baseline.truncated
      },
      current: {
        counts: input.settled.snapshot.counts,
        totalCount: input.settled.snapshot.totalCount,
        truncated: input.settled.snapshot.truncated
      },
      introducedCounts: input.comparison.introducedCounts,
      preExistingCounts: input.comparison.preExistingCounts,
      resolvedCount: input.comparison.resolvedCount,
      requiredAttachedCount: input.comparison.requiredAttachedCount,
      clearedAttachedCount: input.comparison.clearedAttachedCount,
      remainingAttachedCount: input.comparison.remainingAttachedCount,
      introduced,
      preExisting,
      omittedDiagnostics,
      reportTruncated,
      testCommand
    });

    if (byteLength(createReport()) > this.limits.maxReportBytes) {
      // Command metadata is advisory. Preserve the important configured/not-run
      // signal while dropping optional display fields before diagnostic data.
      testCommand = { configured: testCommand.configured, execution: "not-run" };
      reportTruncated = true;
    }

    for (const candidate of allCandidates.slice(0, this.limits.maxReportedDiagnostics)) {
      const reported = this.reportDiagnostic(candidate.diagnostic);
      const target = candidate.bucket === "introduced" ? introduced : preExisting;
      target.push(reported);
      omittedDiagnostics -= 1;
      reportTruncated = omittedDiagnostics > 0 || input.settled.snapshot.truncated || input.baseline.truncated;
      if (byteLength(createReport()) > this.limits.maxReportBytes) {
        target.pop();
        omittedDiagnostics += 1;
        reportTruncated = true;
        break;
      }
    }

    reportTruncated = reportTruncated || omittedDiagnostics > 0;
    return createReport();
  }

  private reportDiagnostic(diagnostic: VerificationDiagnostic): ReportedDiagnostic {
    return {
      id: diagnosticVerificationFingerprint(diagnostic).slice(0, 16),
      uri: truncateUtf8(this.redact(diagnostic.uri), this.limits.maxUriBytes),
      range: diagnostic.range,
      severity: diagnostic.severity,
      message: truncateUtf8(this.redact(diagnostic.message), this.limits.maxMessageBytes),
      ...(diagnostic.source
        ? { source: truncateUtf8(this.redact(diagnostic.source), this.limits.maxMetadataFieldBytes) }
        : {}),
      ...(diagnostic.code !== undefined
        ? { code: truncateUtf8(this.redact(String(diagnostic.code)), this.limits.maxMetadataFieldBytes) }
        : {})
    };
  }

  private reportTestCommand(metadata: VerificationTestCommandMetadata | undefined): ReportedTestCommandMetadata {
    if (!metadata) {
      return { configured: false, execution: "not-run" };
    }
    const field = (value: string): string =>
      truncateUtf8(this.redact(value), this.limits.maxMetadataFieldBytes);
    return {
      configured: true,
      execution: "not-run",
      ...(metadata.label ? { label: field(metadata.label) } : {}),
      ...(metadata.command ? { command: field(metadata.command) } : {}),
      ...(metadata.cwd ? { cwd: field(metadata.cwd) } : {}),
      ...(metadata.environmentKeys
        ? { environmentKeys: metadata.environmentKeys.slice(0, this.limits.maxEnvironmentKeys).map(field) }
        : {})
    };
  }
}

interface DiagnosticComparison {
  readonly introduced: readonly VerificationDiagnostic[];
  readonly preExisting: readonly VerificationDiagnostic[];
  readonly introducedCounts: DiagnosticCounts;
  readonly preExistingCounts: DiagnosticCounts;
  readonly resolvedCount: number;
  readonly requiredAttachedCount: number;
  readonly clearedAttachedCount: number;
  readonly remainingAttachedCount: number;
}

interface VerificationDisposition {
  readonly status: VerificationStatus;
  readonly blocksCompletion: boolean;
  readonly reason: string;
}

function compareSnapshots(baseline: DiagnosticBaseline, current: Snapshot): DiagnosticComparison {
  const remaining = new Map<string, number>();
  for (const fingerprint of baseline.fingerprints) {
    remaining.set(fingerprint, (remaining.get(fingerprint) ?? 0) + 1);
  }
  const introduced: VerificationDiagnostic[] = [];
  const preExisting: VerificationDiagnostic[] = [];
  current.diagnostics.forEach((diagnostic, index) => {
    const fingerprint = current.fingerprints[index];
    const available = remaining.get(fingerprint) ?? 0;
    if (available > 0) {
      preExisting.push(diagnostic);
      remaining.set(fingerprint, available - 1);
    } else {
      introduced.push(diagnostic);
    }
  });
  const resolvedCount = [...remaining.values()].reduce((sum, value) => sum + value, 0);
  const currentCounts = fingerprintCounts(current.fingerprints);
  const requiredCounts = fingerprintCounts(baseline.requiredFingerprints);
  let remainingAttachedCount = 0;
  for (const [fingerprint, requiredCount] of requiredCounts) {
    remainingAttachedCount += Math.min(requiredCount, currentCounts.get(fingerprint) ?? 0);
  }
  const requiredAttachedCount = baseline.requiredFingerprints.length;
  return {
    introduced,
    preExisting,
    introducedCounts: countDiagnostics(introduced),
    preExistingCounts: countDiagnostics(preExisting),
    resolvedCount,
    requiredAttachedCount,
    clearedAttachedCount: requiredAttachedCount - remainingAttachedCount,
    remainingAttachedCount
  };
}

function dispositionFor(
  baseline: DiagnosticBaseline,
  settled: SettledSnapshot,
  comparison: DiagnosticComparison
): VerificationDisposition {
  if (baseline.sourceError || settled.outcome === "unavailable") {
    return { status: "unavailable", blocksCompletion: true, reason: "Diagnostics were unavailable." };
  }
  if (settled.outcome === "cancelled") {
    return { status: "cancelled", blocksCompletion: true, reason: "Verification was cancelled." };
  }
  if (settled.outcome === "timed-out") {
    return { status: "timed-out", blocksCompletion: true, reason: "Diagnostics did not settle before timeout." };
  }
  if (baseline.truncated || settled.snapshot.truncated) {
    return { status: "incomplete", blocksCompletion: true, reason: "The bounded diagnostic snapshot was incomplete." };
  }
  if (comparison.remainingAttachedCount > 0) {
    return {
      status: "failed",
      blocksCompletion: true,
      reason: `Verification found ${comparison.remainingAttachedCount} attached diagnostic${comparison.remainingAttachedCount === 1 ? "" : "s"} still present.`
    };
  }
  if (comparison.introducedCounts.error > 0) {
    return {
      status: "failed",
      blocksCompletion: true,
      reason: `Verification found ${comparison.introducedCounts.error} introduced error${comparison.introducedCounts.error === 1 ? "" : "s"}.`
    };
  }
  if (comparison.requiredAttachedCount > 0) {
    return {
      status: "passed",
      blocksCompletion: false,
      reason: `All ${comparison.requiredAttachedCount} attached diagnostic${comparison.requiredAttachedCount === 1 ? " was" : "s were"} cleared and no new diagnostic errors were introduced.`
    };
  }
  return { status: "passed", blocksCompletion: false, reason: "No new diagnostic errors were introduced." };
}

function normalizeDiagnostic(diagnostic: VerificationDiagnostic): VerificationDiagnostic {
  return {
    uri: String(diagnostic.uri),
    range: {
      start: normalizePosition(diagnostic.range.start),
      end: normalizePosition(diagnostic.range.end)
    },
    severity: normalizeSeverity(diagnostic.severity),
    message: String(diagnostic.message),
    ...(diagnostic.source !== undefined ? { source: String(diagnostic.source) } : {}),
    ...(diagnostic.code !== undefined ? { code: String(diagnostic.code) } : {})
  };
}

function normalizePosition(position: VerificationPosition): VerificationPosition {
  return {
    line: nonNegativeInteger(position.line),
    character: nonNegativeInteger(position.character)
  };
}

function normalizeSeverity(severity: VerificationDiagnosticSeverity): VerificationDiagnosticSeverity {
  return severity === "error" || severity === "warning" || severity === "information" || severity === "hint"
    ? severity
    : "error";
}

function hashBaseline(
  fingerprints: readonly string[],
  requiredFingerprints: readonly string[],
  scope?: DiagnosticsVerificationScope
): string {
  return createHash("sha256").update(JSON.stringify({
    snapshot: [...fingerprints].sort(),
    required: [...requiredFingerprints].sort(),
    workspaceRoot: scope?.workspaceRoot ?? null
  })).digest("hex");
}

function normalizeScope(scope: DiagnosticsVerificationScope | undefined): DiagnosticsVerificationScope | undefined {
  const workspaceRoot = typeof scope?.workspaceRoot === "string" ? scope.workspaceRoot.trim() : "";
  return workspaceRoot ? { workspaceRoot } : undefined;
}

function normalizeRequiredFingerprints(fingerprints: readonly string[]): string[] {
  return fingerprints
    .map((fingerprint) => String(fingerprint).toLowerCase())
    .filter((fingerprint) => /^[0-9a-f]{64}$/.test(fingerprint));
}

function fingerprintCounts(fingerprints: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const fingerprint of fingerprints) {
    counts.set(fingerprint, (counts.get(fingerprint) ?? 0) + 1);
  }
  return counts;
}

function isValidBaseline(baseline: DiagnosticBaseline): boolean {
  const severityTotal = baseline.counts.error
    + baseline.counts.warning
    + baseline.counts.information
    + baseline.counts.hint;
  return baseline.schemaVersion === 1
    && !baseline.sourceError
    && Number.isFinite(Date.parse(baseline.capturedAt))
    && baseline.counts.total === severityTotal
    && baseline.counts.total === baseline.fingerprints.length
    && baseline.totalCount >= baseline.fingerprints.length
    && (baseline.truncated || baseline.totalCount === baseline.fingerprints.length)
    && baseline.fingerprints.every((fingerprint) => /^[0-9a-f]{64}$/.test(fingerprint))
    && Array.isArray(baseline.requiredFingerprints)
    && baseline.requiredFingerprints.every((fingerprint) => /^[0-9a-f]{64}$/.test(fingerprint))
    && isValidScope(baseline.scope)
    && baseline.snapshotHash === hashBaseline(baseline.fingerprints, baseline.requiredFingerprints, baseline.scope);
}

function isValidScope(scope: unknown): scope is DiagnosticsVerificationScope | undefined {
  if (scope === undefined) {
    return true;
  }
  if (typeof scope !== "object" || scope === null) {
    return false;
  }
  const workspaceRoot = (scope as { workspaceRoot?: unknown }).workspaceRoot;
  return typeof workspaceRoot === "string"
    && normalizeScope({ workspaceRoot })?.workspaceRoot === workspaceRoot;
}

function countDiagnostics(diagnostics: readonly VerificationDiagnostic[]): DiagnosticCounts {
  const counts = { error: 0, warning: 0, information: 0, hint: 0, total: diagnostics.length };
  for (const diagnostic of diagnostics) {
    counts[diagnostic.severity] += 1;
  }
  return counts;
}

function emptyCounts(): DiagnosticCounts {
  return { error: 0, warning: 0, information: 0, hint: 0, total: 0 };
}

function emptySnapshot(): Snapshot {
  return {
    diagnostics: [],
    fingerprints: [],
    counts: emptyCounts(),
    totalCount: 0,
    truncated: false
  };
}

function sortDiagnostics(diagnostics: readonly VerificationDiagnostic[]): VerificationDiagnostic[] {
  const priority: Record<VerificationDiagnosticSeverity, number> = {
    error: 0,
    warning: 1,
    information: 2,
    hint: 3
  };
  return [...diagnostics].sort((left, right) =>
    priority[left.severity] - priority[right.severity]
    || left.uri.localeCompare(right.uri)
    || left.range.start.line - right.range.start.line
    || left.range.start.character - right.range.start.character
    || left.message.localeCompare(right.message)
  );
}

function normalizeLimits(overrides: Partial<DiagnosticsVerificationGateLimits> | undefined): DiagnosticsVerificationGateLimits {
  return {
    maxDiagnosticsRead: clampInteger(overrides?.maxDiagnosticsRead ?? DEFAULT_LIMITS.maxDiagnosticsRead, 1, 100_000),
    maxReportedDiagnostics: clampInteger(
      overrides?.maxReportedDiagnostics ?? DEFAULT_LIMITS.maxReportedDiagnostics,
      0,
      10_000
    ),
    maxMessageBytes: clampInteger(overrides?.maxMessageBytes ?? DEFAULT_LIMITS.maxMessageBytes, 16, 64 * 1_024),
    maxUriBytes: clampInteger(overrides?.maxUriBytes ?? DEFAULT_LIMITS.maxUriBytes, 16, 16 * 1_024),
    maxMetadataFieldBytes: clampInteger(
      overrides?.maxMetadataFieldBytes ?? DEFAULT_LIMITS.maxMetadataFieldBytes,
      16,
      64 * 1_024
    ),
    maxEnvironmentKeys: clampInteger(overrides?.maxEnvironmentKeys ?? DEFAULT_LIMITS.maxEnvironmentKeys, 0, 1_000),
    maxReportBytes: clampInteger(overrides?.maxReportBytes ?? DEFAULT_LIMITS.maxReportBytes, 2_048, 4 * 1_024 * 1_024)
  };
}

function clampInteger(value: number, minimum: number, maximum: number): number {
  if (!Number.isFinite(value)) {
    return minimum;
  }
  return Math.min(maximum, Math.max(minimum, Math.floor(value)));
}

function nonNegativeInteger(value: number): number {
  return clampInteger(value, 0, Number.MAX_SAFE_INTEGER);
}

function truncateUtf8(value: string, maxBytes: number): string {
  if (Buffer.byteLength(value, "utf8") <= maxBytes) {
    return value;
  }
  const suffix = "…";
  const suffixBytes = Buffer.byteLength(suffix, "utf8");
  if (maxBytes <= suffixBytes) {
    return "";
  }
  let low = 0;
  let high = value.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    const candidate = value.slice(0, middle);
    if (Buffer.byteLength(candidate, "utf8") + suffixBytes <= maxBytes) {
      low = middle;
    } else {
      high = middle - 1;
    }
  }
  return `${value.slice(0, low)}${suffix}`;
}

function byteLength(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value), "utf8");
}
