import {
  chmod,
  link,
  lstat,
  mkdir,
  open,
  readFile,
  rename,
  rm
} from "node:fs/promises";
import { createHash, randomBytes } from "node:crypto";
import { basename, dirname, isAbsolute, parse, resolve } from "node:path";

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
const SHA256_RE = /^[0-9a-f]{64}$/;

export interface BrowserPreview {
  key: string;
  path: string;
  sha256: string;
  sizeBytes: number;
}

/**
 * Private, content-addressed screenshot previews. Callers provide already
 * bounded bytes; this class independently verifies PNG, size and digest before
 * an atomic no-clobber publication.
 */
export class BrowserPreviewStore {
  private readonly root: string;
  // Every filesystem operation runs on one serial queue. Startup cleanup is a recursive delete of the
  // whole private root and must never interleave with a concurrent publish or save.
  private queue: Promise<unknown> = Promise.resolve();

  public constructor(rootPath: string, private readonly maxBytes = 10 * 1024 * 1024) {
    this.root = validatedRoot(rootPath);
    if (!Number.isInteger(maxBytes) || maxBytes < PNG_SIGNATURE.length || maxBytes > 20 * 1024 * 1024) {
      throw new Error("Browser preview byte limit is invalid.");
    }
  }

  public store(
    payload: Uint8Array,
    expected: { sizeBytes: number; sha256: string }
  ): Promise<BrowserPreview> {
    return this.serialize(() => this.storeOnce(payload, expected));
  }

  public save(preview: BrowserPreview, destination: string, overwrite = false): Promise<void> {
    return this.serialize(() => this.saveOnce(preview, destination, overwrite));
  }

  public remove(preview: BrowserPreview): Promise<void> {
    return this.serialize(async () => {
      await rm(this.assertOwnedPreview(preview), { force: true });
    });
  }

  public clear(): Promise<void> {
    return this.serialize(async () => {
      await rm(this.root, { recursive: true, force: true });
    });
  }

  private serialize<T>(operation: () => Promise<T>): Promise<T> {
    // Chain on the settled queue so one rejected operation never poisons the ones behind it.
    const result = this.queue.then(operation, operation);
    this.queue = result.then(() => undefined, () => undefined);
    return result;
  }

  private async storeOnce(
    payload: Uint8Array,
    expected: { sizeBytes: number; sha256: string }
  ): Promise<BrowserPreview> {
    const bytes = Buffer.from(payload);
    const sha256 = normalizedDigest(expected.sha256);
    verifyPayload(bytes, expected.sizeBytes, sha256, this.maxBytes);
    await mkdir(this.root, { recursive: true, mode: 0o700 });
    await chmod(this.root, 0o700);
    const key = `${sha256}.png`;
    const target = resolve(this.root, key);
    assertPreviewPath(this.root, target, key);
    const temporary = resolve(
      this.root,
      `.${sha256}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`
    );
    try {
      const handle = await open(temporary, "wx", 0o600);
      try {
        await handle.writeFile(bytes);
        await handle.sync();
      } finally {
        await handle.close();
      }
      try {
        await link(temporary, target);
      } catch (error) {
        if (!isAlreadyExists(error)) {
          throw error;
        }
        await this.verifyExisting(target, expected.sizeBytes, sha256);
      }
    } finally {
      await rm(temporary, { force: true });
    }
    return { key, path: target, sha256, sizeBytes: bytes.length };
  }

  private async saveOnce(preview: BrowserPreview, destination: string, overwrite = false): Promise<void> {
    const source = this.assertOwnedPreview(preview);
    const bytes = await readFile(source);
    verifyPayload(bytes, preview.sizeBytes, normalizedDigest(preview.sha256), this.maxBytes);
    const target = resolveDestination(destination);
    await mkdir(dirname(target), { recursive: true });
    const temporary = resolve(
      dirname(target),
      `.${basename(target)}.${process.pid}.${randomBytes(8).toString("hex")}.tmp`
    );
    try {
      const handle = await open(temporary, "wx", 0o600);
      try {
        await handle.writeFile(bytes);
        await handle.sync();
      } finally {
        await handle.close();
      }
      if (overwrite) {
        await rename(temporary, target);
      } else {
        await link(temporary, target);
      }
    } finally {
      await rm(temporary, { force: true });
    }
  }

  private assertOwnedPreview(preview: BrowserPreview): string {
    const digest = normalizedDigest(preview.sha256);
    const key = `${digest}.png`;
    if (preview.key !== key) {
      throw new Error("Browser preview identity is invalid.");
    }
    const target = resolve(preview.path);
    assertPreviewPath(this.root, target, key);
    return target;
  }

  private async verifyExisting(target: string, sizeBytes: number, sha256: string): Promise<void> {
    const info = await lstat(target);
    if (!info.isFile() || info.isSymbolicLink() || info.size !== sizeBytes) {
      throw new Error("Browser preview cache entry is not trusted.");
    }
    const bytes = await readFile(target);
    verifyPayload(bytes, sizeBytes, sha256, this.maxBytes);
  }
}

function validatedRoot(value: string): string {
  if (typeof value !== "string" || !value.trim() || !isAbsolute(value)) {
    throw new Error("Browser preview root must be an absolute path.");
  }
  const root = resolve(value);
  if (root === parse(root).root || dirname(root) === parse(root).root) {
    throw new Error("Browser preview root is too broad.");
  }
  return root;
}

function resolveDestination(value: string): string {
  if (typeof value !== "string" || !value.trim() || !isAbsolute(value)) {
    throw new Error("Browser screenshot destination must be an absolute path.");
  }
  const target = resolve(value);
  if (target === parse(target).root) {
    throw new Error("Browser screenshot destination is invalid.");
  }
  return target;
}

function normalizedDigest(value: string): string {
  const digest = String(value || "").trim().toLowerCase();
  if (!SHA256_RE.test(digest)) {
    throw new Error("Browser screenshot digest is invalid.");
  }
  return digest;
}

function verifyPayload(bytes: Buffer, sizeBytes: number, sha256: string, maxBytes: number): void {
  if (!Number.isInteger(sizeBytes) || sizeBytes !== bytes.length || bytes.length > maxBytes) {
    throw new Error("Browser screenshot size verification failed.");
  }
  if (bytes.length < PNG_SIGNATURE.length || !bytes.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) {
    throw new Error("Browser screenshot is not a trusted PNG.");
  }
  const actual = createHash("sha256").update(bytes).digest("hex");
  if (actual !== sha256) {
    throw new Error("Browser screenshot digest verification failed.");
  }
}

function assertPreviewPath(root: string, target: string, key: string): void {
  if (basename(target) !== key || dirname(target) !== root) {
    throw new Error("Browser preview path escaped its private root.");
  }
}

function isAlreadyExists(error: unknown): boolean {
  return error instanceof Error && "code" in error && (error as NodeJS.ErrnoException).code === "EEXIST";
}
