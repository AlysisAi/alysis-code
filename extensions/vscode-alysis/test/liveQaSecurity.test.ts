import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  allowlistedQaRuntimeEnvironment,
  assertSecretAbsentFromTrees,
  liveExtensionHostEnvironment,
  restrictLiveQaEnvironment,
  safeErrorEvidence,
  safeTextEvidence,
  takeEnvironmentSecret,
  validateLiveQaEvidenceIdentifier,
  validateLiveQaProviderEndpoint
} from "./integration/liveQaSecurity";

test("generic QA runtime environment excludes release and provider credentials", () => {
  const environment = allowlistedQaRuntimeEnvironment({
    PATH: "safe-path",
    SYSTEMROOT: "safe-system-root",
    GH_TOKEN: "github-secret",
    AWS_SESSION_TOKEN: "aws-secret",
    OPENAI_API_KEY: "provider-secret",
    ALYSIS_MANAGED_CLI_SIGNING_KEY_PEM: "private-signing-key"
  });

  assert.deepEqual(environment, { PATH: "safe-path", SYSTEMROOT: "safe-system-root" });
});

test("takeEnvironmentSecret consumes the QA credential before returning it", () => {
  const name = "ALYSIS_TEST_LIVE_API_KEY";
  process.env[name] = "  qa-secret-value  ";

  const value = takeEnvironmentSecret(name);

  assert.equal(value, "qa-secret-value");
  assert.equal(process.env[name], undefined);
});

test("takeEnvironmentSecret deletes an empty QA credential before failing", () => {
  const name = "ALYSIS_TEST_EMPTY_API_KEY";
  process.env[name] = "   ";

  assert.throws(() => takeEnvironmentSecret(name), /must be configured/);
  assert.equal(process.env[name], undefined);
});

test("live provider endpoint accepts only the exact reviewed public HTTPS origin", async () => {
  const endpoint = await validateLiveQaProviderEndpoint(
    "https://api.provider.example/v1",
    "https://api.provider.example",
    async () => ["93.184.216.34"]
  );

  assert.deepEqual(endpoint, {
    baseUrl: "https://api.provider.example/v1",
    origin: "https://api.provider.example"
  });
});

test("live provider endpoint rejects an origin different from the protected reviewed value", async () => {
  await assert.rejects(
    validateLiveQaProviderEndpoint(
      "https://api.unreviewed.example/v1",
      "https://api.provider.example",
      async () => ["93.184.216.34"]
    ),
    /does not match the approved HTTPS origin/
  );
});

test("live provider endpoint rejects unsafe URL forms before credential use", async () => {
  const cases = [
    ["http://api.provider.example/v1", "https://api.provider.example"],
    ["https://user@api.provider.example/v1", "https://api.provider.example"],
    ["https://api.provider.example/v1?key=value", "https://api.provider.example"],
    ["https://api.provider.example/v1#fragment", "https://api.provider.example"],
    ["https://api.provider.example:8443/v1", "https://api.provider.example:8443"],
    ["https://api.provider.example/v1", "https://api.provider.example/"],
    ["https://localhost/v1", "https://localhost"],
    ["https://127.0.0.1/v1", "https://127.0.0.1"],
    ["https://10.0.0.1/v1", "https://10.0.0.1"]
  ] as const;
  for (const [baseUrl, approvedOrigin] of cases) {
    await assert.rejects(
      validateLiveQaProviderEndpoint(baseUrl, approvedOrigin, async () => ["93.184.216.34"])
    );
  }
  await assert.rejects(
    validateLiveQaProviderEndpoint(
      "https://api.provider.example/v1",
      "https://api.provider.example",
      async () => ["169.254.169.254"]
    ),
    /non-public address/
  );
});

test("live provider and model evidence identifiers are compact and credential-safe", () => {
  assert.equal(
    validateLiveQaEvidenceIdentifier("provider-name", "provider", 128),
    "provider-name"
  );
  assert.equal(
    validateLiveQaEvidenceIdentifier("org/model:v2", "model", 256),
    "org/model:v2"
  );
  assert.throws(
    () => validateLiveQaEvidenceIdentifier("api_key=value", "provider", 128),
    /safe retained evidence/
  );
  assert.throws(
    () => validateLiveQaEvidenceIdentifier("m".repeat(257), "model", 256),
    /safe retained evidence/
  );
});

test("live Extension Host environment uses a strict runtime allowlist", () => {
  const secret = "qa-secret-not-for-child-inheritance";
  const environment = liveExtensionHostEnvironment(
    {
      PATH: "safe-path",
      GH_TOKEN: "github-secret",
      AWS_SECRET_ACCESS_KEY: "aws-secret",
      OPENAI_API_KEY: "provider-secret",
      ALYSIS_MANAGED_CLI_SIGNING_KEY_PEM: "private-signing-key",
      UNRELATED_VARIABLE: "not-needed-by-the-runtime",
      DUPLICATE_VALUE: `prefix-${secret}-suffix`
    },
    "ALYSIS_LIVE_API_KEY",
    secret
  );

  assert.equal(environment.PATH, "safe-path");
  assert.equal(environment.GH_TOKEN, undefined);
  assert.equal(environment.AWS_SECRET_ACCESS_KEY, undefined);
  assert.equal(environment.OPENAI_API_KEY, undefined);
  assert.equal(environment.ALYSIS_MANAGED_CLI_SIGNING_KEY_PEM, undefined);
  assert.equal(environment.UNRELATED_VARIABLE, undefined);
  assert.equal(environment.DUPLICATE_VALUE, undefined);
  assert.equal(environment.ALYSIS_LIVE_API_KEY, secret);
});

