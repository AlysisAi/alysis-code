import assert from "node:assert/strict";
import { mkdtemp, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import test, { TestContext } from "node:test";

import { IdeContextCollector, isSensitivePath } from "../src/context/IdeContextCollector";
import {
  ContextDiagnostic,
  ContextDocument,
  ContextEditor,
  ContextHover,
  ContextLocation,
  ContextPosition,
  ContextRange,
  ContextSymbol,
  ContextUri,
  ContextWorkspaceFolder,
  ExplicitTerminalSelection,
  GitContextRepository,
  IdeContextSource
} from "../src/context/IdeContextTypes";
import { redactContextSecrets } from "../src/context/secretRedaction";

const FIXED_NOW = new Date("2026-07-29T10:00:00.000Z");

test("sensitive path classification is separator-independent and credential directories win", () => {
  const sensitivePaths = [
    "C:\\repo\\.ssh\\.env.example",
    "C:/Repo/.AWS/.ENV.SAMPLE",
    "/workspace/.kube/.env.template",
    "/workspace/.AZURE/.env.defaults",
    "C:\\workspace\\.Docker\\.env.dist",
    "/workspace/.GnUpG/.ENV.EXAMPLE",
    "/workspace/.git/.env.sample",
    "C:\\workspace\\.ALYSIS\\.env.template",
    "/workspace/.env.example/child.txt",
    "C:\\workspace\\.ENV.SAMPLE\\child.txt",
    "/workspace/.env.template/"
  ];
  for (const candidate of sensitivePaths) {
    assert.equal(isSensitivePath(candidate), true, candidate);
  }

  const safeTemplatePaths = [
    ".env.example",
    "/workspace/config/.ENV.EXAMPLE",
    "C:\\workspace\\config\\.env.defaults",
    "C:/workspace/config/.Env.Dist",
    "/workspace/config/.env.sample",
    "C:\\workspace/config\\.ENV.TEMPLATE"
  ];
  for (const candidate of safeTemplatePaths) {
    assert.equal(isSensitivePath(candidate), false, candidate);
  }

  for (const candidate of [
    "/workspace/.env",
    "C:\\workspace\\.ENV.LOCAL",
    "/workspace/.env.example.backup",
    "C:/workspace/keys/DEPLOY.PEM"
  ]) {
    assert.equal(isSensitivePath(candidate), true, candidate);
  }
});

test("safe env templates are collectable only outside credential directories", async (t) => {
  const root = await workspace(t);
  const safePath = await file(root, "config/.env.example", "SAFE_PLACEHOLDER=true");
  const credentialPath = await file(root, ".ssh/.env.example", "PRIVATE_MATERIAL=never-read");
  const source = new FakeSource(root);
  source.documents.set(
    normalize(safePath),
    document(fileUri(safePath), "SAFE_PLACEHOLDER=true")
  );
  source.documents.set(
    normalize(credentialPath),
    document(fileUri(credentialPath), "PRIVATE_MATERIAL=never-read")
  );

  const result = await collector(source).collectFiles([
    fileUri(safePath),
    fileUri(credentialPath)
  ]);

  assert.equal(result.blocks.length, 1);
  assert.equal(result.blocks[0].uri, fileUri(safePath).toString());
  assert.doesNotMatch(JSON.stringify(result.blocks), /never-read/);
  assert.deepEqual(result.skipped, [{ source: "file", reason: "sensitive-path" }]);
});

test("untrusted workspaces fail closed before any source content is inspected", async () => {
  const source = new FakeSource("C:\\untrusted");
  source.isWorkspaceTrusted = false;
  source.activeEditor = editor(document(fileUri("C:\\untrusted\\main.ts"), "secret"), range(0, 0, 0, 6));

  const result = await collector(source).collect({ includeTerminalSelection: true, includeGitDiff: "both" });

  assert.deepEqual(result.blocks, []);
  assert.deepEqual(result.skipped, [{ source: "workspace", reason: "untrusted-workspace" }]);
  assert.equal(source.terminalSelectionCalls, 0);
  assert.equal(source.gitRepositoryCalls, 0);
});

test("selection, open editors, and diagnostics are typed, contained, ignored-aware, and secret-free", async (t) => {
  const root = await workspace(t);
  const outsideRoot = await workspace(t);
  const mainPath = await file(root, "src/main.ts", 'const token = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456";\nrun();');
  const ignoredPath = await file(root, "ignored.log", "ignored");
  const envPath = await file(root, ".env", "API_KEY=do-not-send");
  const outsidePath = await file(outsideRoot, "outside.ts", "outside");
  const main = document(fileUri(mainPath), 'const token = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456";\nrun();', "typescript", 7, true);
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(0, 0, 0, main.getText().length));
  source.openEditors = [source.activeEditor];
  source.openDocuments = [
    main,
    document(fileUri(ignoredPath), "ignored"),
    document(fileUri(envPath), "API_KEY=do-not-send"),
    document(fileUri(outsidePath), "outside")
  ];
  source.ignored.add(normalize(ignoredPath));
  source.diagnostics = [
    {
      uri: fileUri(mainPath),
      range: range(1, 0, 1, 3),
      severity: "error",
      message: "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
      source: "typescript"
    },
    {
      uri: fileUri(envPath),
      range: range(0, 0, 0, 3),
      severity: "warning",
      message: "must not appear"
    }
  ];

  const result = await collector(source).collect();
  const encoded = JSON.stringify(result.blocks);

  assert.deepEqual(result.blocks.map((block) => block.type), ["selection", "open_editors", "diagnostics"]);
  assert.doesNotMatch(encoded, /sk-proj-|abcdefghijklmnopqrstuvwxyz123456|do-not-send|must not appear/);
  assert.match(encoded, /<redacted>/);
  const selection = result.blocks[0];
  assert.equal(selection.document_version, 7);
  assert.equal((selection.content_hash as string).length, 64);
  assert.deepEqual(selection.range, range(0, 0, 0, main.getText().length));
  const openItems = result.blocks[1].items as Array<Record<string, unknown>>;
  assert.equal(openItems.length, 1);
  assert.equal(openItems[0].active, true);
  assert.equal(openItems[0].dirty, true);
  const diagnosticItems = result.blocks[2].items as Array<Record<string, unknown>>;
  assert.equal(diagnosticItems.length, 1);
  assert.match(String(diagnosticItems[0].verification_id), /^[0-9a-f]{64}$/);
  assert.ok(result.skipped.some((item) => item.reason === "ignored"));
  assert.ok(result.skipped.some((item) => item.reason === "sensitive-path"));
  assert.ok(result.skipped.some((item) => item.reason === "outside-workspace"));
});

