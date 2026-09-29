import type * as vscode from "vscode";

import { isFileBackedUriInWorkspaceRoot } from "./fileBackedWorkspace";

export class DirtyWorkspaceDocumentsError extends Error {
  public readonly code = "dirty_workspace_documents";

  public constructor(
    public readonly action: string,
    public readonly documentCount: number
  ) {
    super(
      `Save or revert ${documentCount} unsaved workspace ${documentCount === 1 ? "file" : "files"} before ${action}. ` +
      "Alysis Code will not start a write-capable workflow while editor changes are unsaved. Read-only chat remains available."
    );
    this.name = "DirtyWorkspaceDocumentsError";
  }
}

/** Fail closed before an external backend can race unsaved editor buffers. */
export function assertNoDirtyWorkspaceDocuments(
  vscodeApi: typeof vscode,
  workspaceRoot: string,
  action: string
): void {
  const dirtyCount = vscodeApi.workspace.textDocuments.filter((document) =>
    document.isDirty
    && isFileBackedUriInWorkspaceRoot(vscodeApi.workspace, document.uri, workspaceRoot)
  ).length;
  if (dirtyCount > 0) {
    throw new DirtyWorkspaceDocumentsError(action, dirtyCount);
  }
}

export function isDirtyWorkspaceDocumentsError(error: unknown): error is DirtyWorkspaceDocumentsError {
  return error instanceof DirtyWorkspaceDocumentsError;
}
