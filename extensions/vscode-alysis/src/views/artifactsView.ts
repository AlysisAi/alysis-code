import * as vscode from "vscode";

import { ArtifactSummary } from "../client/AlysisProtocol";

type ArtifactItemKind = "group" | "artifact";

export interface ArtifactGroup {
  id: string;
  label: string;
  sessionId: string;
  planId?: string;
  jobId?: string;
  artifacts: ArtifactSummary[];
  truncated?: boolean;
}

export class ArtifactTreeItem extends vscode.TreeItem {
  public constructor(
    public readonly kind: ArtifactItemKind,
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState = vscode.TreeItemCollapsibleState.None,
    public readonly group?: ArtifactGroup,
    public readonly artifact?: ArtifactSummary
  ) {
    super(label, collapsibleState);
  }
}

export class ArtifactsViewProvider implements vscode.TreeDataProvider<ArtifactTreeItem> {
  private readonly changed = new vscode.EventEmitter<ArtifactTreeItem | undefined | null | void>();
  private groups: ArtifactGroup[] = [];
  public readonly onDidChangeTreeData = this.changed.event;

  public getTreeItem(element: ArtifactTreeItem): vscode.TreeItem {
    return element;
  }

  public getChildren(element?: ArtifactTreeItem): ArtifactTreeItem[] {
    if (!element) {
      if (this.groups.length === 0) {
        return [];
      }
      return this.groups.map((group) =>
        this.decorate(new ArtifactTreeItem("group", group.label, vscode.TreeItemCollapsibleState.Collapsed, group), {
          description: `${group.artifacts.length}${group.truncated ? " | truncated" : ""}`,
          tooltip: group.planId ? `Plan ${group.planId}` : group.sessionId,
          iconPath: new vscode.ThemeIcon("archive")
        })
      );
    }
    if (element.kind === "group" && element.group) {
      const group = element.group;
      return group.artifacts.map((artifact) =>
        this.decorate(new ArtifactTreeItem("artifact", artifact.path, vscode.TreeItemCollapsibleState.None, group, artifact), {
          description: formatBytes(artifact.size_bytes),
          tooltip: `${artifact.root} · ${artifact.path}`,
          iconPath: new vscode.ThemeIcon("file"),
          contextValue: "alysisArtifact",
          command: {
            command: "alysis.artifact.open",
            title: "Open Alysis Code Artifact",
            arguments: [group.sessionId, artifact.artifact_id]
          }
        })
      );
    }
    return [];
  }

  public setGroup(group: ArtifactGroup): void {
    const index = this.groups.findIndex((candidate) => candidate.id === group.id);
    if (index >= 0) {
      this.groups[index] = group;
    } else {
      this.groups = [group, ...this.groups].slice(0, 20);
    }
    this.changed.fire();
  }

  public clear(): void {
    this.groups = [];
    this.changed.fire();
  }

  public state(): ArtifactGroup[] {
    return this.groups.map((group) => ({ ...group, artifacts: [...group.artifacts] }));
  }

  private decorate(
    item: ArtifactTreeItem,
    fields: Partial<Pick<vscode.TreeItem, "description" | "tooltip" | "contextValue" | "command" | "iconPath">>
  ): ArtifactTreeItem {
    Object.assign(item, fields);
    return item;
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(bytes < 10_240 ? 1 : 0)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
