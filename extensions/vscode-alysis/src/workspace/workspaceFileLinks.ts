import { realpath } from "node:fs/promises";
import path from "node:path";
import * as vscode from "vscode";

import { activeWorkspaceFolder } from "./activeWorkspace";

/** Resolve chat file references within the selected workspace, without guessing between files. */
export async function resolveWorkspaceFileReference(
  reference: string
): Promise<{ uri: vscode.Uri; selection?: vscode.Range } | undefined> {
  const folder = activeWorkspaceFolder();
  if (!folder) return undefined;
  const match = /^(.*?)(?::(\d+))?(?::(\d+))?$/.exec(unquote(reference.trim()));
  const name = unquote(match?.[1] ?? "").replaceAll("\\", "/");
  if (!name || name.includes("\0") || /^[a-z][a-z\d+.-]*:\/\//i.test(name)) return undefined;
  const line = match?.[2] === undefined ? undefined : Number(match[2]);
  const column = match?.[3] === undefined ? 1 : Number(match[3]);
  if ((line !== undefined && !Number.isSafeInteger(line)) || !Number.isSafeInteger(column)) return undefined;
  const selection = line === undefined ? undefined : new vscode.Range(
    Math.max(0, line - 1), Math.max(0, column - 1), Math.max(0, line - 1), Math.max(0, column - 1)
  );
  const absolute = path.resolve(folder.uri.fsPath, name);
  if (!isInside(folder.uri.fsPath, absolute)) return undefined;
  const relative = path.relative(folder.uri.fsPath, absolute);
  const candidate = vscode.Uri.joinPath(folder.uri, ...relative.split(path.sep));
  try {
    const root = await realpath(folder.uri.fsPath);
    const safeFile = async (uri: vscode.Uri): Promise<boolean> => {
      try {
        return isInside(folder.uri.fsPath, uri.fsPath)
          && isInside(root, await realpath(uri.fsPath))
          && ((await vscode.workspace.fs.stat(uri)).type & vscode.FileType.File) !== 0;
      } catch {
        return false;
      }
    };
    if (await safeFile(candidate)) return { uri: candidate, selection };
    // Only a bare basename permits a search. Missing explicit paths must not open an unrelated file.
    if (name.includes("/") || path.isAbsolute(name) || /[*?{}\[\]]/.test(name)) return undefined;
    const found = await vscode.workspace.findFiles(
      new vscode.RelativePattern(folder, `**/${name}`), "**/{node_modules,.git}/**", 2
    );
    if (found.length !== 1 || !(await safeFile(found[0]))) return undefined;
    return { uri: found[0], selection };
  } catch {
    return undefined;
  }
}

function unquote(value: string): string {
  return value.length >= 2 && ["\"", "'", "`"].includes(value[0]) && value.at(-1) === value[0]
    ? value.slice(1, -1) : value;
}

function isInside(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative !== "" && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}
