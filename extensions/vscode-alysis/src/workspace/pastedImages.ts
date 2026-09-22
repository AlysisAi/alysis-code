import { randomBytes } from "node:crypto";
import { lstat, mkdir, open, realpath } from "node:fs/promises";
import path from "node:path";

/** Store validated image bytes on the extension host, inside the selected workspace. */
export async function savePastedImage(workspaceRoot: string, bytes: Uint8Array, extension: string): Promise<string> {
  if (!/^(png|jpe?g|gif|webp)$/.test(extension)) {
    throw new Error("Unsupported pasted image file extension.");
  }
  // A workspace itself may have been opened through a link. Anchor scratch paths to its real root,
  // while returning the original spelling expected by the backend action's workspace-scope check.
  const root = await realpath(workspaceRoot);
  const directories = [path.join(root, ".alysis"), path.join(root, ".alysis", "images")];
  for (const directory of directories) {
    try {
      // Never create recursively: inspect .alysis before creating anything beneath it.
      await mkdir(directory, { mode: 0o700 });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
    }
    await assertScratchDirectory(directory);
  }

  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  const filename = `pasted-${stamp}-${randomBytes(16).toString("hex")}.${extension}`;
  const target = path.join(directories[1], filename);
  const file = await open(target, "wx", 0o600);
  try {
    // Recheck after opening and write through the owned handle. Exclusive creation also rejects
    // existing files and final-component links, so concurrent pastes cannot overwrite each other.
    for (const directory of directories) await assertScratchDirectory(directory);
    await file.writeFile(bytes);
  } finally {
    await file.close();
  }
  return path.resolve(workspaceRoot, ".alysis", "images", filename);
}

async function assertScratchDirectory(directory: string): Promise<void> {
  const stat = await lstat(directory);
  if (stat.isSymbolicLink()) {
    throw new Error("Cannot paste an image through a symbolic link or junction in .alysis/images.");
  }
  if (!stat.isDirectory()) {
    throw new Error("Cannot paste an image because .alysis/images is not a directory.");
  }
  if (path.relative(directory, await realpath(directory)) !== "") {
    throw new Error("Cannot paste an image because a symbolic link or junction redirects .alysis/images.");
  }
}
