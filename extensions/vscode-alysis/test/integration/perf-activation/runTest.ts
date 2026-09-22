import {
  appendFileSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";

import { runVsCodeExtensionHostTests } from "../vsCodeExtensionHostInvocation";
import {
  ActivationPhase,
  evaluatePerfSamples,
  parsePerfEvidenceJsonLines,
  PERF_EVIDENCE_SCHEMA_VERSION,
  resolvePerfGateConfig
} from "./perfBudget";

async function main(): Promise<void> {
  const extensionDevelopmentPath = resolve(__dirname, "../../../..");
  const extensionTestsPath = resolve(__dirname, "suite");
  const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim() || undefined;
  const evidencePath = process.env.ALYSIS_PERF_ACTIVATION_EVIDENCE?.trim();
  const config = resolvePerfGateConfig(process.env);

  if (!evidencePath) {
    throw new Error("ALYSIS_PERF_ACTIVATION_EVIDENCE is required.");
  }

  mkdirSync(dirname(evidencePath), { recursive: true });
  writeFileSync(
    evidencePath,
    `${JSON.stringify({
      schemaVersion: PERF_EVIDENCE_SCHEMA_VERSION,
      record: "run",
      samplesPerPhase: config.sampleCount,
      phases: ["cold", "warm"],
      configSource: config.source,
      budgets: config.budgets,
      vscode: vscodeExecutablePath
        ? { source: "explicit_executable", executable: vscodeExecutablePath }
        : { source: "pinned_download", version: config.vscodeVersion },
      extensionDevelopmentPath,
      platform: process.platform,
      arch: process.arch,
      nodeVersion: process.version,
      startedAt: new Date().toISOString()
    })}\n`,
    "utf8"
  );
  delete process.env.ELECTRON_RUN_AS_NODE;

  try {
    for (let index = 1; index <= config.sampleCount; index += 1) {
      await runSamplePair({
        index,
        extensionDevelopmentPath,
        extensionTestsPath,
        vscodeExecutablePath,
        vscodeVersion: config.vscodeVersion,
        evidencePath
      });
    }

    const records = parsePerfEvidenceJsonLines(readFileSync(evidencePath, "utf8"));
    const summary = evaluatePerfSamples(records, config);
    appendFileSync(evidencePath, `${JSON.stringify({ ...summary, completedAt: new Date().toISOString() })}\n`, "utf8");
    console.log(`PERF001_SUMMARY ${JSON.stringify(summary)}`);
    if (!summary.passed) {
      const rendered = summary.violations
        .map((item) => `${item.phase}.${item.metric}=${item.actual} > ${item.limit}`)
        .join(", ");
      throw new Error(`Activation performance budgets exceeded: ${rendered}.`);
    }
  } catch (error) {
    appendFileSync(
      evidencePath,
      `${JSON.stringify({
        schemaVersion: PERF_EVIDENCE_SCHEMA_VERSION,
        record: "failure",
        failedAt: new Date().toISOString(),
        message: error instanceof Error ? error.message : String(error)
      })}\n`,
      "utf8"
    );
    throw error;
  }
}

interface SamplePairOptions {
  readonly index: number;
  readonly extensionDevelopmentPath: string;
  readonly extensionTestsPath: string;
  readonly vscodeExecutablePath: string | undefined;
  readonly vscodeVersion: string;
  readonly evidencePath: string;
}

async function runSamplePair(options: SamplePairOptions): Promise<void> {
  const workspacePath = mkdtempSync(join(tmpdir(), `alysis-perf001-workspace-${options.index}-`));
  const userDataDir = mkdtempSync(join(tmpdir(), `alysis-perf001-user-data-${options.index}-`));
  const extensionsDir = mkdtempSync(join(tmpdir(), `alysis-perf001-extensions-${options.index}-`));
  try {
    for (const phase of ["cold", "warm"] as const) {
      await runPhase(phase, options, workspacePath, userDataDir, extensionsDir);
    }
  } finally {
    rmSync(workspacePath, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    rmSync(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    rmSync(extensionsDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

async function runPhase(
  phase: ActivationPhase,
  options: SamplePairOptions,
  workspacePath: string,
  userDataDir: string,
  extensionsDir: string
): Promise<void> {
  await runVsCodeExtensionHostTests({
    extensionDevelopmentPath: options.extensionDevelopmentPath,
    extensionTestsPath: options.extensionTestsPath,
    vscodeExecutablePath: options.vscodeExecutablePath,
    version: options.vscodeVersion,
    launchArgs: [
      workspacePath,
      "--user-data-dir",
      userDataDir,
      "--extensions-dir",
      extensionsDir,
      "--disable-extensions",
      "--disable-workspace-trust",
      "--skip-welcome",
      "--skip-release-notes"
    ],
    extensionTestsEnv: {
      ...process.env,
      ALYSIS_PERF_ACTIVATION_EVIDENCE: options.evidencePath,
      ALYSIS_PERF_ACTIVATION_SAMPLE: String(options.index),
      ALYSIS_PERF_ACTIVATION_PHASE: phase,
      ALYSIS_PERF_PROFILE_REUSED: String(phase === "warm")
    }
  });
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
