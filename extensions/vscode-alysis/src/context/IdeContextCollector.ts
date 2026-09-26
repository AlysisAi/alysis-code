import { createHash } from "node:crypto";
import path from "node:path";

import { diagnosticVerificationFingerprint } from "../verification/diagnosticFingerprint";
import { pathToFileURL } from "node:url";

import {
  ContextDocument,
  ExplicitTerminalSelection,
  ContextLocation,
  ContextPosition,
  ContextRange,
  ContextSkip,
  ContextSkipReason,
  ContextUri,
  ContextSymbol,
  ContextWorkspaceFolder,
  GitContextRepository,
  IdeContextBlock,
  IdeContextCollectionRequest,
  IdeContextCollectionResult,
  IdeContextCollectorOptions,
  IdeContextLimits,
  IdeContextSource
} from "./IdeContextTypes";
import { redactContextSecrets } from "./secretRedaction";

export const DEFAULT_IDE_CONTEXT_LIMITS: Readonly<IdeContextLimits> = Object.freeze({
  maxBlocks: 24,
  maxBlockBytes: 32 * 1024,
  maxTotalBytes: 128 * 1024,
  maxItemsPerBlock: 100,
  maxContentBytes: 24 * 1024,
  maxOpenEditors: 40,
  maxDiagnostics: 100,
  maxSymbols: 12,
  maxReferences: 12,
  maxLanguageServiceItems: 12,
  maxWorkspaceSymbols: 12
});

const DEFAULT_PROVIDER_TIMEOUT_MS = 3_000;
const MAX_PROVIDER_TIMEOUT_MS = 30_000;

const SENSITIVE_FILE_NAMES = new Set([
  ".env",
  ".git-credentials",
  ".npmrc",
  ".pypirc",
  "application_default_credentials.json",
  "auth.json",
  "credentials",
  "credentials.json",
  "credentials.tfrc.json",
  "dockerconfigjson",
  "id_dsa",
  "id_ecdsa",
  "id_ed25519",
  "id_rsa",
  "kubeconfig",
  "netrc",
  "secrets.json",
  "service-account.json",
  "service_account.json"
]);
const SAFE_ENV_FILE_NAMES = new Set([
  ".env.defaults",
  ".env.dist",
  ".env.example",
  ".env.sample",
  ".env.template"
]);
const SENSITIVE_EXTENSIONS = new Set([".jks", ".key", ".keystore", ".p12", ".pfx", ".pem", ".ppk"]);
const SENSITIVE_DIRECTORY_NAMES = new Set([
  ".aws",
  ".azure",
  ".docker",
  ".git",
  ".gnupg",
  ".kube",
  ".ssh",
  ".alysis"
]);

export interface SafePath {
  readonly uri: ContextUri;
  readonly fsPath: string;
  readonly relativePath: string;
  readonly folder: ContextWorkspaceFolder;
}

export interface RejectedPath {
  readonly reason: ContextSkipReason;
}

export type PathAssessment = SafePath | RejectedPath;

interface WorkspaceRoot {
  readonly folder: ContextWorkspaceFolder;
  readonly lexicalPath: string;
  readonly realPath: string;
}

export class IdeContextCollector {
  private readonly limits: IdeContextLimits;
  private readonly redact: (text: string) => string;
  private readonly now: () => Date;
  private readonly providerTimeoutMs: number;

  public constructor(
    private readonly source: IdeContextSource,
    options: IdeContextCollectorOptions = {}
  ) {
    this.limits = validateLimits({ ...DEFAULT_IDE_CONTEXT_LIMITS, ...options.limits });
    this.redact = options.redact ?? redactContextSecrets;
    this.now = options.now ?? (() => new Date());
    this.providerTimeoutMs = validateProviderTimeout(options.providerTimeoutMs ?? DEFAULT_PROVIDER_TIMEOUT_MS);
  }

  public async collect(request: IdeContextCollectionRequest = {}): Promise<IdeContextCollectionResult> {
    const skipped: ContextSkip[] = [];
    const budget = new ContextBudget(this.limits, skipped);
    if (request.signal?.aborted) {
      budget.stop("cancelled");
      return budget.result();
    }
    if (request.timeoutMs === undefined) {
      return this.collectWithinBudget(request, budget, skipped);
    }
    const timeoutMs = validateProviderTimeout(request.timeoutMs);
    const cancellation = new AbortController();
    try {
      return await withTimeoutAndSignal(
        this.collectWithinBudget({ ...request, signal: cancellation.signal }, budget, skipped),
        timeoutMs,
        request.signal
      );
    } catch (error) {
      if (!(error instanceof ProviderFailure)) {
        throw error;
      }
      cancellation.abort();
      budget.stop(error.reason);
      return budget.result();
    }
  }

