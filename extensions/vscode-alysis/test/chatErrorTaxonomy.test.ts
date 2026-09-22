import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import {
  BUNDLED_RUNTIME_INVALID_CODE,
  BUNDLED_RUNTIME_MISSING_CODE,
  ChatErrorKind,
  classifyChatError,
  plainDetail
} from "../src/chat/ChatErrorTaxonomy";

const CONTRIBUTED_COMMANDS = contributedCommandIds();
const BUILT_IN_COMMANDS = new Set(["workbench.trust.manage"]);

test("checkpoint setup failures explain that the action did not run", () => {
  const result = classifyChatError("checkpoint_unavailable: pending storage");
  assert.equal(result.kind, "checkpoint_unavailable");
  assert.match(result.detail, /before it could change files/);
  assert.match(result.detail, /send your request again/);
});

test("an unavailable Docker runner gets setup recovery instead of a provider or CLI error", () => {
  for (const message of [
    "failed to connect to the docker API at npipe:////./pipe/docker_engine: The system cannot find the file specified.",
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?",
    "Docker is not installed or not on PATH."
  ]) {
    const classified = classifyChatError(message);
    assert.equal(classified.kind, "sandbox_unavailable");
    assert.equal(classified.actions[0].command, "alysis.runDoctor");
    assert.match(classified.detail, /File reading and Git review remain available/);
    assertNoProtocolNoise(classified.title, classified.detail);
  }
});

test("an expired or wrong API key becomes an actionable card with Update API key as the primary", () => {
  const classified = classifyChatError(
    protocolError("auth_error", "Request failed with status 401: invalid api key")
  );

  assert.equal(classified.kind, "auth_invalid");
  assert.equal(classified.actions[0].label, "Update API key");
  assert.equal(classified.actions[0].command, "alysis.configureProvider");
  assert.equal(classified.actions[0].primary, true);
  assertNoProtocolNoise(classified.title, classified.detail);
});

test("a bare backend sentence with an HTTP status still classifies as an auth failure", () => {
  const classified = classifyChatError("The provider rejected the request: Unauthorized (401)");

  assert.equal(classified.kind, "auth_invalid");
  assertNoProtocolNoise(classified.title, classified.detail);
});

test("rate limiting carries the backend's retry window for the countdown", () => {
  const fromDetails = classifyChatError(
    protocolError("rate_limited", "Too many requests.", { retry_after_seconds: 42 })
  );
  assert.equal(fromDetails.kind, "rate_limited");
  assert.equal(fromDetails.retryAfterSeconds, 42);

  const fromMessage = classifyChatError("Rate limit reached (429). Retry after 2 minutes.");
  assert.equal(fromMessage.kind, "rate_limited");
  assert.equal(fromMessage.retryAfterSeconds, 120);

  const withoutWindow = classifyChatError("Rate limit reached for this model.");
  assert.equal(withoutWindow.kind, "rate_limited");
  assert.equal(withoutWindow.retryAfterSeconds, undefined);
});

test("quota, network, and context failures each get their own kind and recovery", () => {
  const quota = classifyChatError("You exceeded your current quota; please check your billing.");
  assert.equal(quota.kind, "quota_exhausted");
  assert.equal(quota.actions[0].command, "alysis.configureProvider");

  const network = classifyChatError(Object.assign(new Error("getaddrinfo ENOTFOUND api.example.com"), {}));
  assert.equal(network.kind, "network_unreachable");
  assert.deepEqual(network.actions.map((action) => action.command), ["alysis.showBridgeHealth"]);

  const overflow = classifyChatError("This model's maximum context length is 128000 tokens.");
  assert.equal(overflow.kind, "context_overflow");
  assert.deepEqual(
    overflow.actions.map((action) => action.command),
    ["alysis.session.compact", "alysis.newSession"]
  );
});

test("a missing runtime offers locate, install, and the setup guide", () => {
  const classified = classifyChatError(protocolError(BUNDLED_RUNTIME_MISSING_CODE, "No bundled runtime."));

  assert.equal(classified.kind, "runtime_missing");
  assert.deepEqual(
    classified.actions.map((action) => action.command),
    ["alysis.locateCli", "alysis.copyCliInstallCommand", "alysis.openSetupGuide"]
  );
});

