import * as vscode from "vscode";

import { ChatController } from "../chat/ChatController";
import { ForgeController } from "../forge/ForgeController";

export function registerCancelCurrentRunCommand(
  context: vscode.ExtensionContext,
  chatController: ChatController,
  forgeController: ForgeController
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.cancelCurrentRun", async () => {
      const chatActive = chatController.hasActiveJob();
      const forgeActive = forgeController.hasActiveJob();
      if (chatActive && forgeActive) {
        const selected = await vscode.window.showWarningMessage(
          "Both Alysis Code Chat and Forge report active jobs. Choose which run to cancel.",
          { modal: true },
          "Cancel Forge",
          "Cancel Chat"
        );
        if (selected === "Cancel Forge") {
          await forgeController.cancelCurrentRun();
        } else if (selected === "Cancel Chat") {
          await chatController.cancelCurrentRun();
        }
        return;
      }
      if (chatActive) {
        await chatController.cancelCurrentRun();
        return;
      }
      if (forgeActive) {
        await forgeController.cancelCurrentRun();
        return;
      }
      if (chatController.hasSession()) {
        await chatController.cancelCurrentRun();
        return;
      }
      await vscode.window.showInformationMessage("No active Alysis Code session is attached.");
    })
  );
}
