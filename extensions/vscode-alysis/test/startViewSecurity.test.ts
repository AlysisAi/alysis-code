import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

const ROOT = resolve(__dirname, "../..");

test("Start Here builds every node with the DOM API and never with raw HTML", () => {
  const mediaDir = resolve(ROOT, "media");
  const scripts = readdirSync(mediaDir).filter((name) => name.endsWith(".js"));

  assert.deepEqual(scripts, ["startView.js"], "the unreachable panel assets no longer ship");
  for (const name of scripts) {
    const source = readFileSync(resolve(mediaDir, name), "utf8");
    assert.doesNotMatch(source, /innerHTML/, name);
    assert.doesNotMatch(source, /outerHTML/, name);
    assert.doesNotMatch(source, /insertAdjacentHTML/, name);
    assert.doesNotMatch(source, /\beval\s*\(/, name);
    assert.doesNotMatch(source, /new Function\s*\(/, name);
    assert.doesNotMatch(source, /document\.write/, name);
    assert.match(source, /textContent/, name);
  }
});

test("Start Here webview script stays syntactically valid", () => {
  const script = readFileSync(resolve(ROOT, "media/startView.js"), "utf8");

  assert.doesNotThrow(() => new Function(script));
});

test("Start Here CSP is strict and its cache-buster is not the script nonce", () => {
  const source = readFileSync(resolve(ROOT, "src/views/StartViewProvider.ts"), "utf8");

  assert.match(source, /randomBytes/);
  assert.doesNotMatch(source, /Math\.random/);
  assert.match(source, /"default-src 'none'"/);
  assert.match(source, /script-src 'nonce-\$\{nonce\}'/);
  assert.match(source, /img-src \$\{webview\.cspSource\}/);
  assert.match(source, /style-src \$\{webview\.cspSource\}/);
  assert.match(source, /base-uri 'none'/);
  assert.match(source, /form-action 'none'/);
  assert.match(source, /frame-src 'none'/);
  // A value that appears in a readable attribute is not a nonce: the asset version is generated
  // independently so ?v= can never leak the value that authorises script execution.
  assert.match(source, /const assetVersion = randomBytes\(\d+\)\.toString\("hex"\)/);
  assert.doesNotMatch(source, /assetVersion = encodeURIComponent\(nonce/);
  assert.doesNotMatch(source, /nonce\.slice/);
  // No remote origins, and no inline handlers in the shipped markup.
  assert.doesNotMatch(source, /https:\/\/cdn|unpkg|jsdelivr|googleapis/);
  assert.doesNotMatch(source, /\son(?:click|load|error)=/);
});

test("the webview can only ask the host to run allowlisted commands", () => {
  const source = readFileSync(resolve(ROOT, "src/views/StartViewProvider.ts"), "utf8");

  assert.match(source, /const RECOVERY_COMMAND_IDS = new Set<string>\(\[/);
  for (const command of [
    "COMMANDS.locateCli",
    "COMMANDS.configureProvider",
    "COMMANDS.showModels",
    "COMMANDS.runDoctor",
    "COMMANDS.showBridgeHealth",
    "COMMANDS.openSetupGuide",
    "COMMANDS.copyCliInstallCommand",
    "COMMANDS.checkForUpdates",
    "\"workbench.trust.manage\""
  ]) {
    assert.ok(source.includes(command), `${command} is reachable from a blocker card`);
  }
  assert.match(source, /START_VIEW_COMMAND_IDS\.has\(record\.command\)/);
  // External links are https-only and never command: URLs.
  assert.match(source, /parsed\.scheme === "https"/);
});