  private async collectWithinBudget(
    request: IdeContextCollectionRequest,
    budget: ContextBudget,
    skipped: ContextSkip[]
  ): Promise<IdeContextCollectionResult> {
    if (!this.source.isWorkspaceTrusted) {
      return {
        blocks: [],
        skipped: [{ source: "workspace", reason: "untrusted-workspace" }],
        totalBytes: 0,
        truncated: false
      };
    }

    const allFolders = this.source.getWorkspaceFolders();
    const folders = selectWorkspaceFolders(
      allFolders,
      request.workspaceRoot,
      this.source.getActiveEditor()?.document.uri
    );
    if (folders.length === 0) {
      return {
        blocks: [],
        skipped: [{
          source: "workspace",
          reason: allFolders.length > 1 ? "ambiguous-workspace" : "workspace-required"
        }],
        totalBytes: 0,
        truncated: false
      };
    }

    const boundary = await WorkspaceBoundary.create(this.source, folders, skipped, request.signal);
    if (request.signal?.aborted) return budget.result();
    if (!boundary.hasRoots) {
      return { blocks: [], skipped, totalBytes: 0, truncated: false };
    }
    const capturedAt = this.now().toISOString();
    const active = this.source.getActiveEditor();

    if (request.includeSelection !== false && active && !isEmptyRange(active.selection)) {
      await this.collectSelection(active.document, active.selection, boundary, budget, capturedAt);
    }
    if (request.signal?.aborted) return budget.result();
    if (request.includeOpenEditors !== false) {
      await this.collectOpenEditors(boundary, budget, capturedAt, active?.document.uri);
    }
    if (request.signal?.aborted) return budget.result();
    if (request.includeDiagnostics !== false) {
      await this.collectDiagnostics(boundary, budget, capturedAt);
    }
    if (request.signal?.aborted) return budget.result();
    if ((request.includeGitDiff ?? "none") !== "none") {
      await this.collectGitDiff(request.includeGitDiff ?? "none", boundary, budget, capturedAt);
    }
    if (request.signal?.aborted) return budget.result();
    if (request.includeTerminalSelection === true) {
      await this.collectTerminalSelection(boundary, budget, capturedAt);
    }
    if ((request.includeDocumentSymbols === true || request.includeSymbols === true) && active) {
      await this.collectSymbols(active.document, request, boundary, budget, capturedAt);
    }
    if (request.includeWorkspaceSymbols === true) {
      await this.collectWorkspaceSymbols(request.workspaceSymbolQuery ?? "", request, boundary, budget, capturedAt);
    }
    const languagePosition = request.languageServicePosition ?? request.referencePosition ?? active?.selection.end;
    if (request.includeDefinitions === true && active && languagePosition) {
      await this.collectLanguageLocations(
        "definitions",
        "Definition",
        "vscode.definitions",
        () => this.source.getDefinitions(active.document, languagePosition, this.limits.maxLanguageServiceItems * 4),
        request,
        boundary,
        budget,
        capturedAt,
        this.limits.maxLanguageServiceItems
      );
    }
    if (request.includeTypeDefinitions === true && active && languagePosition) {
      await this.collectLanguageLocations(
        "type_definitions",
        "Type definition",
        "vscode.type-definitions",
        () => this.source.getTypeDefinitions(active.document, languagePosition, this.limits.maxLanguageServiceItems * 4),
        request,
        boundary,
        budget,
        capturedAt,
        this.limits.maxLanguageServiceItems
      );
    }
    if (request.includeImplementations === true && active && languagePosition) {
      await this.collectLanguageLocations(
        "implementations",
        "Implementation",
        "vscode.implementations",
        () => this.source.getImplementations(active.document, languagePosition, this.limits.maxLanguageServiceItems * 4),
        request,
        boundary,
        budget,
        capturedAt,
        this.limits.maxLanguageServiceItems
      );
    }
    if (request.includeHover === true && active && languagePosition) {
      await this.collectHovers(active.document, languagePosition, request, boundary, budget, capturedAt);
    }
    if (request.includeReferences === true && active) {
      await this.collectReferences(
        active.document,
        languagePosition ?? active.selection.end,
        request,
        boundary,
        budget,
        capturedAt
      );
    }
    if (request.includeIncomingCalls === true && active && languagePosition) {
      await this.collectLanguageSymbols(
        "incoming_calls",
        "Incoming call",
        "vscode.call-hierarchy.incoming",
        () => this.source.getIncomingCalls(active.document, languagePosition, this.limits.maxLanguageServiceItems * 4),
        request,
        boundary,
        budget,
        capturedAt,
        this.limits.maxLanguageServiceItems
      );
    }
    if (request.includeOutgoingCalls === true && active && languagePosition) {
      await this.collectLanguageSymbols(
        "outgoing_calls",
        "Outgoing call",
        "vscode.call-hierarchy.outgoing",
        () => this.source.getOutgoingCalls(active.document, languagePosition, this.limits.maxLanguageServiceItems * 4),
        request,
        boundary,
        budget,
        capturedAt,
        this.limits.maxLanguageServiceItems
      );
    }

    return budget.result();
  }

  /** Collect files explicitly chosen by the user after applying the same workspace boundary as automatic context. */
  public async collectFiles(uris: readonly ContextUri[], workspaceRoot?: string): Promise<IdeContextCollectionResult> {
    const setup = await this.createExplicitCollection("files", workspaceRoot);
    if ("result" in setup) {
      return setup.result;
    }
    const { boundary, budget, capturedAt } = setup;
    const inspected = uris.slice(0, this.limits.maxBlocks * 4);
    if (uris.length > inspected.length) {
      budget.skip("file", "budget-exhausted");
    }
    for (const uri of inspected) {
      const safe = await boundary.assess(uri);
      if (!isSafePath(safe)) {
        budget.skip("file", safe.reason);
        continue;
      }
      let document: ContextDocument | undefined;
      let content: string;
      try {
        document = await this.source.openDocument(safe.uri);
        content = document ? this.cleanContent(document.getText()) : "";
      } catch {
        budget.skip("file", "source-error");
        continue;
      }
      if (!document || !content) {
        budget.skip("file", document ? "empty" : "unavailable");
        continue;
      }
      content = truncateUtf8(content, this.limits.maxContentBytes);
      budget.add({
        type: "file",
        uri: safe.uri.toString(),
        document_version: nonNegative(document.version),
        content_hash: sha256(content),
        language: cleanMetadata(document.languageId, this.redact),
        content,
        provenance: provenance("vscode.file-picker", capturedAt, safe.folder, this.redact)
      });
    }
    return budget.result();
  }

