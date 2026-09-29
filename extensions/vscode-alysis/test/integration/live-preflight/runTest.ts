import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { runVsCodeExtensionHostTests } from "../vsCodeExtensionHostInvocation";

import {
  assertSecretAbsentFromTrees,
  liveExtensionHostEnvironment,
  restrictLiveQaEnvironment,
  safeErrorEvidence,
  takeEnvironmentSecret
} from "../liveQaSecurity";

const LIVE_API_KEY_ENVIRONMENT = "ALYSIS_LIVE_API_KEY";

async function main(): Promise<void> {
  const extensionDevelopmentPath = resolve(__dirname, "../../../../");
  const extensionTestsPath = resolve(__dirname, "suite");
  const workspacePath = process.env.ALYSIS_LIVE_QA_WORKSPACE?.trim();
  const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim();
  if (!workspacePath || !vscodeExecutablePath) {
    throw new Error(
      "ALYSIS_LIVE_QA_WORKSPACE and VSCODE_TEST_EXECUTABLE_PATH are required."
    );
  }

  let qaSecret = takeEnvironmentSecret(LIVE_API_KEY_ENVIRONMENT);
  restrictLiveQaEnvironment(process.env, qaSecret);

  const userDataDir = mkdtempSync(join(tmpdir(), "alysis-live-preflight-user-data-"));
  const extensionsDir = mkdtempSync(join(tmpdir(), "alysis-live-preflight-extensions-"));
  delete process.env.ELECTRON_RUN_AS_NODE;
  try {
    const extensionTestsEnv = liveExtensionHostEnvironment(
      process.env,
      LIVE_API_KEY_ENVIRONMENT,
      qaSecret
    );
    extensionTestsEnv.ALYSIS_LIVE_PREFLIGHT = "1";
    extensionTestsEnv.ALYSIS_CONFIG_DIR = join(userDataDir, "alysis-config");
    extensionTestsEnv.ALYSIS_DATA_DIR = join(userDataDir, "alysis-data");
    await runVsCodeExtensionHostTests({
      extensionDevelopmentPath,
      extensionTestsPath,
      vscodeExecutablePath,
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
      extensionTestsEnv
    });
  } finally {
    try {
      assertSecretAbsentFromTrees([userDataDir, extensionsDir], qaSecret);
    } finally {
      qaSecret = "";
      removeTemporaryDirectory(userDataDir);
      removeTemporaryDirectory(extensionsDir);
    }
  }
}

function removeTemporaryDirectory(path: string): void {
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 20, retryDelay: 500 });
  } catch (error) {
    const code = error instanceof Error && "code" in error ? String(error.code) : "unknown";
    console.error(`Unable to remove isolated live-test directory (${code}).`);
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ evidence: "live_preflight_failure", error: safeErrorEvidence(error) }));
  process.exit(1);
});
