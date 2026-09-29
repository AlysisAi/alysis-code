import * as vscode from "vscode";

import { CLI_INSTALL_COMMAND, CLI_UPGRADE_COMMAND, SETUP_GUIDE_URL } from "../client/compatibility";
import { GETTING_STARTED_WALKTHROUGH } from "./registry";

export function registerCliSetupCommands(context: vscode.ExtensionContext): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.copyCliInstallCommand", async () => {
      await vscode.env.clipboard.writeText(CLI_INSTALL_COMMAND);
      await vscode.window.showInformationMessage("Copied the Alysis Code CLI install command.");
    }),
    vscode.commands.registerCommand("alysis.copyCliUpgradeCommand", async () => {
      await vscode.env.clipboard.writeText(CLI_UPGRADE_COMMAND);
      await vscode.window.showInformationMessage("Copied the Alysis Code CLI upgrade command.");
    }),
    vscode.commands.registerCommand("alysis.openSetupGuide", async () => {
      // Prefer the in-editor Get Started walkthrough; fall back to the docs URL if it is unavailable.
      try {
        await vscode.commands.executeCommand("workbench.action.openWalkthrough", GETTING_STARTED_WALKTHROUGH, false);
      } catch {
        await vscode.env.openExternal(vscode.Uri.parse(SETUP_GUIDE_URL));
      }
    })
  );
}
