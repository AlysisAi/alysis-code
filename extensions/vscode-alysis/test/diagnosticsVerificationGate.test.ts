import assert from "node:assert/strict";
import test from "node:test";

import {
  DiagnosticReadResult,
  DiagnosticsVerificationGate,
  DiagnosticsVerificationSource,
  VerificationDiagnostic,
  VerificationDisposable
} from "../src/verification/DiagnosticsVerificationGate";

test("a settled diagnostic-free workspace passes without blocking completion", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();

  const report = await gate.verifyAfterChanges(baseline, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "passed");
  assert.equal(report.blocksCompletion, false);
  assert.equal(report.settled, true);
  assert.deepEqual(report.introduced, []);
  assert.deepEqual(report.current.counts, counts());
  assert.equal(source.listenerCount, 0);
});

test("diagnostic verification preserves one active-root scope from baseline through settled reads", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline([], { workspaceRoot: "/workspace/primary" });

  assert.deepEqual(baseline.scope, { workspaceRoot: "/workspace/primary" });
  assert.deepEqual(source.lastScope, { workspaceRoot: "/workspace/primary" });
  source.lastScope = undefined;
  const report = await gate.verifyAfterChanges(baseline, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "passed");
  assert.deepEqual(source.lastScope, { workspaceRoot: "/workspace/primary" });
});

test("a newly introduced error blocks completion", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  source.diagnostics = [diagnostic("error", "New type error")];

  const report = await gate.verifyAfterChanges(baseline, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "failed");
  assert.equal(report.blocksCompletion, true);
  assert.equal(report.introducedCounts.error, 1);
  assert.equal(report.introduced[0].message, "New type error");
  assert.equal(report.preExistingCounts.total, 0);
});

test("unattached pre-existing errors remain distinct and do not block a clean change", async () => {
  const source = new FakeDiagnosticsSource();
  const oldError = diagnostic("error", "Already broken");
  source.diagnostics = [oldError, oldError, diagnostic("warning", "Will be resolved", 2)];
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  assert.equal(JSON.stringify(baseline).includes("Already broken"), false);

  source.diagnostics = [oldError, diagnostic("warning", "New warning", 3)];
  const report = await gate.verifyAfterChanges(baseline, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "passed");
  assert.equal(report.blocksCompletion, false);
  assert.equal(report.preExistingCounts.error, 1);
  assert.equal(report.introducedCounts.warning, 1);
  assert.equal(report.introducedCounts.error, 0);
  assert.equal(report.resolvedCount, 2);
  assert.equal(report.preExisting[0].message, "Already broken");
});

test("an attached diagnostic blocks completion while its exact fingerprint remains", async () => {
  const source = new FakeDiagnosticsSource();
  const attached = diagnostic("error", "Fix this attached error");
  source.diagnostics = [attached];
  const gate = new DiagnosticsVerificationGate(source);
  const observed = gate.captureBaseline();
  const baseline = gate.captureBaseline([observed.fingerprints[0]]);

  const report = await gate.verifyAfterChanges(baseline, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "failed");
  assert.equal(report.blocksCompletion, true);
  assert.equal(report.requiredAttachedCount, 1);
  assert.equal(report.clearedAttachedCount, 0);
  assert.equal(report.remainingAttachedCount, 1);
  assert.match(report.reason, /attached diagnostic.*still present/i);
});

test("an attached diagnostic must be observed cleared before completion passes", async () => {
  const source = new FakeDiagnosticsSource();
  const attached = diagnostic("error", "Fix this attached error");
  source.diagnostics = [attached];
  const gate = new DiagnosticsVerificationGate(source);
  const observed = gate.captureBaseline();
  const baseline = gate.captureBaseline([observed.fingerprints[0]]);
  source.diagnostics = [];

  const report = await gate.verifyAfterChanges(baseline, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "passed");
  assert.equal(report.blocksCompletion, false);
  assert.equal(report.requiredAttachedCount, 1);
  assert.equal(report.clearedAttachedCount, 1);
  assert.equal(report.remainingAttachedCount, 0);
  assert.match(report.reason, /attached diagnostic was cleared/i);
});

