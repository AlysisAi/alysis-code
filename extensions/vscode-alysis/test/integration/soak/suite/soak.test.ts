import * as assert from "node:assert/strict";

import * as vscode from "vscode";

import { AlysisExtensionApi } from "../../../../src/extension";

const durationMs = Number(process.env.ALYSIS_SOAK_DURATION_MS ?? 3_660_000);
const taskIntervalMs = Number(process.env.ALYSIS_SOAK_TASK_INTERVAL_MS ?? 5_000);
type ExtensionTestApi = NonNullable<AlysisExtensionApi["test"]>;
type ExtensionTestState = ReturnType<ExtensionTestApi["state"]>;

suite("Alysis Code one-hour Extension Host soak", () => {
  test("remains responsive and memory-bounded for more than one wall-clock hour", async () => {
    assert.equal(process.env.ALYSIS_EXTENSION_SOAK, "1");
    assert.ok(durationMs >= 3_600_000, `soak duration must be at least one hour; got ${durationMs}`);
    const mockCli = process.env.ALYSIS_TEST_CLI_PATH;
    assert.ok(mockCli, "An explicit deterministic CLI fixture is required.");
    // Process paths from workspace settings are intentionally not trusted. This is the isolated
    // test profile, so configure its user setting through the same boundary as Extension Host QA.
    await vscode.workspace.getConfiguration("alysis").update("cliPath", mockCli, vscode.ConfigurationTarget.Global);
    await vscode.workspace.getConfiguration("alysis").update("autoStartBridge", true, vscode.ConfigurationTarget.Global);
    await vscode.workspace.getConfiguration("alysis").update("defaultMode", "readonly", vscode.ConfigurationTarget.Global);
    const windowApi = vscode.window as any;
    const originalInfo = windowApi.showInformationMessage;
    const originalWarn = windowApi.showWarningMessage;
    const originalError = windowApi.showErrorMessage;
    windowApi.showInformationMessage = async () => undefined;
    windowApi.showWarningMessage = async () => undefined;
    windowApi.showErrorMessage = async () => undefined;
    const extension = vscode.extensions.getExtension("alysisai.vscode-alysis") as
      | vscode.Extension<AlysisExtensionApi | undefined>
      | undefined;
    assert.ok(extension, "Alysis Code extension must be discoverable.");
    const activationStarted = performance.now();
    const exports = await extension.activate();
    const activationElapsedMs = performance.now() - activationStarted;
    assert.ok(exports?.test, "ExtensionMode.Test API must be available.");
    const testApi: ExtensionTestApi = exports.test;
    assert.ok(activationElapsedMs < 5_000, `cold activation took ${activationElapsedMs.toFixed(1)}ms`);
    console.log(JSON.stringify({ evidence: "cold_extension_activation", activationElapsedMs }));
    // Runtime validation and the first health probe settle after activation returns.
    await testApi.whenReady();
    console.log(JSON.stringify({ evidence: "soak_setup", phase: "runtime_ready" }));
    await vscode.commands.executeCommand("alysis.openChat");
    console.log(JSON.stringify({ evidence: "soak_setup", phase: "chat_opened" }));
    await vscode.commands.executeCommand("alysis.showModels");
    console.log(JSON.stringify({ evidence: "soak_setup", phase: "models_loaded" }));
    const readinessDeadline = Date.now() + 15_000;
    while (!testApi.state().chat?.readiness.ok && Date.now() < readinessDeadline) {
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 100));
    }
    assert.ok(testApi.state().chat?.readiness.ok,
      `soak provider fixture is not ready: ${JSON.stringify(testApi.state().chat?.readiness)}`);

    const startedAt = Date.now();
    const deadline = startedAt + durationMs;
    const memoryStart = process.memoryUsage();
    let peakHeap = memoryStart.heapUsed;
    let peakRss = memoryStart.rss;
    let completedTasks = 0;
    let submittedTasks = 0;
    let awaitingCompletion = false;
    let maxStateLatencyMs = 0;
    let maxTimerLagMs = 0;
    let nextTaskAt = startedAt;
    let expectedTick = startedAt;
    let nextSampleAt = startedAt + 60_000;

    while (Date.now() < deadline) {
      const now = Date.now();
      maxTimerLagMs = Math.max(maxTimerLagMs, Math.max(0, now - expectedTick));
      expectedTick = now + 250;
      const stateStarted = performance.now();
      const current: ExtensionTestState = testApi.state();
      maxStateLatencyMs = Math.max(maxStateLatencyMs, performance.now() - stateStarted);
      const memory = process.memoryUsage();
      peakHeap = Math.max(peakHeap, memory.heapUsed);
      peakRss = Math.max(peakRss, memory.rss);
      const errorItem: { kind: string; title: string | undefined; text: string } | undefined =
        current.chat?.items.find((item) => item.kind === "error");
      assert.equal(errorItem, undefined, `soak produced an error item: ${errorItem?.text}`);

      if (awaitingCompletion && current.chat?.activeJobId === null) {
        completedTasks += 1;
        awaitingCompletion = false;
      }
      if (now >= nextSampleAt) {
        console.log(JSON.stringify({ evidence: "extension_host_soak_sample", elapsedMs: now - startedAt,
          submittedTasks, completedTasks, visibleItems: current.chat?.items.length ?? 0,
          heapUsed: memory.heapUsed, rss: memory.rss, maxStateLatencyMs, maxTimerLagMs }));
        nextSampleAt = now + 60_000;
      }

      if (now >= nextTaskAt && current.chat?.activeJobId === null) {
        const prompt = `Soak task ${submittedTasks + 1}: inspect the workspace without changes.`;
        let submissionTimeout: ReturnType<typeof setTimeout> | undefined;
        try {
          await Promise.race([
            testApi.submitChatText(prompt),
            new Promise<never>((_resolve, reject) => {
              submissionTimeout = setTimeout(() => reject(new Error(
                `soak submission stalled: ${JSON.stringify({
                  chat: testApi.state().chat?.items.slice(-6),
                  readiness: testApi.state().chat?.readiness,
                  runtime: testApi.state().chat?.cockpit.status.runtime.events.slice(-5)
                })}`
              )), 15_000);
            })
          ]);
        } finally {
          if (submissionTimeout) clearTimeout(submissionTimeout);
        }
        assert.ok(testApi.state().chat?.items.some((item) => item.kind === "user" && item.text === prompt),
          `soak submission was not accepted: ${JSON.stringify(testApi.state().chat?.readiness)}`);
        submittedTasks += 1;
        awaitingCompletion = true;
        if (submittedTasks === 1) console.log(JSON.stringify({ evidence: "soak_setup", phase: "first_task_accepted" }));
        nextTaskAt = now + taskIntervalMs;
      }
      await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
    }

    const finalState = testApi.state();
    windowApi.showInformationMessage = originalInfo;
    windowApi.showWarningMessage = originalWarn;
    windowApi.showErrorMessage = originalError;
    const memoryEnd = process.memoryUsage();
    const elapsedMs = Date.now() - startedAt;
    const heapGrowth = memoryEnd.heapUsed - memoryStart.heapUsed;
    const rssGrowth = memoryEnd.rss - memoryStart.rss;
    const minimumTasks = Math.floor(durationMs / Math.max(taskIntervalMs, 1) / 2);
    const evidence = {
      evidence: "one_hour_extension_host_soak",
      elapsedMs,
      completedTasks,
      submittedTasks,
      visibleItems: finalState.chat?.items.length ?? 0,
      heapStart: memoryStart.heapUsed,
      heapEnd: memoryEnd.heapUsed,
      heapGrowth,
      peakHeap,
      rssStart: memoryStart.rss,
      rssEnd: memoryEnd.rss,
      rssGrowth,
      peakRss,
      maxStateLatencyMs,
      maxTimerLagMs,
      activationElapsedMs
    };
    console.log(JSON.stringify(evidence));

    assert.ok(elapsedMs >= 3_600_000, `wall-clock soak ended early after ${elapsedMs}ms`);
    assert.ok(completedTasks >= minimumTasks, `only ${completedTasks} tasks completed; expected at least ${minimumTasks}`);
    assert.ok((finalState.chat?.items.length ?? 0) <= 200, "visible transcript exceeded its production cap");
    assert.ok(heapGrowth < 256 * 1024 * 1024, `heap grew by ${heapGrowth} bytes`);
    assert.ok(rssGrowth < 768 * 1024 * 1024, `RSS grew by ${rssGrowth} bytes`);
    assert.ok(maxStateLatencyMs < 1_000, `state projection stalled for ${maxStateLatencyMs.toFixed(1)}ms`);
    assert.ok(maxTimerLagMs < 5_000, `Extension Host timer stalled for ${maxTimerLagMs}ms`);
  });
});
