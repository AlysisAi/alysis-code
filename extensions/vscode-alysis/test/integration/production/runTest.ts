import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  accessSync,
  constants,
  copyFileSync,
  createReadStream,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";

import { downloadAndUnzipVSCode } from "@vscode/test-electron";

import { allowlistedQaRuntimeEnvironment } from "../liveQaSecurity";
import { resolveSafeVsCodeCliInvocation } from "../vsCodeCliInvocation";
import { runVsCodeExtensionHostTests } from "../vsCodeExtensionHostInvocation";

function requiredEnvironment(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) {
    throw new Error(`${name} is required for installed production VSIX dogfood.`);
  }
  return value;
}

function verifyFile(path: string, label: string): void {
  const stat = statSync(path);
  if (!stat.isFile() || stat.size <= 0) {
    throw new Error(`${label} is not a non-empty file: ${path}`);
  }
}

function verifyExecutable(path: string): void {
  verifyFile(path, "VS Code executable");
  if (process.platform !== "win32") {
    accessSync(path, constants.X_OK);
  }
}

async function sha256File(path: string): Promise<string> {
  const hash = createHash("sha256");
  await new Promise<void>((resolvePromise, rejectPromise) => {
    const stream = createReadStream(path);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("error", rejectPromise);
    stream.on("end", resolvePromise);
  });
  return hash.digest("hex");
}

function removeTempDirectory(path: string): void {
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    console.warn(`Could not remove production dogfood directory ${path}: ${reason}`);
  }
}

function runCodeCli(
  vscodeExecutablePath: string,
  args: readonly string[]
): { stdout: string; stderr: string } {
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
    throw new Error(
      `VS Code CLI failed with ${String(result.status)}. stdout=${result.stdout} stderr=${result.stderr}`
    );
  }
  return { stdout: result.stdout, stderr: result.stderr };
}

