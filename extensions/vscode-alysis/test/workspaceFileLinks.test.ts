import assert from "node:assert/strict";
import { mkdir, mkdtemp, realpath, rm, stat, symlink, writeFile } from "node:fs/promises";
import Module from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { type TestContext } from "node:test";

const uri = (fsPath: string) => ({ fsPath, path: fsPath.replaceAll("\\", "/"), scheme: "file" });
type Folder = { uri: ReturnType<typeof uri> };
class RelativePattern {
  constructor(public base: Folder, public pattern: string) {}
}
const searchableFiles: string[] = [];
const vscodeStub = {
  Uri: { joinPath: (base: ReturnType<typeof uri>, ...parts: string[]) => uri(path.join(base.fsPath, ...parts)) },
  FileType: { File: 1 },
  RelativePattern,
  Range: class {
    public start: { line: number; character: number };
    constructor(line: number, character: number, _endLine: number, _endCharacter: number) {
      this.start = { line, character };
    }
  },
  window: { activeTextEditor: undefined as { document: { uri: ReturnType<typeof uri> } } | undefined },
  workspace: {
    workspaceFolders: [] as Folder[],
    getWorkspaceFolder: (document: ReturnType<typeof uri>) => vscodeStub.workspace.workspaceFolders.find(
      (folder) => document.fsPath.startsWith(folder.uri.fsPath + path.sep)
    ),
    fs: { stat: async (candidate: ReturnType<typeof uri>) => ({ type: (await stat(candidate.fsPath)).isFile() ? 1 : 2 }) },
    findFiles: async (pattern: string | RelativePattern, _exclude: string, max: number) => searchableFiles.filter((file) => {
      const name = typeof pattern === "string" ? pattern.split("/").pop() : pattern.pattern.split("/").pop();
      return path.basename(file) === name && (typeof pattern === "string" || file.startsWith(pattern.base.uri.fsPath + path.sep));
    }).slice(0, max).map(uri)
  }
};
const loader = Module as unknown as { _load(request: string, parent: NodeModule | null, isMain: boolean): unknown };
const originalLoad = loader._load;
loader._load = function (request, parent, isMain) {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const { StartViewProvider } = require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
loader._load = originalLoad;

async function fixture(t: TestContext) {
  const temp = await mkdtemp(path.join(tmpdir(), "alysis-file-links-"));
  t.after(() => rm(temp, { recursive: true, force: true }));
  const first = path.join(temp, "first");
  const second = path.join(temp, "second");
  const outside = path.join(temp, "outside");
  await Promise.all([first, second, outside].map((folder) => mkdir(folder)));
  vscodeStub.workspace.workspaceFolders = [{ uri: uri(first) }, { uri: uri(second) }];
  vscodeStub.window.activeTextEditor = { document: { uri: uri(path.join(second, "active.ts")) } };
  searchableFiles.length = 0;
  const provider = Object.create(StartViewProvider.prototype) as any;
  const resolve = (reference: string) => provider.resolveWorkspaceFile(reference) as Promise<any>;
  const file = async (folder: string, name: string) => {
    const target = path.join(folder, name);
    await mkdir(path.dirname(target), { recursive: true });
    await writeFile(target, "fixture");
    searchableFiles.push(target);
    return target;
  };
  return { first, second, outside, resolve, file };
}

test("file links use the active root when multiple folders have the same relative path", async (t) => {
  const { first, second, file, resolve } = await fixture(t);
  await file(first, "src/app.ts");
  const expected = await file(second, "src/app.ts");
  assert.equal((await resolve("src/app.ts:12:4"))?.uri.fsPath, expected);
});

test("file links fail closed when the multi-root scope is unresolved", async (t) => {
  const { first, file, resolve } = await fixture(t);
  await file(first, "app.ts");
  vscodeStub.window.activeTextEditor = undefined;
  assert.equal(await resolve("app.ts"), undefined);
});

test("file links cannot traverse outside the workspace", async (t) => {
  const { outside, file, resolve } = await fixture(t);
  await file(outside, "secret.ts");
  assert.equal(await resolve("../outside/secret.ts"), undefined);
});

test("file links cannot follow directory links outside the workspace", async (t) => {
  const { second, outside, file, resolve } = await fixture(t);
  await file(outside, "secret.ts");
  await symlink(outside, path.join(second, "linked"), process.platform === "win32" ? "junction" : "dir");
  assert.equal(await resolve("linked/secret.ts"), undefined);
});

test("absolute file links preserve their workspace and line and column", async (t) => {
  const { second, file, resolve } = await fixture(t);
  const expected = await file(second, "src/file with spaces.ts");
  const result = await resolve(`"${expected}:12:4"`);
  assert.equal(result?.uri.fsPath, expected);
  assert.deepEqual(result?.selection.start, { line: 11, character: 3 });
});

test("ambiguous file basenames never select an arbitrary search result", async (t) => {
  const { second, file, resolve } = await fixture(t);
  await file(second, "one/app.ts");
  await file(second, "two/app.ts");
  assert.equal(await resolve("app.ts"), undefined);
});

test("an explicit missing path never falls back to a different same-named file", async (t) => {
  const { second, file, resolve } = await fixture(t);
  await file(second, "other/app.ts");
  assert.equal(await resolve("missing/app.ts"), undefined);
});

test("a unique basename in the active workspace can still be opened", async (t) => {
  const { first, second, file, resolve } = await fixture(t);
  await file(first, "other/app.ts");
  const expected = await file(second, "src/app.ts");
  assert.equal((await resolve("app.ts"))?.uri.fsPath, expected);
});

test("an internal directory link remains usable within the workspace", async (t) => {
  const { second, file, resolve } = await fixture(t);
  const expected = await file(second, "src/app.ts");
  await symlink(path.dirname(expected), path.join(second, "linked"), process.platform === "win32" ? "junction" : "dir");
  const result = await resolve("linked/app.ts");
  assert.equal(await realpath(result.uri.fsPath), await realpath(expected));
});
