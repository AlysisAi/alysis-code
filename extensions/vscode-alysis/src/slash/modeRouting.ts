import { SlashMode } from "./SlashCommandRouter";

export interface SlashModeRoutingDeps {
  activeSessionId(): string | undefined;
  executeBackendAction(actionId: string, args: string): Promise<string>;
  updateDefaultMode(mode: SlashMode): Promise<void>;
}

export async function routeSlashModeChange(
  mode: SlashMode,
  deps: SlashModeRoutingDeps
): Promise<string | void> {
  if (deps.activeSessionId()) {
    return deps.executeBackendAction("session.setMode", mode);
  }
  await deps.updateDefaultMode(mode);
  return undefined;
}
