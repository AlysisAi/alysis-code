import path from "node:path";

import type * as vscode from "vscode";

type WorkspaceScopeApi = Pick<typeof vscode.workspace, "workspaceFolders">;

/**
 * Return true only for a real document owned by the selected workspace root.
 *
 * Remote workspace schemes are accepted when they match the owning workspace
 * folder. Untitled, Git virtual documents, output channels, and a different
 * multi-root folder are deliberately excluded even when their URI happens to
 * carry a similar path.
 */
export function isFileBackedUriInWorkspaceRoot(
  workspace: WorkspaceScopeApi,
  uri: vscode.Uri,
  workspaceRoot: string
): boolean {
  const root = workspaceRoot.trim();
  if (!root || !uri || typeof uri.fsPath !== "string" || !uri.fsPath) {
    return false;
  }
  const owningFolder = workspace.workspaceFolders?.find((folder) => samePath(folder.uri.fsPath, root));
  if (owningFolder) {
    if (uri.scheme !== owningFolder.uri.scheme || uri.authority !== owningFolder.uri.authority) {
      return false;
    }
  } else if (uri.scheme !== "file") {
    return false;
  }
  return isPathInside(root, uri.fsPath);
}

function isPathInside(root: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function samePath(left: string, right: string): boolean {
  return path.relative(path.resolve(left), path.resolve(right)) === "";
}