test("realpath containment rejects a lexical in-workspace symlink escape", async (t) => {
  const root = await workspace(t);
  const outsideRoot = await workspace(t);
  const lexicalPath = path.join(root, "linked.ts");
  const outsidePath = await file(outsideRoot, "secret.ts", "outside secret");
  const source = new FakeSource(root);
  source.realpathOverrides.set(normalize(lexicalPath), outsidePath);
  const linked = document(fileUri(lexicalPath), "outside secret");
  source.activeEditor = editor(linked, range(0, 0, 0, 14));
  source.openEditors = [source.activeEditor];
  source.openDocuments = [linked];

  const result = await collector(source).collect();

  assert.deepEqual(result.blocks, []);
  assert.ok(result.skipped.every((item) => item.reason === "symlink-escape"));
});

test("terminal context is never read implicitly and accepts only explicit bounded selection", async (t) => {
  const root = await workspace(t);
  const source = new FakeSource(root);
  source.terminalSelection = {
    text: "selected output\n--token super-secret-terminal-token\n" + "x".repeat(10_000),
    terminalName: "PowerShell",
    cwd: root,
    command: "deploy --token super-secret-command-token",
    exitCode: 1
  };

  const withoutOptIn = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false
  });
  assert.equal(source.terminalSelectionCalls, 0);
  assert.deepEqual(withoutOptIn.blocks, []);

  const withOptIn = await collector(source, { maxContentBytes: 512 }).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeTerminalSelection: true
  });
  assert.equal(source.terminalSelectionCalls, 1);
  assert.equal(withOptIn.blocks.length, 1);
  assert.equal(withOptIn.blocks[0].type, "terminal");
  assert.equal(withOptIn.blocks[0].cwd, await realpath(root));
  assert.ok(Buffer.byteLength(withOptIn.blocks[0].content as string) <= 512);
  assert.doesNotMatch(JSON.stringify(withOptIn.blocks), /super-secret/);
});

