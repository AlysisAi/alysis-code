import * as vscode from "vscode";

import { redactForDisplay, type AlysisConfig } from "../client/CliDiscovery";
import { type AlysisBridgeClient } from "../client/AlysisBridgeClient";
import { isStorableProfileName, type AlysisSecretStore } from "../secrets/secretStorage";

type ProviderConfigBridge = Pick<
  AlysisBridgeClient,
  "ensureStarted" | "profileList" | "supportsMethod"
> &
  Partial<Pick<AlysisBridgeClient, "doctorProvidersLive" | "isRunning">>;
type ProviderAction = "openModels" | "setKey" | "clearKey" | "settings";

export function registerConfigureProviderCommand(
  context: vscode.ExtensionContext,
  secrets: AlysisSecretStore,
  bridge: ProviderConfigBridge,
  getConfig: () => AlysisConfig,
  ensureCredentials?: () => Promise<void>
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.configureProvider", async () => {
      const selection = await vscode.window.showQuickPick(
        [
          {
            label: "Connect a provider and model",
            description: "Pick a provider, store a key, and choose a model - all right here",
            action: "openModels" as ProviderAction
          },
          {
            label: "Add or update API key",
            description: "Stored securely by VS Code · applies to the current connection",
            action: "setKey" as ProviderAction
          },
          {
            label: "Remove saved API key",
            description: "Choose a saved provider key to forget",
            action: "clearKey" as ProviderAction
          },
          {
            label: "Open all Alysis Code settings",
            description: "Advanced",
            action: "settings" as ProviderAction
          }
        ],
        {
          title: "AI connection",
          placeHolder: "What would you like to configure?",
          matchOnDescription: true
        }
      );

      if (!selection) {
        return;
      }

      if (selection.action === "openModels") {
        // Runs the inline picker instead of relocating the user to the sidebar. It drives the same
        // catalog controller, so the key still lands on the profile it belongs to - which is what
        // the CLI actually reads for anything other than the default/openai profile.
        await vscode.commands.executeCommand("alysis.pickModel");
        return;
      }

      if (selection.action === "setKey") {
        const apiKey = await vscode.window.showInputBox({
          title: "Add API key",
          prompt: "VS Code stores this securely. It is never written to your project settings.",
          password: true,
          ignoreFocusOut: true,
          validateInput: (value) => {
            const normalized = value.trim();
            if (normalized.length === 0) {
              return "API key is required.";
            }
            return undefined;
          }
        });
        if (apiKey === undefined) {
          return;
        }
        await saveKeyForCurrentProvider(secrets, bridge, getConfig, apiKey.trim(), ensureCredentials);
        return;
      }

      if (selection.action === "clearKey") {
        await removeSavedApiKey(secrets, bridge, ensureCredentials);
        return;
      }

      if (selection.action === "settings") {
        await vscode.commands.executeCommand("workbench.action.openSettings", "alysis");
      }
    })
  );
}

async function removeSavedApiKey(
  secrets: AlysisSecretStore,
  bridge: ProviderConfigBridge,
  ensureCredentials?: () => Promise<void>
): Promise<void> {
  try {
    const profiles = await secrets.listProfilesWithKeys();
    const choices: Array<vscode.QuickPickItem & { profile?: string }> = profiles.map((profile) => ({
      label: profile,
      description: "Saved provider key",
      profile
    }));
    if ((await secrets.getApiKey())?.trim()) {
      choices.push({ label: "Legacy API key", description: "Shared key from an earlier setup" });
    }
    if (choices.length === 0) {
      await vscode.window.showInformationMessage("This extension has no saved API keys.");
      return;
    }
    const selected = choices.length === 1 ? choices[0] : await vscode.window.showQuickPick(choices, {
      title: "Remove saved API key",
      placeHolder: "Choose the provider key to remove",
      ignoreFocusOut: true
    });
    if (!selected) return;
    if (selected.profile) {
      await secrets.deleteProfileApiKey(selected.profile);
    } else {
      await secrets.deleteApiKey();
    }
    const removed = selected.profile
      ? `Saved API key for ${selected.profile} removed.`
      : "Legacy API key removed.";
    // A live child retains its original environment until it restarts. Refresh only an existing
    // connection; removing a stored secret must also work when the CLI is missing or offline.
    if (bridge.isRunning?.() && ensureCredentials) {
      try {
        await ensureCredentials();
      } catch {
        await vscode.window.showWarningMessage(
          `${removed} The current connection could not be refreshed. Stop any active task and restart the connection to stop using its previous credentials.`
        );
        return;
      }
    }
    await vscode.window.showInformationMessage(removed);
  } catch (error) {
    await vscode.window.showErrorMessage(
      `Could not remove the saved API key: ${redactForDisplay(error instanceof Error ? error.message : String(error))}`
    );
  }
}

