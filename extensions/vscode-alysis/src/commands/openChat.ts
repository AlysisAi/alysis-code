import * as vscode from "vscode";

import { ChatController } from "../chat/ChatController";
import { COMMANDS } from "./registry";

export function registerOpenChatCommands(
  context: vscode.ExtensionContext,
  chatController: ChatController
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand(COMMANDS.openChat, () => chatController.openChat()),
    vscode.commands.registerCommand(COMMANDS.newSession, () => chatController.newSession()),
    vscode.commands.registerCommand(COMMANDS.runTask, () => chatController.runTask()),
    vscode.commands.registerCommand(COMMANDS.addSelection, () => chatController.addSelectionToTask())
  );
}
