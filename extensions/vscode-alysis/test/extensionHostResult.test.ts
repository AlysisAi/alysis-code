import assert from "node:assert/strict";
import test from "node:test";
import { assertSuccessfulExtensionHostResult } from "./integration/extensionHostResult";

test("an editor's successful exit cannot hide failing Extension Host tests", () => {
  assert.throws(() => assertSuccessfulExtensionHostResult({ tests: 25, passes: 8, pending: 0, failures: 17 }), /17 Extension Host/);
});

test("missing, empty and inconsistent test receipts fail closed", () => {
  for (const result of [null, {}, { tests: 0, passes: 0, pending: 0, failures: 0 },
    { tests: 25, passes: 1, pending: 0, failures: 0 }, { tests: 1, passes: "1", pending: 0, failures: 0 }]) {
    assert.throws(() => assertSuccessfulExtensionHostResult(result));
  }
});

test("a complete successful suite receipt is accepted", () => {
  assert.doesNotThrow(() => assertSuccessfulExtensionHostResult({ tests: 25, passes: 25, pending: 0, failures: 0 }));
});
