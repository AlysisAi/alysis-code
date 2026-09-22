import assert from "node:assert/strict";
import test from "node:test";

import {
  ActivationPerfSample,
  DEFAULT_PERF_BUDGETS,
  DEFAULT_PERF_SAMPLE_COUNT,
  evaluatePerfSamples,
  parsePerfEvidenceJsonLines,
  PERF_EVIDENCE_SCHEMA_VERSION,
  PerfGateConfig,
  resolvePerfGateConfig
} from "./integration/perf-activation/perfBudget";

test("activation performance defaults are committed and CI rejects every local override", () => {
  const defaults = resolvePerfGateConfig({});
  assert.equal(defaults.source, "committed_defaults");
  assert.equal(defaults.sampleCount, DEFAULT_PERF_SAMPLE_COUNT);
  assert.deepEqual(defaults.budgets, DEFAULT_PERF_BUDGETS);

  assert.throws(
    () => resolvePerfGateConfig({ ALYSIS_PERF_LOCAL_SAMPLES: "3" }),
    /require ALYSIS_PERF_LOCAL_OVERRIDE=1/
  );
  assert.throws(
    () => resolvePerfGateConfig({ CI: "true", ALYSIS_PERF_LOCAL_OVERRIDE: "1" }),
    /forbidden in CI/
  );
  assert.throws(
    () => resolvePerfGateConfig({ GITHUB_ACTIONS: "true", ALYSIS_PERF_LOCAL_OVERRIDE: "1" }),
    /forbidden in CI/
  );
  assert.throws(
    () => resolvePerfGateConfig({ ALYSIS_PERF_LOCAL_OVERRIDE: "true" }),
    /must be exactly 1/
  );
});

test("local performance overrides are explicit, bounded, and internally consistent", () => {
  const local = resolvePerfGateConfig({
    ALYSIS_PERF_LOCAL_OVERRIDE: "1",
    ALYSIS_PERF_LOCAL_SAMPLES: "3",
    ALYSIS_PERF_LOCAL_COLD_ACTIVATION_P95_MS: "9000",
    ALYSIS_PERF_LOCAL_COLD_ACTIVATION_MAX_MS: "10000",
    ALYSIS_PERF_LOCAL_WARM_RSS_AFTER_P95_MIB: "1024"
  });
  assert.equal(local.source, "local_override");
  assert.equal(local.sampleCount, 3);
  assert.equal(local.budgets.cold.activationP95Ms, 9_000);
  assert.equal(local.budgets.warm.rssAfterP95Bytes, 1024 * 1024 * 1024);

  assert.throws(
    () => resolvePerfGateConfig({
      ALYSIS_PERF_LOCAL_OVERRIDE: "1",
      ALYSIS_PERF_LOCAL_SAMPLES: "2"
    }),
    /integer from 3 through 20/
  );
  assert.throws(
    () => resolvePerfGateConfig({
      ALYSIS_PERF_LOCAL_OVERRIDE: "1",
      ALYSIS_PERF_LOCAL_COLD_ACTIVATION_P95_MS: "9000",
      ALYSIS_PERF_LOCAL_COLD_ACTIVATION_MAX_MS: "8000"
    }),
    /max budget must be >= its p95 budget/
  );
});

test("gate evaluates complete cold and warm samples and reports resource violations", () => {
  const passingConfig = resolvePerfGateConfig({
    ALYSIS_PERF_LOCAL_OVERRIDE: "1",
    ALYSIS_PERF_LOCAL_SAMPLES: "3"
  });
  const passing = evaluatePerfSamples(
    [...samples("cold", 3), ...samples("warm", 3)],
    passingConfig
  );
  assert.equal(passing.passed, true);
  assert.equal(passing.phases.cold.samples, 3);
  assert.equal(passing.phases.warm.samples, 3);
  assert.equal(passing.phases.cold.metrics.activationP95Ms, 102.9);

  const strictConfig: PerfGateConfig = {
    ...passingConfig,
    budgets: {
      ...passingConfig.budgets,
      cold: {
        ...passingConfig.budgets.cold,
        rssDeltaP95Bytes: 1,
        cpuP95Ms: 1
      }
    }
  };
  const failing = evaluatePerfSamples(
    [...samples("cold", 3), ...samples("warm", 3)],
    strictConfig
  );
  assert.equal(failing.passed, false);
  assert.deepEqual(
    failing.violations.map((item) => `${item.phase}.${item.metric}`).sort(),
    ["cold.cpuP95Ms", "cold.rssDeltaP95Bytes"]
  );
});

test("gate fails closed on missing, duplicate, malformed, or non-finite evidence", () => {
  const config = resolvePerfGateConfig({
    ALYSIS_PERF_LOCAL_OVERRIDE: "1",
    ALYSIS_PERF_LOCAL_SAMPLES: "3"
  });
  const complete = [...samples("cold", 3), ...samples("warm", 3)];

  assert.throws(
    () => evaluatePerfSamples(complete.slice(1), config),
    /exactly 3 cold samples/
  );
  assert.throws(
    () => evaluatePerfSamples([...complete, complete[0]], config),
    /exactly 3 cold samples/
  );
  assert.throws(
    () => evaluatePerfSamples(
      complete.map((sample, index) => index === 0 ? { ...sample, rssAfterBytes: -1 } : sample),
      config
    ),
    /rssAfterBytes must be a finite non-negative integer/
  );
  assert.throws(
    () => evaluatePerfSamples(
      complete.map((sample, index) => index === 0 ? { ...sample, phase: "lukewarm" } : sample),
      config
    ),
    /phase must be cold or warm/
  );
  assert.throws(
    () => parsePerfEvidenceJsonLines('{"record":"run"}\nnot-json\n'),
    /line 2 is not valid JSON/
  );
});

function samples(phase: "cold" | "warm", count: number): Record<string, unknown>[] {
  return Array.from({ length: count }, (_value, index) => sample(phase, index + 1));
}

function sample(phase: "cold" | "warm", index: number): ActivationPerfSample {
  return {
    schemaVersion: PERF_EVIDENCE_SCHEMA_VERSION,
    record: "sample",
    phase,
    sample: index,
    activationAndCommandMs: 100 + index,
    rssBeforeBytes: 100 * 1024 * 1024,
    rssAfterBytes: 110 * 1024 * 1024,
    rssDeltaBytes: 10 * 1024 * 1024,
    heapUsedAfterBytes: 50 * 1024 * 1024,
    cpuUserMs: 10,
    cpuSystemMs: 5,
    cpuTotalMs: 15
  };
}
