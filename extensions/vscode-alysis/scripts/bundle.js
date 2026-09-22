"use strict";

/**
 * Bundles the extension host entry point into a single file with esbuild.
 *
 * Why: VS Code loads `main` with a plain require, and every module that file pulls in is a
 * separate disk read on the activation critical path. Unbundled, that was 82 files / 1.7 MB.
 * Bundled, it is one file, and the perf-activation suite measures the difference.
 *
 * What stays on tsc: type-checking, the unit tests (they load out/src and out/test directly)
 * and the build-time scripts that require compiled modules. Only the artifact VS Code loads
 * changes. `vscode` is the one external: it is provided by the extension host, never bundled.
 *
 *   node scripts/bundle.js               dev build: sourcemap, no minify
 *   node scripts/bundle.js --production  minified, no sourcemap
 *   node scripts/bundle.js --watch       rebuild on change
 */

const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const outfile = path.join(root, "dist", "extension.js");
const production = process.argv.includes("--production");
const watch = process.argv.includes("--watch");

/** Surfaces esbuild diagnostics in the format the VS Code `$esbuild-watch` problem matcher reads. */
const problemMatcherPlugin = {
  name: "problem-matcher",
  setup(build) {
    build.onStart(() => {
      console.log("[bundle] build started");
    });
    build.onEnd((result) => {
      for (const { text, location } of result.errors) {
        console.error(`✘ [ERROR] ${text}`);
        if (location) {
          console.error(`    ${location.file}:${location.line}:${location.column}:`);
        }
      }
      console.log(`[bundle] build finished${result.errors.length ? ` with ${result.errors.length} error(s)` : ""}`);
    });
  }
};

async function main() {
  let esbuild;
  try {
    esbuild = require("esbuild");
  } catch (error) {
    console.error("esbuild is not installed. Run `npm install` in extensions/vscode-alysis first.");
    console.error(String(error && error.message ? error.message : error));
    process.exit(1);
  }

  const context = await esbuild.context({
    entryPoints: [path.join(root, "src", "extension.ts")],
    outfile,
    bundle: true,
    // Extension host: Node, CommonJS, and the runtime VS Code >= 1.90 ships (Electron 29 / Node 20).
    platform: "node",
    format: "cjs",
    target: "node20",
    external: ["vscode"],
    // Match tsconfig: the sources are compiled as ES2022 by tsc; esbuild reads tsconfig.json for
    // the same options, so decorators/paths behave identically in both outputs.
    tsconfig: path.join(root, "tsconfig.json"),
    minify: production,
    sourcemap: !production,
    sourcesContent: false,
    legalComments: "none",
    logLevel: "silent",
    plugins: [problemMatcherPlugin]
  });

  if (watch) {
    await context.watch();
    return;
  }
  // A production build must not leave a dev sourcemap beside the minified file: .vscodeignore
  // drops *.map from the VSIX anyway, but a stale map pointing at different code misleads anyone
  // debugging the packaged output.
  if (production) {
    fs.rmSync(`${outfile}.map`, { force: true });
  }
  const result = await context.rebuild();
  await context.dispose();
  if (result.errors.length > 0) {
    process.exit(1);
  }
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
