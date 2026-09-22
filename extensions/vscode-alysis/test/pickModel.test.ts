import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import type {
  ModelsSurfaceState,
  ProviderCatalogEntry,
  ProviderConnection
} from "../src/providers/ProviderCatalogController";

const disposable = { dispose: () => undefined };
const infos: string[] = [];
const warnings: string[] = [];
let quickPickLabels: string[] = [];
let inputValues: Array<string | undefined> = [];
const shownQuickPicks: Array<Array<{ label: string; description?: string; detail?: string }>> = [];
let progressRuns = 0;

const vscodeStub = {
  QuickPickItemKind: { Separator: -1, Default: 0 },
  ProgressLocation: { Notification: 15 },
  commands: {
    registerCommand: () => disposable,
    executeCommand: async () => undefined
  },
  window: {
    withProgress: async (_options: unknown, task: () => Promise<unknown>) => {
      progressRuns += 1;
      return task();
    },
    showQuickPick: async (items: Array<{ label: string }>) => {
      shownQuickPicks.push(items);
      const label = quickPickLabels.shift();
      return label === undefined ? undefined : items.find((item) => item.label === label);
    },
    showInputBox: async () => inputValues.shift(),
    showInformationMessage: async (message: string) => {
      infos.push(message);
      return undefined;
    },
    showWarningMessage: async (message: string) => {
      warnings.push(message);
      return undefined;
    }
  }
};

const moduleLoader = Module as unknown as {
  _load: (request: string, parent: NodeModule | null, isMain: boolean) => unknown;
};
const originalLoad = moduleLoader._load;
moduleLoader._load = function patchedLoad(request: string, parent: NodeModule | null, isMain: boolean): unknown {
  if (request === "vscode") {
    return vscodeStub;
  }
  return originalLoad.call(this, request, parent, isMain);
};

const { runModelPicker } = require("../src/commands/pickModel") as typeof import("../src/commands/pickModel");
moduleLoader._load = originalLoad;

function reset(): void {
  infos.length = 0;
  warnings.length = 0;
  shownQuickPicks.length = 0;
  quickPickLabels = [];
  inputValues = [];
  progressRuns = 0;
}

function entry(overrides: Partial<ProviderCatalogEntry> = {}): ProviderCatalogEntry {
  return {
    key: "anthropic",
    label: "Anthropic",
    host: "api.anthropic.com",
    protocol: "anthropic_messages",
    protocolKind: "native",
    protocolLabel: "Native API",
    models: ["claude-sonnet", "claude-haiku"],
    modelDescriptions: { "claude-sonnet": "Balanced coding model." },
    keyEnvVar: "ANTHROPIC_API_KEY",
    notes: "",
    warning: "",
    needsBaseUrl: false,
    local: false,
    recommended: true,
    profileName: "anthropic",
    connected: false,
    ...overrides
  };
}

function connection(overrides: Partial<ProviderConnection> = {}): ProviderConnection {
  return {
    profile: "openai",
    host: "api.openai.com",
    baseUrl: "https://api.openai.com/v1",
    protocol: "openai_responses",
    model: "gpt-5",
    models: ["gpt-5", "gpt-5-mini"],
    modelDescriptions: { "gpt-5-mini": "Fast and cheap." },
    presetKey: "openai",
    protocolKind: "native",
    active: true,
    hasKey: true,
    keySource: "vscode",
    keyEnvVar: "OPENAI_API_KEY",
    storedInVsCode: true,
    authProvider: "",
    reasoningEffort: "",
    selectionReady: true,
    ...overrides
  };
}

function surface(overrides: Partial<ModelsSurfaceState> = {}): ModelsSurfaceState {
  return {
    supported: true,
    reason: "",
    loaded: true,
    busy: "",
    busyPhase: "",
    error: "",
    activeProfile: "",
    activeModel: "",
    selectionStatus: "ready",
    selectionReason: "",
    providers: [],
    connections: [],
    ...overrides
  };
}

