import type * as vscode from "vscode";

const PROFILE_WITNESS_KEY = "alysis.installedLiveProfileWitness";

export interface InstalledLiveProviderDriverApi {
  writeProfileWitness(value: string): Promise<void>;
  readProfileWitness(): string | undefined;
}

export function activate(context: vscode.ExtensionContext): InstalledLiveProviderDriverApi {
  return {
    async writeProfileWitness(value: string): Promise<void> {
      await context.globalState.update(PROFILE_WITNESS_KEY, value);
    },
    readProfileWitness(): string | undefined {
      return context.globalState.get<string>(PROFILE_WITNESS_KEY);
    }
  };
}

export function deactivate(): void {
  // No resources are owned by the driver.
}
