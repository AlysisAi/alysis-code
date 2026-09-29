import * as vscode from "vscode";

import {
  type ModelsSurfaceState,
  type ProviderCatalogController,
  type ProviderCatalogEntry,
  type ProviderConnection
} from "../providers/ProviderCatalogController";

/**
 * The catalog surface this picker drives. Deliberately the same controller the sidebar uses, so
 * connect/switch/set-model semantics, key prompting, and live verification stay in exactly one
 * place - the picker is a second front door, never a second implementation.
 */
export type ModelPickerCatalog = Pick<
  ProviderCatalogController,
  "refresh" | "snapshot" | "connect" | "useConnection" | "setModel"
>;

export interface ModelPickerDeps {
  catalog: ModelPickerCatalog;
  /**
   * Apply the model to the live session as well, when one is running. Returning a notice keeps the
   * chat transcript's feedback identical to what `/model <name>` produced before.
   */
  setSessionModel?: (model: string) => Promise<string>;
  hasActiveSession?: () => boolean;
}

export interface ModelPickerOutcome {
  /** False when the user escaped out; callers use this to stay silent rather than claim success. */
  changed: boolean;
  notice: string;
}

const CANCELLED: ModelPickerOutcome = { changed: false, notice: "" };

type Step1Pick =
  | { kind: "model-only"; connection: ProviderConnection }
  | { kind: "switch"; connection: ProviderConnection }
  | { kind: "connect"; entry: ProviderCatalogEntry };

type Step1Item = vscode.QuickPickItem & { pick?: Step1Pick };
type ModelItem = vscode.QuickPickItem & { model?: string; other?: true };

/**
 * One multi-step QuickPick that actually configures the connection: provider -> key (only when it
 * is missing) -> model. It never bounces the user into the sidebar, and it works with no active
 * session, which is the exact moment most people need it.
 */
export async function runModelPicker(
  deps: ModelPickerDeps,
  requestedModel?: string
): Promise<ModelPickerOutcome> {
  const state = await loadState(deps.catalog);
  if (!state.supported) {
    await reportUnsupported(state);
    return CANCELLED;
  }

  // `/model <name>` with a working connection stays a one-shot: no menus, same as before.
  const direct = (requestedModel ?? "").trim();
  if (direct.length > 0) {
    const active = state.connections.find((entry) => entry.active);
    if (active) {
      return applyModel(deps, direct);
    }
    void vscode.window.showInformationMessage(
      `No provider is connected yet, so ${direct} has nowhere to run. Pick a provider first.`
    );
  }

  const first = await pickProviderStep(state);
  if (!first) {
    return CANCELLED;
  }

  if (first.kind === "model-only") {
    const model = await pickModelStep(
      first.connection.models,
      first.connection.model,
      first.connection.modelDescriptions,
      first.connection.profile
    );
    if (!model) {
      return CANCELLED;
    }
    if (model === first.connection.model) {
      return { changed: false, notice: `Already using ${model}.` };
    }
    return applyModel(deps, model);
  }

  if (first.kind === "switch") {
    await deps.catalog.useConnection(first.connection.profile);
    const after = deps.catalog.snapshot();
    const target = after.connections.find((entry) => entry.profile === first.connection.profile);
    if (!target?.active) {
      // useConnection surfaces its own error; do not double-report or claim a change that failed.
      return CANCELLED;
    }
    const model = await pickModelStep(target.models, target.model, target.modelDescriptions, target.profile);
    if (!model || model === target.model) {
      return { changed: true, notice: `Now using ${target.profile} with ${target.model || "its default model"}.` };
    }
    return applyModel(deps, model);
  }

  const entry = first.entry;
  const model = await pickModelStep(entry.models, "", entry.modelDescriptions, entry.label);
  if (!model) {
    return CANCELLED;
  }
  let baseUrl = "";
  if (entry.needsBaseUrl) {
    const typed = await vscode.window.showInputBox({
      title: `${entry.label} base URL`,
      prompt: `${entry.label} has no built-in endpoint. Enter the base URL of your instance.`,
      placeHolder: "https://localhost:11434/v1",
      ignoreFocusOut: true,
      validateInput: (value) => (value.trim().length === 0 ? "A base URL is required." : undefined)
    });
    if (typed === undefined) {
      return CANCELLED;
    }
    baseUrl = typed.trim();
  }

  // connect() owns the key prompt, profile creation, activation, and live verification, and it
  // reports its own outcome. Escaping its key prompt is a clean cancel.
  await deps.catalog.connect({ presetKey: entry.key, model, baseUrl: baseUrl || undefined });
  const after = deps.catalog.snapshot();
  const connected = after.connections.find((row) => row.profile === entry.profileName && row.active);
  if (!connected) {
    return CANCELLED;
  }
  await syncSession(deps, model);
  return { changed: true, notice: `Connected ${entry.label} with ${model}.` };
}

async function loadState(catalog: ModelPickerCatalog): Promise<ModelsSurfaceState> {
  const current = catalog.snapshot();
  if (current.loaded) {
    return current;
  }
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "Loading Alysis Code providers..." },
    () => catalog.refresh()
  );
  return catalog.snapshot();
}

