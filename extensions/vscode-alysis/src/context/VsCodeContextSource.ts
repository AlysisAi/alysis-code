import { realpath } from "node:fs/promises";
import path from "node:path";

import type * as vscode from "vscode";

import { GitIgnoreProbe, IgnoreProbe } from "./GitIgnoreProbe";
import {
  ContextDiagnostic,
  ContextDiagnosticSeverity,
  ContextDocument,
  ContextEditor,
  ContextHover,
  ContextLocation,
  ContextPosition,
  ContextRange,
  ContextSymbol,
  ContextUri,
  ContextWorkspaceFolder,
  ExplicitTerminalSelectionProvider,
  GitContextRepository,
  IdeContextSource
} from "./IdeContextTypes";

export interface VsCodeContextSourceOptions {
  /** Deliberately absent by default: VS Code exposes no safe terminal-selection API. */
  readonly terminalSelectionProvider?: ExplicitTerminalSelectionProvider;
  readonly ignoreProbe?: IgnoreProbe;
}

interface GitApiRepository {
  readonly rootUri: vscode.Uri;
  diff(cached?: boolean): Promise<string>;
}

interface GitApi {
  readonly repositories: readonly GitApiRepository[];
}

interface GitExtensionExports {
  readonly enabled: boolean;
  getAPI(version: 1): GitApi;
}

/**
 * Build the production VS Code adapter without making the collector itself
 * depend on the `vscode` runtime. The adapter only uses public VS Code APIs and
 * the stable v1 API exported by the built-in Git extension.
 */
