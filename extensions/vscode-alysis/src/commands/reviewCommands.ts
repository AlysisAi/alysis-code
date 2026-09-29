import * as vscode from "vscode";

import type { BackendActionController } from "../backend/BackendActionController";
import { redactForDisplay } from "../client/CliDiscovery";
import { COMMANDS } from "./registry";

/** Shared Palette/Cockpit boundary, including prompts and the full presenter lifecycle. */
export function registerReviewCommands(
  context: vscode.ExtensionContext,
  backendActions: Pick<BackendActionController, "executeAction">
): () => Promise<void> {
  let reviewing = false;
  const reviewGitChanges = async (): Promise<void> => {
    if (reviewing) return;
    reviewing = true;
    try {
      await backendActions.executeAction("code.review.start");
    } catch (error) {
      await vscode.window.showWarningMessage(
        redactForDisplay(error instanceof Error ? error.message : String(error))
      );
    } finally {
      reviewing = false;
    }
  };
  context.subscriptions.push(
    vscode.commands.registerCommand(COMMANDS.reviewGitChanges, reviewGitChanges),
    vscode.commands.registerCommand(COMMANDS.openSourceControl, async () => {
      try {
        await vscode.commands.executeCommand("workbench.view.scm");
      } catch (error) {
        await vscode.window.showWarningMessage(
          redactForDisplay(error instanceof Error ? error.message : String(error))
        );
      }
    })
  );
  return reviewGitChanges;
}
