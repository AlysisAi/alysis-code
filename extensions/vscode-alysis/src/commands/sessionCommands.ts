import * as vscode from "vscode";

import { BackendActionController } from "../backend/BackendActionController";
import { redactForDisplay } from "../client/CliDiscovery";

// Per-session tree actions. Each routes through BackendActionController.executeAction so the existing
// gate chain (capability + Workspace-Trust + confirm modal for mutations) and the result-card
// lifecycle (running -> ok/error, redacted) all apply. Menu visibility in package.json restricts
// live-session mutations (compact/clear) to the active row.
const SESSION_ITEM_COMMANDS: ReadonlyArray<{ commandId: string; actionId: string; passSessionId: boolean }> = [
  { commandId: "alysis.session.show", actionId: "session.show", passSessionId: true },
  { commandId: "alysis.session.score", actionId: "session.score", passSessionId: true },
  { commandId: "alysis.session.resume", actionId: "session.resume", passSessionId: true },
  { commandId: "alysis.session.usage", actionId: "session.usage", passSessionId: true },
  // Active-session mutations ignore the arg and operate on the live session via active context.
  { commandId: "alysis.session.compact", actionId: "session.compact", passSessionId: false },
  { commandId: "alysis.session.clear", actionId: "session.clear", passSessionId: false }
];

export function registerSessionCommands(
  context: vscode.ExtensionContext,
  backendActions: BackendActionController
): void {
  for (const { commandId, actionId, passSessionId } of SESSION_ITEM_COMMANDS) {
    context.subscriptions.push(
      vscode.commands.registerCommand(commandId, async (item: unknown) => {
        const sessionId = sessionIdFromTreeItem(item);
        try {
          await backendActions.executeAction(actionId, passSessionId ? sessionId : "");
        } catch (error) {
          await vscode.window.showWarningMessage(
            redactForDisplay(error instanceof Error ? error.message : String(error))
          );
        }
      })
    );
  }
}

function sessionIdFromTreeItem(item: unknown): string {
  if (item && typeof item === "object" && "sessionId" in item) {
    const sessionId = (item as { sessionId?: unknown }).sessionId;
    if (typeof sessionId === "string") {
      return sessionId;
    }
  }
  if (item && typeof item === "object" && "label" in item) {
    const label = (item as { label?: unknown }).label;
    if (typeof label === "string") {
      return label;
    }
    if (label && typeof label === "object" && "label" in label) {
      return String((label as { label?: unknown }).label ?? "");
    }
  }
  return "";
}
