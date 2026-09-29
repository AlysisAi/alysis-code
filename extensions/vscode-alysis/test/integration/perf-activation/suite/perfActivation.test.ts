import * as assert from "node:assert/strict";
import { appendFileSync } from "node:fs";
import { performance } from "node:perf_hooks";

import * as vscode from "vscode";

import { PERF_EVIDENCE_SCHEMA_VERSION } from "../perfBudget";

suite("Alysis Code lazy activation performance", () => {
  test("activates on the first contributed command with bounded host resources", async () => {
    const evidencePath = process.env.ALYSIS_PERF_ACTIVATION_EVIDENCE?.trim();
    const sample = Number(process.env.ALYSIS_PERF_ACTIVATION_SAMPLE);
    const phase = process.env.ALYSIS_PERF_ACTIVATION_PHASE?.trim();
    const profileReused = process.env.ALYSIS_PERF_PROFILE_REUSED === "true";
    assert.ok(evidencePath, "ALYSIS_PERF_ACTIVATION_EVIDENCE must be configured.");
    assert.ok(Number.isInteger(sample) && sample > 0, "sample index must be a positive integer.");
    assert.ok(phase === "cold" || phase === "warm", "phase must be cold or warm.");
    assert.equal(profileReused, phase === "warm", "warm samples must reuse the cold profile.");

    const extension = vscode.extensions.getExtension("alysisai.vscode-alysis");
    assert.ok(extension, "Alysis Code extension must be discoverable.");

    const isActiveBefore = extension.isActive;
    const memoryBefore = process.memoryUsage();
    const cpuBefore = process.cpuUsage();
    const started = performance.now();
    await vscode.commands.executeCommand("alysis.showSettings");
    const activationAndCommandMs = performance.now() - started;
    const cpu = process.cpuUsage(cpuBefore);
    const memoryAfter = process.memoryUsage();
    const isActiveAfter = extension.isActive;

    const record = {
      schemaVersion: PERF_EVIDENCE_SCHEMA_VERSION,
      record: "sample",
      phase,
      sample,
      timestamp: new Date().toISOString(),
      vscodeVersion: vscode.version,
      platform: process.platform,
      arch: process.arch,
      processPid: process.pid,
      trigger: "alysis.showSettings",
      autoStartBridge: vscode.workspace.getConfiguration("alysis").get<boolean>("autoStartBridge"),
      isActiveBefore,
      isActiveAfter,
      profileReused,
      activationAndCommandMs,
      rssBeforeBytes: memoryBefore.rss,
      rssAfterBytes: memoryAfter.rss,
      rssDeltaBytes: Math.max(0, memoryAfter.rss - memoryBefore.rss),
      heapUsedAfterBytes: memoryAfter.heapUsed,
      cpuUserMs: cpu.user / 1_000,
      cpuSystemMs: cpu.system / 1_000,
      cpuTotalMs: (cpu.user + cpu.system) / 1_000
    };
    appendFileSync(evidencePath, `${JSON.stringify(record)}\n`, "utf8");
    console.log(`PERF001_SAMPLE ${JSON.stringify(record)}`);

    assert.equal(isActiveBefore, false, "extension must remain lazy before its first activation event.");
    assert.equal(isActiveAfter, true, "the contributed command must activate the extension.");
    assert.ok(
      Number.isFinite(activationAndCommandMs) && activationAndCommandMs > 0,
      `activation duration must be finite and positive; got ${activationAndCommandMs}.`
    );
    assert.ok(memoryAfter.rss > 0 && memoryAfter.heapUsed > 0, "Extension Host memory metrics must be positive.");
    assert.ok(cpu.user >= 0 && cpu.system >= 0, "Extension Host CPU metrics must be non-negative.");
  });
});