  /** Collect terminal text that the user explicitly copied and approved for this attachment. */
  public async collectTerminalExcerpt(
    selection: ExplicitTerminalSelection,
    workspaceRoot?: string
  ): Promise<IdeContextCollectionResult> {
    const setup = await this.createExplicitCollection("terminal", workspaceRoot);
    if ("result" in setup) {
      return setup.result;
    }
    await this.collectExplicitTerminalSelection(selection, setup.boundary, setup.budget, setup.capturedAt);
    return setup.budget.result();
  }

  private async createExplicitCollection(source: string, workspaceRoot?: string): Promise<
    | { result: IdeContextCollectionResult }
    | { boundary: WorkspaceBoundary; budget: ContextBudget; capturedAt: string }
  > {
    const skipped: ContextSkip[] = [];
    if (!this.source.isWorkspaceTrusted) {
      return {
        result: { blocks: [], skipped: [{ source, reason: "untrusted-workspace" }], totalBytes: 0, truncated: false }
      };
    }
    const allFolders = this.source.getWorkspaceFolders();
    const folders = selectWorkspaceFolders(allFolders, workspaceRoot, this.source.getActiveEditor()?.document.uri);
    if (folders.length === 0) {
      return {
        result: {
          blocks: [],
          skipped: [{ source, reason: allFolders.length > 1 ? "ambiguous-workspace" : "workspace-required" }],
          totalBytes: 0,
          truncated: false
        }
      };
    }
    const boundary = await WorkspaceBoundary.create(this.source, folders, skipped);
    if (!boundary.hasRoots) {
      return { result: { blocks: [], skipped, totalBytes: 0, truncated: false } };
    }
    return {
      boundary,
      budget: new ContextBudget(this.limits, skipped),
      capturedAt: this.now().toISOString()
    };
  }

  private async collectSelection(
    document: ContextDocument,
    range: ContextRange,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    const safe = await boundary.assess(document.uri);
    if (!isSafePath(safe)) {
      budget.skip("selection", safe.reason);
      return;
    }
    let content: string;
    try {
      content = this.cleanContent(document.getText(range));
    } catch {
      budget.skip("selection", "source-error");
      return;
    }
    if (!content) {
      budget.skip("selection", "empty");
      return;
    }
    content = truncateUtf8(content, this.limits.maxContentBytes);
    budget.add({
      type: "selection",
      uri: safe.uri.toString(),
      document_version: nonNegative(document.version),
      content_hash: sha256(content),
      language: cleanMetadata(document.languageId, this.redact),
      range: backendRange(range),
      content,
      provenance: provenance("vscode.selection", capturedAt, safe.folder, this.redact)
    });
  }

  private async collectOpenEditors(
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string,
    activeUri?: ContextUri
  ): Promise<void> {
    const editorByUri = new Map(this.source.getOpenEditors().map((editor) => [uriKey(editor.document.uri), editor]));
    const documents = [...this.source.getOpenDocuments()];
    for (const editor of this.source.getOpenEditors()) {
      if (!documents.some((document) => uriKey(document.uri) === uriKey(editor.document.uri))) {
        documents.push(editor.document);
      }
    }

    const items: Record<string, unknown>[] = [];
    const seen = new Set<string>();
    let truncated = false;
    let inspected = 0;
    const inspectionCap = Math.max(this.limits.maxOpenEditors, this.limits.maxItemsPerBlock) * 4;
    for (const document of documents) {
      inspected += 1;
      if (inspected > inspectionCap) {
        truncated = true;
        break;
      }
      if (items.length >= this.limits.maxOpenEditors || items.length >= this.limits.maxItemsPerBlock) {
        truncated = true;
        break;
      }
      const safe = await boundary.assess(document.uri);
      if (!isSafePath(safe)) {
        budget.skip("open_editors", safe.reason);
        continue;
      }
      const key = safe.uri.toString();
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      const editor = editorByUri.get(uriKey(document.uri));
      const item: Record<string, unknown> = {
        uri: key,
        document_version: nonNegative(document.version),
        language: cleanMetadata(document.languageId, this.redact),
        active: activeUri ? uriKey(activeUri) === uriKey(document.uri) : false,
        dirty: document.isDirty
      };
      if (editor && !isEmptyRange(editor.selection)) {
        item.range = backendRange(editor.selection);
      }
      items.push(item);
    }
    if (items.length === 0) {
      return;
    }
    budget.add({
      type: "open_editors",
      items,
      provenance: genericProvenance("vscode.open-editors", capturedAt),
      ...(truncated ? { truncated: true } : {})
    });
  }

  private async collectDiagnostics(
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    const items: Record<string, unknown>[] = [];
    let truncated = false;
    const inspectionCap = Math.max(this.limits.maxDiagnostics, this.limits.maxItemsPerBlock) * 4;
    for (const diagnostic of this.source.getDiagnostics(inspectionCap)) {
      if (items.length >= this.limits.maxDiagnostics || items.length >= this.limits.maxItemsPerBlock) {
        truncated = true;
        break;
      }
      const safe = await boundary.assess(diagnostic.uri);
      if (!isSafePath(safe)) {
        budget.skip("diagnostics", safe.reason);
        continue;
      }
      const message = truncateUtf8(this.cleanContent(diagnostic.message), 8 * 1024);
      if (!message) {
        continue;
      }
      const uri = safe.uri.toString();
      const range = backendRange(diagnostic.range);
      items.push({
        uri,
        range,
        severity: diagnostic.severity,
        message,
        // Host-local correlation only. ChatController strips this before the
        // context crosses JSONL to the bridge. Verification reads the editor's
        // original URI; the context itself still uses the checked physical URI.
        verification_id: diagnosticVerificationFingerprint({
          uri: diagnostic.uri.toString(),
          range: diagnostic.range,
          severity: diagnostic.severity,
          message: diagnostic.message,
          ...(diagnostic.source !== undefined ? { source: diagnostic.source } : {}),
          ...(diagnostic.code !== undefined ? { code: diagnostic.code } : {})
        }),
        ...(diagnostic.source ? { source: cleanMetadata(diagnostic.source, this.redact) } : {}),
        ...(diagnostic.code !== undefined
          ? { code: typeof diagnostic.code === "number" ? diagnostic.code : cleanMetadata(diagnostic.code, this.redact) }
          : {})
      });
    }
    if (items.length === 0) {
      return;
    }
    budget.add({
      type: "diagnostics",
      items,
      provenance: genericProvenance("vscode.diagnostics", capturedAt),
      ...(truncated ? { truncated: true } : {})
    });
  }

