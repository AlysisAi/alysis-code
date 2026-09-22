import { createHash } from "node:crypto";

export interface DiagnosticFingerprintInput {
  readonly uri: string;
  readonly range: {
    readonly start: { readonly line: number; readonly character: number };
    readonly end: { readonly line: number; readonly character: number };
  };
  readonly severity: string;
  readonly message: string;
  readonly source?: string;
  readonly code?: string | number;
}

/**
 * Opaque identity shared by IDE context attachment and post-run verification.
 * Callers keep it host-local; it is correlation metadata, not bridge context.
 */
export function diagnosticVerificationFingerprint(diagnostic: DiagnosticFingerprintInput): string {
  return createHash("sha256").update(JSON.stringify([
    String(diagnostic.uri),
    nonNegativeInteger(diagnostic.range.start.line),
    nonNegativeInteger(diagnostic.range.start.character),
    nonNegativeInteger(diagnostic.range.end.line),
    nonNegativeInteger(diagnostic.range.end.character),
    String(diagnostic.severity),
    String(diagnostic.message),
    diagnostic.source === undefined ? "" : String(diagnostic.source),
    diagnostic.code === undefined ? "" : String(diagnostic.code)
  ])).digest("hex");
}

function nonNegativeInteger(value: number): number {
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}
