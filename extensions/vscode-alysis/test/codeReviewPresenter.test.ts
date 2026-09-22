import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, realpath, rm, writeFile } from "node:fs/promises";
import Module from "node:module";
import path from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

class FakeRange {
  public readonly start: { line: number; character: number };
  public readonly end: { line: number; character: number };

  public constructor(startLine: number, startCharacter: number, endLine: number, endCharacter: number) {
    this.start = { line: startLine, character: startCharacter };
    this.end = { line: endLine, character: endCharacter };
  }
}

class FakeDiagnostic {
  public source: string | undefined;
  public code: string | number | undefined;

  public constructor(
    public readonly range: FakeRange,
    public readonly message: string,
    public readonly severity: number
  ) {}
}

const vscodeStub = {
  Diagnostic: FakeDiagnostic,
  Range: FakeRange,
  DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
  ProgressLocation: { Notification: 15 },
  Uri: {
    file: (fsPath: string) => ({
      scheme: "file",
      fsPath,
      toString: () => `file://${fsPath.replaceAll("\\", "/")}`
    })
  },
  languages: {
    createDiagnosticCollection: () => {
      throw new Error("tests inject their owned diagnostic collection");
    }
  },
  window: {
    withProgress: () => {
      throw new Error("tests inject bounded progress");
    }
  }
};

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};
const { CodeReviewPresenter } = require("../src/review/CodeReviewPresenter") as typeof import("../src/review/CodeReviewPresenter");
moduleLoader._load = originalLoad;

test("code review Problems projection rejects traversal, sensitive, ignored, and escaped paths while clamping ranges", async () => {
  await withWorkspace(async ({ root, source, collection, output }) => {
    await workspaceFile(root, "src/app.ts", "one\ntwo\nthree\nfour\nfive");
    await workspaceFile(root, "src/ignored.ts", "ignored");
    source.ignored.add("src/ignored.ts");
    source.realpathOverrides.set(
      normalized(path.join(root, "src/link.ts")),
      path.join(path.dirname(root), "outside.ts")
    );
    const findings = [
      finding("src/app.ts", "high", -5, 999_999, "Unsafe sk-test-secret-value-123456"),
      finding("src/app.ts", "low", null, null, "Low confidence note"),
      finding("../outside.ts", "critical", 1, 1, "Traversal"),
      finding(".env", "critical", 1, 1, "Sensitive"),
      finding("private.pem", "critical", 1, 1, "Sensitive extension"),
      finding("src/ignored.ts", "medium", 1, 1, "Ignored"),
      finding("src/link.ts", "medium", 1, 1, "Symlink escape")
    ];
    const bridge = bridgeWithResults([completedResult(findings)]);
    const presenter = presenterFor(bridge, source, collection, output);

    const result = await presenter.run(startParams());

    assert.equal(result.findings?.length, 2);
    assert.deepEqual(result.findings?.map((item) => item.path), ["src/app.ts", "src/app.ts"]);
    assert.equal(result.summary?.truncated, true);
    assert.equal(result.summary?.omitted_file_count, 5);
    assert.equal(collection.values.size, 1);
    const diagnostics = [...collection.values.values()][0];
    assert.equal(diagnostics.length, 2);
    assert.equal(diagnostics[0].severity, vscodeStub.DiagnosticSeverity.Error);
    assert.equal(diagnostics[1].severity, vscodeStub.DiagnosticSeverity.Information);
    assert.deepEqual(diagnostics[0].range.start, { line: 0, character: 0 });
    assert.deepEqual(diagnostics[0].range.end, { line: 4, character: 4 });
    assert.doesNotMatch(diagnostics[0].message, /sk-test-secret-value-123456/);
    assert.match(diagnostics[0].message, /redacted/i);
    assert.match(diagnostics[0].source ?? "", /Alysis Code code review/);
    assert.equal(diagnostics[0].code, "alysis-review/high");
    assert.doesNotMatch(JSON.stringify(result), /outside|\.env|private\.pem|ignored\.ts|link\.ts/);
    presenter.dispose();
  });
});

