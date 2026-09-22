import type * as vscode from "vscode";

export function activate(_context: vscode.ExtensionContext): void {
  // This development extension only hosts assertions. The installed Alysis Code VSIX remains in
  // ExtensionMode.Production and is loaded from the isolated extensions directory.
}

export function deactivate(): void {
  // No resources are owned by the driver.
}
