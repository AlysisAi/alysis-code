export const PERF_EVIDENCE_SCHEMA_VERSION = 1;
export const DEFAULT_PERF_SAMPLE_COUNT = 5;
export const DEFAULT_PERF_VSCODE_VERSION = "1.90.0";

const MIB = 1024 * 1024;

export type ActivationPhase = "cold" | "warm";

export interface ActivationPhaseBudget {
  readonly activationP95Ms: number;
  readonly activationMaxMs: number;
  readonly rssAfterP95Bytes: number;
  readonly rssDeltaP95Bytes: number;
  readonly heapUsedAfterP95Bytes: number;
  readonly cpuP95Ms: number;
}

export interface PerfGateConfig {
  readonly sampleCount: number;
  readonly source: "committed_defaults" | "local_override";
  readonly vscodeVersion: string;
  readonly budgets: Readonly<Record<ActivationPhase, ActivationPhaseBudget>>;
}

export interface ActivationPerfSample {
  readonly [key: string]: unknown;
  readonly schemaVersion: number;
  readonly record: "sample";
  readonly phase: ActivationPhase;
  readonly sample: number;
  readonly activationAndCommandMs: number;
  readonly rssBeforeBytes: number;
  readonly rssAfterBytes: number;
  readonly rssDeltaBytes: number;
  readonly heapUsedAfterBytes: number;
  readonly cpuUserMs: number;
  readonly cpuSystemMs: number;
  readonly cpuTotalMs: number;
}

export interface PerfMetricViolation {
  readonly phase: ActivationPhase;
  readonly metric: keyof ActivationPhaseBudget;
  readonly actual: number;
  readonly limit: number;
}

export interface ActivationPhaseSummary {
  readonly samples: number;
  readonly metrics: ActivationPhaseBudget;
  readonly budget: ActivationPhaseBudget;
  readonly violations: readonly PerfMetricViolation[];
}

export interface PerfGateSummary {
  readonly schemaVersion: number;
  readonly record: "summary";
  readonly passed: boolean;
  readonly configSource: PerfGateConfig["source"];
  readonly sampleCount: number;
  readonly phases: Readonly<Record<ActivationPhase, ActivationPhaseSummary>>;
  readonly violations: readonly PerfMetricViolation[];
}

export const DEFAULT_PERF_BUDGETS: Readonly<Record<ActivationPhase, ActivationPhaseBudget>> =
  Object.freeze({
    cold: Object.freeze({
      activationP95Ms: 5_000,
      activationMaxMs: 8_000,
      rssAfterP95Bytes: 512 * MIB,
      rssDeltaP95Bytes: 128 * MIB,
      heapUsedAfterP95Bytes: 192 * MIB,
      cpuP95Ms: 5_000
    }),
    warm: Object.freeze({
      activationP95Ms: 3_500,
      activationMaxMs: 6_000,
      rssAfterP95Bytes: 512 * MIB,
      rssDeltaP95Bytes: 128 * MIB,
      heapUsedAfterP95Bytes: 192 * MIB,
      cpuP95Ms: 3_500
    })
  });

const LOCAL_OVERRIDE_FLAG = "ALYSIS_PERF_LOCAL_OVERRIDE";
const LOCAL_SAMPLE_KEY = "ALYSIS_PERF_LOCAL_SAMPLES";
const LOCAL_BUDGET_KEYS: Readonly<Record<ActivationPhase, Readonly<Record<keyof ActivationPhaseBudget, string>>>> = {
  cold: {
    activationP95Ms: "ALYSIS_PERF_LOCAL_COLD_ACTIVATION_P95_MS",
    activationMaxMs: "ALYSIS_PERF_LOCAL_COLD_ACTIVATION_MAX_MS",
    rssAfterP95Bytes: "ALYSIS_PERF_LOCAL_COLD_RSS_AFTER_P95_MIB",
    rssDeltaP95Bytes: "ALYSIS_PERF_LOCAL_COLD_RSS_DELTA_P95_MIB",
    heapUsedAfterP95Bytes: "ALYSIS_PERF_LOCAL_COLD_HEAP_AFTER_P95_MIB",
    cpuP95Ms: "ALYSIS_PERF_LOCAL_COLD_CPU_P95_MS"
  },
  warm: {
    activationP95Ms: "ALYSIS_PERF_LOCAL_WARM_ACTIVATION_P95_MS",
    activationMaxMs: "ALYSIS_PERF_LOCAL_WARM_ACTIVATION_MAX_MS",
    rssAfterP95Bytes: "ALYSIS_PERF_LOCAL_WARM_RSS_AFTER_P95_MIB",
    rssDeltaP95Bytes: "ALYSIS_PERF_LOCAL_WARM_RSS_DELTA_P95_MIB",
    heapUsedAfterP95Bytes: "ALYSIS_PERF_LOCAL_WARM_HEAP_AFTER_P95_MIB",
    cpuP95Ms: "ALYSIS_PERF_LOCAL_WARM_CPU_P95_MS"
  }
};