test("code review findings are count/text bounded and a later run atomically replaces stale Problems", async () => {
  await withWorkspace(async ({ root, source, collection, output }) => {
    await workspaceFile(root, "src/a.ts", "a");
    await workspaceFile(root, "src/b.ts", "b");
    const first = Array.from({ length: 140 }, (_, index) =>
      finding(index < 70 ? "src/a.ts" : "src/b.ts", "medium", 1, 1, "x".repeat(10_000))
    );
    const bridge = bridgeWithResults([
      completedResult(first),
      completedResult([finding("src/b.ts", "critical", 1, 1, "replacement")])
    ]);
    const presenter = presenterFor(bridge, source, collection, output);

    const bounded = await presenter.run(startParams());
    assert.equal(bounded.findings?.length, 80, "global 100 and per-file 50 limits compose deterministically");
    assert.equal(bounded.summary?.truncated, true);
    assert.ok((bounded.findings?.[0].explanation.length ?? 0) <= 2_000);
    assert.ok([...collection.values.values()].flat()[0].message.length <= 4_000);

    const replaced = await presenter.run(startParams());
    assert.equal(replaced.findings?.length, 1);
    assert.deepEqual([...collection.values.keys()], [normalized(path.join(root, "src/b.ts"))]);
    assert.equal([...collection.values.values()][0].length, 1);
    assert.ok(collection.clearCount >= 2, "every run clears stale findings before polling");
    presenter.dispose();
  });
});

test("code review polling has bounded transient retries, timeout failure, and redacts bridge internals", async () => {
  await withWorkspace(async ({ source, collection, output }) => {
    const retryBridge = bridgeWithResults([]);
    retryBridge.codeReviewResult = async () => {
      retryBridge.resultCalls += 1;
      throw new Error("sk-secret-should-not-escape");
    };
    const retryPresenter = presenterFor(retryBridge, source, collection, output, {
      maxPollAttempts: 20,
      maxConsecutiveErrors: 3
    });
    await assert.rejects(
      retryPresenter.run(startParams()),
      /after 3 bounded attempts/
    );
    assert.equal(retryBridge.resultCalls, 3);
    assert.deepEqual(retryBridge.cancelCalls, ["review-owner"]);
    assert.equal(collection.values.size, 0);
    retryPresenter.dispose();

    const timeoutCollection = new FakeDiagnosticCollection();
    const timeoutBridge = bridgeWithResults([pendingResult(), pendingResult(), pendingResult()]);
    const timeoutPresenter = presenterFor(timeoutBridge, source, timeoutCollection, output, {
      maxPollAttempts: 2
    });
    await assert.rejects(
      timeoutPresenter.run(startParams()),
      /within 2 bounded status checks/
    );
    assert.equal(timeoutBridge.resultCalls, 2);
    assert.deepEqual(timeoutBridge.cancelCalls, ["review-owner"]);
    assert.equal(timeoutCollection.values.size, 0);
    timeoutPresenter.dispose();

    const failedCollection = new FakeDiagnosticCollection();
    const failedBridge = bridgeWithResults([{
      ...pendingResult(),
      status: "failed",
      complete: true
    }]);
    const failedPresenter = presenterFor(failedBridge, source, failedCollection, output);
    await assert.rejects(
      failedPresenter.run(startParams()),
      /failed before producing safe findings/
    );
    assert.deepEqual(failedBridge.cancelCalls, [], "a terminal failed job does not need cancellation");
    failedPresenter.dispose();

    const mismatchedCollection = new FakeDiagnosticCollection();
    const mismatchedBridge = bridgeWithResults([{
      ...completedResult([]),
      job_id: "different-job"
    }]);
    const mismatchedPresenter = presenterFor(mismatchedBridge, source, mismatchedCollection, output);
    await assert.rejects(
      mismatchedPresenter.run(startParams()),
      /scope verification failed/
    );
    assert.deepEqual(mismatchedBridge.cancelCalls, ["review-owner"]);
    mismatchedPresenter.dispose();
  });
});