async function reportUnsupported(state: ModelsSurfaceState): Promise<void> {
  const reason = state.reason.trim() || "The connected Alysis Code CLI does not expose the provider catalog.";
  const choice = await vscode.window.showWarningMessage(reason, "Open Models and Providers");
  if (choice === "Open Models and Providers") {
    await vscode.commands.executeCommand("alysis.showModels");
  }
}

async function pickProviderStep(state: ModelsSurfaceState): Promise<Step1Pick | undefined> {
  const items: Step1Item[] = [];
  const connections = [...state.connections].sort(
    (left, right) => Number(right.active) - Number(left.active) || left.profile.localeCompare(right.profile)
  );

  if (connections.length > 0) {
    items.push({ label: "Your connections", kind: vscode.QuickPickItemKind.Separator });
    for (const connection of connections) {
      items.push({
        label: `${connection.active ? "$(check) " : "$(plug) "}${connection.profile}`,
        description: connection.model || "no model set",
        detail: connectionDetail(connection),
        pick: connection.active
          ? { kind: "model-only", connection }
          : { kind: "switch", connection }
      });
    }
  }

  const catalog = state.providers.filter((entry) => !entry.connected);
  if (catalog.length > 0) {
    items.push({ label: "Add a provider", kind: vscode.QuickPickItemKind.Separator });
    for (const entry of catalog) {
      items.push({
        label: entry.label,
        description: entry.host || entry.protocolLabel,
        detail: entry.warning.trim() || entry.notes.trim() || undefined,
        pick: { kind: "connect", entry }
      });
    }
  }

  if (items.length === 0) {
    void vscode.window.showWarningMessage("No providers are available from the connected Alysis Code CLI.");
    return undefined;
  }

  const selection = await vscode.window.showQuickPick(items, {
    title: "Alysis Code - provider and model",
    placeHolder: state.activeProfile
      ? `Currently ${state.activeProfile} · ${state.activeModel || "no model"}`
      : "Choose a provider to get started",
    matchOnDescription: true,
    matchOnDetail: true,
    ignoreFocusOut: true
  });
  return selection?.pick;
}

function connectionDetail(connection: ProviderConnection): string {
  const parts: string[] = [];
  if (connection.host) {
    parts.push(connection.host);
  }
  parts.push(connection.hasKey ? "key stored" : "$(warning) no key yet");
  if (connection.authProvider) {
    parts.push(`account: ${connection.authProvider}`);
  }
  return parts.join(" · ");
}

async function pickModelStep(
  models: readonly string[],
  current: string,
  descriptions: Record<string, string>,
  contextLabel: string
): Promise<string | undefined> {
  const items: ModelItem[] = models.map((model) => ({
    label: model === current ? `$(check) ${model}` : model,
    description: model === current ? "current" : undefined,
    detail: descriptions[model],
    model
  }));
  items.push({
    label: "$(edit) Other...",
    description: "Type a model name",
    other: true
  });

  const selection = await vscode.window.showQuickPick(items, {
    title: `Model for ${contextLabel}`,
    placeHolder: current ? `Currently ${current}` : "Choose a model",
    matchOnDescription: true,
    matchOnDetail: true,
    ignoreFocusOut: true
  });
  if (!selection) {
    return undefined;
  }
  if (!selection.other) {
    return selection.model;
  }
  const typed = await vscode.window.showInputBox({
    title: `Model for ${contextLabel}`,
    prompt: "Exact model name as the provider spells it.",
    value: current,
    ignoreFocusOut: true,
    validateInput: (value) => (value.trim().length === 0 ? "A model name is required." : undefined)
  });
  const normalized = (typed ?? "").trim();
  return normalized.length > 0 ? normalized : undefined;
}

async function applyModel(deps: ModelPickerDeps, model: string): Promise<ModelPickerOutcome> {
  await deps.catalog.setModel(model);
  const after = deps.catalog.snapshot();
  if (after.activeModel !== model) {
    // setModel reports its own failure; do not announce a change that did not land.
    return CANCELLED;
  }
  const sessionNotice = await syncSession(deps, model);
  return { changed: true, notice: sessionNotice || `Model set to ${model}.` };
}

/**
 * A live session keeps its own model, so a change made here has to reach it too - otherwise the
 * picker silently only affects the *next* session, which is the confusing half-applied state.
 */
async function syncSession(deps: ModelPickerDeps, model: string): Promise<string> {
  if (!deps.setSessionModel || deps.hasActiveSession?.() !== true) {
    return "";
  }
  try {
    const notice = await deps.setSessionModel(model);
    return notice.trim();
  } catch {
    void vscode.window.showWarningMessage(
      `Model set to ${model} for new sessions, but the running session kept its previous model.`
    );
    return `Model set to ${model} for new sessions.`;
  }
}

export function registerPickModelCommand(
  context: vscode.ExtensionContext,
  deps: ModelPickerDeps
): void {
  context.subscriptions.push(
    vscode.commands.registerCommand("alysis.pickModel", async (model?: unknown) => {
      await runModelPicker(deps, typeof model === "string" ? model : undefined);
    })
  );
}
