import assert from "node:assert/strict";
import path from "node:path";
import test from "node:test";

import type * as vscode from "vscode";

import { searchWorkspaceMentions } from "../src/workspace/workspaceMentions";

test("workspace mention search is active-root scoped and hides sensitive or sibling paths", async () => {
  const root = path.resolve("C:\\workspace\\active");
  const folder = {
    name: "active",
    index: 0,
    uri: uri(root)
  } as vscode.WorkspaceFolder;
  let capturedPattern: { base: unknown; pattern: string } | undefined;
  class RelativePattern {
    public constructor(public readonly base: unknown, public readonly pattern: string) {
      capturedPattern = { base, pattern };
    }
  }
  const api = {
    RelativePattern,
    workspace: {
      findFiles: async () => [
        uri(path.join(root, "src", "main.ts")),
        uri(path.join(root, ".env")),
        uri(path.resolve("C:\\workspace\\sibling\\private.ts"))
      ]
    }
  } as unknown as typeof vscode;

  const results = await searchWorkspaceMentions(api, folder, "../../main*?");

  assert.equal(capturedPattern?.base, folder);
  assert.doesNotMatch(capturedPattern?.pattern ?? "", /\.\./);
  assert.deepEqual(results, [{
    label: "main.ts",
    detail: "src/main.ts",
    insert: "src/main.ts",
    kind: "file"
  }]);
});

test("workspace mention search fails closed when multi-root scope is unresolved", async () => {
  let searched = false;
  const api = {
    workspace: {
      findFiles: async () => {
        searched = true;
        return [];
      }
    }
  } as unknown as typeof vscode;

  assert.deepEqual(await searchWorkspaceMentions(api, undefined, "main"), []);
  assert.equal(searched, false);
});

function uri(fsPath: string): vscode.Uri {
  return {
    scheme: "file",
    fsPath,
    toString: () => `file:///${fsPath.replaceAll("\\", "/")}`
  } as vscode.Uri;
}
