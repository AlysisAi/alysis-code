"use strict";

const { spawnSync } = require("node:child_process");
const { readdirSync } = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
// Node 20 on Windows does not expand shell globs. Enumerate both suites explicitly
// so every platform runs the same tests and an empty suite cannot silently pass.
const files = [
  [path.join(root, "out", "test"), ".test.js"],
  [path.resolve(root, "../shared-ui/test"), ".test.cjs"]
].flatMap(([directory, suffix]) => {
  const names = readdirSync(directory).filter((name) => name.endsWith(suffix)).sort();
  if (!names.length) throw new Error(`No unit tests found in ${directory}`);
  return names.map((name) => path.join(directory, name));
});

// Keep the time-budgeted DOM test off the CPU pool used by the other suites.
// Its existing wall-clock budget must measure rendering, not parallel test load.
const timed = files.filter((file) => path.basename(file) === "startViewRender.test.js");
if (timed.length !== 1) throw new Error("The rendering budget suite is missing or ambiguous");
const suites = [files.filter((file) => !timed.includes(file)), timed];
for (const suite of suites) {
  const result = spawnSync(process.execPath, ["--test", ...suite], {
    cwd: root,
    stdio: "inherit",
    windowsHide: true
  });
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