/** Fake catalog whose snapshot actually moves, so the picker's post-mutation checks are exercised. */
function fakeCatalog(initial: ModelsSurfaceState) {
  let state = initial;
  const calls: Array<{ method: string; arg: unknown }> = [];
  return {
    calls,
    state: () => state,
    refresh: async () => {
      calls.push({ method: "refresh", arg: undefined });
      state = { ...state, loaded: true };
    },
    snapshot: () => state,
    connect: async (request: { presetKey: string; model?: string; baseUrl?: string }) => {
      calls.push({ method: "connect", arg: request });
      const target = state.providers.find((row) => row.key === request.presetKey);
      state = {
        ...state,
        activeProfile: target?.profileName ?? "",
        activeModel: request.model ?? "",
        connections: [
          ...state.connections.map((row) => ({ ...row, active: false })),
          connection({
            profile: target?.profileName ?? "",
            model: request.model ?? "",
            models: target?.models ?? [],
            active: true
          })
        ]
      };
    },
    useConnection: async (profile: string) => {
      calls.push({ method: "useConnection", arg: profile });
      const next = state.connections.map((row) => ({ ...row, active: row.profile === profile }));
      const target = next.find((row) => row.active);
      state = { ...state, connections: next, activeProfile: profile, activeModel: target?.model ?? "" };
    },
    setModel: async (model: string) => {
      calls.push({ method: "setModel", arg: model });
      state = {
        ...state,
        activeModel: model,
        connections: state.connections.map((row) => (row.active ? { ...row, model } : row))
      };
    }
  };
}

test("cold start with no session connects a provider and model without leaving the picker", async () => {
  reset();
  const catalog = fakeCatalog(surface({ providers: [entry()] }));
  // Provider step, then model step. No session exists — the whole point of the fix.
  quickPickLabels = ["Anthropic", "claude-sonnet"];

  const outcome = await runModelPicker({ catalog });

  assert.equal(outcome.changed, true);
  assert.deepEqual(catalog.calls, [
    { method: "connect", arg: { presetKey: "anthropic", model: "claude-sonnet", baseUrl: undefined } }
  ]);
  assert.match(outcome.notice, /Anthropic/);
  assert.match(outcome.notice, /claude-sonnet/);
});

test("the model step shows catalog descriptions forwarded by the bridge", async () => {
  reset();
  const catalog = fakeCatalog(surface({ providers: [entry()] }));
  quickPickLabels = ["Anthropic", "claude-haiku"];

  await runModelPicker({ catalog });

  const modelStep = shownQuickPicks[1];
  const sonnet = modelStep.find((item) => item.label === "claude-sonnet");
  assert.equal(sonnet?.detail, "Balanced coding model.");
});

test("an active connection goes straight to its model list", async () => {
  reset();
  const catalog = fakeCatalog(
    surface({ connections: [connection()], activeProfile: "openai", activeModel: "gpt-5" })
  );
  quickPickLabels = ["$(check) openai", "gpt-5-mini"];

  const outcome = await runModelPicker({ catalog });

  assert.equal(outcome.changed, true);
  assert.deepEqual(catalog.calls, [{ method: "setModel", arg: "gpt-5-mini" }]);
});

test("switching to another configured connection activates it before the model step", async () => {
  reset();
  const catalog = fakeCatalog(
    surface({
      connections: [connection(), connection({ profile: "anthropic", model: "claude-sonnet", models: ["claude-sonnet"], active: false })],
      activeProfile: "openai",
      activeModel: "gpt-5"
    })
  );
  quickPickLabels = ["$(plug) anthropic", "$(check) claude-sonnet"];

  const outcome = await runModelPicker({ catalog });

  assert.equal(outcome.changed, true);
  assert.equal(catalog.calls[0]?.method, "useConnection");
  assert.equal(catalog.calls[0]?.arg, "anthropic");
});

