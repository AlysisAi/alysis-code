import { spawn } from "node:child_process";

import {
  downloadAndUnzipVSCode,
  runTests,
  type TestOptions
} from "@vscode/test-electron";

const REQUIRED_ISOLATION_ARGUMENTS = ["user-data-dir", "extensions-dir"] as const;

function hasArgument(args: readonly string[], name: string): boolean {
  return args.some((value, index) =>
    value === `--${name}` ? Boolean(args[index + 1]) : value.startsWith(`--${name}=`)
  );
}

export function buildVsCodeExtensionHostArguments(options: TestOptions): string[] {
  const launchArgs = [...(options.launchArgs ?? [])];
  for (const name of REQUIRED_ISOLATION_ARGUMENTS) {
    if (!hasArgument(launchArgs, name)) {
      throw new Error(`VS Code Extension Host tests require an explicit --${name}.`);
    }
  }
  const extensionDevelopmentPaths = Array.isArray(options.extensionDevelopmentPath)
    ? options.extensionDevelopmentPath
    : [options.extensionDevelopmentPath];
  return [
    ...launchArgs,
    "--no-sandbox",
    "--disable-gpu-sandbox",
    "--disable-updates",
    "--skip-welcome",
    "--skip-release-notes",
    "--disable-workspace-trust",
    `--extensionTestsPath=${options.extensionTestsPath}`,
    ...extensionDevelopmentPaths.map((value) => `--extensionDevelopmentPath=${value}`)
  ];
}

/**
 * Run Extension Host tests without Node's unsafe Windows shell-mode fallback. Upstream remains
 * responsible for non-Windows launches and downloads; Windows executes the trusted Code.exe path
 * directly with an argument array and an explicitly isolated profile.
 */
export async function runVsCodeExtensionHostTests(options: TestOptions): Promise<number> {
  if (process.platform !== "win32") {
    return runTests(options);
  }
  const executable = options.vscodeExecutablePath?.trim()
    ? options.vscodeExecutablePath
    : await downloadAndUnzipVSCode(options);
  const args = buildVsCodeExtensionHostArguments(options);
  return await new Promise<number>((resolvePromise, reject) => {
    const child = spawn(executable, args, {
      env: { ...process.env, ...(options.extensionTestsEnv ?? {}) },
      shell: false,
      stdio: "inherit",
      windowsHide: true
    });
    let settled = false;
    const finish = (error?: Error, code = 0): void => {
      if (settled) {
        return;
      }
      settled = true;
      process.removeListener("SIGINT", handleInterrupt);
      if (error) {
        reject(error);
      } else {
        resolvePromise(code);
      }
    };
    const handleInterrupt = (): void => {
      child.kill("SIGINT");
    };
    process.once("SIGINT", handleInterrupt);
    child.once("error", (error) => finish(error));
    child.once("close", (code, signal) => {
      if (code === 0) {
        finish(undefined, 0);
        return;
      }
      finish(
        new Error(
          signal
            ? `VS Code Extension Host tests terminated by ${signal}.`
            : `VS Code Extension Host tests failed with exit code ${String(code)}.`
        )
      );
    });
  });
}