  private async collectGitDiff(
    mode: Exclude<IdeContextCollectionRequest["includeGitDiff"], undefined>,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    let repositories: readonly GitContextRepository[];
    try {
      repositories = await this.source.getGitRepositories();
    } catch {
      budget.skip("git_diff", "source-error");
      return;
    }
    for (const repository of repositories) {
      const root = await boundary.assess(repository.rootUri, { checkIgnored: false });
      if (!isSafePath(root)) {
        budget.skip("git_diff", root.reason);
        continue;
      }
      const stagedModes = mode === "both" ? [false, true] : [mode === "staged"];
      for (const staged of stagedModes) {
        let diff: string;
        try {
          diff = await repository.diff(staged);
        } catch {
          budget.skip("git_diff", "source-error", staged ? "staged" : "working");
          continue;
        }
        if (!diff.trim()) {
          continue;
        }
        const sanitized = await sanitizeUnifiedDiff(diff, root.fsPath, boundary, this.redact);
        if (!sanitized.content) {
          budget.skip("git_diff", sanitized.unsafe ? "unsafe-diff" : "empty");
          continue;
        }
        const content = truncateUtf8(sanitized.content, this.limits.maxContentBytes);
        budget.add({
          type: "git_diff",
          content,
          repository: root.fsPath,
          staged,
          provenance: provenance("vscode.git", capturedAt, root.folder, this.redact),
          ...(Buffer.byteLength(content) < Buffer.byteLength(sanitized.content) || sanitized.filtered
            ? { truncated: true }
            : {})
        });
      }
    }
  }

  private async collectTerminalSelection(
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    let selection;
    try {
      selection = await this.source.getExplicitTerminalSelection();
    } catch {
      budget.skip("terminal", "source-error");
      return;
    }
    if (!selection?.text) {
      budget.skip("terminal", "empty");
      return;
    }
    await this.collectExplicitTerminalSelection(selection, boundary, budget, capturedAt);
  }

  private async collectExplicitTerminalSelection(
    selection: ExplicitTerminalSelection,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    const content = truncateUtf8(this.cleanContent(selection.text), this.limits.maxContentBytes);
    if (!content) {
      budget.skip("terminal", "empty");
      return;
    }
    const block: IdeContextBlock = {
      type: "terminal",
      content,
      provenance: genericProvenance("vscode.terminal-selection", capturedAt),
      ...(selection.terminalName ? { terminal_name: cleanMetadata(selection.terminalName, this.redact) } : {}),
      ...(selection.command ? { command: cleanMetadata(selection.command, this.redact) } : {}),
      ...(selection.exitCode !== undefined ? { exit_code: selection.exitCode } : {})
    };
    if (selection.cwd) {
      const cwd = await boundary.assessFsPath(selection.cwd, { checkIgnored: false, allowMissing: false });
      if (isSafePath(cwd)) {
        block.cwd = cwd.fsPath;
      } else {
        budget.skip("terminal.cwd", cwd.reason);
      }
    }
    budget.add(block);
  }

  private async collectSymbols(
    document: ContextDocument,
    request: IdeContextCollectionRequest,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    const symbols = await this.runProvider(
      "symbols",
      () => this.source.getDocumentSymbols(document, this.limits.maxSymbols * 4),
      request,
      budget
    );
    if (!symbols) {
      return;
    }
    await this.addSymbolResults(
      symbols,
      "symbols",
      "Symbol",
      "vscode.symbols",
      boundary,
      budget,
      capturedAt,
      this.limits.maxSymbols
    );
  }

  private async collectWorkspaceSymbols(
    query: string,
    request: IdeContextCollectionRequest,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    const safeQuery = cleanMetadata(query, this.redact);
    const symbols = await this.runProvider(
      "workspace_symbols",
      () => this.source.getWorkspaceSymbols(safeQuery, this.limits.maxWorkspaceSymbols * 4),
      request,
      budget
    );
    if (!symbols) {
      return;
    }
    await this.addSymbolResults(
      symbols,
      "workspace_symbols",
      "Workspace symbol",
      "vscode.workspace-symbols",
      boundary,
      budget,
      capturedAt,
      this.limits.maxWorkspaceSymbols
    );
  }

  private async collectReferences(
    document: ContextDocument,
    position: ContextPosition,
    request: IdeContextCollectionRequest,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    await this.collectLanguageLocations(
      "references",
      "Reference",
      "vscode.references",
      () => this.source.getReferences(document, position, this.limits.maxReferences * 4),
      request,
      boundary,
      budget,
      capturedAt,
      this.limits.maxReferences
    );
  }