export function resolvePerfGateConfig(env: NodeJS.ProcessEnv): PerfGateConfig {
  const localKeys = [
    LOCAL_SAMPLE_KEY,
    ...Object.values(LOCAL_BUDGET_KEYS.cold),
    ...Object.values(LOCAL_BUDGET_KEYS.warm)
  ];
  const configuredLocalKeys = localKeys.filter((key) => env[key]?.trim());
  const overrideFlag = env[LOCAL_OVERRIDE_FLAG]?.trim();
  if (overrideFlag && overrideFlag !== "1") {
    throw new Error(`${LOCAL_OVERRIDE_FLAG} must be exactly 1 when local overrides are enabled.`);
  }
  if (configuredLocalKeys.length > 0 && overrideFlag !== "1") {
    throw new Error(
      `Local performance overrides require ${LOCAL_OVERRIDE_FLAG}=1; found ${configuredLocalKeys.join(", ")}.`
    );
  }
  if (overrideFlag === "1" && isCiEnvironment(env)) {
    throw new Error("Local activation-performance overrides are forbidden in CI and release workflows.");
  }

  const localOverride = overrideFlag === "1";
  const sampleCount = localOverride && env[LOCAL_SAMPLE_KEY]?.trim()
    ? parseInteger(env[LOCAL_SAMPLE_KEY], LOCAL_SAMPLE_KEY, 3, 20)
    : DEFAULT_PERF_SAMPLE_COUNT;
  const budgets = {
    cold: resolvePhaseBudget("cold", env, localOverride),
    warm: resolvePhaseBudget("warm", env, localOverride)
  };
  for (const phase of ["cold", "warm"] as const) {
    if (budgets[phase].activationMaxMs < budgets[phase].activationP95Ms) {
      throw new Error(`${phase} activation max budget must be >= its p95 budget.`);
    }
  }
  return {
    sampleCount,
    source: localOverride ? "local_override" : "committed_defaults",
    vscodeVersion: DEFAULT_PERF_VSCODE_VERSION,
    budgets
  };
}

export function parsePerfEvidenceJsonLines(raw: string): readonly Record<string, unknown>[] {
  return raw.split(/\r?\n/).filter((line) => line.trim()).map((line, index) => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(line);
    } catch {
      throw new Error(`Activation performance evidence line ${index + 1} is not valid JSON.`);
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error(`Activation performance evidence line ${index + 1} must be an object.`);
    }
    return parsed as Record<string, unknown>;
  });
}

export function evaluatePerfSamples(
  records: readonly Record<string, unknown>[],
  config: PerfGateConfig
): PerfGateSummary {
  const samples = records
    .filter((record) => record.record === "sample")
    .map(validateSample);
  const phases = {
    cold: summarizePhase("cold", samples, config),
    warm: summarizePhase("warm", samples, config)
  };
  const violations = [...phases.cold.violations, ...phases.warm.violations];
  return {
    schemaVersion: PERF_EVIDENCE_SCHEMA_VERSION,
    record: "summary",
    passed: violations.length === 0,
    configSource: config.source,
    sampleCount: config.sampleCount,
    phases,
    violations
  };
}

function resolvePhaseBudget(
  phase: ActivationPhase,
  env: NodeJS.ProcessEnv,
  localOverride: boolean
): ActivationPhaseBudget {
  const defaults = DEFAULT_PERF_BUDGETS[phase];
  if (!localOverride) {
    return defaults;
  }
  const keys = LOCAL_BUDGET_KEYS[phase];
  return {
    activationP95Ms: parseOptionalPositive(env[keys.activationP95Ms], keys.activationP95Ms, defaults.activationP95Ms),
    activationMaxMs: parseOptionalPositive(env[keys.activationMaxMs], keys.activationMaxMs, defaults.activationMaxMs),
    rssAfterP95Bytes: parseOptionalMib(env[keys.rssAfterP95Bytes], keys.rssAfterP95Bytes, defaults.rssAfterP95Bytes),
    rssDeltaP95Bytes: parseOptionalMib(env[keys.rssDeltaP95Bytes], keys.rssDeltaP95Bytes, defaults.rssDeltaP95Bytes),
    heapUsedAfterP95Bytes: parseOptionalMib(
      env[keys.heapUsedAfterP95Bytes],
      keys.heapUsedAfterP95Bytes,
      defaults.heapUsedAfterP95Bytes
    ),
    cpuP95Ms: parseOptionalPositive(env[keys.cpuP95Ms], keys.cpuP95Ms, defaults.cpuP95Ms)
  };
}

