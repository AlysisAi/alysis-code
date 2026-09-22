import assert from "node:assert/strict";
import Module from "node:module";
import test from "node:test";

import { CockpitRuntimeState } from "../src/chat/CockpitRuntimeState";
import { PROTOCOL_VERSION, REQUIRED_BRIDGE_METHODS } from "../src/client/AlysisProtocol";

class FakeTreeItem {
  public description?: string;
  public tooltip?: string;
  public iconPath?: unknown;
  public contextValue?: string;
  public constructor(public label: string, public collapsibleState: unknown) {}
}

class FakeEventEmitter {
  private readonly listeners = new Set<(event?: unknown) => void>();

  public event = (listener: (event?: unknown) => void): { dispose(): void } => {
    this.listeners.add(listener);
    return {
      dispose: () => {
        this.listeners.delete(listener);
      }
    };
  };

  public fire(event?: unknown): void {
    for (const listener of this.listeners) {
      listener(event);
    }
  }
}

const vscodeStub = {
  TreeItem: FakeTreeItem,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  EventEmitter: FakeEventEmitter,
  ThemeIcon: class {
    public constructor(public id: string) {}
  },
  env: { remoteName: undefined },
  workspace: { workspaceFolders: [{ uri: { fsPath: "/workspace" } }] }
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

const { ManageViewProvider } = require("../src/views/manageView") as typeof import("../src/views/manageView");

function compatibility(supported: Record<string, boolean>): any {
  const features: Record<string, unknown> = {};
  for (const [id, value] of Object.entries(supported)) {
    features[id] = {
      id,
      label: id,
      supported: value,
      requiredMethods: [],
      missingMethods: value ? [] : [`${id}.list`],
      reason: value
        ? null
        : {
            category: "version_gate",
            message: `Needs a newer Alysis Code CLI — ${id}.list`
          },
      disabledReason: value ? null : `${id} is disabled.`
    };
  }
  return { features };
}

function fakeBridge(overrides: Record<string, unknown> = {}): any {
  const featureValues = (overrides.featureValues as Record<string, unknown> | undefined) ?? {};
  const unsupportedMethods = new Set((overrides.unsupportedMethods as string[] | undefined) ?? []);
  return {
    supportsMethod: (method: string) => !unsupportedMethods.has(method),
    featureValue: (path: readonly string[]) => featureValues[path.join(".")] ?? true,
    // Real payload field names (tool.list -> "trust"; mcp.status -> "rows"/"server_id").
    toolList: async () => ({ tools: [{ name: "fmt", trust: "trusted" }, { name: "lint", trust: "untrusted" }] }),
    skillList: async () => ({ skills: [] }),
    mcpStatus: async () => ({ rows: [{ server_id: "ctx7", transport: "stdio" }] }),
    mcpAuthStatus: async () => ({ rows: [{ server_id: "ctx7", auth_enabled: "no", token: "absent" }] }),
    hooksList: async () => ({ hooks: [] }),
    conventionsList: async () => ({ documents: [] }),
    extList: async () => ({ extensions: [] }),
    ...overrides
  };
}

test("ManageView gates domains: supported -> expandable group, unsupported -> a gated note", async () => {
  const view = new ManageViewProvider(
    fakeBridge(),
    () => compatibility({ tools: true, skills: false, mcp: false, hooks: false, conventions: false, ext: false })
  );
  const groups = await view.getChildren();
  const tools = groups.find((node) => node.label === "Tools");
  const skills = groups.find((node) => node.label === "Skills");
  assert.equal(tools?.nodeKind, "group");
  assert.equal(tools?.collapsibleState, vscodeStub.TreeItemCollapsibleState.Collapsed);
  assert.equal(skills?.nodeKind, "note");
  assert.match(String(skills?.description), /Needs a newer Alysis Code CLI/);
  assert.match(String(skills?.description), /skills\.list/);
  assert.equal(skills?.collapsibleState, vscodeStub.TreeItemCollapsibleState.None);
});

test("ManageView reads updated runtime compatibility after bridge health is applied", async () => {
  const runtime = new CockpitRuntimeState();
  const view = new ManageViewProvider(fakeBridge(), () => runtime.snapshot().compatibility);
  let treeEvents = 0;
  view.onDidChangeTreeData(() => {
    treeEvents += 1;
  });
  runtime.onDidChange(() => view.refreshTree());

  const before = await view.getChildren();
  assert.equal(before.length, 1, "unchecked compatibility is summarized once instead of repeated per domain");
  assert.equal(before[0].nodeKind, "note");
  assert.equal(before[0].label, "Check Alysis Code CLI");
  assert.equal(before[0].description, "Load management features");
  assert.equal(before[0].command?.command, "alysis.manage.refresh");
  assert.equal(view.compatibilityNeedsCheck(), true);

  runtime.applyBridgeHealth(fullManageHealth());

  const after = await view.getChildren();
  assert.equal(treeEvents > 0, true);
  assert.equal(after.every((node) => node.nodeKind === "group"), true);
  assert.equal(after.some((node) => String(node.description).includes("Needs a newer Alysis Code CLI")), false);
  assert.equal(view.compatibilityNeedsCheck(), false);
});

test("ManageView refresh re-probes bridge health before redrawing", async () => {
  let probes = 0;
  let treeEvents = 0;
  const view = new ManageViewProvider(
    fakeBridge(),
    () => compatibility({ tools: false }),
    () => true,
    async () => {
      probes += 1;
    }
  );
  view.onDidChangeTreeData(() => {
    treeEvents += 1;
  });

  await view.refresh();

  assert.equal(probes, 1);
  assert.equal(treeEvents, 1);
});

test("ManageView lists items from the bridge with real target ids + domain contextValue", async () => {
  const view = new ManageViewProvider(fakeBridge(), () => compatibility({ tools: true }));
  const toolsGroup = (await view.getChildren()).find((node) => node.label === "Tools");
  const items = await view.getChildren(toolsGroup);
  assert.equal(items.length, 2);
  assert.deepEqual(items.map((item) => item.targetId), ["fmt", "lint"]);
  assert.match(String(items[0].contextValue), /alysisManageTool/);
  assert.equal(items[0].nodeKind, "item");
});

test("ManageView lists MCP servers from the real `rows` payload with server_id as the target", async () => {
  const view = new ManageViewProvider(fakeBridge(), () => compatibility({ mcp: true }));
  const mcpGroup = (await view.getChildren()).find((node) => node.label === "MCP Servers");
  const items = await view.getChildren(mcpGroup);
  assert.equal(items.length, 1, "MCP browses from rows (not the absent `servers` field)");
  assert.equal(items[0].targetId, "ctx7", "targetId is the canonical server_id for login/logout");
  assert.match(String(items[0].contextValue), /alysisManageMcp/);
});

test("ManageView state-gates tool trust and untrust actions", async () => {
  const view = new ManageViewProvider(fakeBridge(), () => compatibility({ tools: true }), () => true);
  const toolsGroup = (await view.getChildren()).find((node) => node.label === "Tools");
  const items = await view.getChildren(toolsGroup);
  const byId = (id: string) => items.find((item) => item.targetId === id);

  assert.match(String(byId("fmt")?.contextValue), /alysisManageActionInfo/);
  assert.match(String(byId("fmt")?.contextValue), /alysisManageActionUntrust/);
  assert.doesNotMatch(String(byId("fmt")?.contextValue), /alysisManageActionTrust/);
  assert.match(String(byId("lint")?.contextValue), /alysisManageActionTrust/);
  assert.doesNotMatch(String(byId("lint")?.contextValue), /alysisManageActionUntrust/);
});

test("ManageView state-gates enable disable and remove actions", async () => {
  const view = new ManageViewProvider(
    fakeBridge({
      skillList: async () => ({
        skills: [
          { name: "python", enabled: false, managed: true },
          { name: "typescript", enabled: true, managed: false }
        ]
      }),
      extList: async () => ({
        extensions: [
          { id: "pub.enabled", enabled_effective: true },
          { id: "pub.disabled", enabled_effective: false }
        ]
      })
    }),
    () => compatibility({ skills: true, ext: true }),
    () => true
  );

  const groups = await view.getChildren();
  const skills = await view.getChildren(groups.find((node) => node.label === "Skills"));
  const extensions = await view.getChildren(groups.find((node) => node.label === "Extensions"));
  const skillById = (id: string) => skills.find((item) => item.targetId === id);
  const extById = (id: string) => extensions.find((item) => item.targetId === id);

  assert.match(String(skillById("python")?.contextValue), /alysisManageActionEnable/);
  assert.match(String(skillById("python")?.contextValue), /alysisManageActionRemove/);
  assert.doesNotMatch(String(skillById("python")?.contextValue), /alysisManageActionDisable/);
  assert.match(String(skillById("typescript")?.contextValue), /alysisManageActionDisable/);
  assert.doesNotMatch(String(skillById("typescript")?.contextValue), /alysisManageActionEnable/);
  assert.doesNotMatch(String(skillById("typescript")?.contextValue), /alysisManageActionRemove/);

  assert.match(String(extById("pub.enabled")?.contextValue), /alysisManageActionDisable/);
  assert.match(String(extById("pub.enabled")?.contextValue), /alysisManageActionRemove/);
  assert.match(String(extById("pub.disabled")?.contextValue), /alysisManageActionEnable/);
  assert.doesNotMatch(String(extById("pub.disabled")?.contextValue), /alysisManageActionDisable/);
});

test("ManageView hides mutating actions in untrusted workspaces but keeps read-only details", async () => {
  const view = new ManageViewProvider(fakeBridge(), () => compatibility({ tools: true }), () => false);
  const toolsGroup = (await view.getChildren()).find((node) => node.label === "Tools");
  const items = await view.getChildren(toolsGroup);

  assert.match(String(items[0].contextValue), /alysisManageActionInfo/);
  assert.doesNotMatch(String(items[0].contextValue), /alysisManageActionTrust/);
  assert.doesNotMatch(String(items[0].contextValue), /alysisManageActionUntrust/);
});

test("ManageView gates MCP auth actions by exact capability and token state", async () => {
  const unsupportedLogin = new ManageViewProvider(
    fakeBridge({
      featureValues: { "management.methods.mcp.auth.login.start.supported": false },
      mcpAuthStatus: async () => ({ rows: [{ server_id: "ctx7", auth_enabled: "yes", token: "absent" }] })
    }),
    () => compatibility({ mcp: true }),
    () => true
  );
  const unsupportedGroup = (await unsupportedLogin.getChildren()).find((node) => node.label === "MCP Servers");
  const unsupportedItems = await unsupportedLogin.getChildren(unsupportedGroup);
  assert.doesNotMatch(String(unsupportedItems[0].contextValue), /alysisManageActionMcpLogin/);
  assert.doesNotMatch(String(unsupportedItems[0].contextValue), /alysisManageActionMcpLogout/);

  const supportedLogin = new ManageViewProvider(
    fakeBridge({
      mcpAuthStatus: async () => ({ rows: [{ server_id: "ctx7", auth_enabled: "yes", token: "absent" }] })
    }),
    () => compatibility({ mcp: true }),
    () => true
  );
  const supportedGroup = (await supportedLogin.getChildren()).find((node) => node.label === "MCP Servers");
  const supportedItems = await supportedLogin.getChildren(supportedGroup);
  assert.match(String(supportedItems[0].contextValue), /alysisManageActionMcpLogin/);
  assert.doesNotMatch(String(supportedItems[0].contextValue), /alysisManageActionMcpLogout/);

  const logout = new ManageViewProvider(
    fakeBridge({
      mcpAuthStatus: async () => ({ rows: [{ server_id: "ctx7", auth_enabled: "yes", token: "present" }] })
    }),
    () => compatibility({ mcp: true }),
    () => true
  );
  const logoutGroup = (await logout.getChildren()).find((node) => node.label === "MCP Servers");
  const logoutItems = await logout.getChildren(logoutGroup);
  assert.match(String(logoutItems[0].contextValue), /alysisManageActionMcpLogout/);
  assert.doesNotMatch(String(logoutItems[0].contextValue), /alysisManageActionMcpLogin/);

  const unsupportedLogout = new ManageViewProvider(
    fakeBridge({
      featureValues: { "management.methods.mcp.auth.logout.supported": false },
      mcpAuthStatus: async () => ({ rows: [{ server_id: "ctx7", auth_enabled: "yes", token: "present" }] })
    }),
    () => compatibility({ mcp: true }),
    () => true
  );
  const unsupportedLogoutGroup = (await unsupportedLogout.getChildren()).find((node) => node.label === "MCP Servers");
  const unsupportedLogoutItems = await unsupportedLogout.getChildren(unsupportedLogoutGroup);
  assert.doesNotMatch(String(unsupportedLogoutItems[0].contextValue), /alysisManageActionMcpLogout/);
});

test("ManageView hides remote MCP OAuth login and shows an explicit loopback reason", async () => {
  const remote = new ManageViewProvider(
    fakeBridge({
      mcpAuthStatus: async () => ({ rows: [{ server_id: "ctx7", auth_enabled: "yes", token: "absent" }] })
    }),
    () => compatibility({ mcp: true }),
    () => true,
    () => Promise.resolve(),
    () => "ssh-remote"
  );

  const group = (await remote.getChildren()).find((node) => node.label === "MCP Servers");
  const items = await remote.getChildren(group);
  assert.doesNotMatch(String(items[0].contextValue), /alysisManageActionMcpLogin/);
  assert.match(String(items[0].description), /unavailable in a remote VS Code extension host/i);
  assert.match(String(items[0].tooltip), /loopback callback runs remotely/i);
});

test("ManageView redacts secret-looking state in item descriptions", async () => {
  const bridge = fakeBridge({ toolList: async () => ({ tools: [{ name: "t", trust: "sk-ABCDEFGHIJ1234567890" }] }) });
  const view = new ManageViewProvider(bridge, () => compatibility({ tools: true }));
  const toolsGroup = (await view.getChildren()).find((node) => node.label === "Tools");
  const items = await view.getChildren(toolsGroup);
  assert.match(String(items[0].description), /<redacted>/);
  assert.doesNotMatch(String(items[0].description), /sk-[A-Za-z0-9]{12}/);
});

test("ManageView release dogfood loads supported domains without passive network calls", async () => {
  const calls: string[] = [];
  const bridge = fakeBridge({
    toolList: async () => {
      calls.push("tool.list");
      return { tools: [{ name: "fmt", trust: "trusted" }] };
    },
    skillList: async () => {
      calls.push("skill.list");
      return { skills: [{ name: "python", enabled: true, managed: false }] };
    },
    mcpStatus: async () => {
      calls.push("mcp.status");
      return { rows: [{ server_id: "ctx7", transport: "stdio" }] };
    },
    mcpAuthStatus: async () => {
      calls.push("mcp.auth.status");
      return { rows: [{ server_id: "ctx7", auth_enabled: "no", token: "absent" }] };
    },
    hooksList: async () => {
      calls.push("hooks.list");
      return { hooks: [{ id: "pre-tool", event: "PreToolUse" }] };
    },
    conventionsList: async () => {
      calls.push("conventions.list");
      return { documents: [{ workspace_relative_path: "docs/conventions.md", trust_level: "trusted" }] };
    },
    extList: async () => {
      calls.push("ext.list");
      return { extensions: [{ id: "publisher.plugin", enabled_effective: true }] };
    },
    updateCheck: async () => {
      calls.push("update.check");
      throw new Error("update.check must be explicit");
    },
    extSearch: async () => {
      calls.push("ext.search");
      throw new Error("ext.search must be explicit");
    }
  });
  const view = new ManageViewProvider(
    bridge,
    () => compatibility({ tools: true, skills: true, mcp: true, hooks: true, conventions: true, ext: true }),
    () => true
  );

  for (const group of await view.getChildren()) {
    if (group.nodeKind === "group") {
      await view.getChildren(group);
    }
  }

  assert.deepEqual(calls, [
    "tool.list",
    "skill.list",
    "mcp.status",
    "mcp.auth.status",
    "hooks.list",
    "conventions.list",
    "ext.list"
  ]);
  assert.equal(calls.includes("update.check"), false);
  assert.equal(calls.includes("ext.search"), false);
});

test("ManageView shows a (none) note when a supported domain has no items, and (failed) on error", async () => {
  const empty = new ManageViewProvider(fakeBridge({ toolList: async () => ({ tools: [] }) }), () => compatibility({ tools: true }));
  const emptyGroup = (await empty.getChildren()).find((node) => node.label === "Tools");
  const emptyItems = await empty.getChildren(emptyGroup);
  assert.equal(emptyItems.length, 1);
  assert.equal(emptyItems[0].nodeKind, "note");
  assert.equal(String(emptyItems[0].label), "(none)");

  const failing = new ManageViewProvider(
    fakeBridge({
      toolList: async () => {
        throw new Error("bridge down");
      }
    }),
    () => compatibility({ tools: true })
  );
  const failGroup = (await failing.getChildren()).find((node) => node.label === "Tools");
  const failItems = await failing.getChildren(failGroup);
  assert.equal(failItems.length, 1);
  assert.match(String(failItems[0].label), /Could not list/);
});

function fullManageHealth() {
  return {
    ok: true,
    name: "alysis",
    protocol_version: PROTOCOL_VERSION,
    alysis_version: "0.1.4",
    capabilities: {
      protocol_version: PROTOCOL_VERSION,
      methods: [
        ...REQUIRED_BRIDGE_METHODS,
        "tools.catalog",
        "tool.list",
        "tool.info",
        "skill.list",
        "skill.info",
        "mcp.status",
        "mcp.prompts.list",
        "hooks.list",
        "hooks.effective",
        "conventions.list",
        "conventions.render",
        "ext.list",
        "ext.info",
        "ext.search"
      ],
      events: [],
      modes: ["readonly"],
      transport: "stdio-jsonl"
    }
  };
}
