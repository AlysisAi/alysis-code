import { isAbsolute } from "node:path";

import * as vscode from "vscode";

import { CliRunner, AlysisConfig, cliCommandEnvironment } from "../client/CliDiscovery";
import { CliValidationResult, discoverCliCandidates, validateCliExecutable } from "../client/cliLocator";
import { isPathInsideAnyWorkspace } from "../client/ExecutableResolver";
import { COMMANDS } from "./registry";

export interface LocateCliDeps {
  /** Runner used to probe a candidate (`--version` + `ide-bridge health`). */
  runner: CliRunner;
  getConfig: () => AlysisConfig;
  /** Re-check bridge health after a new CLI path is saved. */
  reconnect: () => Promise<void> | void;
  /** Best-effort extra dirs to probe (e.g. the active Python interpreter's dir). */
  pythonScriptDirs?: () => readonly string[];
}

interface LocatePick extends vscode.QuickPickItem {
  action: "candidate" | "browse" | "manual";
  cliPath?: string;
}

export function registerLocateCliCommand(context: vscode.ExtensionContext, deps: LocateCliDeps): void {
  context.subscriptions.push(
    vscode.commands.registerCommand(COMMANDS.locateCli, () => runLocateCli(deps))
  );
}

export async function runLocateCli(deps: LocateCliDeps): Promise<void> {
  const config = deps.getConfig();
  const candidates = discoverCliCandidates({
    cliPathSetting: config.cliPath,
    extraDirs: deps.pythonScriptDirs ? deps.pythonScriptDirs() : []
  });

  const picks: LocatePick[] = [
    ...candidates.map((candidate) => ({
      label: candidate.cliPath,
      description: candidate.label,
      action: "candidate" as const,
      cliPath: candidate.cliPath
    })),
    { label: "$(folder-opened) Browse…", description: "Pick an alysis executable", action: "browse" },
    { label: "$(edit) Enter path manually…", action: "manual" }
  ];

  const picked = await vscode.window.showQuickPick(picks, {
    title: "Use Development CLI Override",
    placeHolder: candidates.length
      ? "Select a non-production development executable, or browse"
      : "No development executable auto-detected — browse or enter a path"
  });
  if (!picked) {
    return;
  }

  const chosen = await resolveChosenPath(picked);
  if (!chosen) {
    return;
  }

  // Trust + executable-origin gate: never validate/spawn a workspace-local binary while the
  // workspace is untrusted (mirrors the bridge's executable-origin policy).
  if (!vscode.workspace.isTrusted && isPathInsideAnyWorkspace(chosen, workspaceRoots())) {
    await vscode.window.showWarningMessage(
      "That executable is inside this workspace. Grant Workspace Trust before Alysis Code will run a workspace-local CLI."
    );
    return;
  }

  const validation = await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `Validating ${chosen}…`, cancellable: false },
    () => validateCliExecutable(deps.runner, chosen, cliCommandEnvironment(config))
  );
  if (!validation.ok) {
    // Validation failed -> do NOT save. Surface a redacted reason.
    await vscode.window.showErrorMessage(`Alysis Code CLI at "${chosen}" failed validation: ${describeFailure(validation)}`);
    return;
  }

  const target = await pickSaveTarget();
  if (target === undefined) {
    return;
  }
  await vscode.workspace.getConfiguration("alysis").update("cliPath", chosen, target);
  await vscode.window.showInformationMessage(
    `Development CLI override set to ${chosen}${validation.alysisVersion ? ` (alysis ${validation.alysisVersion})` : ""}. This runtime is not release-verified.`
  );
  await deps.reconnect();
}

async function resolveChosenPath(picked: LocatePick): Promise<string | undefined> {
  if (picked.action === "candidate") {
    return picked.cliPath;
  }
  if (picked.action === "browse") {
    const uris = await vscode.window.showOpenDialog({
      canSelectMany: false,
      canSelectFolders: false,
      openLabel: "Use this Alysis Code CLI",
      title: "Select the alysis executable"
    });
    return uris?.[0]?.fsPath;
  }
  const entered = await vscode.window.showInputBox({
    title: "Alysis Code CLI path",
    prompt: "Absolute path to the alysis executable.",
    ignoreFocusOut: true,
    validateInput: (value) => {
      const trimmed = value.trim();
      if (trimmed.length === 0) {
        return "A path is required.";
      }
      // Require an absolute path so the trust/origin gate inspects the same path that gets spawned
      // (a bare name is cwd-relative to the gate but PATH-resolved by the runner). Bare names on
      // PATH are surfaced as detected candidates instead.
      if (!isAbsolute(trimmed)) {
        return "Enter an absolute path, or use Browse.";
      }
      return undefined;
    }
  });
  return entered?.trim() || undefined;
}

async function pickSaveTarget(): Promise<vscode.ConfigurationTarget | undefined> {
  const hasWorkspace = (vscode.workspace.workspaceFolders ?? []).length > 0;
  // A workspace-scoped cliPath is a restricted setting — ignored until the workspace is trusted —
  // so saving it there while untrusted would look successful but never take effect. Save globally.
  if (!hasWorkspace || !vscode.workspace.isTrusted) {
    return vscode.ConfigurationTarget.Global;
  }
  const scope = await vscode.window.showQuickPick(
    [
      { label: "This workspace", target: vscode.ConfigurationTarget.Workspace },
      { label: "All workspaces (global)", target: vscode.ConfigurationTarget.Global }
    ],
    { title: "Save Alysis Code CLI path", placeHolder: "Where should this CLI path apply?" }
  );
  return scope?.target;
}

function workspaceRoots(): string[] {
  return (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath);
}

function describeFailure(validation: CliValidationResult): string {
  return validation.message ?? validation.code ?? "the binary did not pass the health probe.";
}