test("exhausted API credits explain billing recovery instead of a connection check", () => {
  for (const message of [
    "OpenAI Responses stream error: You have no credits remaining.",
    "You have no credits left.",
    "Your credit balance is exhausted.",
    "HTTP 429: You exceeded your current quota; please check your billing."
  ]) {
    const classified = classifyChatError(message);
    assert.equal(classified.kind, "quota_exhausted", message);
    assert.match(classified.detail, /credits.*limits/i);
    assert.equal(classified.actions.some((action) => action.command === "alysis.showBridgeHealth"), false);
    assert.equal(classified.actions.some((action) => action.label === "Choose another model"), false);
  }
});

test("explicit credit and spending codes take precedence over an HTTP rate-limit status", () => {
  for (const code of [
    "insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded", "organization_usage_limit_exceeded"
  ]) {
    assert.equal(classifyChatError(protocolError(code, "HTTP 429")).kind, "quota_exhausted", code);
  }
  assert.equal(
    classifyChatError(protocolError("rate_limit_exceeded", "Request quota reached. Retry in 30 seconds.")).kind,
    "rate_limited"
  );
});

test("a tampered bundle asks for a reinstall instead of a CLI install", () => {
  const classified = classifyChatError(protocolError(BUNDLED_RUNTIME_INVALID_CODE, "Signature mismatch."));

  assert.equal(classified.kind, "runtime_missing");
  assert.equal(
    classified.actions.some((action) => action.command === "alysis.copyCliInstallCommand"),
    false
  );
  assert.match(classified.detail, /verification/);
});

test("a managed runtime failure never points the user at a separate pipx CLI", () => {
  const classified = classifyChatError(protocolError("cli_missing", "not found"), { runtimeOrigin: "managed" });

  assert.equal(classified.kind, "runtime_missing");
  assert.deepEqual(
    classified.actions.map((action) => action.command),
    ["alysis.showBridgeHealth", "alysis.openSetupGuide"]
  );
});

test("an extension-outdated protocol mismatch says Update extension, not upgrade the CLI", () => {
  const classified = classifyChatError(
    protocolError("unsupported_protocol_version", "Unsupported Alysis Code IDE protocol version: 3."),
    { protocolDirection: "extension_outdated" }
  );

  assert.equal(classified.kind, "extension_outdated");
  assert.equal(classified.actions[0].label, "Update extension");
  assert.equal(classified.actions[0].command, "alysis.checkForUpdates");
  assert.doesNotMatch(classified.detail, /upgrade the (?:Alysis Code )?CLI/i);
});

test("an older CLI is classified as cli_outdated with an upgrade path", () => {
  const classified = classifyChatError(
    protocolError("unsupported_protocol_version", "Unsupported Alysis Code IDE protocol version: 1."),
    { protocolDirection: "cli_outdated" }
  );

  assert.equal(classified.kind, "cli_outdated");
  assert.equal(classified.actions[0].command, "alysis.copyCliUpgradeCommand");
});

test("a feature-compatibility failure is an engine version gap, not an unknown error", () => {
  const error = new Error("Run Task requires a newer Alysis Code CLI.");
  error.name = "FeatureCompatibilityError";

  assert.equal(classifyChatError(error).kind, "cli_outdated");
});

test("cancellation is an expected outcome, not a failure", () => {
  const classified = classifyChatError(protocolError("cancelled", "The job was cancelled."));

  assert.equal(classified.kind, "cancelled");
  assert.deepEqual(classified.actions, []);
});

test("workspace trust failures offer the built-in trust editor", () => {
  const classified = classifyChatError(protocolError("cli_untrusted", "CLI execution is blocked by Workspace Trust."));

  assert.equal(classified.kind, "workspace_untrusted");
  assert.equal(classified.actions[0].command, "workbench.trust.manage");
});