test("/model <name> with an active connection applies directly and shows no picker", async () => {
  reset();
  const catalog = fakeCatalog(
    surface({ connections: [connection()], activeProfile: "openai", activeModel: "gpt-5" })
  );

  const outcome = await runModelPicker({ catalog }, "gpt-5-mini");

  assert.equal(outcome.changed, true);
  assert.deepEqual(catalog.calls, [{ method: "setModel", arg: "gpt-5-mini" }]);
  assert.equal(shownQuickPicks.length, 0);
});

test("/model <name> with nothing connected explains itself and falls back to the picker", async () => {
  reset();
  const catalog = fakeCatalog(surface({ providers: [entry()] }));
  quickPickLabels = [];

  const outcome = await runModelPicker({ catalog }, "claude-sonnet");

  assert.equal(outcome.changed, false);
  assert.equal(catalog.calls.length, 0);
  assert.ok(infos.some((message) => message.includes("No provider is connected yet")));
});

test("escaping the provider step changes nothing", async () => {
  reset();
  const catalog = fakeCatalog(surface({ providers: [entry()] }));
  quickPickLabels = [];

  const outcome = await runModelPicker({ catalog });

  assert.deepEqual(outcome, { changed: false, notice: "" });
  assert.equal(catalog.calls.length, 0);
});

test("escaping the model step never connects a keyless half-configured provider", async () => {
  reset();
  const catalog = fakeCatalog(surface({ providers: [entry()] }));
  quickPickLabels = ["Anthropic"];

  const outcome = await runModelPicker({ catalog });

  assert.equal(outcome.changed, false);
  assert.equal(catalog.calls.length, 0);
});

test("a base-URL provider is asked for its endpoint before connecting", async () => {
  reset();
  const catalog = fakeCatalog(
    surface({ providers: [entry({ key: "vllm", label: "vLLM", profileName: "vllm", needsBaseUrl: true, models: ["local-model"] })] })
  );
  quickPickLabels = ["vLLM", "local-model"];
  inputValues = ["https://localhost:8000/v1"];

  await runModelPicker({ catalog });

  assert.deepEqual(catalog.calls, [
    { method: "connect", arg: { presetKey: "vllm", model: "local-model", baseUrl: "https://localhost:8000/v1" } }
  ]);
});

test("an unloaded catalog is refreshed behind a progress notification", async () => {
  reset();
  const catalog = fakeCatalog(surface({ loaded: false, providers: [entry()] }));
  quickPickLabels = [];

  await runModelPicker({ catalog });

  assert.equal(progressRuns, 1);
  assert.equal(catalog.calls[0]?.method, "refresh");
});

test("an unsupported catalog warns instead of showing an empty picker", async () => {
  reset();
  const catalog = fakeCatalog(surface({ supported: false, reason: "This CLI build has no provider catalog." }));

  const outcome = await runModelPicker({ catalog });

  assert.equal(outcome.changed, false);
  assert.deepEqual(warnings, ["This CLI build has no provider catalog."]);
  assert.equal(shownQuickPicks.length, 0);
});

test("a live session receives the model change too", async () => {
  reset();
  const catalog = fakeCatalog(
    surface({ connections: [connection()], activeProfile: "openai", activeModel: "gpt-5" })
  );
  const sessionModels: string[] = [];

  const outcome = await runModelPicker(
    {
      catalog,
      hasActiveSession: () => true,
      setSessionModel: async (model) => {
        sessionModels.push(model);
        return `Session now uses ${model}.`;
      }
    },
    "gpt-5-mini"
  );

  assert.deepEqual(sessionModels, ["gpt-5-mini"]);
  assert.equal(outcome.notice, "Session now uses gpt-5-mini.");
});

test("no live session means the session setter is never called", async () => {
  reset();
  const catalog = fakeCatalog(
    surface({ connections: [connection()], activeProfile: "openai", activeModel: "gpt-5" })
  );
  let sessionCalls = 0;

  const outcome = await runModelPicker(
    {
      catalog,
      hasActiveSession: () => false,
      setSessionModel: async () => {
        sessionCalls += 1;
        return "";
      }
    },
    "gpt-5-mini"
  );

  assert.equal(sessionCalls, 0);
  assert.equal(outcome.notice, "Model set to gpt-5-mini.");
});
