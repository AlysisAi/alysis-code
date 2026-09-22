import * as vscode from "vscode";

/**
 * One-time migration of `sylliptor.*` settings to their `alysis.*` equivalents.
 *
 * VS Code keys settings by their full dotted name, so the rename orphans every
 * value a user had configured — `cliPath` in particular, without which the
 * extension cannot find a CLI installed outside PATH. Contributed settings that
 * no longer exist are also flagged by VS Code as "Unknown Configuration
 * Setting", which looks like a bug in the extension.
 *
 * Values are copied, not moved: the legacy keys stay in settings.json so that
 * downgrading the extension still works. They are inert once the new keys
 * exist. A workspace value migrates to workspace scope and a user value to user
 * scope, so a repo-specific override never leaks into global settings.
 */

const LEGACY_SECTION = "sylliptor";
const CURRENT_SECTION = "alysis";
const MIGRATION_STATE_KEY = "alysis.migratedLegacySettings.v1";

/** Settings that existed under the legacy section and still exist today. */
const MIGRATABLE_KEYS = [
  "cliPath",
  "defaultMode",
  "defaultModel",
  "baseUrl",
  "autoStartBridge",
  "enableForge",
  "forgeExecute",
  "forgeExecuteMaxSteps",
  "forgeExecuteNoLog"
] as const;

export interface LegacySettingsMigrationResult {
  migrated: string[];
  alreadyDone: boolean;
}

export async function migrateLegacySettings(
  context: vscode.ExtensionContext,
  configProvider: typeof vscode.workspace.getConfiguration = vscode.workspace.getConfiguration
): Promise<LegacySettingsMigrationResult> {
  if (context.globalState.get<boolean>(MIGRATION_STATE_KEY) === true) {
    return { migrated: [], alreadyDone: true };
  }

  const legacy = configProvider(LEGACY_SECTION);
  const current = configProvider(CURRENT_SECTION);
  const migrated: string[] = [];

  for (const key of MIGRATABLE_KEYS) {
    const legacyInfo = legacy.inspect(key);
    if (!legacyInfo) {
      continue;
    }
    const currentInfo = current.inspect(key);

    // Workspace scope first: it is the more specific of the two, and a user who
    // set both should keep that distinction.
    if (
      legacyInfo.workspaceValue !== undefined &&
      currentInfo?.workspaceValue === undefined
    ) {
      await current.update(key, legacyInfo.workspaceValue, vscode.ConfigurationTarget.Workspace);
      migrated.push(`${CURRENT_SECTION}.${key} (workspace)`);
    }

    if (legacyInfo.globalValue !== undefined && currentInfo?.globalValue === undefined) {
      await current.update(key, legacyInfo.globalValue, vscode.ConfigurationTarget.Global);
      migrated.push(`${CURRENT_SECTION}.${key} (user)`);
    }
  }

  await context.globalState.update(MIGRATION_STATE_KEY, true);
  return { migrated, alreadyDone: false };
}