test("explicit file picker context reads only contained non-sensitive files", async (t) => {
  const root = await workspace(t);
  const outsideRoot = await workspace(t);
  const safePath = await file(root, "src/selected.ts", "const token = 'sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456';");
  const ppkPath = await file(root, "keys/deploy.ppk", "PuTTY-User-Key-File-2: ssh-rsa\nprivate material");
  const dockerPath = await file(root, ".docker/config.json", "secret");
  const outsidePath = await file(outsideRoot, "outside.ts", "outside");
  const source = new FakeSource(root);
  source.documents.set(normalize(safePath), document(fileUri(safePath), "const token = 'sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456';", "typescript", 4));
  source.documents.set(normalize(ppkPath), document(fileUri(ppkPath), "private material"));
  source.documents.set(normalize(dockerPath), document(fileUri(dockerPath), "secret"));
  source.documents.set(normalize(outsidePath), document(fileUri(outsidePath), "outside"));

  const result = await collector(source).collectFiles([
    fileUri(safePath),
    fileUri(ppkPath),
    fileUri(dockerPath),
    fileUri(outsidePath)
  ]);

  assert.deepEqual(result.blocks.map((block) => block.type), ["file"]);
  assert.equal(result.blocks[0].document_version, 4);
  assert.match(String(result.blocks[0].content), /<redacted>/);
  assert.doesNotMatch(JSON.stringify(result.blocks), /sk-proj-|private material|outside/);
  assert.equal(result.skipped.filter((item) => item.reason === "sensitive-path").length, 2);
  assert.equal(result.skipped.filter((item) => item.reason === "outside-workspace").length, 1);
});

test("multi-root collection is confined to the exact workspace root owning the job", async (t) => {
  const selectedRoot = await workspace(t);
  const siblingRoot = await workspace(t);
  const selectedPath = await file(selectedRoot, "src/selected.ts", "export const selected = true;");
  const siblingPath = await file(siblingRoot, "src/sibling.ts", "export const siblingSecret = 'do-not-cross-roots';");
  const selectedDocument = document(fileUri(selectedPath), "export const selected = true;", "typescript");
  const siblingDocument = document(
    fileUri(siblingPath),
    "export const siblingSecret = 'do-not-cross-roots';",
    "typescript"
  );
  const source = new FakeSource(selectedRoot);
  source.workspaceFolders.push({ name: "sibling", index: 1, uri: fileUri(siblingRoot) });
  source.activeEditor = editor(selectedDocument, range(0, 0, 0, 0));
  source.openDocuments = [selectedDocument, siblingDocument];
  source.documents.set(normalize(selectedPath), selectedDocument);
  source.documents.set(normalize(siblingPath), siblingDocument);
  source.definitions = [{ uri: fileUri(siblingPath), range: range(0, 0, 0, 12) }];

  const automatic = await collector(source).collect({
    workspaceRoot: selectedRoot,
    includeSelection: false,
    includeOpenEditors: true,
    includeDiagnostics: false,
    includeDefinitions: true
  });
  const explicit = await collector(source).collectFiles(
    [fileUri(selectedPath), fileUri(siblingPath)],
    selectedRoot
  );

  assert.doesNotMatch(JSON.stringify([...automatic.blocks, ...explicit.blocks]), /siblingSecret|do-not-cross-roots/);
  assert.ok(automatic.skipped.some((item) => item.reason === "outside-workspace"));
  assert.ok(explicit.skipped.some((item) => item.reason === "outside-workspace"));
  assert.match(JSON.stringify(explicit.blocks), /selected = true/);

  source.activeEditor = undefined;
  const ambiguous = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: true,
    includeDiagnostics: false
  });
  assert.deepEqual(ambiguous.blocks, []);
  assert.equal(ambiguous.skipped[0]?.reason, "ambiguous-workspace");
});

