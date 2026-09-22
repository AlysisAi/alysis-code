import * as vscode from "vscode";

import { ForgeController } from "../forge/ForgeController";

export function registerForgeExecuteCommand(
  context: vscode.ExtensionContext,
  forgeController: ForgeController
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.forgeExecute", () => forgeController.execute())
  );
}