  private async collectLanguageLocations(
    requestName: string,
    label: string,
    provenanceName: string,
    provider: () => Promise<readonly ContextLocation[]>,
    request: IdeContextCollectionRequest,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string,
    limit: number
  ): Promise<void> {
    const locations = await this.runProvider(requestName, provider, request, budget);
    if (!locations) {
      return;
    }
    if (locations.some((location) => !isValidContextLocation(location))) {
      budget.skip(requestName, "source-error");
      return;
    }
    const seen = new Set<string>();
    let count = 0;
    for (const location of locations) {
      const key = `${uriKey(location.uri)}:${rangeKey(location.range)}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      if (count >= limit) {
        budget.skip(requestName, "budget-exhausted");
        break;
      }
      const added = await this.addLocationBlock(
        location,
        label,
        provenanceName,
        boundary,
        budget,
        capturedAt
      );
      if (added) {
        count += 1;
      }
    }
  }

  private async collectLanguageSymbols(
    requestName: string,
    labelPrefix: string,
    provenanceName: string,
    provider: () => Promise<readonly ContextSymbol[]>,
    request: IdeContextCollectionRequest,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string,
    limit: number
  ): Promise<void> {
    const symbols = await this.runProvider(requestName, provider, request, budget);
    if (!symbols) {
      return;
    }
    await this.addSymbolResults(
      symbols,
      requestName,
      labelPrefix,
      provenanceName,
      boundary,
      budget,
      capturedAt,
      limit
    );
  }

  private async addSymbolResults(
    symbols: readonly ContextSymbol[],
    requestName: string,
    labelPrefix: string,
    provenanceName: string,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string,
    limit: number
  ): Promise<void> {
    if (symbols.some((symbol) => !isValidContextLocation(symbol) || typeof symbol.name !== "string")) {
      budget.skip(requestName, "source-error");
      return;
    }
    const seen = new Set<string>();
    let count = 0;
    for (const symbol of symbols) {
      const key = `${uriKey(symbol.uri)}:${rangeKey(symbol.range)}`;
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      if (count >= limit) {
        budget.skip(requestName, "budget-exhausted");
        break;
      }
      const name = cleanMetadata(symbol.name, this.redact);
      const detail = symbol.detail ? ` — ${cleanMetadata(symbol.detail, this.redact)}` : "";
      const added = await this.addLocationBlock(
        symbol,
        `${labelPrefix}: ${name}${detail}`,
        provenanceName,
        boundary,
        budget,
        capturedAt
      );
      if (added) {
        count += 1;
      }
    }
  }

  private async collectHovers(
    document: ContextDocument,
    position: ContextPosition,
    request: IdeContextCollectionRequest,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<void> {
    const hovers = await this.runProvider(
      "hover",
      () => this.source.getHovers(document, position, this.limits.maxLanguageServiceItems * 4),
      request,
      budget
    );
    if (!hovers) {
      return;
    }
    if (hovers.some((hover) => !isValidContextLocation(hover) || typeof hover.contents !== "string")) {
      budget.skip("hover", "source-error");
      return;
    }
    let count = 0;
    for (const hover of hovers) {
      if (count >= this.limits.maxLanguageServiceItems) {
        budget.skip("hover", "budget-exhausted");
        break;
      }
      const summary = cleanMetadata(hover.contents.replace(/\s+/gu, " ").trim(), this.redact);
      if (!summary) {
        budget.skip("hover", "empty");
        continue;
      }
      const added = await this.addLocationBlock(
        hover,
        `Hover: ${summary}`,
        "vscode.hover",
        boundary,
        budget,
        capturedAt
      );
      if (added) {
        count += 1;
      }
    }
  }

  private async runProvider<T extends readonly unknown[]>(
    sourceName: string,
    provider: () => Promise<T>,
    request: IdeContextCollectionRequest,
    budget: ContextBudget
  ): Promise<T | undefined> {
    if (request.signal?.aborted) {
      budget.skip(sourceName, "cancelled");
      return undefined;
    }
    try {
      const result = await withTimeoutAndSignal(provider(), this.providerTimeoutMs, request.signal);
      if (!Array.isArray(result)) {
        budget.skip(sourceName, "source-error");
        return undefined;
      }
      return result;
    } catch (error) {
      budget.skip(sourceName, error instanceof ProviderFailure ? error.reason : "source-error");
      return undefined;
    }
  }

  private async addLocationBlock(
    location: ContextLocation,
    label: string,
    sourceName: string,
    boundary: WorkspaceBoundary,
    budget: ContextBudget,
    capturedAt: string
  ): Promise<boolean> {
    if (!isValidContextLocation(location)) {
      budget.skip(sourceName, "source-error");
      return false;
    }
    const safe = await boundary.assess(location.uri);
    if (!isSafePath(safe)) {
      budget.skip(sourceName, safe.reason);
      return false;
    }
    let document: ContextDocument | undefined;
    try {
      document = await this.source.openDocument(location.uri);
    } catch {
      budget.skip(sourceName, "source-error");
      return false;
    }
    if (!document) {
      budget.skip(sourceName, "unavailable");
      return false;
    }
    let range: ContextRange;
    let content: string;
    try {
      range = isEmptyRange(location.range)
        ? document.getLineRange(Math.min(location.range.start.line, Math.max(0, document.lineCount - 1)))
        : location.range;
      content = truncateUtf8(this.cleanContent(document.getText(range)), this.limits.maxContentBytes);
    } catch {
      budget.skip(sourceName, "source-error");
      return false;
    }
    if (!content) {
      budget.skip(sourceName, "empty");
      return false;
    }
    return budget.add({
      type: "file_range",
      uri: safe.uri.toString(),
      document_version: nonNegative(document.version),
      content_hash: sha256(content),
      language: cleanMetadata(document.languageId, this.redact),
      range: backendRange(range),
      content,
      label,
      provenance: provenance(sourceName, capturedAt, safe.folder, this.redact)
    });
  }

  private cleanContent(value: string): string {
    return this.redact(value.replaceAll("\u0000", ""));
  }
}

export class WorkspaceBoundary {
  private readonly cache = new Map<string, Promise<PathAssessment>>();

  private constructor(
    private readonly source: IdeContextSource,
    private readonly roots: readonly WorkspaceRoot[],
    private readonly signal?: AbortSignal
  ) {}

  public get hasRoots(): boolean {
    return this.roots.length > 0;
  }

  public static async create(
    source: IdeContextSource,
    folders: readonly ContextWorkspaceFolder[],
    skipped: ContextSkip[],
    signal?: AbortSignal
  ): Promise<WorkspaceBoundary> {
    const roots: WorkspaceRoot[] = [];
    for (const folder of folders) {
      if (signal?.aborted) break;
      if (folder.uri.scheme.toLowerCase() !== "file") {
        skipped.push({ source: "workspace", reason: "unsupported-uri" });
        continue;
      }
      try {
        roots.push({
          folder,
          lexicalPath: path.resolve(folder.uri.fsPath),
          realPath: path.resolve(await source.realpath(folder.uri.fsPath))
        });
      } catch {
        skipped.push({ source: "workspace", reason: "unavailable" });
      }
    }
    return new WorkspaceBoundary(source, roots, signal);
  }

  public async assess(
    uri: ContextUri,
    options: { checkIgnored?: boolean; allowMissing?: boolean } = {}
  ): Promise<PathAssessment> {
    if (uri.scheme.toLowerCase() !== "file") {
      return { reason: "unsupported-uri" };
    }
    return this.assessFsPath(uri.fsPath, options);
  }

  public async assessFsPath(
    fsPath: string,
    options: { checkIgnored?: boolean; allowMissing?: boolean } = {}
  ): Promise<PathAssessment> {
    if (this.signal?.aborted) return { reason: "cancelled" };
    const checkIgnored = options.checkIgnored !== false;
    const allowMissing = options.allowMissing === true;
    const key = `${path.resolve(fsPath)}:${checkIgnored}:${allowMissing}`;
    let pending = this.cache.get(key);
    if (!pending) {
      pending = this.assessUncached(fsPath, checkIgnored, allowMissing);
      this.cache.set(key, pending);
    }
    return pending;
  }

  private async assessUncached(
    fsPath: string,
    checkIgnored: boolean,
    allowMissing: boolean
  ): Promise<PathAssessment> {
    const lexical = path.resolve(fsPath);
    if (!this.roots.some((root) => isContained(root.lexicalPath, lexical))) {
      return { reason: "outside-workspace" };
    }
    if (isSensitivePath(lexical)) {
      return { reason: "sensitive-path" };
    }
    let real: string;
    try {
      real = path.resolve(await this.source.realpath(lexical));
    } catch {
      if (!allowMissing) {
        return { reason: "unavailable" };
      }
      try {
        real = await this.resolveMissingPath(lexical);
      } catch {
        return { reason: "unavailable" };
      }
    }
    if (this.signal?.aborted) return { reason: "cancelled" };
    const root = this.roots.find((candidate) => isContained(candidate.realPath, real));
    if (!root) {
      return { reason: "symlink-escape" };
    }
    if (isSensitivePath(real)) {
      return { reason: "sensitive-path" };
    }
    const uri = fileUri(real);
    if (checkIgnored) {
      try {
        if (await this.source.isIgnored(uri, root.folder)) {
          return { reason: "ignored" };
        }
      } catch {
        // Ignore policy failure is fail-closed. Returning source-error keeps the
        // reason visible without leaking the evaluated path.
        return { reason: "source-error" };
      }
    }
    if (this.signal?.aborted) return { reason: "cancelled" };
    return {
      uri,
      fsPath: real,
      relativePath: toPosix(path.relative(root.realPath, real)) || ".",
      folder: root.folder
    };
  }

  private async resolveMissingPath(candidate: string): Promise<string> {
    const suffix: string[] = [];
    let cursor = candidate;
    while (true) {
      if (this.signal?.aborted) throw new ProviderFailure("cancelled");
      try {
        const existing = path.resolve(await this.source.realpath(cursor));
        return path.resolve(existing, ...suffix.reverse());
      } catch {
        const parent = path.dirname(cursor);
        if (parent === cursor) {
          throw new Error("No existing ancestor");
        }
        suffix.push(path.basename(cursor));
        cursor = parent;
      }
    }
  }
}

class ContextBudget {
  private readonly blocks: IdeContextBlock[] = [];
  private readonly skips: ContextSkip[];
  private bytes = 0;
  private wasTruncated = false;
  private stopped = false;

  public constructor(private readonly limits: IdeContextLimits, skips: ContextSkip[]) {
    this.skips = skips;
  }

  public add(block: IdeContextBlock): boolean {
    if (this.stopped) return false;
    if (this.blocks.length >= this.limits.maxBlocks) {
      this.skip(block.type, "budget-exhausted");
      this.wasTruncated = true;
      return false;
    }
    const remaining = this.limits.maxTotalBytes - this.bytes;
    if (remaining < 128) {
      this.skip(block.type, "budget-exhausted");
      this.wasTruncated = true;
      return false;
    }
    const fitted = fitBlock(block, Math.min(this.limits.maxBlockBytes, remaining));
    if (!fitted) {
      this.skip(block.type, "budget-exhausted");
      this.wasTruncated = true;
      return false;
    }
    const size = jsonBytes(fitted.block);
    this.blocks.push(fitted.block);
    this.bytes += size;
    this.wasTruncated ||= fitted.truncated;
    return true;
  }

  public skip(source: string, reason: ContextSkipReason, detail?: string): void {
    if (this.stopped) return;
    this.skips.push({ source, reason, ...(detail ? { detail } : {}) });
  }

  public stop(reason: ContextSkipReason): void {
    this.skip("automatic-context", reason);
    this.wasTruncated = true;
    this.stopped = true;
  }

  public result(): IdeContextCollectionResult {
    return {
      blocks: [...this.blocks],
      skipped: [...this.skips],
      totalBytes: this.bytes,
      truncated: this.wasTruncated
    };
  }
}

async function sanitizeUnifiedDiff(
  diff: string,
  repositoryRoot: string,
  boundary: WorkspaceBoundary,
  redact: (text: string) => string
): Promise<{ content: string; filtered: boolean; unsafe: boolean }> {
  const sections = splitUnifiedDiff(diff.replaceAll("\u0000", ""));
  if (!sections) {
    return { content: "", filtered: false, unsafe: true };
  }
  const accepted: string[] = [];
  let filtered = false;
  for (const section of sections) {
    const relative = diffSectionPath(section);
    if (!relative || !isSafeDiffRelativePath(relative)) {
      filtered = true;
      continue;
    }
    const candidate = path.resolve(repositoryRoot, ...relative.split("/"));
    const safe = await boundary.assessFsPath(candidate, { allowMissing: true });
    if (!isSafePath(safe)) {
      filtered = true;
      continue;
    }
    accepted.push(redact(section));
  }
  return { content: accepted.join("\n"), filtered, unsafe: false };
}

function splitUnifiedDiff(diff: string): string[] | undefined {
  const first = diff.search(/^diff --git /m);
  if (first < 0 || diff.slice(0, first).trim()) {
    return undefined;
  }
  return diff.slice(first).split(/\n(?=diff --git )/g);
}

function diffSectionPath(section: string): string | undefined {
  const lines = section.split(/\r?\n/);
  for (const prefix of ["+++ ", "--- "]) {
    const line = lines.find((candidate) => candidate.startsWith(prefix));
    if (!line) {
      continue;
    }
    const value = line.slice(prefix.length).trim();
    if (value === "/dev/null") {
      continue;
    }
    if (value.startsWith("\"") || value.includes("\t")) {
      // Git's quoted/octal path syntax is deliberately rejected rather than
      // incompletely decoded, which could bypass path policy.
      return undefined;
    }
    return value.replace(/^[ab]\//, "");
  }
  return undefined;
}

function isSafeDiffRelativePath(value: string): boolean {
  if (!value || value.includes("\\") || value.includes("\u0000") || path.posix.isAbsolute(value)) {
    return false;
  }
  const normalized = path.posix.normalize(value);
  return normalized !== ".." && !normalized.startsWith("../") && normalized === value;
}

function fitBlock(block: IdeContextBlock, cap: number): { block: IdeContextBlock; truncated: boolean } | undefined {
  const fitted: IdeContextBlock = { ...block };
  if (Array.isArray(block.items)) {
    fitted.items = block.items.map((item) => ({ ...(item as Record<string, unknown>) }));
  }
  if (jsonBytes(fitted) <= cap) {
    return { block: fitted, truncated: false };
  }
  fitted.truncated = true;
  const items = Array.isArray(fitted.items) ? fitted.items : undefined;
  while (items && items.length > 0 && jsonBytes(fitted) > cap) {
    items.pop();
  }
  if (jsonBytes(fitted) <= cap) {
    return { block: fitted, truncated: true };
  }
  if (typeof fitted.content === "string") {
    const original = fitted.content;
    let low = 0;
    let high = Buffer.byteLength(original);
    let best: string | undefined;
    while (low <= high) {
      const midpoint = Math.floor((low + high) / 2);
      const candidate = truncateUtf8(original, midpoint);
      fitted.content = candidate;
      if (jsonBytes(fitted) <= cap) {
        best = candidate;
        low = midpoint + 1;
      } else {
        high = midpoint - 1;
      }
    }
    if (best !== undefined) {
      fitted.content = best;
    }
  }
  return jsonBytes(fitted) <= cap ? { block: fitted, truncated: true } : undefined;
}

function validateLimits(limits: IdeContextLimits): IdeContextLimits {
  for (const [name, value] of Object.entries(limits)) {
    if (!Number.isSafeInteger(value) || value <= 0) {
      throw new Error(`IDE context limit ${name} must be a positive integer.`);
    }
  }
  if (limits.maxBlockBytes < 128 || limits.maxTotalBytes < 128) {
    throw new Error("IDE context byte limits must be at least 128 bytes.");
  }
  return limits;
}

function validateProviderTimeout(value: number): number {
  if (!Number.isSafeInteger(value) || value <= 0 || value > MAX_PROVIDER_TIMEOUT_MS) {
    throw new Error(`IDE context provider timeout must be between 1 and ${MAX_PROVIDER_TIMEOUT_MS} ms.`);
  }
  return value;
}

class ProviderFailure extends Error {
  public constructor(public readonly reason: Extract<ContextSkipReason, "provider-timeout" | "cancelled">) {
    super(reason);
  }
}

function withTimeoutAndSignal<T>(promise: Promise<T>, timeoutMs: number, signal?: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    let settled = false;
    const finish = (callback: () => void): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      callback();
    };
    const onAbort = (): void => finish(() => reject(new ProviderFailure("cancelled")));
    const timer = setTimeout(
      () => finish(() => reject(new ProviderFailure("provider-timeout"))),
      timeoutMs
    );
    signal?.addEventListener("abort", onAbort, { once: true });
    if (signal?.aborted) {
      onAbort();
      return;
    }
    promise.then(
      (value) => finish(() => resolve(value)),
      (error: unknown) => finish(() => reject(error))
    );
  });
}

function isValidContextLocation(value: unknown): value is ContextLocation {
  try {
    if (!value || typeof value !== "object") {
      return false;
    }
    const candidate = value as { uri?: unknown; range?: unknown };
    if (!candidate.uri || typeof candidate.uri !== "object") {
      return false;
    }
    const uri = candidate.uri as Partial<ContextUri>;
    if (typeof uri.scheme !== "string" || typeof uri.fsPath !== "string" || typeof uri.toString !== "function") {
      return false;
    }
    return isValidContextRange(candidate.range);
  } catch {
    return false;
  }
}

function isValidContextRange(value: unknown): value is ContextRange {
  if (!value || typeof value !== "object") {
    return false;
  }
  const range = value as Partial<ContextRange>;
  if (!isValidContextPosition(range.start) || !isValidContextPosition(range.end)) {
    return false;
  }
  return range.start.line < range.end.line
    || (range.start.line === range.end.line && range.start.character <= range.end.character);
}

function isValidContextPosition(value: unknown): value is ContextPosition {
  if (!value || typeof value !== "object") {
    return false;
  }
  const position = value as Partial<ContextPosition>;
  return Number.isSafeInteger(position.line)
    && (position.line ?? -1) >= 0
    && Number.isSafeInteger(position.character)
    && (position.character ?? -1) >= 0;
}

export function isSensitivePath(fsPath: string): boolean {
  const hasTrailingSeparator = /[\\/]$/.test(fsPath);
  const rawParts = fsPath.split(/[\\/]+/).filter((part) => part.length > 0);
  const parts = rawParts.map((part) => part.toLowerCase());
  const basenameIndex = hasTrailingSeparator ? -1 : parts.length - 1;

  if (rawParts.some((part, index) =>
    SENSITIVE_DIRECTORY_NAMES.has(parts[index])
    || redactContextSecrets(part) !== part
    || (index !== basenameIndex && (parts[index] === ".env" || parts[index].startsWith(".env.")))
  )) {
    return true;
  }

  const name = basenameIndex >= 0 ? parts[basenameIndex] : "";
  if (SAFE_ENV_FILE_NAMES.has(name)) {
    return false;
  }
  if (name === ".env" || name.startsWith(".env.")) {
    return true;
  }
  if (SENSITIVE_FILE_NAMES.has(name) || SENSITIVE_EXTENSIONS.has(path.extname(name).toLowerCase())) {
    return true;
  }
  return false;
}

function isContained(root: string, candidate: string): boolean {
  const relative = path.relative(root, candidate);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

function selectWorkspaceFolders(
  folders: readonly ContextWorkspaceFolder[],
  requestedRoot: string | undefined,
  activeUri: ContextUri | undefined
): readonly ContextWorkspaceFolder[] {
  if (folders.length === 0) {
    return [];
  }
  if (requestedRoot) {
    const normalizedRoot = comparablePath(requestedRoot);
    const selected = folders.find((folder) => comparablePath(folder.uri.fsPath) === normalizedRoot);
    return selected ? [selected] : [];
  }
  if (folders.length === 1) {
    return folders;
  }
  if (!activeUri || activeUri.scheme.toLowerCase() !== "file") {
    return [];
  }
  const activePath = path.resolve(activeUri.fsPath);
  const candidates = folders
    .filter((folder) => folder.uri.scheme.toLowerCase() === "file" && isContained(path.resolve(folder.uri.fsPath), activePath))
    .sort((left, right) => path.resolve(right.uri.fsPath).length - path.resolve(left.uri.fsPath).length);
  return candidates.length > 0 ? [candidates[0]] : [];
}

function comparablePath(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

export function isSafePath(value: PathAssessment): value is SafePath {
  return "uri" in value;
}

function fileUri(fsPath: string): ContextUri {
  const url = pathToFileURL(fsPath);
  return {
    scheme: "file",
    fsPath,
    toString: () => url.toString()
  };
}

function provenance(
  source: string,
  capturedAt: string,
  folder: ContextWorkspaceFolder,
  redact: (text: string) => string
): Record<string, unknown> {
  return {
    source,
    captured_at: capturedAt,
    workspace_folder: cleanMetadata(folder.name, redact),
    trust: true,
    version: 1
  };
}

function genericProvenance(source: string, capturedAt: string): Record<string, unknown> {
  return { source, captured_at: capturedAt, trust: true, version: 1 };
}

function backendRange(range: ContextRange): Record<string, Record<string, number>> {
  return {
    start: { line: nonNegative(range.start.line), character: nonNegative(range.start.character) },
    end: { line: nonNegative(range.end.line), character: nonNegative(range.end.character) }
  };
}

function nonNegative(value: number): number {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function isEmptyRange(range: ContextRange): boolean {
  return range.start.line === range.end.line && range.start.character === range.end.character;
}

function rangeKey(range: ContextRange): string {
  return `${range.start.line}:${range.start.character}-${range.end.line}:${range.end.character}`;
}

function cleanMetadata(value: string, redact: (text: string) => string): string {
  return truncateUtf8(redact(value.replaceAll("\u0000", "")), 1024);
}

function sha256(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

function truncateUtf8(value: string, maxBytes: number): string {
  const encoded = Buffer.from(value, "utf8");
  if (encoded.length <= maxBytes) {
    return value;
  }
  const marker = "\n[...truncated...]";
  const markerBytes = Buffer.byteLength(marker);
  if (maxBytes <= markerBytes) {
    return encoded.subarray(0, maxBytes).toString("utf8").replace(/\uFFFD$/u, "");
  }
  return encoded.subarray(0, maxBytes - markerBytes).toString("utf8").replace(/\uFFFD$/u, "") + marker;
}

function jsonBytes(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value), "utf8");
}

function uriKey(uri: ContextUri): string {
  return `${uri.scheme.toLowerCase()}:${path.resolve(uri.fsPath).toLowerCase()}`;
}

function toPosix(value: string): string {
  return value.split(path.sep).join("/");
}
