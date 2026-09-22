import assert from "node:assert/strict";
import test from "node:test";

import type * as vscode from "vscode";

import {
  assertNoDirtyWorkspaceDocuments,
  DirtyWorkspaceDocumentsError
} from "../src/workspace/DirtyWorkspaceGuard";
import { isFileBackedUriInWorkspaceRoot } from "../src/workspace/fileBackedWorkspace";

test("dirty workspace guard counts only real files owned by the selected multi-root folder", () => {
  const api = workspaceApi(
    [
      folder("file", "", "/workspace/primary"),
      folder("file", "", "/workspace/secondary")
    ],
    [
      document(true, uri("file", "", "/workspace/primary/src/dirty.ts")),
      document(true, uri("file", "", "/workspace/secondary/src/other.ts")),
      document(true, uri("file", "", "/workspace/primary-sibling/not-owned.ts")),
      document(true, uri("untitled", "", "/workspace/primary/src/scratch.ts")),
      document(true, uri("git", "", "/workspace/primary/src/dirty.ts")),
      document(false, uri("file", "", "/workspace/primary/src/clean.ts"))
    ]
  );

  assert.throws(
    () => assertNoDirtyWorkspaceDocuments(api, "/workspace/primary", "starting this run"),
    (error) => error instanceof DirtyWorkspaceDocumentsError
      && error.documentCount === 1
      && /Save or revert 1 unsaved workspace file/.test(error.message)
      && /Read-only chat remains available/.test(error.message)
  );
});

test("dirty workspace guard does not block a selected root for dirty files in another root or virtual documents", () => {
  const api = workspaceApi(
    [
      folder("file", "", "/workspace/primary"),
      folder("file", "", "/workspace/secondary")
    ],
    [
      document(true, uri("file", "", "/workspace/secondary/src/other.ts")),
      document(true, uri("untitled", "", "/workspace/primary/src/scratch.ts")),
      document(true, uri("git", "", "/workspace/primary/src/version.ts"))
    ]
  );

  assert.doesNotThrow(() => assertNoDirtyWorkspaceDocuments(api, "/workspace/primary", "starting this run"));
});

test("file-backed workspace scope accepts only the matching remote authority", () => {
  const api = workspaceApi(
    [folder("vscode-remote", "ssh-remote+dev", "/work/project")],
    []
  );

  assert.equal(
    isFileBackedUriInWorkspaceRoot(api.workspace, uri("vscode-remote", "ssh-remote+dev", "/work/project/a.ts"), "/work/project"),
    true
  );
  assert.equal(
    isFileBackedUriInWorkspaceRoot(api.workspace, uri("vscode-remote", "ssh-remote+other", "/work/project/a.ts"), "/work/project"),
    false
  );
  assert.equal(
    isFileBackedUriInWorkspaceRoot(api.workspace, uri("file", "", "/work/project/a.ts"), "/work/project"),
    false
  );
});

function workspaceApi(
  workspaceFolders: vscode.WorkspaceFolder[],
  textDocuments: vscode.TextDocument[]
): typeof vscode {
  return { workspace: { workspaceFolders, textDocuments } } as unknown as typeof vscode;
}

function folder(scheme: string, authority: string, fsPath: string): vscode.WorkspaceFolder {
  return { uri: uri(scheme, authority, fsPath) } as vscode.WorkspaceFolder;
}

function document(isDirty: boolean, documentUri: vscode.Uri): vscode.TextDocument {
  return { isDirty, uri: documentUri } as vscode.TextDocument;
}

function uri(scheme: string, authority: string, fsPath: string): vscode.Uri {
  return {
    scheme,
    authority,
    fsPath,
    toString: () => `${scheme}://${authority}${fsPath}`
  } as vscode.Uri;
}
