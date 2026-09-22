import type * as vscode from "vscode";

import { isFileBackedUriInWorkspaceRoot } from "../workspace/fileBackedWorkspace";
import {
  DiagnosticReadResult,
  DiagnosticsVerificationScope,
  DiagnosticsVerificationSource,
  VerificationDiagnostic,
  VerificationDiagnosticSeverity
} from "./DiagnosticsVerificationGate";

/**
 * Production adapter backed only by VS Code's public languages diagnostic API.
 * The adapter copies at most `maxItems` diagnostics but reports the exact total
 * so the gate can fail closed when its bounded snapshot is incomplete.
 */
export function createVsCodeDiagnosticsSource(vscodeApi: typeof vscode): DiagnosticsVerificationSource {
  return {
    readDiagnostics(maxItems: number, scope?: DiagnosticsVerificationScope): DiagnosticReadResult {
      const limit = Number.isFinite(maxItems) ? Math.min(100_000, Math.max(0, Math.floor(maxItems))) : 0;
      const diagnostics: VerificationDiagnostic[] = [];
      let totalCount = 0;
      for (const [uri, entries] of vscodeApi.languages.getDiagnostics()) {
        if (scope && !isFileBackedUriInWorkspaceRoot(vscodeApi.workspace, uri, scope.workspaceRoot)) {
          continue;
        }
        totalCount += entries.length;
        const remaining = Math.max(0, limit - diagnostics.length);
        for (const entry of entries.slice(0, remaining)) {
          diagnostics.push({
            uri: uri.toString(),
            range: {
              start: { line: entry.range.start.line, character: entry.range.start.character },
              end: { line: entry.range.end.line, character: entry.range.end.character }
            },
            severity: diagnosticSeverity(vscodeApi, entry.severity),
            message: entry.message,
            ...(entry.source !== undefined ? { source: entry.source } : {}),
            ...(entry.code !== undefined ? { code: diagnosticCode(entry.code) } : {})
          });
        }
      }
      return {
        diagnostics,
        totalCount,
        truncated: totalCount > diagnostics.length
      };
    },

    onDidChangeDiagnostics(listener: () => void) {
      const disposable = vscodeApi.languages.onDidChangeDiagnostics(() => listener());
      return { dispose: () => disposable.dispose() };
    }
  };
}

function diagnosticSeverity(
  vscodeApi: typeof vscode,
  severity: vscode.DiagnosticSeverity
): VerificationDiagnosticSeverity {
  switch (severity) {
    case vscodeApi.DiagnosticSeverity.Error:
      return "error";
    case vscodeApi.DiagnosticSeverity.Warning:
      return "warning";
    case vscodeApi.DiagnosticSeverity.Information:
      return "information";
    case vscodeApi.DiagnosticSeverity.Hint:
      return "hint";
    default:
      return "error";
  }
}

function diagnosticCode(code: NonNullable<vscode.Diagnostic["code"]>): string | number {
  if (typeof code === "object") {
    return code.value;
  }
  return code;
}