function summarizePhase(
  phase: ActivationPhase,
  allSamples: readonly ActivationPerfSample[],
  config: PerfGateConfig
): ActivationPhaseSummary {
  const samples = allSamples.filter((sample) => sample.phase === phase);
  if (samples.length !== config.sampleCount) {
    throw new Error(
      `Expected exactly ${config.sampleCount} ${phase} samples; found ${samples.length}.`
    );
  }
  const indexes = samples.map((sample) => sample.sample).sort((left, right) => left - right);
  const expected = Array.from({ length: config.sampleCount }, (_value, index) => index + 1);
  if (indexes.some((value, index) => value !== expected[index])) {
    throw new Error(`${phase} samples must contain each index from 1 through ${config.sampleCount} exactly once.`);
  }
  const metrics: ActivationPhaseBudget = {
    activationP95Ms: percentile(samples.map((sample) => sample.activationAndCommandMs), 0.95),
    activationMaxMs: Math.max(...samples.map((sample) => sample.activationAndCommandMs)),
    rssAfterP95Bytes: percentile(samples.map((sample) => sample.rssAfterBytes), 0.95),
    rssDeltaP95Bytes: percentile(samples.map((sample) => sample.rssDeltaBytes), 0.95),
    heapUsedAfterP95Bytes: percentile(samples.map((sample) => sample.heapUsedAfterBytes), 0.95),
    cpuP95Ms: percentile(samples.map((sample) => sample.cpuTotalMs), 0.95)
  };
  const budget = config.budgets[phase];
  const violations = (Object.keys(budget) as Array<keyof ActivationPhaseBudget>)
    .filter((metric) => metrics[metric] > budget[metric])
    .map((metric) => ({ phase, metric, actual: metrics[metric], limit: budget[metric] }));
  return { samples: samples.length, metrics, budget, violations };
}

function validateSample(record: Record<string, unknown>): ActivationPerfSample {
  if (record.schemaVersion !== PERF_EVIDENCE_SCHEMA_VERSION) {
    throw new Error("Activation performance sample has an unsupported schemaVersion.");
  }
  if (record.phase !== "cold" && record.phase !== "warm") {
    throw new Error("Activation performance sample phase must be cold or warm.");
  }
  const sample = finiteNumber(record.sample, "sample", true);
  if (sample < 1) {
    throw new Error("Activation performance sample index must be positive.");
  }
  return {
    schemaVersion: PERF_EVIDENCE_SCHEMA_VERSION,
    record: "sample",
    phase: record.phase,
    sample,
    activationAndCommandMs: positiveNumber(record.activationAndCommandMs, "activationAndCommandMs"),
    rssBeforeBytes: finiteNumber(record.rssBeforeBytes, "rssBeforeBytes", true),
    rssAfterBytes: finiteNumber(record.rssAfterBytes, "rssAfterBytes", true),
    rssDeltaBytes: finiteNumber(record.rssDeltaBytes, "rssDeltaBytes", true),
    heapUsedAfterBytes: finiteNumber(record.heapUsedAfterBytes, "heapUsedAfterBytes", true),
    cpuUserMs: finiteNumber(record.cpuUserMs, "cpuUserMs"),
    cpuSystemMs: finiteNumber(record.cpuSystemMs, "cpuSystemMs"),
    cpuTotalMs: finiteNumber(record.cpuTotalMs, "cpuTotalMs")
  };
}

function percentile(values: readonly number[], fraction: number): number {
  const sorted = [...values].sort((left, right) => left - right);
  const rank = (sorted.length - 1) * fraction;
  const lower = Math.floor(rank);
  const upper = Math.ceil(rank);
  if (lower === upper) {
    return sorted[lower];
  }
  const weight = rank - lower;
  return sorted[lower] + (sorted[upper] - sorted[lower]) * weight;
}

function positiveNumber(value: unknown, field: string): number {
  const result = finiteNumber(value, field);
  if (result <= 0) {
    throw new Error(`Activation performance ${field} must be positive.`);
  }
  return result;
}

function finiteNumber(value: unknown, field: string, integer = false): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || (integer && !Number.isSafeInteger(value))) {
    throw new Error(`Activation performance ${field} must be a finite non-negative${integer ? " integer" : " number"}.`);
  }
  return value;
}

function parseInteger(
  value: string | undefined,
  field: string,
  minimum: number,
  maximum: number
): number {
  const parsed = Number(value);
  if (!Number.isSafeInteger(parsed) || parsed < minimum || parsed > maximum) {
    throw new Error(`${field} must be an integer from ${minimum} through ${maximum}.`);
  }
  return parsed;
}

function parseOptionalPositive(value: string | undefined, field: string, fallback: number): number {
  if (!value?.trim()) {
    return fallback;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${field} must be a finite positive number.`);
  }
  return parsed;
}

function parseOptionalMib(value: string | undefined, field: string, fallback: number): number {
  return value?.trim() ? parseOptionalPositive(value, field, fallback) * MIB : fallback;
}

function isCiEnvironment(env: NodeJS.ProcessEnv): boolean {
  return [env.CI, env.GITHUB_ACTIONS, env.TF_BUILD, env.BUILDKITE].some((value) =>
    ["1", "true"].includes(value?.trim().toLowerCase() ?? "")
  );
}