test("explicit terminal excerpt does not invoke a terminal provider and is redacted", async (t) => {
  const root = await workspace(t);
  const source = new FakeSource(root);
  const result = await collector(source).collectTerminalExcerpt({
    text: "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456\ncommand failed",
    terminalName: "PowerShell",
    cwd: root,
    exitCode: 1
  });

  assert.equal(source.terminalSelectionCalls, 0);
  assert.equal(result.blocks.length, 1);
  assert.equal(result.blocks[0].type, "terminal");
  assert.match(String(result.blocks[0].content), /<redacted>/);
  assert.doesNotMatch(String(result.blocks[0].content), /abcdefghijklmnopqrstuvwxyz123456/);
});

test("Git context uses the Git API, filters unsafe sections, and redacts safe-file secrets", async (t) => {
  const root = await workspace(t);
  await file(root, "src/safe.ts", "old");
  await file(root, ".env", "TOKEN=nope");
  const source = new FakeSource(root);
  const working = [
    "diff --git a/src/safe.ts b/src/safe.ts",
    "--- a/src/safe.ts",
    "+++ b/src/safe.ts",
    "@@ -1 +1 @@",
    "-old",
    "+const apiKey = 'sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456';",
    "diff --git a/.env b/.env",
    "--- a/.env",
    "+++ b/.env",
    "@@ -1 +1 @@",
    "-TOKEN=old",
    "+TOKEN=raw-secret"
  ].join("\n");
  const staged = [
    "diff --git a/src/safe.ts b/src/safe.ts",
    "--- a/src/safe.ts",
    "+++ b/src/safe.ts",
    "@@ -1 +1 @@",
    "-old",
    "+staged"
  ].join("\n");
  const calls: boolean[] = [];
  source.repositories = [{
    rootUri: fileUri(root),
    async diff(isStaged: boolean): Promise<string> {
      calls.push(isStaged);
      return isStaged ? staged : working;
    }
  }];

  const result = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeGitDiff: "both"
  });
  const encoded = JSON.stringify(result.blocks);

  assert.deepEqual(calls, [false, true]);
  assert.equal(result.blocks.length, 2);
  assert.ok(result.blocks.every((block) => block.type === "git_diff"));
  assert.equal(result.blocks[0].truncated, true);
  assert.doesNotMatch(encoded, /\.env|raw-secret|sk-proj-/);
  assert.match(encoded, /<redacted>/);
  assert.match(encoded, /\+staged/);
});

test("malformed Git output fails closed instead of bypassing path policy", async (t) => {
  const root = await workspace(t);
  const source = new FakeSource(root);
  source.repositories = [{ rootUri: fileUri(root), async diff() { return "not a unified diff\nsecret"; } }];

  const result = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeGitDiff: "working"
  });

  assert.deepEqual(result.blocks, []);
  assert.ok(result.skipped.some((item) => item.reason === "unsafe-diff"));
});

test("symbols and references are opt-in bounded file_range blocks", async (t) => {
  const root = await workspace(t);
  const mainPath = await file(root, "main.ts", "function example() { return 1; }\nexample();\n");
  const ignoredPath = await file(root, "generated.ts", "example();");
  const main = document(fileUri(mainPath), "function example() { return 1; }\nexample();\n", "typescript", 2);
  const generated = document(fileUri(ignoredPath), "example();", "typescript", 1);
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(0, 9, 0, 9));
  source.openDocuments = [main, generated];
  source.documents.set(normalize(mainPath), main);
  source.documents.set(normalize(ignoredPath), generated);
  source.symbols = [{ name: "example", uri: fileUri(mainPath), range: range(0, 0, 0, 32) }];
  source.references = [
    { uri: fileUri(mainPath), range: range(1, 0, 1, 7) },
    { uri: fileUri(ignoredPath), range: range(0, 0, 0, 7) }
  ];
  source.ignored.add(normalize(ignoredPath));

  const result = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeSymbols: true,
    includeReferences: true
  });

  assert.deepEqual(result.blocks.map((block) => block.type), ["file_range", "file_range"]);
  assert.equal(result.blocks[0].label, "Symbol: example");
  assert.equal(result.blocks[1].label, "Reference");
  assert.ok(result.skipped.some((item) => item.source === "vscode.references" && item.reason === "ignored"));
});

