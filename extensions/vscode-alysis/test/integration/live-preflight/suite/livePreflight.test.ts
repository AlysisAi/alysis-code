import * as assert from "node:assert/strict";

import * as vscode from "vscode";

import type { AlysisExtensionApi } from "../../../../src/extension";
import {
  restrictLiveQaEnvironment,
  safeTextEvidence,
  takeEnvironmentSecret
} from "../../liveQaSecurity";

const responseMarker = "ALYSIS_LIVE_PREFLIGHT_OK_726";
// Xiaomi MiMo's public endpoint is the default live QA target, configured purely through the
// generic provider/baseUrl/defaultModel settings — nothing here is provider-specific.
const liveProvider = "Xiaomi MiMo";
const liveModel = "mimo-v2.5-pro";
const liveBaseUrl = "https://api.xiaomimimo.com/v1";
const liveApiKeyEnvironment = "ALYSIS_LIVE_API_KEY";
type ExtensionTestApi = NonNullable<AlysisExtensionApi["test"]>;

suite("Alysis Code live provider preflight", () => {
  test("activates and returns a real model response through ChatController", async () => {
    assert.equal(process.env.ALYSIS_LIVE_PREFLIGHT, "1");
    const cliPath = process.env.ALYSIS_LIVE_CLI_PATH?.trim();
    let apiKey = takeEnvironmentSecret(liveApiKeyEnvironment);
    restrictLiveQaEnvironment(process.env, apiKey);
    assert.ok(cliPath, "ALYSIS_LIVE_CLI_PATH must be configured.");
    let testApi: ExtensionTestApi | undefined;

    try {
      const config = vscode.workspace.getConfiguration("alysis");
      await config.update("cliPath", cliPath, vscode.ConfigurationTarget.Global);
      await config.update("defaultMode", "readonly", vscode.ConfigurationTarget.Global);
      await config.update("defaultModel", liveModel, vscode.ConfigurationTarget.Global);
      await config.update("baseUrl", liveBaseUrl, vscode.ConfigurationTarget.Global);
      await config.update("provider", liveProvider, vscode.ConfigurationTarget.Global);
      const liveConfig = vscode.workspace.getConfiguration("alysis");
      assert.equal(liveConfig.get<string>("defaultModel"), liveModel);
      assert.equal(liveConfig.get<string>("baseUrl"), liveBaseUrl);
      assert.equal(liveConfig.get<string>("provider"), liveProvider);

      const extension = vscode.extensions.getExtension("alysisai.vscode-alysis") as
        | vscode.Extension<AlysisExtensionApi | undefined>
        | undefined;
      assert.ok(extension, "Alysis Code extension must be discoverable.");
      const exports = await extension.activate();
      assert.equal(extension.isActive, true);
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
      await activeTestApi.submitChatText(
        `This is an automated live preflight. Reply with exactly ${responseMarker} and do not call tools.`
      );

      const deadline = Date.now() + 150_000;
      let latest = activeTestApi.state();
      while (Date.now() < deadline) {
        latest = activeTestApi.state();
        const error = latest.chat?.items.find((item) => item.kind === "error");
        if (error) {
          assert.fail(
            `Live extension run returned a redacted error: ${JSON.stringify(safeTextEvidence(error.text))}`
          );
        }
        const assistant = latest.chat?.items.find(
          (item) => item.kind === "assistant" && item.text.trim().length > 0
        );
        if (assistant && latest.chat?.activeJobId === null) {
          assert.ok(
            assistant.text.trim().length > 0,
            "The live provider must return a non-empty assistant response through the extension."
          );
          console.log(
            JSON.stringify({
              evidence: "real_model_response",
              sessionId: latest.chat?.sessionId,
              requestedMarker: responseMarker,
              markerMatched: new RegExp(responseMarker).test(assistant.text),
              responseEvidence: safeTextEvidence(assistant.text),
              statusBarEvidence: safeTextEvidence(JSON.stringify(latest.statusBar ?? null)),
              activeJobPresent: Boolean(latest.chat?.activeJobId)
            })
          );
          return;
        }
        await new Promise((resolvePromise) => setTimeout(resolvePromise, 250));
      }
      const items = latest.chat?.items ?? [];
      assert.fail(
        `Timed out waiting for a live provider response. Latest redacted evidence: ${JSON.stringify({
          itemCount: items.length,
          assistantCount: items.filter((item) => item.kind === "assistant").length,
          errorCount: items.filter((item) => item.kind === "error").length,
          activeJobPresent: Boolean(latest.chat?.activeJobId),
          statusBarEvidence: safeTextEvidence(JSON.stringify(latest.statusBar ?? null))
        })}`
      );
    } finally {
      if (testApi) {
        await testApi.clearConfiguredApiKey();
      }
      apiKey = "";
    }
  });
});
