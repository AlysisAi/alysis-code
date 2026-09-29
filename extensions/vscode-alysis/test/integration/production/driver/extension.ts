import type * as vscode from "vscode";

export function activate(_context: vscode.ExtensionContext): void {
  // The driver exists only to host production-install assertions. Alysis Code itself is installed
  // into the isolated profile and therefore activates in ExtensionMode.Production.
}

export function deactivate(): void {
  // No resources are owned by the driver.
}
