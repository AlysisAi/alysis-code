import assert from "node:assert/strict";
import test from "node:test";

import type * as vscode from "vscode";

import { createVsCodeContextSource } from "../src/context/VsCodeContextSource";
import { ContextDocument, ContextRange } from "../src/context/IdeContextTypes";

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
