import assert from "node:assert/strict";
import test from "node:test";

import type * as vscode from "vscode";

import { createVsCodeDiagnosticsSource } from "../src/verification/VsCodeDiagnosticsSource";

test("VS Code diagnostic adapter maps public API data and preserves exact bounded totals", () => {
  const fixture = createFixture();
  const source = createVsCodeDiagnosticsSource(fixture.api);

  const result = source.readDiagnostics(2);

  assert.equal(result.totalCount, 3);
  assert.equal(result.truncated, true);
  assert.equal(result.diagnostics.length, 2);
  assert.deepEqual(result.diagnostics.map((item) => item.severity), ["error", "warning"]);
  assert.equal(result.diagnostics[0].uri, "file:///workspace/a.ts");
  assert.equal(result.diagnostics[0].code, "TS1");
  assert.equal(result.diagnostics[1].code, 2);
  assert.deepEqual(result.diagnostics[0].range, {
    start: { line: 1, character: 2 },
    end: { line: 1, character: 7 }
  });
});

test("VS Code diagnostic adapter forwards changes and disposes the public event subscription", () => {
  const fixture = createFixture();
  const source = createVsCodeDiagnosticsSource(fixture.api);
  let changes = 0;
  const subscription = source.onDidChangeDiagnostics(() => changes += 1);

  fixture.emitChange();
  assert.equal(changes, 1);
  subscription.dispose();
  fixture.emitChange();

  assert.equal(changes, 1);
  assert.equal(fixture.listenerCount(), 0);
});

test("VS Code diagnostic adapter scopes totals and items to file-backed documents in the active root", () => {
  const severity = { Error: 0, Warning: 1, Information: 2, Hint: 3 } as const;
  const primary = uri("file:///workspace/primary/a.ts");
  const secondary = uri("file:///workspace/secondary/b.ts");
  const untitled = uri("untitled:///workspace/primary/scratch.ts");
  const virtual = uri("git:///workspace/primary/a.ts");
  const api = {
    DiagnosticSeverity: severity,
    workspace: {
      workspaceFolders: [
        { uri: uri("file:///workspace/primary") },
        { uri: uri("file:///workspace/secondary") }
      ]
    },
    languages: {
      getDiagnostics: () => [
        [primary, [vscodeDiagnostic(severity.Error, "primary")]],
        [secondary, [vscodeDiagnostic(severity.Error, "secondary")]],
        [untitled, [vscodeDiagnostic(severity.Error, "scratch")]],
        [virtual, [vscodeDiagnostic(severity.Error, "version")]]
      ],
      onDidChangeDiagnostics: () => ({ dispose: () => undefined })
    }
  } as unknown as typeof vscode;

  const result = createVsCodeDiagnosticsSource(api).readDiagnostics(10, {
    workspaceRoot: "/workspace/primary"
  });

  assert.equal(result.totalCount, 1);
  assert.equal(result.truncated, false);
  assert.deepEqual(result.diagnostics.map((item) => item.message), ["primary"]);
});

function createFixture(): {
  api: typeof vscode;
  emitChange(): void;
  listenerCount(): number;
} {
  const severity = { Error: 0, Warning: 1, Information: 2, Hint: 3 } as const;
  const listeners = new Set<() => void>();
  const a = uri("file:///workspace/a.ts");
  const b = uri("file:///workspace/b.ts");
  const diagnostics = [
    [a, [
      vscodeDiagnostic(severity.Error, "error", { value: "TS1", target: uri("https://example.invalid/TS1") }),
      vscodeDiagnostic(severity.Warning, "warning", 2)
    ]],
    [b, [vscodeDiagnostic(severity.Information, "info")]]
  ] as unknown as Array<[vscode.Uri, vscode.Diagnostic[]]>;
  const api = {
    DiagnosticSeverity: severity,
    languages: {
      getDiagnostics: () => diagnostics,
      onDidChangeDiagnostics: (listener: () => void) => {
        listeners.add(listener);
        return { dispose: () => listeners.delete(listener) };
      }
    }
  } as unknown as typeof vscode;
  return {
    api,
    emitChange: () => {
      for (const listener of [...listeners]) {
        listener();
      }
    },
    listenerCount: () => listeners.size
  };
}

function vscodeDiagnostic(
  severity: vscode.DiagnosticSeverity,
  message: string,
  code?: vscode.Diagnostic["code"]
): vscode.Diagnostic {
  return {
    range: {
      start: { line: 1, character: 2 },
      end: { line: 1, character: 7 }
    },
    severity,
    message,
    source: "typescript",
    code
  } as vscode.Diagnostic;
}

function uri(value: string): vscode.Uri {
  const parsed = new URL(value);
  return {
    scheme: parsed.protocol.replace(":", ""),
    authority: parsed.host,
    fsPath: decodeURIComponent(parsed.pathname),
    toString: () => value
  } as vscode.Uri;
}