/**
 * Save an API key where the CLI will actually read it. For any active profile other than the
 * legacy default, the CLI ignores the global ALYSIS_API_KEY entirely — the old behavior of
 * always writing the global secret meant the sidebar kept saying "No key yet" for a key the user
 * had just saved here. Falls back to the legacy global secret only when no active profile can be
 * resolved, so the key is never dropped.
 */
async function saveKeyForCurrentProvider(
  secrets: AlysisSecretStore,
  bridge: ProviderConfigBridge,
  getConfig: () => AlysisConfig,
  apiKey: string,
  ensureCredentials?: () => Promise<void>
): Promise<void> {
  let profile = "";
  let keyEnvVar = "";
  try {
    await bridge.ensureStarted(getConfig(), { stripApiKey: true });
    const result = await bridge.profileList();
    const active = typeof result.active_profile === "string" ? result.active_profile.trim() : "";
    if (active && isStorableProfileName(active)) {
      profile = active;
      const rows = Array.isArray(result.profiles) ? result.profiles : [];
      for (const raw of rows) {
        if (raw && typeof raw === "object" && (raw as Record<string, unknown>).name === active) {
          const envVar = (raw as Record<string, unknown>).key_env_var;
          keyEnvVar = typeof envVar === "string" ? envVar.trim() : "";
          break;
        }
      }
    }
  } catch {
    // Resolving the active profile is best-effort; the legacy path below still stores the key.
  }
  if (!profile) {
    await secrets.setApiKey(apiKey);
    if (getConfig().provider.trim().length === 0) {
      // No active profile and no configured provider: the key is kept safe, but there is nothing
      // to connect it to yet. Point at the generic catalog picker — never at a hardcoded vendor.
      const choice = await vscode.window.showInformationMessage(
        "API key saved securely by VS Code. No provider is connected yet — pick one in Models and Providers.",
        "Open Models and Providers"
      );
      if (choice === "Open Models and Providers") {
        await vscode.commands.executeCommand("alysis.showModels");
      }
      return;
    }
    await vscode.window.showInformationMessage("API key saved securely by VS Code.");
    return;
  }
  await secrets.setProfileApiKey(profile, apiKey, keyEnvVar);
  const live = bridge.doctorProvidersLive?.bind(bridge);
  if (!live || !ensureCredentials) {
    await vscode.window.showInformationMessage(`API key for ${profile} saved securely.`);
    return;
  }
  try {
    await ensureCredentials();
    if (!bridge.supportsMethod("doctor.providers.live")) {
      await vscode.window.showInformationMessage(`API key for ${profile} saved securely.`);
      return;
    }
    const check = await live({ allow_live: true });
    if (check.ok === true) {
      await vscode.window.showInformationMessage(`API key for ${profile} saved and working.`);
      return;
    }
    const message = redactForDisplay(String(check.validation?.message ?? "")).trim();
    const choice = await vscode.window.showWarningMessage(
      `API key for ${profile} saved, but the key check failed: ${message || "the provider rejected the request."}`,
      "Replace key"
    );
    if (choice === "Replace key") {
      await vscode.commands.executeCommand("alysis.configureProvider");
    }
  } catch {
    // The key is stored either way; a check that could not run is not evidence of a bad key.
    await vscode.window.showInformationMessage(`API key for ${profile} saved securely.`);
  }
}