export function createVsCodeContextSource(
  vscodeApi: typeof vscode,
  options: VsCodeContextSourceOptions = {}
): IdeContextSource {
  const ignoreProbe = options.ignoreProbe ?? new GitIgnoreProbe();
  const wrapDocument = (document: vscode.TextDocument): ContextDocument => ({
    uri: wrapUri(document.uri),
    languageId: document.languageId,
    version: document.version,
    isDirty: document.isDirty,
    lineCount: document.lineCount,
    getText: (range?: ContextRange) => document.getText(range ? toVsCodeRange(vscodeApi, range) : undefined),
    getLineRange: (line: number) => fromVsCodeRange(document.lineAt(line).range)
  });

  const wrapEditor = (editor: vscode.TextEditor): ContextEditor => ({
    document: wrapDocument(editor.document),
    selection: fromVsCodeRange(editor.selection),
    visibleRanges: editor.visibleRanges.map(fromVsCodeRange)
  });

  return {
    get isWorkspaceTrusted(): boolean {
      return vscodeApi.workspace.isTrusted;
    },

    getWorkspaceFolders(): readonly ContextWorkspaceFolder[] {
      return (vscodeApi.workspace.workspaceFolders ?? []).map((folder) => ({
        name: folder.name,
        index: folder.index,
        uri: wrapUri(folder.uri)
      }));
    },

    getActiveEditor(): ContextEditor | undefined {
      const editor = vscodeApi.window.activeTextEditor;
      return editor ? wrapEditor(editor) : undefined;
    },

    getOpenEditors(): readonly ContextEditor[] {
      return vscodeApi.window.visibleTextEditors.map(wrapEditor);
    },

    getOpenDocuments(): readonly ContextDocument[] {
      return vscodeApi.workspace.textDocuments.map(wrapDocument);
    },

    getDiagnostics(limit: number): readonly ContextDiagnostic[] {
      const diagnostics: ContextDiagnostic[] = [];
      for (const [uri, entries] of vscodeApi.languages.getDiagnostics()) {
        for (const entry of entries) {
          if (diagnostics.length >= limit) {
            return diagnostics;
          }
          diagnostics.push({
            uri: wrapUri(uri),
            range: fromVsCodeRange(entry.range),
            severity: diagnosticSeverity(vscodeApi, entry.severity),
            message: entry.message,
            ...(entry.source ? { source: entry.source } : {}),
            ...(entry.code !== undefined ? { code: diagnosticCode(entry.code) } : {})
          });
        }
      }
      return diagnostics;
    },

    async getGitRepositories(): Promise<readonly GitContextRepository[]> {
      const extension = vscodeApi.extensions.getExtension<GitExtensionExports>("vscode.git");
      if (!extension) {
        return [];
      }
      const exports = extension.isActive ? extension.exports : await extension.activate();
      if (!exports.enabled) {
        return [];
      }
      return exports.getAPI(1).repositories.map((repository) => ({
        rootUri: wrapUri(repository.rootUri),
        diff: (staged: boolean) => repository.diff(staged)
      }));
    },

    async getDocumentSymbols(document: ContextDocument, limit: number): Promise<readonly ContextSymbol[]> {
      const uri = vscodeApi.Uri.file(document.uri.fsPath);
      const symbols = await vscodeApi.commands.executeCommand<
        Array<vscode.DocumentSymbol | vscode.SymbolInformation> | undefined
      >("vscode.executeDocumentSymbolProvider", uri);
      return flattenSymbols(symbols ?? [], document.uri, limit);
    },

    async getWorkspaceSymbols(query: string, limit: number): Promise<readonly ContextSymbol[]> {
      const symbols = await vscodeApi.commands.executeCommand<unknown>(
        "vscode.executeWorkspaceSymbolProvider",
        query
      );
      if (symbols === undefined) {
        return [];
      }
      if (!Array.isArray(symbols)) {
        throw new Error("Workspace symbol provider returned malformed data.");
      }
      return symbols.slice(0, limit).map((symbol) => contextSymbol(symbol));
    },

    async getDefinitions(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextLocation[]> {
      return executeLocationProvider("vscode.executeDefinitionProvider", document, position, limit);
    },

    async getTypeDefinitions(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextLocation[]> {
      return executeLocationProvider("vscode.executeTypeDefinitionProvider", document, position, limit);
    },

    async getImplementations(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextLocation[]> {
      return executeLocationProvider("vscode.executeImplementationProvider", document, position, limit);
    },

    async getHovers(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextHover[]> {
      const uri = vscodeApi.Uri.file(document.uri.fsPath);
      const raw = await vscodeApi.commands.executeCommand<unknown>(
        "vscode.executeHoverProvider",
        uri,
        new vscodeApi.Position(position.line, position.character)
      );
      if (raw === undefined) {
        return [];
      }
      if (!Array.isArray(raw)) {
        throw new Error("Hover provider returned malformed data.");
      }
      return raw.slice(0, limit).map((hover) => contextHover(hover, document.uri, position));
    },

    async getReferences(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextLocation[]> {
      return executeLocationProvider("vscode.executeReferenceProvider", document, position, limit);
    },

    async getIncomingCalls(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextSymbol[]> {
      const items = await prepareCallHierarchy(document, position);
      const results: ContextSymbol[] = [];
      for (const item of items) {
        const calls = await vscodeApi.commands.executeCommand<unknown>("vscode.provideIncomingCalls", item);
        if (calls === undefined) {
          continue;
        }
        if (!Array.isArray(calls)) {
          throw new Error("Incoming call provider returned malformed data.");
        }
        for (const call of calls) {
          const normalized = incomingCall(call);
          for (const callRange of normalized.ranges) {
            results.push({
              name: normalized.name,
              ...(normalized.detail ? { detail: normalized.detail } : {}),
              uri: normalized.uri,
              range: callRange
            });
            if (results.length >= limit) {
              return results;
            }
          }
        }
      }
      return results;
    },

    async getOutgoingCalls(
      document: ContextDocument,
      position: ContextPosition,
      limit: number
    ): Promise<readonly ContextSymbol[]> {
      const items = await prepareCallHierarchy(document, position);
      const results: ContextSymbol[] = [];
      for (const item of items) {
        const calls = await vscodeApi.commands.executeCommand<unknown>("vscode.provideOutgoingCalls", item);
        if (calls === undefined) {
          continue;
        }
        if (!Array.isArray(calls)) {
          throw new Error("Outgoing call provider returned malformed data.");
        }
        for (const call of calls) {
          results.push(outgoingCall(call));
          if (results.length >= limit) {
            return results;
          }
        }
      }
      return results;
    },

    async openDocument(uri: ContextUri): Promise<ContextDocument | undefined> {
      try {
        return wrapDocument(await vscodeApi.workspace.openTextDocument(vscodeApi.Uri.file(uri.fsPath)));
      } catch {
        return undefined;
      }
    },

    async isIgnored(uri: ContextUri, folder: ContextWorkspaceFolder): Promise<boolean> {
      const candidate = vscodeApi.Uri.file(uri.fsPath);
      try {
        await vscodeApi.workspace.fs.stat(candidate);
      } catch {
        // Deleted tracked files can still appear in a Git diff. The Git API does
        // not return untracked ignored files, so a missing path is not rejected.
        return false;
      }
      // The collector supplies a physical path, while VS Code can retain an
      // alias such as macOS /var or a Windows short-name workspace root.
      const [physicalRoot, physicalCandidate] = await Promise.all([
        realpath(folder.uri.fsPath), realpath(uri.fsPath)
      ]);
      const relativePath = path.relative(physicalRoot, physicalCandidate);
      if (relativePath === ".." || relativePath.startsWith(`..${path.sep}`) || path.isAbsolute(relativePath)) {
        throw new Error("Ignore policy candidate must be inside its workspace root.");
      }
      const relative = relativePath.split(path.sep).join("/");
      if (!relative || relative === ".") {
        return false;
      }
      const pattern = new vscodeApi.RelativePattern(vscodeApi.Uri.file(folder.uri.fsPath), escapeGlob(relative));
      const matches = await vscodeApi.workspace.findFiles(pattern, undefined, 1);
      const wanted = normalizedPath(physicalCandidate);
      const physicalMatches = await Promise.all(matches.map((match) => realpath(match.fsPath)));
      if (!physicalMatches.some((match) => normalizedPath(match) === wanted)) {
        return true;
      }
      return ignoreProbe.isIgnored(physicalRoot, physicalCandidate);
    },

    realpath,

    async getExplicitTerminalSelection() {
      return options.terminalSelectionProvider?.getSelection();
    }
  };

  async function executeLocationProvider(
    command: string,
    document: ContextDocument,
    position: ContextPosition,
    limit: number
  ): Promise<readonly ContextLocation[]> {
    const raw = await vscodeApi.commands.executeCommand<unknown>(
      command,
      vscodeApi.Uri.file(document.uri.fsPath),
      new vscodeApi.Position(position.line, position.character)
    );
    return normalizeProviderLocations(raw, limit);
  }

  async function prepareCallHierarchy(
    document: ContextDocument,
    position: ContextPosition
  ): Promise<readonly unknown[]> {
    const raw = await vscodeApi.commands.executeCommand<unknown>(
      "vscode.prepareCallHierarchy",
      vscodeApi.Uri.file(document.uri.fsPath),
      new vscodeApi.Position(position.line, position.character)
    );
    if (raw === undefined) {
      return [];
    }
    if (!Array.isArray(raw) || raw.some((item) => !isCallHierarchyItem(item))) {
      throw new Error("Call hierarchy provider returned malformed data.");
    }
    return raw;
  }

  function flattenSymbols(
    values: readonly (vscode.DocumentSymbol | vscode.SymbolInformation)[],
    documentUri: ContextUri,
    limit: number
  ): ContextSymbol[] {
    const result: ContextSymbol[] = [];
    for (const value of values) {
      if (result.length >= limit) {
        break;
      }
      if (!isObject(value) || typeof value.name !== "string") {
        throw new Error("Document symbol provider returned malformed data.");
      }
      if ("location" in value) {
        if (!isObject(value.location) || !isUriLike(value.location.uri) || !isRangeLike(value.location.range)) {
          throw new Error("Document symbol provider returned malformed data.");
        }
        result.push({
          name: value.name,
          uri: wrapUri(value.location.uri),
          range: fromVsCodeRange(value.location.range)
        });
        continue;
      }
      if (!isRangeLike(value.range) || !Array.isArray(value.children)) {
        throw new Error("Document symbol provider returned malformed data.");
      }
      result.push({
        name: value.name,
        uri: documentUri,
        range: fromVsCodeRange(value.range)
      });
      result.push(...flattenDocumentSymbolChildren(value.children, result[result.length - 1].uri, limit - result.length));
    }
    return result;
  }

  function flattenDocumentSymbolChildren(
    values: readonly vscode.DocumentSymbol[],
    uri: ContextUri,
    limit: number
  ): ContextSymbol[] {
    const result: ContextSymbol[] = [];
    for (const value of values) {
      if (result.length >= limit) {
        break;
      }
      if (!isObject(value) || typeof value.name !== "string" || !isRangeLike(value.range) || !Array.isArray(value.children)) {
        throw new Error("Document symbol provider returned malformed data.");
      }
      result.push({ name: value.name, uri, range: fromVsCodeRange(value.range) });
      result.push(...flattenDocumentSymbolChildren(value.children, uri, limit - result.length));
    }
    return result;
  }

}

function normalizeProviderLocations(raw: unknown, limit: number): ContextLocation[] {
  if (raw === undefined) {
    return [];
  }
  const values = Array.isArray(raw) ? raw : [raw];
  const results: ContextLocation[] = [];
  for (const value of values.slice(0, limit)) {
    if (isLocation(value)) {
      results.push({ uri: wrapUri(value.uri), range: fromVsCodeRange(value.range) });
      continue;
    }
    if (isLocationLink(value)) {
      results.push({
        uri: wrapUri(value.targetUri),
        range: fromVsCodeRange(value.targetSelectionRange ?? value.targetRange)
      });
      continue;
    }
    throw new Error("Language provider returned malformed location data.");
  }
  return results;
}

function contextSymbol(value: unknown): ContextSymbol {
  if (!isObject(value)
    || typeof value.name !== "string"
    || !isObject(value.location)
    || !isUriLike(value.location.uri)
    || !isRangeLike(value.location.range)) {
    throw new Error("Workspace symbol provider returned malformed data.");
  }
  return {
    name: value.name,
    ...(typeof value.containerName === "string" && value.containerName ? { detail: value.containerName } : {}),
    uri: wrapUri(value.location.uri),
    range: fromVsCodeRange(value.location.range)
  };
}

function contextHover(
  value: unknown,
  documentUri: ContextUri,
  position: ContextPosition
): ContextHover {
  if (!isObject(value) || !Array.isArray(value.contents)) {
    throw new Error("Hover provider returned malformed data.");
  }
  const contents = value.contents.map(markedStringValue).filter((item) => item.length > 0).join("\n");
  if (!contents) {
    throw new Error("Hover provider returned empty or malformed data.");
  }
  const hoverRange = value.range === undefined
    ? {
        start: { line: position.line, character: position.character },
        end: { line: position.line, character: position.character }
      }
    : isRangeLike(value.range)
      ? fromVsCodeRange(value.range)
      : undefined;
  if (!hoverRange) {
    throw new Error("Hover provider returned malformed range data.");
  }
  return { uri: documentUri, range: hoverRange, contents };
}

function markedStringValue(value: unknown): string {
  if (typeof value === "string") {
    return value;
  }
  if (!isObject(value) || typeof value.value !== "string") {
    throw new Error("Hover provider returned malformed contents.");
  }
  return value.value;
}

function incomingCall(value: unknown): { name: string; detail?: string; uri: ContextUri; ranges: ContextRange[] } {
  if (!isObject(value) || !isCallHierarchyItem(value.from) || !Array.isArray(value.fromRanges)) {
    throw new Error("Incoming call provider returned malformed data.");
  }
  const ranges = value.fromRanges.map((callRange) => {
    if (!isRangeLike(callRange)) {
      throw new Error("Incoming call provider returned malformed ranges.");
    }
    return fromVsCodeRange(callRange);
  });
  return {
    name: value.from.name,
    ...(typeof value.from.detail === "string" && value.from.detail ? { detail: value.from.detail } : {}),
    uri: wrapUri(value.from.uri),
    ranges
  };
}

function outgoingCall(value: unknown): ContextSymbol {
  if (!isObject(value) || !isCallHierarchyItem(value.to) || !Array.isArray(value.fromRanges)) {
    throw new Error("Outgoing call provider returned malformed data.");
  }
  if (value.fromRanges.some((callRange) => !isRangeLike(callRange))) {
    throw new Error("Outgoing call provider returned malformed ranges.");
  }
  return {
    name: value.to.name,
    ...(typeof value.to.detail === "string" && value.to.detail ? { detail: value.to.detail } : {}),
    uri: wrapUri(value.to.uri),
    range: fromVsCodeRange(value.to.selectionRange)
  };
}

function isCallHierarchyItem(value: unknown): value is {
  name: string;
  detail?: string;
  uri: vscode.Uri;
  range: vscode.Range;
  selectionRange: vscode.Range;
} {
  return isObject(value)
    && typeof value.name === "string"
    && (value.detail === undefined || typeof value.detail === "string")
    && isUriLike(value.uri)
    && isRangeLike(value.range)
    && isRangeLike(value.selectionRange);
}

function isLocation(value: unknown): value is { uri: vscode.Uri; range: vscode.Range } {
  return isObject(value) && isUriLike(value.uri) && isRangeLike(value.range);
}

function isLocationLink(value: unknown): value is {
  targetUri: vscode.Uri;
  targetRange: vscode.Range;
  targetSelectionRange?: vscode.Range;
} {
  return isObject(value)
    && isUriLike(value.targetUri)
    && isRangeLike(value.targetRange)
    && (value.targetSelectionRange === undefined || isRangeLike(value.targetSelectionRange));
}

function isUriLike(value: unknown): value is vscode.Uri {
  return isObject(value)
    && typeof value.scheme === "string"
    && typeof value.fsPath === "string"
    && typeof value.toString === "function";
}

function isRangeLike(value: unknown): value is vscode.Range {
  return isObject(value) && isPositionLike(value.start) && isPositionLike(value.end);
}

function isPositionLike(value: unknown): boolean {
  return isObject(value)
    && Number.isSafeInteger(value.line)
    && (value.line as number) >= 0
    && Number.isSafeInteger(value.character)
    && (value.character as number) >= 0;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object";
}

function wrapUri(uri: vscode.Uri): ContextUri {
  return { scheme: uri.scheme, fsPath: uri.fsPath, toString: () => uri.toString() };
}

function fromVsCodeRange(range: vscode.Range): ContextRange {
  return {
    start: { line: range.start.line, character: range.start.character },
    end: { line: range.end.line, character: range.end.character }
  };
}

function toVsCodeRange(vscodeApi: typeof vscode, range: ContextRange): vscode.Range {
  return new vscodeApi.Range(
    range.start.line,
    range.start.character,
    range.end.line,
    range.end.character
  );
}

function diagnosticSeverity(
  vscodeApi: typeof vscode,
  severity: vscode.DiagnosticSeverity
): ContextDiagnosticSeverity {
  switch (severity) {
    case vscodeApi.DiagnosticSeverity.Error:
      return "error";
    case vscodeApi.DiagnosticSeverity.Warning:
      return "warning";
    case vscodeApi.DiagnosticSeverity.Information:
      return "information";
    case vscodeApi.DiagnosticSeverity.Hint:
      return "hint";
  }
}

function diagnosticCode(code: string | number | { value: string | number }): string | number {
  return typeof code === "object" ? code.value : code;
}

function escapeGlob(value: string): string {
  const replacements: Record<string, string> = {
    "*": "[*]",
    "?": "[?]",
    "[": "[[]",
    "]": "[]]",
    "{": "[{]",
    "}": "[}]"
  };
  return [...value].map((char) => replacements[char] ?? char).join("");
}

function normalizedPath(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}
