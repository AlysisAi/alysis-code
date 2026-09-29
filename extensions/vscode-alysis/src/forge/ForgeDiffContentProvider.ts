import * as vscode from "vscode";

/** Re-fetches a diff side that is no longer cached, e.g. after a window reload restored the editor. */
export type ForgeDiffDocumentResolver = (uri: vscode.Uri) => Promise<string>;

export class ForgeDiffContentProvider implements vscode.TextDocumentContentProvider, vscode.Disposable {
  // Two entries per open diff, each bounded by the protocol's diff byte cap, so this holds roughly 60
  // simultaneously open diffs. Document keys are deterministic (plan + diff + side), so reopening the
  // same diff reuses its entry instead of minting a new one and evicting a still-open tab.
  private static readonly maxDocuments = 120;
  private readonly changed = new vscode.EventEmitter<vscode.Uri>();
  private readonly documents = new Map<string, string>();
  private resolver: ForgeDiffDocumentResolver | undefined;
  public readonly onDidChange = this.changed.event;

  /** Wire the bridge-backed refetch used when a restored editor asks for content this host lost. */
  public setResolver(resolver: ForgeDiffDocumentResolver | undefined): void {
    this.resolver = resolver;
  }

  public setDocument(uri: vscode.Uri, content: string): void {
    this.remember(uri.toString(), content);
    this.changed.fire(uri);
  }

  public async provideTextDocumentContent(uri: vscode.Uri): Promise<string> {
    const key = uri.toString();
    const cached = this.documents.get(key);
    if (cached !== undefined) {
      return cached;
    }
    // Returning "" here would render both sides empty and make VS Code report "no changes" for a diff
    // that really did change files. Refetch instead, and fail loudly when that is impossible.
    if (!this.resolver) {
      throw new Error("Alysis Code cannot restore this diff. Reopen it from the Alysis Code plan view.");
    }
    const restored = await this.resolver(uri);
    this.remember(key, restored);
    return restored;
  }

  public dispose(): void {
    this.resolver = undefined;
    this.changed.dispose();
    this.documents.clear();
  }

  private remember(key: string, content: string): void {
    // Re-inserting refreshes recency so an evicted entry is always the least recently used one.
    this.documents.delete(key);
    this.documents.set(key, content);
    while (this.documents.size > ForgeDiffContentProvider.maxDocuments) {
      const oldestKey = this.documents.keys().next().value;
      if (!oldestKey) {
        break;
      }
      this.documents.delete(oldestKey);
    }
  }
}
