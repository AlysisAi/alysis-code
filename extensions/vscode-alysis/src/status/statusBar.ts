import * as vscode from "vscode";

import { COMMANDS } from "../commands/registry";

export type AlysisStatus =
  | "missingCli"
  | "idle"
  | "bridgeOk"
  | "activeRun"
  | "approvalNeeded"
  | "untrustedWorkspace"
  | "error";

export class AlysisStatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  private readonly changed = new vscode.EventEmitter<ReturnType<AlysisStatusBar["snapshot"]>>();
  private visible = true;
  private currentState: AlysisStatus = "idle";
  private currentText = "";
  private currentTooltip = "";
  public readonly onDidChangeSnapshot = this.changed.event;

  public constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 80);
    // Primary click opens the cockpit; bridge health stays one click away via the tooltip link.
    this.item.command = COMMANDS.openChat;
    this.setIdle();
  }

  public setVisible(visible: boolean): void {
    this.visible = visible;
    if (visible) {
      this.item.show();
    } else {
      this.item.hide();
    }
  }

  public setMissingCli(): void {
    this.setState("missingCli", "$(circle-outline) Alysis Code: Setup", "Alysis Code needs help finding its local engine");
  }

  public setIdle(): void {
    this.setState("idle", "$(sparkle) Alysis Code", "Alysis Code is idle; the local engine will connect when needed");
  }

  public setBridgeOk(): void {
    this.setState("bridgeOk", "$(sparkle) Alysis Code", "Alysis Code is connected and ready");
  }

  public setActiveRun(_jobId?: string): void {
    // The job id is an internal handle; the tooltip stays in plain language.
    this.setState("activeRun", "$(sync~spin) Alysis Code: Working", "Alysis Code is working on your task");
  }

  public setApprovalNeeded(): void {
    this.setState(
      "approvalNeeded",
      "$(question) Alysis Code: Review",
      "Alysis Code is waiting for your review"
    );
  }

  public setUntrustedWorkspace(): void {
    this.setState(
      "untrustedWorkspace",
      "$(shield) Alysis Code: Limited",
      "This folder is in read-only mode until you trust it"
    );
  }

  public setError(message = "Alysis Code error"): void {
    this.setState("error", "$(error) Alysis Code: Error", message);
  }

  public dispose(): void {
    this.changed.dispose();
    this.item.dispose();
  }

  public snapshot(): {
    state: AlysisStatus;
    text: string;
    tooltip: string;
    visible: boolean;
  } {
    return {
      state: this.currentState,
      text: this.currentText,
      tooltip: this.currentTooltip,
      visible: this.visible
    };
  }

  private setState(state: AlysisStatus, text: string, tooltip: string): void {
    this.currentState = state;
    this.currentText = text;
    this.currentTooltip = tooltip;
    this.item.text = text;
    this.item.tooltip = this.buildTooltip(tooltip);
    if (this.visible) {
      this.item.show();
    }
    this.changed.fire(this.snapshot());
  }

  private buildTooltip(base: string): vscode.MarkdownString {
    const tooltip = new vscode.MarkdownString(
      `${base}\n\n[Open Alysis Code](command:${COMMANDS.openChat}) · [Check connection](command:${COMMANDS.showBridgeHealth})`
    );
    // Scope trust to exactly these two commands so interpolated CLI/bridge error text in `base`
    // can never smuggle an executable command: link into the tooltip.
    tooltip.isTrusted = { enabledCommands: [COMMANDS.openChat, COMMANDS.showBridgeHealth] };
    return tooltip;
  }
}
