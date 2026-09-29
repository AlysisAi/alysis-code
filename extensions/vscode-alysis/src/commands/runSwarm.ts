import * as vscode from "vscode";

import { ForgeController } from "../forge/ForgeController";

// Run Swarm: dispatch the active Forge plan across parallel workers (review-only
// merge — every task lands as a reviewable diff, never auto-merged). The
// controller gates on Workspace Trust, executable origin, and the forge.swarm
// capability before starting anything.
export function registerRunSwarmCommand(
  context: vscode.ExtensionContext,
  forgeController: ForgeController
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.runSwarm", () => forgeController.runSwarm())
  );
}
