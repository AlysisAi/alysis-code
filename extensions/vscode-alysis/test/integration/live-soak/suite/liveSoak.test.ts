import * as assert from "node:assert/strict";

import * as vscode from "vscode";

import type { AlysisExtensionApi } from "../../../../src/extension";
import {
  restrictLiveQaEnvironment,
  safeTextEvidence,
  takeEnvironmentSecret
} from "../../liveQaSecurity";

const durationMs = Number(process.env.ALYSIS_LIVE_SOAK_DURATION_MS ?? 930_000);
const smokeMode = process.env.ALYSIS_LIVE_SOAK_SMOKE === "1";
// Xiaomi MiMo's public endpoint is the default live QA target, configured purely through the
// generic provider/baseUrl/defaultModel settings — nothing here is provider-specific.
const liveProvider = "Xiaomi MiMo";
const liveModel = "mimo-v2.5-pro";
const liveBaseUrl = "https://api.xiaomimimo.com/v1";
const liveApiKeyEnvironment = "ALYSIS_LIVE_API_KEY";
type ExtensionTestApi = NonNullable<AlysisExtensionApi["test"]>;

suite("Alysis Code sustained live provider soak", () => {
  test("streams real model responses continuously for at least fifteen minutes", async () => {
    assert.equal(process.env.ALYSIS_LIVE_SOAK, "1");
    if (!smokeMode) {
      assert.ok(durationMs >= 900_000, `live soak must be at least fifteen minutes; got ${durationMs}`);
    }
    const cliPath = process.env.ALYSIS_LIVE_CLI_PATH?.trim();
    let apiKey = takeEnvironmentSecret(liveApiKeyEnvironment);
    restrictLiveQaEnvironment(process.env, apiKey);
    assert.ok(cliPath, "live CLI must be configured");
    let testApi: ExtensionTestApi | undefined;
    try {
      const config = vscode.workspace.getConfiguration("alysis");
      await config.update("cliPath", cliPath, vscode.ConfigurationTarget.Global);
      await config.update("defaultMode", "readonly", vscode.ConfigurationTarget.Global);
      await config.update("defaultModel", liveModel, vscode.ConfigurationTarget.Global);
      await config.update("baseUrl", liveBaseUrl, vscode.ConfigurationTarget.Global);
      await config.update("provider", liveProvider, vscode.ConfigurationTarget.Global);

      const extension = vscode.extensions.getExtension("alysisai.vscode-alysis") as
        | vscode.Extension<AlysisExtensionApi | undefined>
        | undefined;
      assert.ok(extension, "Alysis Code extension must be discoverable.");
      const exports = await extension.activate();
      assert.equal(extension.isActive, true, "Alysis Code extension must be active.");
      assert.ok(exports?.test, "ExtensionMode.Test API must be available.");
      const activeTestApi: ExtensionTestApi = exports.test;
      testApi = activeTestApi;
      await activeTestApi.whenReady();
      await activeTestApi.setConfiguredApiKey(apiKey);
      assert.equal(
        await activeTestApi.hasConfiguredApiKey(),
        true,
        "SecretStorage must contain the configured API key."
      );
      await vscode.commands.executeCommand("alysis.openChat");
      console.log(
        JSON.stringify({
          evidence: "extension_activation",
          extensionId: extension.id,
          extensionActive: extension.isActive,
          provider: liveProvider,
          model: liveModel,
          cliPathMatchesConfiguredVenv: cliPath.endsWith("\\.venv\\Scripts\\alysis.exe"),
          statusBarEvidence: safeTextEvidence(
            JSON.stringify(activeTestApi.state().statusBar ?? null)
          )
        })
      );

      const startedAt = Date.now();
      const deadline = startedAt + durationMs;
      const latencies: number[] = [];
      let successfulRequests = 0;
      let observedErrors = 0;
      while (Date.now() < deadline) {
        const iteration = successfulRequests + 1;
        const marker = `ALYSIS_LIVE_SOAK_${iteration}`;
        const requestStarted = Date.now();
        const itemCountBeforeRequest = activeTestApi.state().chat?.items.length ?? 0;
        await activeTestApi.submitChatText(
          `In this repository, inspect README.md without changing any files. ` +
            `This is automated sustained live QA request ${iteration}. ` +
            `After the inspection, reply with exactly ${marker} and do not add punctuation.`
        );
        const requestDeadline = Date.now() + 150_000;
        let response: { text: string } | undefined;
        while (Date.now() < requestDeadline) {
          const state = activeTestApi.state();
          const newItems = (state.chat?.items ?? []).slice(itemCountBeforeRequest);
          const error = [...newItems].reverse().find((item) => item.kind === "error");
          if (error) {
            observedErrors += 1;
          }
          assert.equal(
            error,
            undefined,
            `live request ${iteration} returned a redacted error: ${JSON.stringify(
              safeTextEvidence(error?.text ?? "")
            )}`
          );
          const assistant = [...newItems]
            .reverse()
            .find((item) => item.kind === "assistant" && item.text.trim().length > 0);
          if (assistant && state.chat?.activeJobId === null) {
            response = { text: assistant.text };
            break;
          }
          await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
        }
        assert.ok(response, `timed out waiting for a non-empty live response to ${marker}`);
        assert.equal(
          response.text.includes(marker),
          true,
          `live response ${iteration} did not contain ${marker}; redacted evidence=${JSON.stringify(
            safeTextEvidence(response.text)
          )}`
        );
        const latencyMs = Date.now() - requestStarted;
        latencies.push(latencyMs);
        successfulRequests += 1;
        console.log(
          JSON.stringify({
            evidence: "real_model_response",
            request: iteration,
            requestedMarker: marker,
            markerMatched: response.text.includes(marker),
            responseEvidence: safeTextEvidence(response.text),
            latencyMs,
            elapsedMs: Date.now() - startedAt,
            errorsObserved: observedErrors,
            activeJobPresent: Boolean(activeTestApi.state().chat?.activeJobId),
            statusBarEvidence: safeTextEvidence(
              JSON.stringify(activeTestApi.state().statusBar ?? null)
            )
          })
        );
      }

      const elapsedMs = Date.now() - startedAt;
      const sorted = [...latencies].sort((a, b) => a - b);
      const evidence = {
        evidence: "fifteen_minute_live_provider_soak",
        elapsedMs,
        successfulRequests,
        observedErrors,
        minLatencyMs: sorted[0],
        medianLatencyMs: sorted[Math.floor(sorted.length / 2)],
        maxLatencyMs: sorted[sorted.length - 1],
        finalStatusBarEvidence: safeTextEvidence(
          JSON.stringify(activeTestApi.state().statusBar ?? null)
        )
      };
      console.log(JSON.stringify(evidence));
      if (!smokeMode) {
        assert.ok(elapsedMs >= 900_000, `live soak ended early after ${elapsedMs}ms`);
        assert.ok(successfulRequests >= 10, `only ${successfulRequests} real requests completed`);
      } else {
        assert.ok(successfulRequests >= 1, "smoke mode did not complete a real request");
      }
      assert.equal(observedErrors, 0, `observed ${observedErrors} extension errors`);
      assert.equal(activeTestApi.state().chat?.activeJobId, null);
    } finally {
      if (testApi) {
        await testApi.clearConfiguredApiKey();
      }
      apiKey = "";
    }
  });
});
