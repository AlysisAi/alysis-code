import { createHash } from "node:crypto";
import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import test from "node:test";

import { BrowserPreviewStore } from "../src/browser/BrowserPreviewStore";

const PNG = Buffer.concat([
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
  Buffer.from("bounded-browser-preview")
]);
const PNG_SHA = createHash("sha256").update(PNG).digest("hex");

test("BrowserPreviewStore atomically publishes and reuses verified PNG content", async () => {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-preview-"));
  try {
    const store = new BrowserPreviewStore(join(root, "previews"));
    const first = await store.store(PNG, { sizeBytes: PNG.length, sha256: PNG_SHA });
    const second = await store.store(PNG, { sizeBytes: PNG.length, sha256: PNG_SHA });

    assert.equal(first.key, `${PNG_SHA}.png`);
    assert.deepEqual(second, first);
    assert.deepEqual(await readFile(first.path), PNG);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("BrowserPreviewStore rejects invalid PNG, size, digest, and broad roots", async () => {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-preview-invalid-"));
  try {
    const store = new BrowserPreviewStore(join(root, "previews"), PNG.length);
    await assert.rejects(
      store.store(Buffer.from("not png"), {
        sizeBytes: 7,
        sha256: createHash("sha256").update("not png").digest("hex")
      }),
      /trusted PNG/
    );
    await assert.rejects(store.store(PNG, { sizeBytes: PNG.length + 1, sha256: PNG_SHA }), /size/);
    await assert.rejects(store.store(PNG, { sizeBytes: PNG.length, sha256: "0".repeat(64) }), /digest/);
    assert.throws(() => new BrowserPreviewStore("relative/previews"), /absolute/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("BrowserPreviewStore exports only reverified content and honors no-clobber saves", async () => {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-preview-save-"));
  try {
    const store = new BrowserPreviewStore(join(root, "previews"));
    const preview = await store.store(PNG, { sizeBytes: PNG.length, sha256: PNG_SHA });
    const destination = join(root, "saved.png");

    await store.save(preview, destination);
    assert.deepEqual(await readFile(destination), PNG);
    await assert.rejects(store.save(preview, destination), (error: unknown) =>
      error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "EEXIST"
    );
    await writeFile(destination, Buffer.from("replace me"));
    await store.save(preview, destination, true);
    assert.deepEqual(await readFile(destination), PNG);

    await writeFile(preview.path, Buffer.concat([PNG, Buffer.from("tampered")]));
    await assert.rejects(store.save(preview, join(root, "tampered.png")), /size/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("BrowserPreviewStore serializes background cleanup against a concurrent publish", async () => {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-preview-serial-"));
  try {
    const store = new BrowserPreviewStore(join(root, "previews"));
    // Activation kicks off clear() without awaiting it; a screenshot may land in the same tick.
    const cleared = store.clear();
    const stored = await store.store(PNG, { sizeBytes: PNG.length, sha256: PNG_SHA });
    await cleared;

    assert.deepEqual(await readFile(stored.path), PNG, "the publish must survive the recursive delete");
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("BrowserPreviewStore keeps its queue usable after a rejected operation", async () => {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-preview-queue-"));
  try {
    const store = new BrowserPreviewStore(join(root, "previews"));
    await assert.rejects(
      store.store(Buffer.from("not png"), {
        sizeBytes: 7,
        sha256: createHash("sha256").update("not png").digest("hex")
      }),
      /trusted PNG/
    );
    const preview = await store.store(PNG, { sizeBytes: PNG.length, sha256: PNG_SHA });
    assert.equal(preview.key, `${PNG_SHA}.png`);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("BrowserPreviewStore removes only owned digest paths", async () => {
  const root = await mkdtemp(join(tmpdir(), "alysis-browser-preview-cleanup-"));
  try {
    const store = new BrowserPreviewStore(join(root, "previews"));
    const preview = await store.store(PNG, { sizeBytes: PNG.length, sha256: PNG_SHA });
    await assert.rejects(
      store.remove({ ...preview, key: "../outside.png" }),
      /identity/
    );
    await store.remove(preview);
    await assert.rejects(readFile(preview.path), (error: unknown) =>
      error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "ENOENT"
    );
    await store.clear();
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
