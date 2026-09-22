import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  consumeInstalledProductionQaFlag,
  INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT
} from "../src/qa/InstalledProductionQa";

test("installed production QA is unavailable without its explicit one-shot flag", () => {
  const environment: NodeJS.ProcessEnv = {};

  assert.equal(consumeInstalledProductionQaFlag(true, environment), false);
  assert.equal(environment[INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT], undefined);
});

test("installed production QA consumes its exact flag once", () => {
  const environment: NodeJS.ProcessEnv = {
    [INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT]: "1"
  };

  assert.equal(consumeInstalledProductionQaFlag(true, environment), true);
  assert.equal(environment[INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT], undefined);
  assert.equal(consumeInstalledProductionQaFlag(true, environment), false);
});

test("installed production QA never opens in Test or Development mode", () => {
  for (const value of ["1", "true", "enabled", " 1 "]) {
    const environment: NodeJS.ProcessEnv = {
      [INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT]: value
    };

    assert.equal(consumeInstalledProductionQaFlag(false, environment), false);
    assert.equal(environment[INSTALLED_PRODUCTION_QA_ONCE_ENVIRONMENT], undefined);
  }
});

test("extension consumes the private QA gate before async activation and exposes no command", () => {
  const source = readFileSync(`${process.cwd()}/src/extension.ts`, "utf8");
  const consumeIndex = source.indexOf("consumeInstalledProductionQaFlag(");
  const firstActivationAwait = source.indexOf("await managedRuntime.refresh");

  assert.ok(consumeIndex >= 0);
  assert.ok(firstActivationAwait > consumeIndex);
  assert.match(source, /ExtensionMode\.Production &&[\s\S]*installedProductionQaEnabled/);
  assert.doesNotMatch(source, /registerCommand\([^)]*installedProductionQa/);
  assert.match(source, /responseLength: evidence\.length/);
  assert.match(source, /responseSha256: evidence\.sha256/);
  assert.doesNotMatch(source, /responseText:/);
});
