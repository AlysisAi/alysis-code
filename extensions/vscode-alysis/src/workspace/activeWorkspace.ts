import * as vscode from "vscode";

/**
 * Resolve the workspace scope without silently choosing folder zero in a multi-root window.
 * The active editor is the only unambiguous implicit signal; callers must stop and explain
 * how to select a scope when there is no active editor.
 */
export function activeWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders?.length) {
    return undefined;
  }
  if (folders.length === 1) {
    return folders[0];
  }
  const uri = vscode.window.activeTextEditor?.document.uri;
  return uri ? vscode.workspace.getWorkspaceFolder(uri) : undefined;
}

export function activeWorkspaceRoot(): string | undefined {
  return activeWorkspaceFolder()?.uri.fsPath;
}

export function workspaceScopeRequiredMessage(action: string): string {
  const folders = vscode.workspace.workspaceFolders;
  return folders && folders.length > 1
    ? `Focus a file in the workspace folder you want to use before ${action}; Alysis Code will not silently choose a different root.`
    : `Open a workspace folder before ${action}.`;
}
