import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import Module from "node:module";
import { tmpdir } from "node:os";
import path from "node:path";
import test, { type TestContext } from "node:test";

const uri = (fsPath: string) => ({
  fsPath,
  scheme: "file",
  toString: () => `file://${fsPath}`
});
const warnings: string[] = [];
const attachments: string[] = [];
const vscodeStub = {
  Uri: { joinPath: (base: ReturnType<typeof uri>, ...parts: string[]) => uri(path.join(base.fsPath, ...parts)) },
  window: {
    activeTextEditor: undefined as { document: { uri: ReturnType<typeof uri> } } | undefined,
    showWarningMessage: async (message: string) => { warnings.push(message); }
  },
  workspace: {
    workspaceFolders: [] as { uri: ReturnType<typeof uri> }[],
    getWorkspaceFolder: (document: ReturnType<typeof uri>) => vscodeStub.workspace.workspaceFolders.find(
      (folder) => path.dirname(document.fsPath) === folder.uri.fsPath
    ),
    fs: {
      createDirectory: (directory: ReturnType<typeof uri>) => mkdir(directory.fsPath, { recursive: true }),
      writeFile: (target: ReturnType<typeof uri>, bytes: Uint8Array) => writeFile(target.fsPath, bytes)
    }
  },
  commands: {
    executeCommand: async (command: string, target: string) => {
      assert.equal(command, "alysis.backend.session.images.add");
      attachments.push(target);
    }
  }
};
const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function (request, parent, isMain): unknown {
  return request === "vscode" ? vscodeStub : originalLoad.call(this, request, parent, isMain);
};
const { StartViewProvider } = require("../src/views/StartViewProvider") as typeof import("../src/views/StartViewProvider");
moduleLoader._load = originalLoad;

const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9ioAAAAASUVORK5CYII=", "base64");

async function fixture(t: TestContext) {
  const root = await mkdtemp(path.join(tmpdir(), "alysis-pasted-images-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const first = path.join(root, "first");
  const second = path.join(root, "second");
  const outside = path.join(root, "outside");
  await Promise.all([first, second, outside].map((directory) => mkdir(directory)));
  warnings.length = 0;
  attachments.length = 0;
  vscodeStub.workspace.workspaceFolders = [{ uri: uri(first) }, { uri: uri(second) }];
  vscodeStub.window.activeTextEditor = { document: { uri: uri(path.join(second, "file.ts")) } };
  const provider = Object.create(StartViewProvider.prototype) as any;
  provider.isWorkspaceTrusted = () => true;
  const paste = () => provider.attachPastedImage({
    type: "image.paste", name: "screenshot.png", mime: "image/png", data: png.toString("base64")
  }) as Promise<void>;
  return { first, second, outside, provider, paste };
}

test("pasted images use the focused workspace folder and attach the saved bytes", async (t) => {
  const { first, second, paste } = await fixture(t);
  await paste();
  assert.deepEqual(await readdir(first), [], "the unrelated first workspace must stay untouched");
  assert.equal(attachments.length, 1);
  assert.equal(path.dirname(attachments[0]), path.join(second, ".alysis", "images"));
  assert.deepEqual(await readFile(attachments[0]), png);
});

test("pasting in an unresolved multi-root workspace warns before creating files", async (t) => {
  const { first, second, paste } = await fixture(t);
  vscodeStub.window.activeTextEditor = undefined;
  await paste();
  assert.deepEqual(await readdir(first), []);
  assert.deepEqual(await readdir(second), []);
  assert.deepEqual(attachments, []);
  assert.match(warnings[0], /Focus a file in the workspace folder/);
});

test("pasting still works in a single workspace without an active editor", async (t) => {
  const { second, paste } = await fixture(t);
  vscodeStub.workspace.workspaceFolders = [{ uri: uri(second) }];
  vscodeStub.window.activeTextEditor = undefined;
  await paste();
  assert.equal(attachments.length, 1);
  assert.deepEqual(await readFile(attachments[0]), png);
});

test("pasting in an untrusted workspace never writes or attaches an image", async (t) => {
  const { first, second, provider, paste } = await fixture(t);
  provider.isWorkspaceTrusted = () => false;
  await paste();
  assert.deepEqual(await readdir(first), []);
  assert.deepEqual(await readdir(second), []);
  assert.deepEqual(attachments, []);
  assert.match(warnings[0], /Trust this folder/);
});

for (const component of [".alysis", "images"]) {
  test(`pasting refuses a linked ${component} directory before writing outside the workspace`, async (t) => {
    const { second, outside, paste } = await fixture(t);
    vscodeStub.workspace.workspaceFolders = [{ uri: uri(second) }];
    const parent = component === ".alysis" ? second : path.join(second, ".alysis");
    if (component === "images") await mkdir(parent);
    await symlink(outside, path.join(parent, component), process.platform === "win32" ? "junction" : "dir");
    await assert.rejects(paste, /symbolic link|junction/i);
    assert.deepEqual(await readdir(outside), [], "neither directories nor image bytes may be written through the link");
    assert.deepEqual(attachments, []);
  });
}

test("pasting rejects a non-directory scratch path without replacing it", async (t) => {
  const { second, paste } = await fixture(t);
  vscodeStub.workspace.workspaceFolders = [{ uri: uri(second) }];
  const scratch = path.join(second, ".alysis");
  await writeFile(scratch, "keep me");
  await assert.rejects(paste, /directory/i);
  assert.equal(await readFile(scratch, "utf8"), "keep me");
  assert.deepEqual(attachments, []);
});

test("a workspace opened through a directory link can store images in its own real root", async (t) => {
  const { first, second, paste } = await fixture(t);
  const linkedRoot = path.join(first, "linked-workspace");
  await symlink(second, linkedRoot, process.platform === "win32" ? "junction" : "dir");
  vscodeStub.workspace.workspaceFolders = [{ uri: uri(linkedRoot) }];
  await paste();
  assert.equal(path.dirname(attachments[0]), path.join(linkedRoot, ".alysis", "images"));
  assert.deepEqual(await readFile(attachments[0]), png);
  assert.equal((await readdir(path.join(second, ".alysis", "images"))).length, 1);
});

test("concurrent image pastes create distinct files without overwriting attachments", async (t) => {
  const { second, paste } = await fixture(t);
  vscodeStub.workspace.workspaceFolders = [{ uri: uri(second) }];
  await Promise.all([paste(), paste()]);
  assert.equal(new Set(attachments).size, 2);
  for (const target of attachments) assert.deepEqual(await readFile(target), png);
});
