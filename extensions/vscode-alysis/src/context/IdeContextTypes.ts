export interface ContextPosition {
  line: number;
  character: number;
}

export interface ContextRange {
  start: ContextPosition;
  end: ContextPosition;
}

export interface ContextUri {
  readonly scheme: string;
  readonly fsPath: string;
  toString(): string;
}

export interface ContextDocument {
  readonly uri: ContextUri;
  readonly languageId: string;
  readonly version: number;
  readonly isDirty: boolean;
  readonly lineCount: number;
  getText(range?: ContextRange): string;
  getLineRange(line: number): ContextRange;
}

export interface ContextEditor {
  readonly document: ContextDocument;
  readonly selection: ContextRange;
  readonly visibleRanges: readonly ContextRange[];
}

export interface ContextWorkspaceFolder {
  readonly name: string;
  readonly index: number;
  readonly uri: ContextUri;
}

export type ContextDiagnosticSeverity = "error" | "warning" | "information" | "hint";

export interface ContextDiagnostic {
  readonly uri: ContextUri;
  readonly range: ContextRange;
  readonly severity: ContextDiagnosticSeverity;
  readonly message: string;
  readonly source?: string;
  readonly code?: string | number;
}

export interface ContextLocation {
  readonly uri: ContextUri;
  readonly range: ContextRange;
}

export interface ContextSymbol extends ContextLocation {
  readonly name: string;
  readonly detail?: string;
}

export interface ContextHover extends ContextLocation {
  readonly contents: string;
}

export interface GitContextRepository {
  readonly rootUri: ContextUri;
  diff(staged: boolean): Promise<string>;
}

/**
 * Terminal text can only enter the collector through this explicit provider.
 * Implementations must return only text the user explicitly selected or copied
 * for attachment, never passive scrollback or command-history scraping.
 */
export interface ExplicitTerminalSelection {
  readonly text: string;
  readonly terminalName?: string;
  readonly cwd?: string;
  readonly command?: string;
  readonly exitCode?: number | null;
}

export interface ExplicitTerminalSelectionProvider {
  getSelection(): Promise<ExplicitTerminalSelection | undefined>;
}

/** Runtime-independent seam used by the collector and its unit tests. */
export interface IdeContextSource {
  readonly isWorkspaceTrusted: boolean;
  getWorkspaceFolders(): readonly ContextWorkspaceFolder[];
  getActiveEditor(): ContextEditor | undefined;
  getOpenEditors(): readonly ContextEditor[];
  getOpenDocuments(): readonly ContextDocument[];
  getDiagnostics(limit: number): readonly ContextDiagnostic[];
  getGitRepositories(): Promise<readonly GitContextRepository[]>;
  getDocumentSymbols(document: ContextDocument, limit: number): Promise<readonly ContextSymbol[]>;
  getWorkspaceSymbols(query: string, limit: number): Promise<readonly ContextSymbol[]>;
  getDefinitions(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextLocation[]>;
  getTypeDefinitions(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextLocation[]>;
  getImplementations(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextLocation[]>;
  getHovers(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextHover[]>;
  getReferences(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextLocation[]>;
  getIncomingCalls(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextSymbol[]>;
  getOutgoingCalls(
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextSymbol[]>;
  openDocument(uri: ContextUri): Promise<ContextDocument | undefined>;
  isIgnored(uri: ContextUri, folder: ContextWorkspaceFolder): Promise<boolean>;
  realpath(fsPath: string): Promise<string>;
  getExplicitTerminalSelection(): Promise<ExplicitTerminalSelection | undefined>;
}

export type GitDiffMode = "none" | "working" | "staged" | "both";

export interface IdeContextCollectionRequest {
  /**
   * Exact VS Code workspace folder owning the job. Multi-root callers should always set this so
   * context from a sibling root cannot cross the job's authority boundary.
   */
  workspaceRoot?: string;
  /** Optional total budget for automatic context; returns already validated blocks on expiry. */
  timeoutMs?: number;
  includeSelection?: boolean;
  includeOpenEditors?: boolean;
  includeDiagnostics?: boolean;
  includeGitDiff?: GitDiffMode;
  includeTerminalSelection?: boolean;
  /** @deprecated Use includeDocumentSymbols. */
  includeSymbols?: boolean;
  includeDocumentSymbols?: boolean;
  includeWorkspaceSymbols?: boolean;
  workspaceSymbolQuery?: string;
  includeDefinitions?: boolean;
  includeTypeDefinitions?: boolean;
  includeImplementations?: boolean;
  includeHover?: boolean;
  includeReferences?: boolean;
  includeIncomingCalls?: boolean;
  includeOutgoingCalls?: boolean;
  languageServicePosition?: ContextPosition;
  /** @deprecated Use languageServicePosition. */
  referencePosition?: ContextPosition;
  /** Cancels collection locally; VS Code providers that are already running may still finish. */
  signal?: AbortSignal;
}

export interface IdeContextLimits {
  maxBlocks: number;
  maxBlockBytes: number;
  maxTotalBytes: number;
  maxItemsPerBlock: number;
  maxContentBytes: number;
  maxOpenEditors: number;
  maxDiagnostics: number;
  maxSymbols: number;
  maxReferences: number;
  maxLanguageServiceItems: number;
  maxWorkspaceSymbols: number;
}

export type ContextSkipReason =
  | "untrusted-workspace"
  | "workspace-required"
  | "ambiguous-workspace"
  | "unsupported-uri"
  | "outside-workspace"
  | "symlink-escape"
  | "ignored"
  | "sensitive-path"
  | "unavailable"
  | "empty"
  | "budget-exhausted"
  | "source-error"
  | "provider-timeout"
  | "cancelled"
  | "unsafe-diff";

export interface ContextSkip {
  readonly source: string;
  readonly reason: ContextSkipReason;
  readonly detail?: string;
}

export type IdeContextBlock = Record<string, unknown> & { type: string };

export interface IdeContextCollectionResult {
  readonly blocks: readonly IdeContextBlock[];
  readonly skipped: readonly ContextSkip[];
  readonly totalBytes: number;
  readonly truncated: boolean;
}

export interface IdeContextCollectorOptions {
  readonly limits?: Partial<IdeContextLimits>;
  readonly redact?: (text: string) => string;
  readonly now?: () => Date;
  readonly providerTimeoutMs?: number;
}