test("code review start failures are sanitized before they reach the caller", async () => {
  await withWorkspace(async ({ source, collection, output }) => {
    const bridge = bridgeWithResults([]);
    bridge.codeReviewStart = async () => {
      throw Object.assign(
        new Error("bridge exploded reading /home/user/.alysis/secrets sk-live-should-not-escape"),
        { code: "code_review_unavailable" }
      );
    };
    const presenter = presenterFor(bridge, source, collection, output);

    await assert.rejects(presenter.run(startParams()), (error: unknown) => {
      assert.ok(error instanceof Error);
      assert.match(error.message, /code_review_unavailable/);
      assert.equal(error.message.includes("sk-live-should-not-escape"), false);
      return true;
    });
    presenter.dispose();
  });
});

test("code review cancellation requests exact session cancellation once and never publishes late findings", async () => {
  await withWorkspace(async ({ root, source, collection, output }) => {
    await workspaceFile(root, "src/app.ts", "content");
    const token = new FakeCancellationToken();
    const bridge = bridgeWithResults([completedResult([finding("src/app.ts")])]);
    bridge.codeReviewStart = async () => {
      token.cancel();
      return startedResult();
    };
    const presenter = presenterFor(bridge, source, collection, output, {
      runWithProgress: (task: any) => task({ report: () => undefined }, token)
    });

    const result = await presenter.run(startParams());
    assert.equal(result.cancelled, true);
    assert.equal(result.findings?.length, 0);
    assert.deepEqual(bridge.cancelCalls, ["review-owner"]);
    assert.equal(bridge.resultCalls, 0);
    assert.equal(collection.values.size, 0);

    presenter.dispose();
    assert.equal(collection.disposed, true);
    await assert.rejects(presenter.run(startParams()), /disposed/);
  });
});

test("manual review results are active-session bound and relative findings never fall through to a second workspace root", async () => {
  await withWorkspace(async ({ root, source, collection, output }) => {
    const secondRoot = path.join(root, "second-workspace");
    await workspaceFile(secondRoot, "src/second.ts", "second workspace only");
    source.getWorkspaceFolders = () => [
      { name: "first", index: 0, uri: vscodeStub.Uri.file(root) },
      { name: "second", index: 1, uri: vscodeStub.Uri.file(secondRoot) }
    ];
    const result = completedResult([finding("src/second.ts")]);
    const presenter = presenterFor(bridgeWithResults([]), source, collection, output);

    const projected = await presenter.present(result, "review-owner");
    assert.equal(projected.findings?.length, 0);
    assert.equal(collection.values.size, 0);

    const explicitlyScoped = await presenter.present(result, "review-owner", secondRoot);
    assert.equal(explicitlyScoped.findings?.length, 1);
    assert.equal(collection.values.has(normalized(path.join(secondRoot, "src/second.ts"))), true);
    collection.clear();

    collection.values.set(normalized(path.join(root, "stale.ts")), [
      new FakeDiagnostic(new FakeRange(0, 0, 0, 1), "stale", 1)
    ]);
    await assert.rejects(
      presenter.present(result, "different-live-session"),
      /session verification failed/
    );
    assert.equal(collection.values.size, 0, "session mismatch clears stale Problems before rejecting");
    presenter.dispose();
  });
});

class FakeDiagnosticCollection {
  public readonly values = new Map<string, FakeDiagnostic[]>();
  public clearCount = 0;
  public disposed = false;

  public set(entries: Array<[{ fsPath: string }, FakeDiagnostic[]]>): void {
    this.values.clear();
    for (const [uri, diagnostics] of entries) {
      this.values.set(normalized(uri.fsPath), diagnostics);
    }
  }

  public clear(): void {
    this.clearCount += 1;
    this.values.clear();
  }

  public dispose(): void {
    this.disposed = true;
  }
}

class FakeCancellationToken {
  public isCancellationRequested = false;
  private readonly listeners = new Set<() => void>();

  public onCancellationRequested(listener: () => void): { dispose(): void } {
    this.listeners.add(listener);
    return { dispose: () => this.listeners.delete(listener) };
  }

  public cancel(): void {
    this.isCancellationRequested = true;
    for (const listener of this.listeners) {
      listener();
    }
  }
}