test("restrictLiveQaEnvironment removes every non-allowlisted process variable in place", () => {
  const environment: NodeJS.ProcessEnv = {
    PATH: "safe-path",
    LANG: "en_US.UTF-8",
    SERVICE_PASSWORD: "password-value",
    ALYSIS_MANAGED_CLI_SIGNING_KEY_PEM: "private-signing-key",
    AWS_SESSION_TOKEN: "aws-token",
    GH_TOKEN: "github-token",
    OPENAI_API_KEY: "provider-key",
    ARBITRARY_BUILD_METADATA: "not-runtime-state",
    SAFE_DUPLICATE: "qa-secret"
  };

  restrictLiveQaEnvironment(environment, "qa-secret");

  assert.deepEqual(environment, { PATH: "safe-path", LANG: "en_US.UTF-8" });
});

test("safe text and error evidence never contain the original body", () => {
  const body = "provider body that must not be retained";
  const textEvidence = safeTextEvidence(body);
  const errorEvidence = safeErrorEvidence(new TypeError(body));
  const rendered = JSON.stringify({ textEvidence, errorEvidence });

  assert.equal(textEvidence.length, body.length);
  assert.match(textEvidence.sha256, /^[0-9a-f]{64}$/);
  assert.equal(errorEvidence.name, "TypeError");
  assert.equal(errorEvidence.length, body.length);
  assert.equal(rendered.includes(body), false);
});

test("artifact scanner accepts clean runner-owned trees", () => {
  withTemporaryTree((root) => {
    mkdirSync(join(root, "logs"));
    writeFileSync(join(root, "logs", "extension.log"), "redacted output", "utf8");

    assert.doesNotThrow(() => assertSecretAbsentFromTrees([root], "exact-qa-secret"));
  });
});

test("artifact scanner detects UTF-8 and UTF-16LE exact secret bytes without echoing them", () => {
  const secret = "exact-qa-secret-726";
  for (const encoding of ["utf8", "utf16le"] as const) {
    withTemporaryTree((root) => {
      const output = join(root, `extension-${encoding}.log`);
      writeFileSync(output, `prefix ${secret} suffix`, encoding);

      assert.throws(
        () => assertSecretAbsentFromTrees([root], secret),
        (error: unknown) => {
          assert.ok(error instanceof Error);
          assert.match(error.message, /secret leak check failed/);
          assert.equal(error.message.includes(secret), false);
          return true;
        }
      );
    });
  }
});

test("live suites consume and scrub the QA secret before activating Alysis Code", () => {
  for (const relativePath of [
    "live-preflight/suite/livePreflight.test.ts",
    "live-soak/suite/liveSoak.test.ts"
  ]) {
    const source = liveIntegrationSource(relativePath);
    const takeIndex = source.indexOf("takeEnvironmentSecret(liveApiKeyEnvironment)");
    const scrubIndex = source.indexOf("restrictLiveQaEnvironment(process.env, apiKey)");
    const activationIndex = source.indexOf("await extension.activate()");

    assert.ok(takeIndex >= 0, `${relativePath} must consume the QA secret`);
    assert.ok(scrubIndex > takeIndex, `${relativePath} must scrub duplicate inherited secrets`);
    assert.ok(activationIndex > scrubIndex, `${relativePath} must scrub before extension activation`);
    assert.doesNotMatch(source, /response:\s*(?:assistant|response)\.text/);
    assert.doesNotMatch(source, /statusBar:\s*(?:latest|testApi|activeTestApi)/);
    assert.doesNotMatch(source, /Latest state:/);
    assert.doesNotMatch(source, /console\.error\(error\)/);
  }
});

