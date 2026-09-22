import path from "node:path";

import type * as vscode from "vscode";

import { isSensitivePath } from "../context/IdeContextCollector";
import { StartViewMention } from "../views/StartViewProvider";

/**
 * Search only the workspace folder that owns the current job. The explicit folder parameter keeps
 * multi-root resolution in the extension host and makes it impossible for the webview to widen the
 * search scope with a crafted query.
 */
export async function searchWorkspaceMentions(
  vscodeApi: typeof vscode,
  folder: vscode.WorkspaceFolder | undefined,
  query: string
): Promise<StartViewMention[]> {
  if (!folder) {
    return [];
  }
  const normalized = query.trim().replace(/[^\w./-]/g, "").replace(/\.{2,}/g, "");
  const pattern = normalized.length > 0 ? `**/*${normalized}*` : "**/*";
  try {
    const uris = await vscodeApi.workspace.findFiles(
      new vscodeApi.RelativePattern(folder, pattern),
      "**/{node_modules,.git,out,dist,.venv}/**",
      30
    );
    return uris.flatMap((uri) => {
      const relative = path.relative(folder.uri.fsPath, uri.fsPath);
      if (!isContainedRelativePath(relative) || isSensitivePath(uri.fsPath)) {
        return [];
      }
      const normalizedRelative = relative.replace(/\\/g, "/");
      const segments = normalizedRelative.split("/");
      return [{
        label: segments[segments.length - 1] ?? normalizedRelative,
        detail: normalizedRelative,
        insert: normalizedRelative,
        kind: "file" as const
      }];
    });
  } catch {
    return [];
  }
}

function isContainedRelativePath(relative: string): boolean {
  return relative.length > 0
    && relative !== ".."
    && !relative.startsWith(`..${path.sep}`)
    && !path.isAbsolute(relative);
}