test("language-service context is explicit, bounded, redacted, and backend-compatible", async (t) => {
  const root = await workspace(t);
  const mainPath = await file(
    root,
    "main.ts",
    "function target() { return 1; }\ntype Alias = number;\ntarget();\n"
  );
  const main = document(
    fileUri(mainPath),
    "function target() { return 1; }\ntype Alias = number;\ntarget();\n",
    "typescript",
    3
  );
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(2, 1, 2, 1));
  source.documents.set(normalize(mainPath), main);
  source.symbols = [{ name: "target", uri: main.uri, range: range(0, 0, 0, 31) }];
  source.workspaceSymbols = [{ name: "Alias", detail: "main.ts", uri: main.uri, range: range(1, 0, 1, 20) }];
  source.definitions = [{ uri: main.uri, range: range(0, 9, 0, 15) }];
  source.typeDefinitions = [{ uri: main.uri, range: range(1, 5, 1, 10) }];
  source.implementations = [{ uri: main.uri, range: range(0, 0, 0, 31) }];
  source.hovers = [{
    uri: main.uri,
    range: range(2, 0, 2, 6),
    contents: "target(): number Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
  }];
  source.references = [{ uri: main.uri, range: range(2, 0, 2, 6) }];
  source.incomingCalls = [{ name: "caller", uri: main.uri, range: range(2, 0, 2, 6) }];
  source.outgoingCalls = [{ name: "callee", detail: "main.ts", uri: main.uri, range: range(0, 9, 0, 15) }];

  const cheap = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false
  });
  assert.deepEqual(cheap.blocks, []);
  assert.deepEqual([...source.languageProviderCalls], []);

  const result = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeDocumentSymbols: true,
    includeWorkspaceSymbols: true,
    workspaceSymbolQuery: "Ali sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
    includeDefinitions: true,
    includeTypeDefinitions: true,
    includeImplementations: true,
    includeHover: true,
    includeReferences: true,
    includeIncomingCalls: true,
    includeOutgoingCalls: true,
    languageServicePosition: { line: 2, character: 2 }
  });

  assert.equal(result.blocks.length, 9);
  assert.ok(result.blocks.every((block) => block.type === "file_range"));
  assert.deepEqual(result.blocks.map((block) => block.label), [
    "Symbol: target",
    "Workspace symbol: Alias — main.ts",
    "Definition",
    "Type definition",
    "Implementation",
    "Hover: target(): number Authorization: <redacted>",
    "Reference",
    "Incoming call: caller",
    "Outgoing call: callee — main.ts"
  ]);
  assert.deepEqual(
    result.blocks.map((block) => (block.provenance as Record<string, unknown>).source),
    [
      "vscode.symbols",
      "vscode.workspace-symbols",
      "vscode.definitions",
      "vscode.type-definitions",
      "vscode.implementations",
      "vscode.hover",
      "vscode.references",
      "vscode.call-hierarchy.incoming",
      "vscode.call-hierarchy.outgoing"
    ]
  );
  assert.doesNotMatch(JSON.stringify(result.blocks), /abcdefghijklmnopqrstuvwxyz123456|sk-proj-/);
  assert.deepEqual([...source.languageProviderCalls.keys()], [
    "documentSymbols",
    "workspaceSymbols",
    "definitions",
    "typeDefinitions",
    "implementations",
    "hovers",
    "references",
    "incomingCalls",
    "outgoingCalls"
  ]);
});

test("malformed language providers fail closed before emitting partial context", async (t) => {
  const root = await workspace(t);
  const mainPath = await file(root, "main.ts", "const value = 1;\n");
  const main = document(fileUri(mainPath), "const value = 1;\n", "typescript");
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(0, 6, 0, 6));
  source.documents.set(normalize(mainPath), main);
  source.definitions = [
    { uri: main.uri, range: range(0, 6, 0, 11) },
    { uri: main.uri, range: { start: { line: -1, character: 0 }, end: { line: 0, character: 1 } } }
  ];

  const result = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeDefinitions: true
  });

  assert.deepEqual(result.blocks, []);
  assert.ok(result.skipped.some((item) => item.source === "definitions" && item.reason === "source-error"));
});