function presenterFor(
  bridge: ReturnType<typeof bridgeWithResults>,
  source: ReturnType<typeof reviewSource>,
  collection: FakeDiagnosticCollection,
  output: { lines: string[]; appendLine(value: string): void },
  overrides: Record<string, unknown> = {}
): InstanceType<typeof CodeReviewPresenter> {
  return new CodeReviewPresenter(bridge as any, source as any, output as any, {
    diagnostics: collection as any,
    maxPollAttempts: 5,
    maxConsecutiveErrors: 3,
    pollIntervalMs: 0,
    sleep: async () => undefined,
    runWithProgress: (task: any) => task(
      { report: () => undefined },
      new FakeCancellationToken()
    ),
    ...overrides
  });
}

function bridgeWithResults(results: any[]) {
  return {
    resultCalls: 0,
    cancelCalls: [] as string[],
    codeReviewStart: async () => startedResult(),
    codeReviewResult: async function () {
      this.resultCalls += 1;
      return results.shift() ?? pendingResult();
    },
    cancelSession: async function (sessionId: string) {
      this.cancelCalls.push(sessionId);
      return {};
    }
  };
}

function reviewSource(root: string) {
  const ignored = new Set<string>();
  const realpathOverrides = new Map<string, string>();
  return {
    ignored,
    realpathOverrides,
    isWorkspaceTrusted: true,
    getWorkspaceFolders: () => [{
      name: "workspace",
      index: 0,
      uri: vscodeStub.Uri.file(root)
    }],
    realpath: async (fsPath: string) => realpathOverrides.get(normalized(fsPath)) ?? realpath(fsPath),
    isIgnored: async (uri: { fsPath: string }) => ignored.has(
      path.relative(root, uri.fsPath).split(path.sep).join("/")
    ),
    openDocument: async (uri: { fsPath: string }) => {
      try {
        const content = await readFile(uri.fsPath, "utf8");
        const lines = content.split(/\r?\n/);
        return {
          uri,
          languageId: "typescript",
          version: 1,
          isDirty: false,
          lineCount: lines.length,
          getText: () => content,
          getLineRange: (line: number) => ({
            start: { line, character: 0 },
            end: { line, character: lines[line]?.length ?? 0 }
          })
        };
      } catch {
        return undefined;
      }
    }
  };
}

async function withWorkspace(
  run: (fixture: {
    root: string;
    source: ReturnType<typeof reviewSource>;
    collection: FakeDiagnosticCollection;
    output: { lines: string[]; appendLine(value: string): void };
  }) => Promise<void>
): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "alysis-code-review-"));
  const source = reviewSource(root);
  const collection = new FakeDiagnosticCollection();
  const output = {
    lines: [] as string[],
    appendLine(value: string) {
      this.lines.push(value);
    }
  };
  try {
    await run({ root, source, collection, output });
  } finally {
    await rm(root, { recursive: true, force: true });
  }
}

async function workspaceFile(root: string, relative: string, content: string): Promise<void> {
  const destination = path.join(root, ...relative.split("/"));
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, content, "utf8");
}

function startParams() {
  return {
    session_id: "review-owner",
    scope: "working_tree" as const,
    workspace_trusted: true
  };
}

function startedResult() {
  return {
    session_id: "review-owner",
    job_id: "review-job",
    status: "started",
    scope: "working_tree" as const
  };
}

function pendingResult() {
  return {
    session_id: "review-owner",
    job_id: "review-job",
    status: "running",
    scope: "working_tree" as const,
    complete: false,
    findings: []
  };
}

function completedResult(findings: any[]) {
  return {
    session_id: "review-owner",
    job_id: "review-job",
    status: "completed",
    scope: "working_tree" as const,
    complete: true,
    findings,
    summary: {
      verdict: "request_changes" as const,
      overview: "Review completed.",
      finding_counts: {},
      changed_file_count: 2,
      reviewed_file_count: 2,
      omitted_file_count: 0,
      truncated: false,
      warnings: []
    }
  };
}

function finding(
  reviewPath = "src/app.ts",
  severity: "critical" | "high" | "medium" | "low" = "medium",
  lineStart: number | null = 1,
  lineEnd: number | null = 1,
  explanation = "Explain the issue"
) {
  return {
    severity,
    title: "Review finding",
    explanation,
    path: reviewPath,
    line_start: lineStart,
    line_end: lineEnd,
    evidence: "Evidence",
    suggested_fix: "Suggested fix",
    confidence: "high" as const
  };
}

function normalized(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}
