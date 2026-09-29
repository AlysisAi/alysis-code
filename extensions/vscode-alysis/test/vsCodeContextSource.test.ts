import assert from "node:assert/strict";
import { mkdir, mkdtemp, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import test from "node:test";

import type * as vscode from "vscode";

import { createVsCodeContextSource } from "../src/context/VsCodeContextSource";
import { IdeContextCollector } from "../src/context/IdeContextCollector";
import { ContextDocument, ContextRange } from "../src/context/IdeContextTypes";
import { diagnosticVerificationFingerprint } from "../src/verification/diagnosticFingerprint";

test("workspace aliases preserve ignore policy and the editor's diagnostic identity", async (t) => {
  const temporary = await realpath(await mkdtemp(path.join(tmpdir(), "alysis-context-alias-")));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const physicalRoot = path.join(temporary, "physical");
  const aliasRoot = path.join(temporary, "alias");
  await mkdir(physicalRoot);
  await writeFile(path.join(physicalRoot, "main.ts"), "export const value = 1;\n");
  await symlink(physicalRoot, aliasRoot, process.platform === "win32" ? "junction" : "dir");
  const fileUri = (fsPath: string) => ({
    scheme: "file", authority: "", fsPath, toString: () => pathToFileURL(fsPath).toString()
  });
  const editorUri = fileUri(path.join(aliasRoot, "main.ts"));
  const diagnostic = { range: range(0, 0, 0, 5), severity: 0, message: "Must be cleared", code: "attached" };
  const probeCalls: Array<[string, string]> = [];
  let excludedByEditor = false;
  let excludedByGit = false;
  const api = {
    DiagnosticSeverity: { Error: 0, Warning: 1, Information: 2, Hint: 3 },
    Uri: { file: fileUri },
    RelativePattern: class {
      constructor(public readonly baseUri: unknown, public readonly pattern: string) {}
    },
    window: { activeTextEditor: undefined },
    workspace: {
      isTrusted: true,
      workspaceFolders: [{ name: "alias", index: 0, uri: fileUri(aliasRoot) }],
      fs: { stat: (candidate: { fsPath: string }) => stat(candidate.fsPath) },
      findFiles: async (pattern: { baseUri: { fsPath: string }; pattern: string }) => {
        assert.equal(pattern.baseUri.fsPath, aliasRoot);
        assert.equal(pattern.pattern, "main.ts");
        return excludedByEditor ? [] : [editorUri];
      }
    },
    languages: { getDiagnostics: () => [[editorUri, [diagnostic]]] }
  } as unknown as typeof vscode;
  const source = createVsCodeContextSource(api, {
    ignoreProbe: { isIgnored: async (root, candidate) => {
      probeCalls.push([root, candidate]);
      return excludedByGit;
    } }
  });
  const collector = new IdeContextCollector(source);
  const collect = () => collector.collect({ includeSelection: false, includeOpenEditors: false });
  const result = await collect();
  assert.deepEqual(result.skipped, []);
  const items = result.blocks[0]?.items as Array<Record<string, unknown>>;
  assert.equal(items?.length, 1);
  assert.equal(items[0].uri, fileUri(path.join(physicalRoot, "main.ts")).toString());
  assert.equal(items[0].verification_id, diagnosticVerificationFingerprint({
    ...diagnostic, uri: editorUri.toString(), severity: "error"
  }));
  assert.deepEqual(probeCalls, [[physicalRoot, path.join(physicalRoot, "main.ts")]]);

  excludedByEditor = true;
  assert.deepEqual((await collect()).blocks, []);
  assert.equal(probeCalls.length, 1, "editor exclusions must not be overridden by Git");
  excludedByEditor = false;
  excludedByGit = true;
  assert.deepEqual((await collect()).blocks, []);
});

test("VS Code language-service adapter uses public commands and normalizes bounded results", async () => {
  const mainUri = uri("C:\\workspace\\main.ts");
  const targetUri = uri("C:\\workspace\\target.ts");
  const item = {
    name: "caller",
    detail: "main.ts",
    uri: mainUri,
    range: range(0, 0, 4, 0),
    selectionRange: range(0, 9, 0, 15)
  };
  const targetItem = {
    name: "target",
    detail: "target.ts",
    uri: targetUri,
    range: range(1, 0, 3, 0),
    selectionRange: range(1, 9, 1, 15)
  };
  const calls: string[] = [];
  const api = vscodeApi(async (command) => {
    calls.push(command);
    switch (command) {
      case "vscode.executeDocumentSymbolProvider":
        return [{ name: "local", range: range(0, 0, 0, 10), children: [] }];
      case "vscode.executeWorkspaceSymbolProvider":
        return [{ name: "global", containerName: "pkg", location: { uri: targetUri, range: range(1, 0, 1, 6) } }];
      case "vscode.executeDefinitionProvider":
        return [{ targetUri, targetRange: range(1, 0, 1, 20), targetSelectionRange: range(1, 9, 1, 15) }];
      case "vscode.executeTypeDefinitionProvider":
      case "vscode.executeImplementationProvider":
      case "vscode.executeReferenceProvider":
        return [{ uri: targetUri, range: range(1, 9, 1, 15) }];
      case "vscode.executeHoverProvider":
        return [{ contents: ["target(): number", { value: "documentation" }], range: range(0, 9, 0, 15) }];
      case "vscode.prepareCallHierarchy":
        return [item];
      case "vscode.provideIncomingCalls":
        return [{ from: targetItem, fromRanges: [range(2, 2, 2, 8)] }];
      case "vscode.provideOutgoingCalls":
        return [{ to: targetItem, fromRanges: [range(2, 2, 2, 8)] }];
      default:
        throw new Error(`Unexpected command ${command}`);
    }
  });
  const source = createVsCodeContextSource(api);
  const document = contextDocument(mainUri);
  const position = { line: 0, character: 10 };

  assert.equal((await source.getDocumentSymbols(document, 5))[0].name, "local");
  const workspaceSymbol = (await source.getWorkspaceSymbols("glob", 5))[0];
  assert.equal(workspaceSymbol.name, "global");
  assert.equal(workspaceSymbol.detail, "pkg");
  assert.equal(workspaceSymbol.uri.fsPath, targetUri.fsPath);
  assert.deepEqual(workspaceSymbol.range, range(1, 0, 1, 6));
  assert.equal((await source.getDefinitions(document, position, 5))[0].range.start.character, 9);
  assert.equal((await source.getTypeDefinitions(document, position, 5)).length, 1);
  assert.equal((await source.getImplementations(document, position, 5)).length, 1);
  assert.equal((await source.getReferences(document, position, 5)).length, 1);
  assert.equal((await source.getHovers(document, position, 5))[0].contents, "target(): number\ndocumentation");
  assert.equal((await source.getIncomingCalls(document, position, 5))[0].range.start.line, 2);
  assert.equal((await source.getOutgoingCalls(document, position, 5))[0].range.start.line, 1);

  assert.deepEqual(calls, [
    "vscode.executeDocumentSymbolProvider",
    "vscode.executeWorkspaceSymbolProvider",
    "vscode.executeDefinitionProvider",
    "vscode.executeTypeDefinitionProvider",
    "vscode.executeImplementationProvider",
    "vscode.executeReferenceProvider",
    "vscode.executeHoverProvider",
    "vscode.prepareCallHierarchy",
    "vscode.provideIncomingCalls",
    "vscode.prepareCallHierarchy",
    "vscode.provideOutgoingCalls"
  ]);
});

test("VS Code language-service adapter rejects malformed provider payloads", async () => {
  const api = vscodeApi(async (command) => {
    if (command === "vscode.executeDefinitionProvider") {
      return [{ targetUri: uri("C:\\workspace\\bad.ts") }];
    }
    return undefined;
  });
  const source = createVsCodeContextSource(api);

  await assert.rejects(
    source.getDefinitions(contextDocument(uri("C:\\workspace\\main.ts")), { line: 0, character: 0 }, 5),
    /malformed/i
  );
});

function vscodeApi(executeCommand: (command: string, ...args: unknown[]) => Promise<unknown>): typeof vscode {
  class Position {
    public constructor(public readonly line: number, public readonly character: number) {}
  }
  return {
    Uri: { file: (fsPath: string) => uri(fsPath) },
    Position,
    commands: { executeCommand },
    workspace: {},
    window: {},
    languages: {},
    extensions: {}
  } as unknown as typeof vscode;
}

function contextDocument(documentUri: vscode.Uri): ContextDocument {
  return {
    uri: { scheme: documentUri.scheme, fsPath: documentUri.fsPath, toString: () => documentUri.toString() },
    languageId: "typescript",
    version: 1,
    isDirty: false,
    lineCount: 1,
    getText: () => "const target = 1;",
    getLineRange: () => range(0, 0, 0, 17)
  };
}

function uri(fsPath: string): vscode.Uri {
  return {
    scheme: "file",
    fsPath,
    toString: () => `file:///${fsPath.replaceAll("\\", "/")}`
  } as vscode.Uri;
}

function range(startLine: number, startCharacter: number, endLine: number, endCharacter: number): ContextRange {
  return {
    start: { line: startLine, character: startCharacter },
    end: { line: endLine, character: endCharacter }
  };
}