test("live runners hand off only a scrubbed credential and scan isolated artifacts", () => {
  for (const relativePath of ["live-preflight/runTest.ts", "live-soak/runTest.ts"]) {
    const source = liveIntegrationSource(relativePath);
    const takeIndex = source.indexOf("takeEnvironmentSecret(LIVE_API_KEY_ENVIRONMENT)");
    const restrictIndex = source.indexOf(
      "restrictLiveQaEnvironment(process.env, qaSecret)"
    );
    const environmentIndex = source.indexOf("liveExtensionHostEnvironment(");
    const runIndex = source.indexOf("await runVsCodeExtensionHostTests(");
    const scanIndex = source.indexOf("assertSecretAbsentFromTrees(");

    assert.ok(takeIndex >= 0, `${relativePath} must consume the parent QA secret`);
    assert.ok(restrictIndex > takeIndex, `${relativePath} must restrict the parent environment`);
    assert.ok(environmentIndex > restrictIndex, `${relativePath} must build an allowlisted child env`);
    assert.ok(runIndex > environmentIndex, `${relativePath} must scrub before Extension Host launch`);
    assert.ok(scanIndex > runIndex, `${relativePath} must scan artifacts after the live run`);
    assert.match(source, /ALYSIS_CONFIG_DIR = join\(userDataDir, "alysis-config"\)/);
    assert.match(source, /ALYSIS_DATA_DIR = join\(userDataDir, "alysis-data"\)/);
    assert.doesNotMatch(source, /extensionTestsEnv:\s*\{\s*\.\.\.process\.env/);
    assert.doesNotMatch(source, /console\.error\(error\)/);
  }
});

test("production dogfood launches its Extension Host with an allowlisted environment", () => {
  const source = liveIntegrationSource("production/runTest.ts");

  assert.match(source, /allowlistedQaRuntimeEnvironment\(process\.env\)/);
  assert.doesNotMatch(source, /extensionTestsEnv:\s*\{\s*\.\.\.process\.env/);
  assert.match(source, /ALYSIS_EXPECTED_PLATFORM_TARGET: expectedTarget/);
  assert.match(source, /ALYSIS_NATIVE_SIGNATURE_CHECK: nativeSignatureCheck/);
  assert.match(source, /ALYSIS_RELEASE_SIGNATURE_CHECK: releaseSignatureCheck/);
});

test("installed production live-provider runner consumes the secret and reuses one profile", () => {
  const source = liveIntegrationSource("production-live-provider/runTest.ts");
  const takeIndex = source.indexOf("takeEnvironmentSecret(LIVE_API_KEY_ENVIRONMENT)");
  const originIndex = source.indexOf("await validateLiveQaProviderEndpoint(");
  const restrictIndex = source.indexOf("restrictLiveQaEnvironment(process.env, qaSecret)");
  const installIndex = source.indexOf('"--install-extension"');
  const runIndex = source.indexOf("await runVsCodeExtensionHostTests({");
  const scanIndex = source.indexOf("assertSecretAbsentFromTrees([tempRoot], qaSecret)");

  assert.ok(originIndex >= 0, "runner must validate the protected reviewed provider origin");
  assert.ok(takeIndex > originIndex, "origin validation must precede protected secret consumption");
  assert.ok(restrictIndex > takeIndex, "runner must scrub its environment immediately");
  assert.ok(installIndex > restrictIndex, "scrubbing must precede candidate install and activation");
  assert.ok(runIndex > installIndex, "installed VSIX must launch only after clean install");
  assert.ok(scanIndex > runIndex, "owned artifacts must be scanned after live execution");
  assert.match(source, /for \(const phase of \["chat", "restart"\] as const\)/);
  assert.match(source, /workspacePath,[\s\S]*userDataDir,[\s\S]*extensionsDir/);
  assert.match(source, /requiredEnvironment\("ALYSIS_LIVE_QA_APPROVED_ORIGIN"\)/);
  assert.match(source, /\[PROFILE_NONCE_ENVIRONMENT\]: profileNonce/);
  assert.match(source, /chat\.profile_witness !== "written"/);
  assert.match(source, /restart\.profile_witness !== "observed"/);
  assert.match(source, /provider_origin: providerEndpoint\.origin/);
  // The live provider is configured purely through generic settings; no origin may trigger a
  // provider-specific settings write.
  assert.doesNotMatch(source, /xiaomiApiPlan/);
  assert.match(source, /retained_response_content: false/);
  assert.doesNotMatch(source, /console\.error\(error\)/);
});

test("installed production driver deletes the handoff before target activation and clears storage", () => {
  const source = liveIntegrationSource(
    "production-live-provider/driver/testRunner.ts"
  );
  const takeIndex = source.indexOf("takeEnvironmentSecret(LIVE_API_KEY_ENVIRONMENT)");
  const activationIndex = source.indexOf("await extension.activate()");
  const clearIndex = source.indexOf("await qa.clearProviderSecret()");

  assert.ok(takeIndex >= 0, "driver must consume the handoff secret");
  assert.ok(activationIndex > takeIndex, "secret consumption must precede target activation");
  assert.ok(clearIndex > activationIndex, "SecretStorage must be cleared after activation");
  assert.match(source, /assert\.equal\(extension\.isActive, false/);
  assert.match(source, /exports\.test, undefined/);
  assert.match(source, /driver\.writeProfileWitness\(profileNonce\)/);
  assert.match(source, /driver\.readProfileWitness\(\)/);
  assert.match(source, /profile_witness: profileWitness/);
  assert.doesNotMatch(source, /response(?:Text|Content|Body)\s*:/i);
  assert.doesNotMatch(source, /console\.(?:log|error)\(/);
});

function withTemporaryTree(callback: (root: string) => void): void {
  const root = mkdtempSync(join(tmpdir(), "alysis-live-security-test-"));
  try {
    callback(root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

function liveIntegrationSource(relativePath: string): string {
  const packageRoot = join(__dirname, "..", "..");
  return readFileSync(join(packageRoot, "test", "integration", relativePath), "utf8");
}
