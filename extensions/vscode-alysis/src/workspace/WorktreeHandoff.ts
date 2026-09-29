import { createHash } from "node:crypto";
import path from "node:path";
import type * as vscode from "vscode";
import type { ChatSessionReference } from "../chat/ChatController";

export interface WorktreeHandoff { root: string; reference?: ChatSessionReference; createdAt: number }

export function worktreeHandoffKey(root: string): string {
  const normalized = path.resolve(root);
  return `alysis.worktreeHandoff.${createHash("sha256").update(process.platform === "win32" ? normalized.toLowerCase() : normalized).digest("hex")}`;
}

/** Only the destination window may consume its bounded, one-time handoff. No secrets. */
export async function consumeWorktreeHandoff(globalState: vscode.Memento, workspaceState: vscode.Memento,
  root: string): Promise<WorktreeHandoff | undefined> {
  const key = worktreeHandoffKey(root);
  const handoff = globalState.get<WorktreeHandoff>(key);
  if (!handoff || typeof handoff.root !== "string" || worktreeHandoffKey(handoff.root) !== key) return undefined;
  if (!Number.isFinite(handoff.createdAt) || Date.now() - handoff.createdAt > 24 * 60 * 60 * 1000) {
    await globalState.update(key, undefined);
    return undefined;
  }
  const reference = handoff.reference;
  if (reference) {
    if (reference.workspace?.scheme !== "file" || reference.workspace.authority !== ""
      || typeof reference.workspace.root !== "string" || worktreeHandoffKey(reference.workspace.root) !== key
      || typeof reference.sessionId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(reference.sessionId)) return undefined;
    // Save in workspace storage before removing the handoff so a crash can retry safely.
    await workspaceState.update("alysis.activeSessionId", reference);
  }
  await globalState.update(key, undefined);
  return handoff;
}