async function main(): Promise<void> {
  const startedAt = new Date().toISOString();
  const vsixPath = resolve(requiredEnvironment("ALYSIS_PRODUCTION_VSIX_PATH"));
  const expectedTarget = requiredEnvironment("ALYSIS_EXPECTED_PLATFORM_TARGET");
  const nativeSignatureCheck = requiredEnvironment("ALYSIS_NATIVE_SIGNATURE_CHECK");
  const releaseSignatureCheck = requiredEnvironment("ALYSIS_RELEASE_SIGNATURE_CHECK");
  verifyFile(vsixPath, "Target VSIX");
  if (!basename(vsixPath).includes(expectedTarget)) {
    throw new Error(`Target VSIX filename does not identify ${expectedTarget}: ${vsixPath}`);
  }

  const tempRoot = mkdtempSync(join(tmpdir(), "alysis-production-vsix-"));
  const workspacePath = join(tempRoot, "workspace");
  const userDataDir = join(tempRoot, "user-data");
  const extensionsDir = join(tempRoot, "extensions");
  const driverRoot = join(tempRoot, "release-driver");
  const resultPaths = [
    join(tempRoot, "production-result-initial.json"),
    join(tempRoot, "production-result-restart.json")
  ];
  try {
    for (const directory of [workspacePath, userDataDir, extensionsDir, driverRoot]) {
      mkdirSync(directory, { recursive: true });
    }
    if (readdirSync(extensionsDir).length !== 0) {
      throw new Error("Production dogfood extensions directory was not empty before install.");
    }
    const settingsDirectory = join(workspacePath, ".vscode");
    mkdirSync(settingsDirectory, { recursive: true });
    writeFileSync(
      join(settingsDirectory, "settings.json"),
      JSON.stringify({ "alysis.cliPath": "", "alysis.autoStartBridge": false }, null, 2),
      { encoding: "utf8", mode: 0o600 }
    );

    const compiledDriverRoot = resolve(__dirname, "driver");
    copyFileSync(join(compiledDriverRoot, "extension.js"), join(driverRoot, "extension.js"));
    copyFileSync(join(compiledDriverRoot, "testRunner.js"), join(driverRoot, "testRunner.js"));
    writeFileSync(
      join(driverRoot, "package.json"),
      JSON.stringify(
        {
          name: "alysis-release-dogfood-driver",
          displayName: "Alysis Code Release Dogfood Driver",
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

    const requestedVersion = process.env.VSCODE_TEST_VERSION?.trim() || "1.90.0";
    const vscodeExecutablePath = process.env.VSCODE_TEST_EXECUTABLE_PATH?.trim()
      ? resolve(process.env.VSCODE_TEST_EXECUTABLE_PATH)
      : await downloadAndUnzipVSCode(requestedVersion);
    verifyExecutable(vscodeExecutablePath);
    const vscodeProbe = runCodeCli(vscodeExecutablePath, ["--version"]).stdout
      .split(/\r?\n/)
      .map((value) => value.trim())
      .filter(Boolean);
    if (
      vscodeProbe.length < 3 ||
      !/^\d+\.\d+\.\d+$/.test(vscodeProbe[0] ?? "") ||
      !/^[0-9a-f]{40}$/.test(vscodeProbe[1] ?? "") ||
      !/^(?:x64|arm64)$/.test(vscodeProbe[2] ?? "")
    ) {
      throw new Error("VS Code --version did not return a concrete version, commit, and architecture.");
    }
    if (requestedVersion !== "stable" && vscodeProbe[0] !== requestedVersion) {
      throw new Error(
        `Requested VS Code ${requestedVersion} but executable reports ${vscodeProbe[0]}.`
      );
    }
    const vscodeExecutableSha256 = await sha256File(vscodeExecutablePath);

    runCodeCli(vscodeExecutablePath, [
      "--install-extension",
      vsixPath,
      "--force",
      "--extensions-dir",
      extensionsDir,
      "--user-data-dir",
      userDataDir
    ]);
    const installed = runCodeCli(vscodeExecutablePath, [
      "--list-extensions",
      "--show-versions",
      "--extensions-dir",
      extensionsDir,
      "--user-data-dir",
      userDataDir
    ]).stdout.toLowerCase();
    if (!/^alysisai\.vscode-alysis@\S+$/m.test(installed)) {
      throw new Error(`Installed extension inventory did not contain Alysis Code: ${installed}`);
    }

    const phaseEvidence: Array<Record<string, unknown>> = [];
    for (const resultPath of resultPaths) {
      delete process.env.ELECTRON_RUN_AS_NODE;
      await runVsCodeExtensionHostTests({
        extensionDevelopmentPath: driverRoot,
        extensionTestsPath: join(driverRoot, "testRunner.js"),
        vscodeExecutablePath,
        version: requestedVersion,
        launchArgs: [
          workspacePath,
          "--user-data-dir",
          userDataDir,
          "--extensions-dir",
          extensionsDir,
          "--skip-welcome",
          "--skip-release-notes"
        ],
        extensionTestsEnv: {
          ...allowlistedQaRuntimeEnvironment(process.env),
          ALYSIS_PRODUCTION_DOGFOOD_RESULT: resultPath,
          ALYSIS_EXPECTED_EXTENSIONS_DIR: extensionsDir,
          ALYSIS_EXPECTED_PLATFORM_TARGET: expectedTarget,
          ALYSIS_NATIVE_SIGNATURE_CHECK: nativeSignatureCheck,
          ALYSIS_RELEASE_SIGNATURE_CHECK: releaseSignatureCheck
        }
      });
      if (!existsSync(resultPath)) {
        throw new Error("Production driver completed without writing runtime evidence.");
      }
      const evidence = JSON.parse(readFileSync(resultPath, "utf8")) as Record<string, unknown>;
      if (evidence.status !== "passed") {
        throw new Error("Production runtime evidence did not report passed status.");
      }
      phaseEvidence.push(evidence);
    }
    const [initialEvidence, evidence] = phaseEvidence;
    if (!initialEvidence || !evidence) {
      throw new Error("Production dogfood did not complete both Extension Host phases.");
    }
    for (const field of [
      "extension_version",
      "managed_artifact_version",
      "managed_cli_version",
      "managed_protocol_version",
      "managed_cli_sha256",
      "release_tag",
      "source_repository",
      "source_sha",
      "platform_target"
    ]) {
      if (initialEvidence[field] !== evidence[field]) {
        throw new Error(`Production restart changed immutable runtime evidence field ${field}.`);
      }
    }
    const summary = {
      ...evidence,
      schema_name: "installed-production-vsix",
      schema_version: 2,
      started_at: startedAt,
      completed_at: new Date().toISOString(),
      mode: "installed-production-vsix",
      release_valid: true,
      release_valid_reason:
        "Clean isolated-profile install selected and health-checked the signed managed runtime across an Extension Host restart.",
      extension_host: "passed",
      extension_under_test: "packaged_vsix",
      restart_check: "passed",
      restart_profile_reused: true,
      restart_runtime_identity_check: "passed",
      extension_host_launches: 2,
      vscode: {
        requested_version: requestedVersion,
        version: vscodeProbe[0],
        commit: vscodeProbe[1],
        arch: vscodeProbe[2],
        executable_sha256: vscodeExecutableSha256
      },
      vsix: vsixPath,
      vsix_sha256: createHash("sha256").update(readFileSync(vsixPath)).digest("hex")
    };
    const installedAfter = runCodeCli(vscodeExecutablePath, [
      "--list-extensions",
      "--show-versions",
      "--extensions-dir",
      extensionsDir,
      "--user-data-dir",
      userDataDir
    ]).stdout
      .split(/\r?\n/)
      .map((value) => value.trim().toLowerCase())
      .filter(Boolean);
    const expectedInstalled = `alysisai.vscode-alysis@${String(evidence.extension_version)}`;
    if (installedAfter.length !== 1 || installedAfter[0] !== expectedInstalled) {
      throw new Error(
        `Installed extension inventory was not exact. expected=${expectedInstalled} actual=${installedAfter.join(",")}`
      );
    }
    const outputPath = process.env.ALYSIS_PRODUCTION_DOGFOOD_OUTPUT?.trim();
    if (outputPath) {
      writeFileSync(resolve(outputPath), JSON.stringify(summary, null, 2) + "\n", {
        encoding: "utf8",
        mode: 0o600
      });
    }
    process.stdout.write(`${JSON.stringify(summary, null, 2)}\n`);
  } finally {
    removeTempDirectory(tempRoot);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
