import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

test("activation performance npm and workflow wiring retain evidence and use committed defaults", () => {
  const packageRoot = resolve(__dirname, "../..");
  const repoRoot = resolve(packageRoot, "../..");
  const packageJson = JSON.parse(readFileSync(resolve(packageRoot, "package.json"), "utf8")) as {
    scripts?: Record<string, string>;
  };
  const workflow = readFileSync(
    resolve(repoRoot, ".github/workflows/vscode-extension-activation-performance.yml"),
    "utf8"
  );
  const runner = readFileSync(
    resolve(packageRoot, "test/integration/perf-activation/runTest.ts"),
    "utf8"
  );

  assert.equal(
    packageJson.scripts?.["test:perf-activation"],
    "npm run compile && node ./out/test/integration/perf-activation/runTest.js"
  );
  assert.match(workflow, /xvfb-run -a npm run test:perf-activation/);
  assert.match(workflow, /ALYSIS_PERF_ACTIVATION_EVIDENCE:/);
  assert.match(workflow, /CI: "true"/);
  assert.match(workflow, /if: always\(\)/);
  assert.match(workflow, /actions\/upload-artifact@[0-9a-f]{40}/);
  assert.match(workflow, /if-no-files-found: error/);
  assert.match(workflow, /retention-days: 30/);
  assert.doesNotMatch(workflow, /ALYSIS_PERF_LOCAL_/);
  assert.doesNotMatch(workflow, /PERF.*\$\{\{\s*inputs\./);

  const invokingWorkflows = readdirSync(resolve(repoRoot, ".github/workflows"))
    .filter((name) => /\.ya?ml$/.test(name))
    .map((name) => ({
      name,
      source: readFileSync(resolve(repoRoot, ".github/workflows", name), "utf8")
    }))
    .filter((item) => item.source.includes("test:perf-activation"));
  assert.ok(invokingWorkflows.length > 0, "a workflow must enforce the activation performance gate");
  for (const item of invokingWorkflows) {
    assert.doesNotMatch(item.source, /ALYSIS_PERF_LOCAL_/, item.name);
  }

  assert.match(runner, /resolvePerfGateConfig\(process\.env\)/);
  assert.match(runner, /phase of \["cold", "warm"\]/);
  assert.match(runner, /evaluatePerfSamples\(records, config\)/);
  assert.doesNotMatch(runner, /ALYSIS_PERF_ACTIVATION_SAMPLES/);
});