test("an unclassified failure keeps its meaning but loses codes, statuses, and stack frames", () => {
  const classified = classifyChatError(
    protocolError("config_error", "Subscription is not configured. See docs.")
  );

  assert.equal(classified.kind, "unknown");
  assert.equal(classified.detail, "Subscription is not configured.");
  assertNoProtocolNoise(classified.title, classified.detail);

  assert.equal(
    plainDetail("Backend blew up (internal_error)\n    at Object.<anonymous> (/tmp/x.js:1:1)"),
    "Backend blew up."
  );
  assert.equal(
    plainDetail("Upstream returned HTTP 503 Service Unavailable"),
    "Upstream returned Service Unavailable."
  );
  assert.match(plainDetail(""), /Check the Alysis Code output/);
});

test("classification runs through redaction so secrets never reach a card", () => {
  const classified = classifyChatError("Unexpected failure for Authorization: Bearer abcdefghijklmnop1234");

  assert.equal(classified.detail.includes("abcdefghijklmnop1234"), false);
  assert.match(classified.detail, /redacted/);
});

test("every kind renders a bounded, code-free card whose actions are real commands", () => {
  const samples: Array<[ChatErrorKind, unknown]> = [
    ["auth_invalid", protocolError("invalid_api_key", "bad key")],
    ["rate_limited", protocolError("rate_limited", "slow down")],
    ["quota_exhausted", protocolError("insufficient_quota", "no credit")],
    ["network_unreachable", protocolError("ECONNREFUSED", "refused")],
    ["context_overflow", protocolError("context_length_exceeded", "too long")],
    ["runtime_missing", protocolError("cli_missing", "missing")],
    ["cli_outdated", protocolError("ide_bridge_missing", "old")],
    ["extension_outdated", protocolError("unsupported_protocol_version", "newer than this extension")],
    ["workspace_untrusted", protocolError("cli_untrusted", "trust required")],
    ["cancelled", protocolError("cancelled", "stopped")],
    ["interrupted", protocolError("interrupted_indeterminate", "interrupted_indeterminate")],
    ["unknown", protocolError("weird_code", "something happened")]
  ];

  for (const [expectedKind, error] of samples) {
    const classified = classifyChatError(error);
    assert.equal(classified.kind, expectedKind);
    assert.ok(classified.title.length <= 60, `title too long: ${classified.title}`);
    assert.ok(classified.detail.length <= 220);
    assertNoProtocolNoise(classified.title, classified.detail);
    assert.ok(classified.actions.filter((action) => action.primary).length <= 1);
    for (const action of classified.actions) {
      assert.ok(action.label.length <= 24, `label too long: ${action.label}`);
      assert.ok(
        CONTRIBUTED_COMMANDS.has(action.command) || BUILT_IN_COMMANDS.has(action.command),
        `unknown command: ${action.command}`
      );
    }
  }
});

test("an interrupted approval gives recovery guidance without promising a clean rollback or automatic retry", () => {
  for (const error of ["interrupted_indeterminate", protocolError("interrupted_indeterminate", "internal task id")]) {
    const classified = classifyChatError(error);
    assert.equal(classified.kind, "interrupted");
    assert.match(classified.detail, /Some actions may have finished/);
    assert.match(classified.detail, /Pending approvals need a fresh review/);
    assert.equal(classified.actions.length, 0);
    assertNoProtocolNoise(classified.title, classified.detail);
  }
});

function assertNoProtocolNoise(title: string, detail: string): void {
  for (const text of [title, detail]) {
    assert.doesNotMatch(text, /\b[1-5]\d{2}\b/, `status code leaked: ${text}`);
    assert.doesNotMatch(text, /\b[a-z]+_[a-z_]+\b/, `protocol code leaked: ${text}`);
    assert.doesNotMatch(text, /\s+at\s+\S+:\d+/, `stack frame leaked: ${text}`);
  }
}

function protocolError(code: string, message: string, details?: Record<string, unknown>): Error {
  return Object.assign(new Error(message), { code, details });
}

function contributedCommandIds(): Set<string> {
  const manifest = JSON.parse(
    readFileSync(resolve(__dirname, "../..", "package.json"), "utf8")
  ) as { contributes?: { commands?: Array<{ command?: string }> } };
  return new Set(
    (manifest.contributes?.commands ?? [])
      .map((entry) => entry.command)
      .filter((command): command is string => typeof command === "string")
  );
}
