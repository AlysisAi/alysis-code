import * as vscode from "vscode";

import { BackendActionController } from "../backend/BackendActionController";
import { redactForDisplay } from "../client/CliDiscovery";
import { ManageTreeItem, ManageViewProvider } from "../views/manageView";
import { MANAGE_DOMAIN_ACTIONS, MANAGE_ITEM_COMMANDS } from "../views/manageActionRoutes";

export function registerManageCommands(
  context: vscode.ExtensionContext,
  backendActions: BackendActionController,
  manageView: ManageViewProvider,
  // FE-19: re-sync the runtime capability snapshot from the LIVE bridge before re-rendering, so Refresh
  // reflects the current connected CLI rather than a stale snapshot.
  resyncRuntime: () => void = () => undefined
): void {
  for (const { commandId, actionKey } of MANAGE_ITEM_COMMANDS) {
    context.subscriptions.push(
      vscode.commands.registerCommand(commandId, async (item: unknown) => {
        if (!(item instanceof ManageTreeItem) || item.nodeKind !== "item") {
          return;
        }
        const actionId = MANAGE_DOMAIN_ACTIONS[item.domainId]?.[actionKey];
        if (!actionId) {
          await vscode.window.showWarningMessage(`That action is not available for ${item.domainId}.`);
          return;
        }
        await runAction(backendActions, actionId, item.actionArg);
        await refreshManageView(manageView);
      })
    );
  }

  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.manage.refresh", async () => {
      // Re-read live health/capabilities first (so a healthy connected CLI un-gates), then re-render.
      resyncRuntime();
      await refreshManageView(manageView);
    })
  );

  // Update check is an explicit, user-initiated action (never auto-run on activation) — apply stays
  // CLI-only. Lives here (not in a passive-activation file) so the no-auto-update-check pin holds.
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.checkForUpdates", () =>
      runAction(backendActions, "update.check", "")
    )
  );
}

async function runAction(backendActions: BackendActionController, actionId: string, args: string): Promise<void> {
  try {
    await backendActions.executeAction(actionId, args);
  } catch (error) {
    await vscode.window.showWarningMessage(
      redactForDisplay(error instanceof Error ? error.message : String(error))
    );
  }
}

async function refreshManageView(manageView: ManageViewProvider): Promise<void> {
  try {
    await manageView.refresh();
  } catch (error) {
    await vscode.window.showWarningMessage(
      `Could not refresh Alysis Code settings: ${redactForDisplay(error instanceof Error ? error.message : String(error))}`
    );
  }
}