test("diagnostic change events reset the quiet period before the final sample", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  const verification = gate.verifyAfterChanges(baseline, { settleMs: 20, timeoutMs: 150 });
  setTimeout(() => {
    source.diagnostics = [diagnostic("error", "Late provider result")];
    source.emitChange();
  }, 8);

  const report = await verification;

  assert.equal(report.status, "failed");
  assert.equal(report.introduced[0].message, "Late provider result");
  assert.equal(source.listenerCount, 0);
});

test("continuous provider activity times out and fails closed", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  const interval = setInterval(() => source.emitChange(), 5);
  try {
    const report = await gate.verifyAfterChanges(baseline, { settleMs: 25, timeoutMs: 40 });
    assert.equal(report.status, "timed-out");
    assert.equal(report.blocksCompletion, true);
    assert.equal(report.settled, false);
  } finally {
    clearInterval(interval);
  }
  assert.equal(source.listenerCount, 0);
});

test("cancellation is prompt, deterministic, and releases the event subscription", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  const controller = new AbortController();
  const verification = gate.verifyAfterChanges(baseline, {
    settleMs: 1_000,
    timeoutMs: 2_000,
    signal: controller.signal
  });
  controller.abort();

  const report = await verification;

  assert.equal(report.status, "cancelled");
  assert.equal(report.blocksCompletion, true);
  assert.equal(report.settled, false);
  assert.equal(source.listenerCount, 0);
});

test("diagnostic and test-command metadata are redacted and are never executed", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  source.diagnostics = [{
    ...diagnostic("error", "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"),
    uri: "file:///workspace/token=Abcdef1234567890_SecretValue987654321",
    source: "token=Abcdef1234567890_SecretValue987654321",
    code: "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
  }];

  const report = await gate.verifyAfterChanges(baseline, {
    settleMs: 5,
    timeoutMs: 100,
    testCommand: {
      label: "Secret test",
      command: "npm test --token sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
      cwd: "C:/workspace?password=Abcdef1234567890_SecretValue987654321",
      environmentKeys: ["CI", "API_TOKEN"]
    }
  });
  const encoded = JSON.stringify(report);

  assert.equal(report.testCommand.configured, true);
  assert.equal(report.testCommand.execution, "not-run");
  assert.deepEqual(report.testCommand.environmentKeys, ["CI", "API_TOKEN"]);
  assert.match(encoded, /<redacted>/);
  assert.doesNotMatch(encoded, /abcdefghijklmnopqrstuvwxyz123456|ABCDEFGHIJKLMNOPQRSTUVWXYZ123456|SecretValue/);
});

test("diagnostic reads, fields, item samples, and the final report are bounded", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source, {
    limits: {
      maxDiagnosticsRead: 5,
      maxReportedDiagnostics: 2,
      maxMessageBytes: 48,
      maxUriBytes: 48,
      maxMetadataFieldBytes: 48,
      maxEnvironmentKeys: 2,
      maxReportBytes: 2_048
    }
  });
  const baseline = gate.captureBaseline();
  source.diagnostics = Array.from({ length: 25 }, (_, index) => ({
    ...diagnostic("error", `${index}:${"problem".repeat(1_000)}`, index),
    uri: `file:///workspace/${"deep/".repeat(1_000)}file-${index}.ts`
  }));

  const report = await gate.verifyAfterChanges(baseline, {
    settleMs: 5,
    timeoutMs: 100,
    testCommand: {
      command: "x".repeat(10_000),
      environmentKeys: ["ONE", "TWO", "THREE"]
    }
  });

  assert.equal(source.lastReadLimit, 5);
  assert.equal(report.status, "incomplete");
  assert.equal(report.blocksCompletion, true);
  assert.equal(report.current.totalCount, 25);
  assert.equal(report.current.truncated, true);
  assert.ok(report.introduced.length + report.preExisting.length <= 2);
  assert.equal(report.reportTruncated, true);
  assert.ok(report.omittedDiagnostics > 0);
  assert.ok(Buffer.byteLength(report.testCommand.command ?? "", "utf8") <= 48);
  assert.deepEqual(report.testCommand.environmentKeys, ["ONE", "TWO"]);
  assert.ok(Buffer.byteLength(JSON.stringify(report), "utf8") <= 2_048);
});

