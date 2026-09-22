import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  accessSync,
  chmodSync,
  constants,
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  statSync,
  writeFileSync
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";

import { downloadAndUnzipVSCode } from "@vscode/test-electron";

import { allowlistedQaRuntimeEnvironment } from "../liveQaSecurity";
import { resolveSafeVsCodeCliInvocation } from "../vsCodeCliInvocation";
import {
  APPROVED_UNTRUSTED_VSCODE_VERSION,
  buildUntrustedProductionSummary,
  requireExactAlysisInventory,
  UntrustedExtensionHostEvidence
} from "./contract";

const EXTENSION_HOST_TIMEOUT_MS = 180_000;

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required for untrusted-production Extension Host QA.`);
  }
  return value;
}

function verifyFile(path: string, label: string): void {
  const stat = statSync(path);
  if (!stat.isFile() || stat.size <= 0) {
    throw new Error(`${label} is not a non-empty file.`);
  }
}

function verifyExecutable(path: string): void {
  verifyFile(path, "VS Code executable");
  if (process.platform !== "win32") {
    accessSync(path, constants.X_OK);
  }
}

function runCodeCli(vscodeExecutablePath: string, args: readonly string[]): string {
  const invocation = resolveSafeVsCodeCliInvocation(vscodeExecutablePath);
  const result = spawnSync(invocation.command, [...invocation.prefixArgs, ...args], {
    encoding: "utf8",
    maxBuffer: 8 * 1024 * 1024,
    shell: false,
    windowsHide: true,
    env: {
      ...allowlistedQaRuntimeEnvironment(process.env),
      ...invocation.environment
    }
  });
  if (result.error) {
    throw result.error;
  }
  if (result.status !== 0) {
    throw new Error(`VS Code CLI failed with exit code ${String(result.status)}.`);
  }
  return String(result.stdout ?? "");
}

async function runExtensionHost(
  vscodeExecutablePath: string,
  args: readonly string[],
  environment: NodeJS.ProcessEnv
): Promise<void> {
  if (args.some((value) => value === "--disable-workspace-trust" || value.startsWith("--disable-workspace-trust="))) {
    throw new Error("Untrusted-production QA must not disable Workspace Trust.");
  }
  await new Promise<void>((resolvePromise, reject) => {
    const child = spawn(vscodeExecutablePath, [...args], {
      env: environment,
      shell: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"]
    });
    const stdoutHash = createHash("sha256");
    const stderrHash = createHash("sha256");
    let stdoutLength = 0;
    let stderrLength = 0;
    let settled = false;
    child.stdout.on("data", (value: Buffer) => {
      stdoutLength += value.length;
      stdoutHash.update(value);
    });
    child.stderr.on("data", (value: Buffer) => {
      stderrLength += value.length;
      stderrHash.update(value);
    });
    const finish = (error?: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      if (error) {
        reject(error);
      } else {
        resolvePromise();
      }
    };
    const timeout = setTimeout(() => {
      child.kill("SIGKILL");
      finish(new Error("Untrusted-production Extension Host timed out."));
    }, EXTENSION_HOST_TIMEOUT_MS);
    child.once("error", (error) => finish(error));
    child.once("close", (code, signal) => {
      if (code === 0) {
        finish();
        return;
      }
      finish(
        new Error(
          `Untrusted-production Extension Host failed (${String(code ?? signal)}; ` +
            `stdout=${stdoutLength}:${stdoutHash.digest("hex")}; ` +
            `stderr=${stderrLength}:${stderrHash.digest("hex")}).`
        )
      );
    });
  });
}

function writeMaliciousCli(workspacePath: string, sentinelPath: string): string {
  const executablePath = join(
    workspacePath,
    process.platform === "win32" ? "workspace-alysis.cmd" : "workspace-alysis"
  );
  const content = process.platform === "win32"
    ? `@echo off\r\n>"${sentinelPath}" echo executed\r\nexit /b 97\r\n`
    : `#!/bin/sh\nprintf executed > '${sentinelPath.replaceAll("'", "'\\''")}'\nexit 97\n`;
  writeFileSync(executablePath, content, { encoding: "utf8", mode: 0o700 });
  if (process.platform !== "win32") {
    chmodSync(executablePath, 0o700);
  }
  return executablePath;
}

