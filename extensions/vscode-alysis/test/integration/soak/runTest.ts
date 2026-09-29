import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { runVsCodeExtensionHostTests } from "../vsCodeExtensionHostInvocation";

async function main(): Promise<void> {
  const extensionDevelopmentPath = resolve(__dirname, "../../../..");
  const extensionTestsPath = resolve(__dirname, "suite");
  const workspacePath = mkdtempSync(join(tmpdir(), "alysis-soak-workspace-"));
  const userDataDir = mkdtempSync(join(tmpdir(), "alysis-soak-user-data-"));
  const extensionsDir = mkdtempSync(join(tmpdir(), "alysis-soak-extensions-"));
  const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim();
  if (!vscodeExecutablePath) {
    throw new Error("VSCODE_TEST_EXECUTABLE_PATH is required for the one-hour Extension Host soak.");
  }
  const mockCliPath = resolve(
    extensionDevelopmentPath,
    "test",
    "integration",
    "fixtures",
    "mock-alysis-cli.js"
  );
  chmodSync(mockCliPath, 0o755);
  mkdirSync(join(workspacePath, ".vscode"), { recursive: true });
  writeFileSync(
    join(workspacePath, ".vscode", "settings.json"),
    JSON.stringify(
      {
        "alysis.cliPath": mockCliPath,
        "alysis.autoStartBridge": true,
        "alysis.defaultMode": "readonly"
      },
      null,
      2
    )
  );

  delete process.env.ELECTRON_RUN_AS_NODE;
  try {
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
      extensionTestsEnv: {
        ...process.env,
        ALYSIS_TEST_NODE_PATH: process.execPath,
        ALYSIS_TEST_CLI_PATH: mockCliPath,
        ALYSIS_EXTENSION_SOAK: "1"
      }
    });
  } finally {
    rmSync(workspacePath, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    rmSync(userDataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    rmSync(extensionsDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