test("language providers honor cancellation and timeouts without leaking late results", async (t) => {
  const root = await workspace(t);
  const mainPath = await file(root, "main.ts", "const value = 1;\n");
  const main = document(fileUri(mainPath), "const value = 1;\n", "typescript");
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(0, 6, 0, 6));
  source.documents.set(normalize(mainPath), main);
  source.getDefinitions = async () => new Promise<readonly ContextLocation[]>((resolve) => {
    setTimeout(() => resolve([{ uri: main.uri, range: range(0, 6, 0, 11) }]), 50);
  });

  const timeoutResult = await new IdeContextCollector(source, {
    now: () => FIXED_NOW,
    providerTimeoutMs: 5
  }).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeDefinitions: true
  });
  assert.deepEqual(timeoutResult.blocks, []);
  assert.ok(timeoutResult.skipped.some((item) => item.reason === "provider-timeout"));

  const abort = new AbortController();
  abort.abort();
  const cancelled = await collector(source).collect({
    includeSelection: false,
    includeOpenEditors: false,
    includeDiagnostics: false,
    includeDefinitions: true,
    signal: abort.signal
  });
  assert.deepEqual(cancelled.blocks, []);
  assert.ok(cancelled.skipped.some((item) => item.reason === "cancelled"));
});

test("automatic context deadline preserves safe selection and ignores late Git results", async (t) => {
  const root = await workspace(t);
  const mainPath = await file(root, "main.ts", "const value = 1;\n");
  const main = document(fileUri(mainPath), "const value = 1;\n", "typescript");
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(0, 0, 0, 15));
  let releaseDiff!: (value: string) => void;
  const diff = new Promise<string>((resolve) => { releaseDiff = resolve; });
  let diffStarted = false;
  let symbolCalls = 0;
  source.getGitRepositories = async () => [{
    rootUri: fileUri(root),
    diff: async () => { diffStarted = true; return diff; }
  }];
  source.getDocumentSymbols = async () => { symbolCalls += 1; return []; };
  const result = await collector(source).collect({
    includeOpenEditors: false, includeDiagnostics: false,
    includeGitDiff: "working", includeSymbols: true, timeoutMs: 100
  });
  assert.equal(diffStarted, true);
  assert.equal(result.blocks.length, 1);
  assert.equal(result.blocks[0].type, "selection");
  assert.equal(result.truncated, true);
  assert.ok(result.skipped.some((item) => item.source === "automatic-context" && item.reason === "provider-timeout"));
  const snapshot = JSON.stringify(result);
  releaseDiff("diff --git a/main.ts b/main.ts\n--- a/main.ts\n+++ b/main.ts\n@@ -1 +1 @@\n-before\n+late\n");
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(JSON.stringify(result), snapshot, "late provider output cannot mutate sent context");
  assert.equal(symbolCalls, 0, "expiry stops subsequent context sources");
});

test("automatic context deadline also bounds a stalled workspace boundary", async (t) => {
  const root = await workspace(t);
  const source = new FakeSource(root);
  let releasePath!: (value: string) => void;
  source.realpath = async () => new Promise<string>((resolve) => { releasePath = resolve; });
  let editorReads = 0;
  source.getOpenDocuments = () => { editorReads += 1; return []; };
  const result = await collector(source).collect({ timeoutMs: 10 });
  assert.deepEqual(result.blocks, []);
  assert.equal(result.truncated, true);
  releasePath(root);
  await new Promise<void>((resolve) => setImmediate(resolve));
  assert.equal(editorReads, 0);
});

test("per-block and total byte budgets truncate deterministically", async (t) => {
  const root = await workspace(t);
  const mainPath = await file(root, "large.ts", "x".repeat(20_000));
  const main = document(fileUri(mainPath), "x".repeat(20_000));
  const source = new FakeSource(root);
  source.activeEditor = editor(main, range(0, 0, 0, 20_000));

  const result = await collector(source, {
    maxBlockBytes: 700,
    maxTotalBytes: 700,
    maxContentBytes: 20_000
  }).collect({ includeOpenEditors: false, includeDiagnostics: false });

  assert.equal(result.blocks.length, 1);
  assert.ok(result.totalBytes <= 700);
  assert.equal(result.blocks[0].truncated, true);
  assert.equal(result.truncated, true);
});

