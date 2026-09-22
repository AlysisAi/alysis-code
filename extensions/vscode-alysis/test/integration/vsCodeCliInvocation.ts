import { readFileSync, statSync } from "node:fs";
import { dirname, extname, isAbsolute, relative, resolve } from "node:path";

import { resolveCliArgsFromVSCodeExecutablePath } from "@vscode/test-electron";

export interface VsCodeCliInvocation {
  command: string;
  prefixArgs: string[];
  environment: NodeJS.ProcessEnv;
}

/**
 * Resolve the VS Code CLI without asking Node to concatenate arguments through a shell. On
 * Windows, @vscode/test-electron returns code.cmd; reproduce its two fixed environment settings
 * and invoke the trusted Code.exe + cli.js pair directly instead.
 */
export function resolveSafeVsCodeCliInvocation(
  vscodeExecutablePath: string,
  platform: NodeJS.Platform = process.platform
): VsCodeCliInvocation {
  const [resolvedCommand, ...prefixArgs] = resolveCliArgsFromVSCodeExecutablePath(
    vscodeExecutablePath,
    { reuseMachineInstall: true }
  );
  if (!resolvedCommand) {
    throw new Error("VS Code CLI resolver returned no executable.");
  }
  if (platform !== "win32" || extname(resolvedCommand).toLowerCase() !== ".cmd") {
    return { command: resolvedCommand, prefixArgs, environment: {} };
  }
  return resolveWindowsVsCodeCliWrapper(
    vscodeExecutablePath,
    resolvedCommand,
    readFileSync(resolvedCommand, "utf8"),
    prefixArgs
  );
}

export function resolveWindowsVsCodeCliWrapper(
  vscodeExecutablePath: string,
  wrapperPath: string,
  wrapperSource: string,
  prefixArgs: readonly string[] = []
): VsCodeCliInvocation {
  const executable = resolve(vscodeExecutablePath);
  const root = dirname(executable);
  const wrapper = resolve(wrapperPath);
  requireRegularFile(executable, "VS Code executable");
  requireRegularFile(wrapper, "VS Code CLI wrapper");
  requireContainedPath(root, wrapper, "VS Code CLI wrapper");

  const pattern = /"%~dp0\.\.\\(?:(?<version>[^"\\\r\n]+)\\)?resources\\app\\out\\cli\.js"\s+%\*/gim;
  const matches = [...wrapperSource.matchAll(pattern)];
  if (matches.length !== 1) {
    throw new Error("VS Code CLI wrapper does not contain one exact cli.js invocation.");
  }
  const versionDirectory = matches[0]?.groups?.version;
  const cliScript = resolve(
    dirname(wrapper),
    "..",
    ...(versionDirectory ? [versionDirectory] : []),
    "resources",
    "app",
    "out",
    "cli.js"
  );
  requireRegularFile(cliScript, "VS Code CLI script");
  requireContainedPath(root, cliScript, "VS Code CLI script");
  return {
    command: executable,
    prefixArgs: [cliScript, ...prefixArgs],
    environment: { ELECTRON_RUN_AS_NODE: "1", VSCODE_DEV: "" }
  };
}

function requireRegularFile(path: string, label: string): void {
  const stat = statSync(path);
  if (!stat.isFile()) {
    throw new Error(`${label} is not a regular file.`);
  }
}

function requireContainedPath(root: string, candidate: string, label: string): void {
  const child = relative(root, candidate);
  if (!child || child.startsWith("..") || isAbsolute(child)) {
    throw new Error(`${label} escapes the VS Code installation.`);
  }
}