async function main(): Promise<void> {
  const startedAt = new Date().toISOString();
  const vsixPath = resolve(requiredEnvironment("ALYSIS_PRODUCTION_VSIX_PATH"));
  const expectedTarget = requiredEnvironment("ALYSIS_EXPECTED_PLATFORM_TARGET");
  const outputPath = resolve(requiredEnvironment("ALYSIS_UNTRUSTED_PRODUCTION_OUTPUT"));
  const releaseTag = requiredEnvironment("ALYSIS_RELEASE_TAG");
  const sourceSha = requiredEnvironment("ALYSIS_SOURCE_SHA");
  const candidateRunIdText = requiredEnvironment("ALYSIS_CANDIDATE_RUN_ID");
  const workflowRunIdText = requiredEnvironment("ALYSIS_WORKFLOW_RUN_ID");
  const workflowRunAttemptText = requiredEnvironment("ALYSIS_WORKFLOW_RUN_ATTEMPT");
  const expectedVsixSha256 = requiredEnvironment("ALYSIS_EXPECTED_VSIX_SHA256");
  const nativeSignatureCheck = requiredEnvironment("ALYSIS_NATIVE_SIGNATURE_CHECK");
  const releaseSignatureCheck = requiredEnvironment("ALYSIS_RELEASE_SIGNATURE_CHECK");
  if (!/^v\d+\.\d+\.\d+$/.test(releaseTag) || !/^[0-9a-f]{40}$/.test(sourceSha)) {
    throw new Error("Release identity is invalid.");
  }
  if (
    !/^[1-9][0-9]*$/.test(candidateRunIdText) ||
    !/^[1-9][0-9]*$/.test(workflowRunIdText) ||
    !/^[1-9][0-9]*$/.test(workflowRunAttemptText)
  ) {
    throw new Error("Workflow run identity is invalid.");
  }
  if (!/^[0-9a-f]{64}$/.test(expectedVsixSha256)) {
    throw new Error("Expected candidate SHA-256 is invalid.");
  }
  verifyFile(vsixPath, "Target VSIX");
  if (basename(vsixPath) !== `vscode-alysis-${expectedTarget}.vsix`) {
    throw new Error("Target VSIX filename does not exactly identify the expected platform.");
  }
  const vsixSha256 = createHash("sha256").update(readFileSync(vsixPath)).digest("hex");
  if (vsixSha256 !== expectedVsixSha256) {
    throw new Error("Target VSIX SHA-256 differs from the attestation-verified candidate.");
  }

  const tempRoot = mkdtempSync(join(tmpdir(), "alysis-untrusted-production-"));
  const workspacePath = join(tempRoot, "workspace");
  const userDataDir = join(tempRoot, "user-data");
  const extensionsDir = join(tempRoot, "extensions");
  const driverRoot = join(tempRoot, "untrusted-driver");
  const resultPath = join(tempRoot, "untrusted-extension-host-result.json");
  const maliciousSentinel = join(tempRoot, "malicious-cli-executed");
  try {
    for (const directory of [workspacePath, userDataDir, extensionsDir, driverRoot]) {
      mkdirSync(directory, { recursive: true });
    }
    if (readdirSync(userDataDir).length !== 0 || readdirSync(extensionsDir).length !== 0) {
      throw new Error("Untrusted-production profile was not empty before setup.");
    }
    const maliciousCliPath = writeMaliciousCli(workspacePath, maliciousSentinel);
    const settingsDirectory = join(workspacePath, ".vscode");
    mkdirSync(settingsDirectory, { recursive: true });
    writeFileSync(
      join(settingsDirectory, "settings.json"),
      JSON.stringify(
        {
          "alysis.cliPath": maliciousCliPath,
          "alysis.autoStartBridge": true
        },
        null,
        2
      ),
      { encoding: "utf8", mode: 0o600 }
    );

    const compiledDriverRoot = resolve(__dirname, "driver");
    copyFileSync(join(compiledDriverRoot, "extension.js"), join(driverRoot, "extension.js"));
    copyFileSync(join(compiledDriverRoot, "testRunner.js"), join(driverRoot, "testRunner.js"));
    writeFileSync(
      join(driverRoot, "package.json"),
      JSON.stringify(
        {
          name: "alysis-untrusted-production-driver",
          displayName: "Alysis Code Untrusted Production Driver",
          publisher: "alysisai",
          version: "0.0.1",
          private: true,
          engines: { vscode: "^1.90.0" },
          activationEvents: ["*"],
          main: "./extension.js"
        },
        null,
        2
      ),
      { encoding: "utf8", mode: 0o600 }
    );

    const requestedVersion =
      process.env.VSCODE_TEST_VERSION?.trim() || APPROVED_UNTRUSTED_VSCODE_VERSION;
    if (requestedVersion !== APPROVED_UNTRUSTED_VSCODE_VERSION) {
      throw new Error(
        `Untrusted-production QA requires VS Code ${APPROVED_UNTRUSTED_VSCODE_VERSION}.`
      );
    }
    const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim()
      ? resolve(process.env.VSCODE_TEST_EXECUTABLE_PATH)
      : await downloadAndUnzipVSCode(requestedVersion);
    verifyExecutable(vscodeExecutablePath);

    runCodeCli(vscodeExecutablePath, [
      "--install-extension",
      vsixPath,
      "--force",
      "--extensions-dir",
      extensionsDir,
      "--user-data-dir",
      userDataDir
    ]);
    requireExactAlysisInventory(
      runCodeCli(vscodeExecutablePath, [
        "--list-extensions",
        "--show-versions",
        "--extensions-dir",
        extensionsDir,
        "--user-data-dir",
        userDataDir
      ])
    );

    const launchArgs = [
      workspacePath,
      "--user-data-dir",
      userDataDir,
      "--extensions-dir",
      extensionsDir,
      "--no-sandbox",
      "--disable-gpu-sandbox",
      "--disable-updates",
      "--skip-welcome",
      "--skip-release-notes",
      `--extensionDevelopmentPath=${driverRoot}`,
      `--extensionTestsPath=${join(driverRoot, "testRunner.js")}`
    ];
    const childEnvironment = allowlistedQaRuntimeEnvironment(process.env);
    Object.assign(childEnvironment, {
      ALYSIS_UNTRUSTED_PRODUCTION_RESULT: resultPath,
      ALYSIS_EXPECTED_EXTENSIONS_DIR: extensionsDir,
      ALYSIS_EXPECTED_PLATFORM_TARGET: expectedTarget,
      ALYSIS_EXPECTED_VSCODE_VERSION: APPROVED_UNTRUSTED_VSCODE_VERSION,
      ALYSIS_MALICIOUS_WORKSPACE_CLI: maliciousCliPath,
      ALYSIS_MALICIOUS_CLI_SENTINEL: maliciousSentinel,
      ALYSIS_NATIVE_SIGNATURE_CHECK: nativeSignatureCheck,
      ALYSIS_RELEASE_SIGNATURE_CHECK: releaseSignatureCheck
    });
    await runExtensionHost(vscodeExecutablePath, launchArgs, childEnvironment);
    if (!existsSync(resultPath)) {
      throw new Error("Untrusted-production driver did not write Extension Host evidence.");
    }
    const evidence = JSON.parse(
      readFileSync(resultPath, "utf8")
    ) as UntrustedExtensionHostEvidence;
    const installedInventory = requireExactAlysisInventory(
      runCodeCli(vscodeExecutablePath, [
        "--list-extensions",
        "--show-versions",
        "--extensions-dir",
        extensionsDir,
        "--user-data-dir",
        userDataDir
      ]),
      String(evidence.extension_version ?? "")
    );
    const summary = buildUntrustedProductionSummary({
      startedAt,
      completedAt: new Date().toISOString(),
      releaseTag,
      sourceSha,
      candidateRunId: Number(candidateRunIdText),
      workflowRunId: Number(workflowRunIdText),
      workflowRunAttempt: Number(workflowRunAttemptText),
      expectedTarget,
      vsixName: basename(vsixPath),
      vsixSha256,
      installedInventory,
      evidence
    });
    writeFileSync(outputPath, JSON.stringify(summary, null, 2) + "\n", {
      encoding: "utf8",
      mode: 0o600
    });
    process.stdout.write(
      `${JSON.stringify({ status: "passed", target: expectedTarget, report_written: true })}\n`
    );
  } finally {
    rmSync(tempRoot, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

main().catch((error) => {
  const name = error instanceof Error ? error.name : "UnknownError";
  const message = error instanceof Error ? error.message : String(error);
  process.stderr.write(
    `${JSON.stringify({
      status: "failed",
      error: {
        name,
        length: message.length,
        sha256: createHash("sha256").update(message, "utf8").digest("hex")
      }
    })}\n`
  );
  process.exit(1);
});