test("secret redaction covers provider tokens, private keys, URLs, structured fields, and entropy fallback", () => {
  const raw = [
    "OPENAI_API_KEY=sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
    "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
    "https://user:password@example.com/path",
    '"client_secret": "raw-json-secret"',
    "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----",
    "abcDEF0123456789_abcDEF0123456789_xyz"
  ].join("\n");
  const sanitized = redactContextSecrets(raw);
  assert.doesNotMatch(sanitized, /sk-proj-|abcdefghijklmnopqrstuvwxyz|password@example|raw-json|private-material|abcDEF/);
  assert.ok((sanitized.match(/<redacted>/g) ?? []).length >= 6);
});

test("secret redaction covers Authorization headers whose scheme is not bearer or basic", () => {
  // Regression: AUTHORIZATION_VALUE only matched `bearer`/`basic`, and `authorization` was
  // absent from the keyword lists, so an opaque header value passed straight through to the
  // model. Lowercase hex has two character classes, so the entropy fallback never caught it.
  const cases = [
    "Authorization: 9f8c1b2d3e4a5f60718293a4b5c6d7e8",
    "Proxy-Authorization: 9f8c1b2d3e4a5f60718293a4b5c6d7e8",
    "authorization=9f8c1b2d3e4a5f60718293a4b5c6d7e8",
    '{"authorization": "9f8c1b2d3e4a5f60718293a4b5c6d7e8"}',
    "Authorization: Token 9f8c1b2d3e4a5f60718293a4b5c6d7e8"
  ];
  for (const raw of cases) {
    const sanitized = redactContextSecrets(raw);
    assert.doesNotMatch(sanitized, /9f8c1b2d3e4a5f60718293a4b5c6d7e8/, raw);
    assert.match(sanitized, /<redacted>/, raw);
  }
  // The header name itself and unrelated prose survive.
  assert.equal(redactContextSecrets("Authorization: 9f8c1b2d3e4a5f60718293a4b5c6d7e8"), "Authorization: <redacted>");
  assert.equal(redactContextSecrets("the authorization flow is documented"), "the authorization flow is documented");
});

function collector(source: IdeContextSource, limits: Record<string, number> = {}): IdeContextCollector {
  return new IdeContextCollector(source, { limits, now: () => FIXED_NOW });
}

class FakeSource implements IdeContextSource {
  public isWorkspaceTrusted = true;
  public activeEditor: ContextEditor | undefined;
  public openEditors: ContextEditor[] = [];
  public openDocuments: ContextDocument[] = [];
  public diagnostics: ContextDiagnostic[] = [];
  public repositories: GitContextRepository[] = [];
  public symbols: ContextSymbol[] = [];
  public workspaceSymbols: ContextSymbol[] = [];
  public definitions: ContextLocation[] = [];
  public typeDefinitions: ContextLocation[] = [];
  public implementations: ContextLocation[] = [];
  public hovers: ContextHover[] = [];
  public references: ContextLocation[] = [];
  public incomingCalls: ContextSymbol[] = [];
  public outgoingCalls: ContextSymbol[] = [];
  public readonly languageProviderCalls = new Map<string, number>();
  public terminalSelection: ExplicitTerminalSelection | undefined;
  public terminalSelectionCalls = 0;
  public gitRepositoryCalls = 0;
  public readonly ignored = new Set<string>();
  public readonly documents = new Map<string, ContextDocument>();
  public readonly realpathOverrides = new Map<string, string>();
  public readonly workspaceFolders: ContextWorkspaceFolder[];

  public constructor(root: string) {
    this.workspaceFolders = [{ name: "workspace", index: 0, uri: fileUri(root) }];
  }