test("source read and subscription failures produce unavailable blocking reports", async () => {
  const readFailure = new FakeDiagnosticsSource();
  readFailure.throwOnRead = true;
  const readGate = new DiagnosticsVerificationGate(readFailure);
  const failedBaseline = readGate.captureBaseline();
  assert.equal(failedBaseline.sourceError, true);
  const unavailableFromBaseline = await readGate.verifyAfterChanges(failedBaseline);
  assert.equal(unavailableFromBaseline.status, "unavailable");
  assert.equal(unavailableFromBaseline.blocksCompletion, true);

  const subscriptionFailure = new FakeDiagnosticsSource();
  const subscriptionGate = new DiagnosticsVerificationGate(subscriptionFailure);
  const validBaseline = subscriptionGate.captureBaseline();
  subscriptionFailure.throwOnSubscribe = true;
  const unavailableFromSubscription = await subscriptionGate.verifyAfterChanges(validBaseline);
  assert.equal(unavailableFromSubscription.status, "unavailable");
  assert.equal(unavailableFromSubscription.blocksCompletion, true);
});

test("tampered or malformed baselines fail closed before subscribing to providers", async () => {
  const source = new FakeDiagnosticsSource();
  const gate = new DiagnosticsVerificationGate(source);
  const baseline = gate.captureBaseline();
  const tampered = { ...baseline, snapshotHash: "0".repeat(64) };

  const report = await gate.verifyAfterChanges(tampered, { settleMs: 5, timeoutMs: 100 });

  assert.equal(report.status, "unavailable");
  assert.equal(report.blocksCompletion, true);
  assert.equal(source.listenerCount, 0);

  const malformedScope = await gate.verifyAfterChanges({ ...baseline, scope: null } as any);
  assert.equal(malformedScope.status, "unavailable");
  assert.equal(malformedScope.blocksCompletion, true);
  assert.equal(source.listenerCount, 0);
});

class FakeDiagnosticsSource implements DiagnosticsVerificationSource {
  public diagnostics: VerificationDiagnostic[] = [];
  public throwOnRead = false;
  public throwOnSubscribe = false;
  public lastReadLimit = -1;
  public lastScope: { workspaceRoot: string } | undefined;
  private readonly listeners = new Set<() => void>();

  public get listenerCount(): number {
    return this.listeners.size;
  }

  public readDiagnostics(maxItems: number, scope?: { workspaceRoot: string }): DiagnosticReadResult {
    if (this.throwOnRead) {
      throw new Error("provider failed with token=do-not-report");
    }
    this.lastReadLimit = maxItems;
    this.lastScope = scope;
    return {
      diagnostics: this.diagnostics.slice(0, maxItems),
      totalCount: this.diagnostics.length,
      truncated: this.diagnostics.length > maxItems
    };
  }

  public onDidChangeDiagnostics(listener: () => void): VerificationDisposable {
    if (this.throwOnSubscribe) {
      throw new Error("subscribe failed with token=do-not-report");
    }
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  }

  public emitChange(): void {
    for (const listener of [...this.listeners]) {
      listener();
    }
  }
}

function diagnostic(
  severity: VerificationDiagnostic["severity"],
  message: string,
  line = 1
): VerificationDiagnostic {
  return {
    uri: "file:///workspace/main.ts",
    range: {
      start: { line, character: 1 },
      end: { line, character: 5 }
    },
    severity,
    message,
    source: "typescript",
    code: "TS1000"
  };
}

interface CountsShape {
  error: number;
  warning: number;
  information: number;
  hint: number;
  total: number;
}

function counts(overrides: Partial<CountsShape> = {}): CountsShape {
  return { error: 0, warning: 0, information: 0, hint: 0, total: 0, ...overrides };
}
