import assert from "node:assert/strict";
import test from "node:test";

import { buildVsCodeExtensionHostArguments } from "./integration/vsCodeExtensionHostInvocation";

test("Extension Host arguments preserve isolation and never require a shell command", () => {
  const args = buildVsCodeExtensionHostArguments({
    extensionDevelopmentPath: ["C:\\extension one", "C:\\extension two"],
    extensionTestsPath: "C:\\driver\\tests.js",
    launchArgs: [
      "C:\\workspace",
      "--user-data-dir",
      "C:\\profile",
      "--extensions-dir=C:\\extensions"
    ]
  });

  assert.equal(args[0], "C:\\workspace");
  assert.ok(args.includes("--user-data-dir"));
  assert.ok(args.includes("--extensions-dir=C:\\extensions"));
  assert.ok(args.includes("--extensionTestsPath=C:\\driver\\tests.js"));
  assert.ok(args.includes("--extensionDevelopmentPath=C:\\extension one"));
  assert.ok(args.includes("--extensionDevelopmentPath=C:\\extension two"));
  assert.ok(args.includes("--disable-workspace-trust"));
  assert.ok(args.every((value) => !/[&|<>]/.test(value)));
});

test("Extension Host arguments fail closed without both isolated profile directories", () => {
  const base = {
    extensionDevelopmentPath: "C:\\extension",
    extensionTestsPath: "C:\\tests.js"
  };
  assert.throws(
    () => buildVsCodeExtensionHostArguments({ ...base, launchArgs: ["--extensions-dir", "x"] }),
    /--user-data-dir/
  );
  assert.throws(
    () => buildVsCodeExtensionHostArguments({ ...base, launchArgs: ["--user-data-dir", "x"] }),
    /--extensions-dir/
  );
});