  public getWorkspaceFolders(): readonly ContextWorkspaceFolder[] { return this.workspaceFolders; }
  public getActiveEditor(): ContextEditor | undefined { return this.activeEditor; }
  public getOpenEditors(): readonly ContextEditor[] { return this.openEditors; }
  public getOpenDocuments(): readonly ContextDocument[] { return this.openDocuments; }
  public getDiagnostics(): readonly ContextDiagnostic[] { return this.diagnostics; }
  public async getGitRepositories(): Promise<readonly GitContextRepository[]> {
    this.gitRepositoryCalls += 1;
    return this.repositories;
  }
  public async getDocumentSymbols(): Promise<readonly ContextSymbol[]> {
    this.countProvider("documentSymbols");
    return this.symbols;
  }
  public async getWorkspaceSymbols(): Promise<readonly ContextSymbol[]> {
    this.countProvider("workspaceSymbols");
    return this.workspaceSymbols;
  }
  public async getDefinitions(): Promise<readonly ContextLocation[]> {
    this.countProvider("definitions");
    return this.definitions;
  }
  public async getTypeDefinitions(): Promise<readonly ContextLocation[]> {
    this.countProvider("typeDefinitions");
    return this.typeDefinitions;
  }
  public async getImplementations(): Promise<readonly ContextLocation[]> {
    this.countProvider("implementations");
    return this.implementations;
  }
  public async getHovers(): Promise<readonly ContextHover[]> {
    this.countProvider("hovers");
    return this.hovers;
  }
  public async getReferences(_document: ContextDocument, _position: ContextPosition): Promise<readonly ContextLocation[]> {
    this.countProvider("references");
    return this.references;
  }
  public async getIncomingCalls(): Promise<readonly ContextSymbol[]> {
    this.countProvider("incomingCalls");
    return this.incomingCalls;
  }
  public async getOutgoingCalls(): Promise<readonly ContextSymbol[]> {
    this.countProvider("outgoingCalls");
    return this.outgoingCalls;
  }
  public async openDocument(uri: ContextUri): Promise<ContextDocument | undefined> {
    return this.documents.get(normalize(uri.fsPath))
      ?? this.openDocuments.find((item) => normalize(item.uri.fsPath) === normalize(uri.fsPath))
      ?? (this.activeEditor && normalize(this.activeEditor.document.uri.fsPath) === normalize(uri.fsPath)
        ? this.activeEditor.document
        : undefined);
  }
  public async isIgnored(uri: ContextUri): Promise<boolean> { return this.ignored.has(normalize(uri.fsPath)); }
  public async realpath(fsPath: string): Promise<string> {
    return this.realpathOverrides.get(normalize(fsPath)) ?? realpath(fsPath);
  }
  public async getExplicitTerminalSelection(): Promise<ExplicitTerminalSelection | undefined> {
    this.terminalSelectionCalls += 1;
    return this.terminalSelection;
  }
  private countProvider(name: string): void {
    this.languageProviderCalls.set(name, (this.languageProviderCalls.get(name) ?? 0) + 1);
  }
}

function document(
  uri: ContextUri,
  content: string,
  languageId = "plaintext",
  version = 1,
  isDirty = false
): ContextDocument {
  const lines = content.split("\n");
  return {
    uri,
    languageId,
    version,
    isDirty,
    lineCount: lines.length,
    getText(requested?: ContextRange): string {
      if (!requested) {
        return content;
      }
      return content.slice(offset(lines, requested.start), offset(lines, requested.end));
    },
    getLineRange(line: number): ContextRange {
      const bounded = Math.max(0, Math.min(line, lines.length - 1));
      return range(bounded, 0, bounded, lines[bounded].length);
    }
  };
}

function editor(doc: ContextDocument, selection: ContextRange): ContextEditor {
  return { document: doc, selection, visibleRanges: [selection] };
}

function offset(lines: readonly string[], position: ContextPosition): number {
  let result = 0;
  for (let index = 0; index < Math.min(position.line, lines.length); index += 1) {
    result += lines[index].length + 1;
  }
  return result + position.character;
}

function range(startLine: number, startCharacter: number, endLine: number, endCharacter: number): ContextRange {
  return {
    start: { line: startLine, character: startCharacter },
    end: { line: endLine, character: endCharacter }
  };
}

function fileUri(fsPath: string): ContextUri {
  return { scheme: "file", fsPath, toString: () => pathToFileURL(fsPath).toString() };
}

async function workspace(t: TestContext): Promise<string> {
  // macOS exposes /var through /private/var; mocks must key their data by the
  // same physical paths returned by the production containment checks.
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "alysis-context-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
}

async function file(root: string, relative: string, content: string): Promise<string> {
  const fsPath = path.join(root, ...relative.split("/"));
  const { mkdir } = await import("node:fs/promises");
  await mkdir(path.dirname(fsPath), { recursive: true });
  await writeFile(fsPath, content, "utf8");
  return fsPath;
}

function normalize(value: string): string {
  const resolved = path.resolve(value);
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}
