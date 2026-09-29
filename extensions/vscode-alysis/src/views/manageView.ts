import * as vscode from "vscode";
import { activeWorkspaceRoot } from "../workspace/activeWorkspace";

import { redactForDisplay } from "../client/CliDiscovery";
import { BridgeCompatibilitySnapshot, CompatibilityFeatureId, featureGateReason } from "../client/compatibility";
import { AlysisBridgeClient } from "../client/AlysisBridgeClient";
import {
  backendActionById,
  BackendActionMetadata,
  isBackendActionSupportedByCapabilities
} from "../backend/BackendActionMetadata";
import { MANAGE_DOMAIN_ACTIONS, ManageActionKey } from "./manageActionRoutes";
import { mcpOAuthRemoteUnavailableReason } from "../security/remoteHost";

// One browsable platform domain. fetch() returns redacted rows from the domain's read-only list
// method; the group only expands when its capability gate is supported (gated otherwise, with a
// reason — never a faked control). Per-item mutations go through BackendActionController.executeAction
// (capability + Workspace-Trust + confirm + the FE-11 result card) via manageCommands, NOT here.
interface ManageDomain {
  readonly domainId: string;
  readonly label: string;
  readonly gate: CompatibilityFeatureId;
  readonly icon: string;
  readonly contextValue: string;
  fetch(bridge: AlysisBridgeClient, workspace: string): Promise<ManageRow[]>;
}

interface ManageRow {
  id: string;
  description: string;
  actionArg?: string;
  state: ManageRowState;
}

export interface ManageRowState {
  enabled?: boolean;
  trusted?: boolean;
  removable?: boolean;
  authEnabled?: boolean;
  authenticated?: boolean;
}

export class ManageTreeItem extends vscode.TreeItem {
  public constructor(
    public readonly nodeKind: "group" | "item" | "note",
    public readonly domainId: string,
    public readonly targetId: string,
    label: string,
    collapsibleState: vscode.TreeItemCollapsibleState,
    public readonly actionArg: string = targetId,
    public readonly itemState: ManageRowState = {}
  ) {
    super(label, collapsibleState);
  }
}

const MANAGE_DOMAINS: readonly ManageDomain[] = [
  {
    domainId: "tools",
    label: "Tools",
    gate: "tools",
    icon: "tools",
    contextValue: "alysisManageTool",
    // tool.list entries expose the trust state under "trust" (a string), not a boolean "trusted".
    fetch: async (bridge, workspace) =>
      rows((await bridge.toolList({ workspace })).tools, ["name", "id"], {
        stateKey: "trust",
        statePrefix: "trust",
        stateFrom: (item) => ({ trusted: trustedFromValue(item.trust ?? item.trusted) })
      })
  },
  {
    domainId: "skills",
    label: "Skills",
    gate: "skills",
    icon: "sparkle",
    contextValue: "alysisManageSkill",
    fetch: async (bridge, workspace) =>
      rows((await bridge.skillList({ workspace })).skills, ["name", "id"], {
        stateKey: "enabled",
        statePrefix: "enabled",
        stateFrom: (item) => ({
          enabled: booleanFromValue(item.enabled),
          removable: booleanFromValue(item.managed) === true || isRecord(item.install_record)
        })
      })
  },
  {
    domainId: "mcp",
    label: "MCP Servers",
    gate: "mcp",
    icon: "server-environment",
    contextValue: "alysisManageMcp",
    // mcp.status returns the per-server list under "rows" (the typed result only declares the legacy
    // "servers", so read defensively). server_id is the canonical target the login/logout actions act
    // on (collectParams maps the arg -> server_id).
    fetch: async (bridge, workspace) => {
      const result = (await bridge.mcpStatus({ workspace })) as Record<string, unknown>;
      const authByServer = await mcpAuthStateByServer(bridge, workspace);
      return rows(result.rows ?? result.servers ?? [], ["server_id", "id", "name"], {
        stateKey: "transport",
        statePrefix: "transport",
        stateFrom: (item) => {
          const serverId = firstString(item, ["server_id", "id", "name"]);
          return authByServer.get(serverId) ?? {};
        },
        descriptionFrom: (item, row) => {
          const parts = [`transport: ${String(item.transport ?? "")}`.trim()];
          if (row.state.authEnabled !== undefined) {
            parts.push(`auth: ${row.state.authEnabled ? "yes" : "no"}`);
          }
          if (row.state.authenticated !== undefined) {
            parts.push(`token: ${row.state.authenticated ? "present" : "absent"}`);
          }
          return parts.filter((part) => part && !part.endsWith(":")).join(" | ");
        }
      });
    }
  },
  {
    domainId: "hooks",
    label: "Hooks",
    gate: "hooks",
    icon: "zap",
    contextValue: "alysisManageHook",
    fetch: async (bridge, workspace) =>
      rows((await bridge.hooksList({ workspace })).hooks, ["id", "hook_id", "name"], {
        stateKey: "event",
        statePrefix: "event",
        stateFrom: () => ({ enabled: true })
      })
  },
  {
    domainId: "conventions",
    label: "Conventions",
    gate: "conventions",
    icon: "book",
    contextValue: "alysisManageConvention",
    // Prefer the workspace-relative path for the label; trust_level is the available state field.
    fetch: async (bridge, workspace) =>
      rows((await bridge.conventionsList({ workspace })).documents, ["workspace_relative_path", "name", "path"], {
        stateKey: "trust_level",
        statePrefix: "trust",
        stateFrom: (item) => ({ trusted: trustedFromValue(item.trust_level) })
      })
  },
  {
    domainId: "extensions",
    label: "Extensions",
    gate: "ext",
    icon: "extensions",
    contextValue: "alysisManageExtension",
    // ext.list entries expose enablement under "enabled_effective", not "enabled".
    fetch: async (bridge, workspace) =>
      rows((await bridge.extList({ workspace })).extensions, ["id", "name"], {
        stateKey: "enabled_effective",
        statePrefix: "enabled",
        stateFrom: (item) => ({
          enabled: booleanFromValue(item.enabled_effective ?? item.enabled),
          removable: item.removable === undefined ? true : booleanFromValue(item.removable)
        })
      })
  }
];

export class ManageViewProvider implements vscode.TreeDataProvider<ManageTreeItem> {
  private readonly changed = new vscode.EventEmitter<ManageTreeItem | undefined | null | void>();
  private refreshPromise: Promise<void> | undefined;
  public readonly onDidChangeTreeData = this.changed.event;

  public constructor(
    private readonly bridge: AlysisBridgeClient,
    private readonly getCompatibility: () => BridgeCompatibilitySnapshot,
    private readonly isWorkspaceTrusted: () => boolean = () => vscode.workspace.isTrusted,
    private readonly refreshBridgeStatus: () => Promise<void> = () => Promise.resolve(),
    private readonly getRemoteName: () => string | undefined = () => vscode.env.remoteName
  ) {}

  public getTreeItem(element: ManageTreeItem): vscode.TreeItem {
    return element;
  }

  public async getChildren(element?: ManageTreeItem): Promise<ManageTreeItem[]> {
    if (!element) {
      return this.domainNodes();
    }
    if (element.nodeKind !== "group") {
      return [];
    }
    return this.itemNodes(element.domainId);
  }

  public refreshTree(): void {
    this.changed.fire();
  }

  public refresh(): Promise<void> {
    if (this.refreshPromise) {
      return this.refreshPromise;
    }
    this.refreshPromise = this.refreshBridgeStatus()
      .then(() => this.refreshTree())
      .finally(() => {
        this.refreshPromise = undefined;
      });
    return this.refreshPromise;
  }

  public compatibilityNeedsCheck(): boolean {
    const features = this.getCompatibility().features;
    return MANAGE_DOMAINS.every((domain) => {
      const feature = features[domain.gate];
      return feature?.supported !== true
        && featureGateReason(feature).toLowerCase().includes("compatibility has not been checked");
    });
  }

  private domainNodes(): ManageTreeItem[] {
    if (this.compatibilityNeedsCheck()) {
      const status = new ManageTreeItem(
        "note",
        "status",
        "",
        "Check Alysis Code CLI",
        vscode.TreeItemCollapsibleState.None
      );
      status.description = "Load management features";
      status.tooltip = "Check the local Alysis Code CLI and load tools, skills, MCP servers, hooks, conventions, and extensions.";
      status.iconPath = new vscode.ThemeIcon("refresh");
      status.contextValue = "alysisManageCompatibilityUnchecked";
      status.command = {
        command: "alysis.manage.refresh",
        title: "Check Alysis Code CLI"
      };
      return [status];
    }

    const features = this.getCompatibility().features;
    return MANAGE_DOMAINS.map((domain) => {
      const feature = features[domain.gate];
      const supported = feature?.supported === true;
      if (!supported) {
        // Gated domain: visible but not expandable, with a reason. Never faked as available. FE-19:
        // name the specific missing IDE bridge method(s) when known instead of a generic upgrade note.
        const missing = (feature?.missingMethods ?? []).filter(Boolean);
        const reason = featureGateReason(feature);
        const compatibilityUnchecked = reason.toLowerCase().includes("compatibility has not been checked");
        const note = new ManageTreeItem("note", domain.domainId, "", domain.label, vscode.TreeItemCollapsibleState.None);
        note.description = missing.length > 0 && !compatibilityUnchecked
          ? `Needs a newer Alysis Code CLI: ${missing.join(", ")}`
          : reason;
        note.tooltip = missing.length > 0 && !compatibilityUnchecked
          ? `${domain.label} is unavailable — the connected Alysis Code CLI does not expose: ${missing.join(", ")}.`
          : feature?.reason?.detail ?? reason;
        note.contextValue = "alysisManageGroupGated";
        note.iconPath = new vscode.ThemeIcon("circle-slash");
        return note;
      }
      const group = new ManageTreeItem(
        "group",
        domain.domainId,
        "",
        domain.label,
        vscode.TreeItemCollapsibleState.Collapsed
      );
      group.contextValue = "alysisManageGroup";
      group.iconPath = new vscode.ThemeIcon(domain.icon);
      return group;
    });
  }

  private async itemNodes(domainId: string): Promise<ManageTreeItem[]> {
    const domain = MANAGE_DOMAINS.find((entry) => entry.domainId === domainId);
    if (!domain) {
      return [];
    }
    try {
      const fetched = await domain.fetch(this.bridge, workspaceRoot());
      if (fetched.length === 0) {
        return [emptyNote(domainId, "(none)")];
      }
      return fetched.map((row) => {
        const oauthUnavailable = domainId === "mcp"
          && row.state.authEnabled === true
          && row.state.authenticated !== true
          ? mcpOAuthRemoteUnavailableReason(this.getRemoteName())
          : undefined;
        const item = new ManageTreeItem(
          "item",
          domainId,
          row.id,
          row.id,
          vscode.TreeItemCollapsibleState.None,
          row.actionArg ?? row.id,
          row.state
        );
        item.description = oauthUnavailable ?? row.description;
        item.contextValue = manageItemContextValue(
          domain,
          row,
          this.bridge,
          this.isWorkspaceTrusted(),
          this.getRemoteName()
        );
        item.tooltip = oauthUnavailable ?? `${domain.label}: ${row.id}`;
        return item;
      });
    } catch (error) {
      return [emptyNote(domainId, "Could not list — " + redactForDisplay(error instanceof Error ? error.message : String(error)))];
    }
  }
}

function emptyNote(domainId: string, label: string): ManageTreeItem {
  const note = new ManageTreeItem("note", domainId, "", label, vscode.TreeItemCollapsibleState.None);
  note.contextValue = "alysisManageNote";
  return note;
}

interface RowOptions {
  stateKey?: string;
  statePrefix?: string;
  stateFrom?(item: Record<string, unknown>): ManageRowState;
  descriptionFrom?(item: Record<string, unknown>, row: ManageRow): string;
}

// Builds redacted rows. The id stays the real target (an identifier, not a secret) so executeAction
// can act on it; the description (free text/state) is redacted for display.
function rows(value: unknown, idKeys: string[], options: RowOptions = {}): ManageRow[] {
  const items = Array.isArray(value)
    ? (value.filter((entry) => entry && typeof entry === "object" && !Array.isArray(entry)) as Record<string, unknown>[])
    : [];
  return items
    .map((item) => {
      const id = firstString(item, idKeys);
      const stateValue = options.stateKey === undefined ? undefined : item[options.stateKey];
      const state = compactState(options.stateFrom?.(item) ?? {});
      const row: ManageRow = {
        id,
        description:
          stateValue === undefined
            ? ""
            : redactForDisplay((options.statePrefix ? options.statePrefix + ": " : "") + String(stateValue)),
        state
      };
      if (options.descriptionFrom) {
        row.description = redactForDisplay(options.descriptionFrom(item, row));
      }
      return row;
    })
    .filter((row) => row.id.length > 0);
}

async function mcpAuthStateByServer(
  bridge: AlysisBridgeClient,
  workspace: string
): Promise<Map<string, ManageRowState>> {
  const states = new Map<string, ManageRowState>();
  if (!bridge.supportsMethod("mcp.auth.status")) {
    return states;
  }
  try {
    const auth = await bridge.mcpAuthStatus({ workspace });
    for (const row of records(auth.rows)) {
      const serverId = firstString(row, ["server_id", "server", "id", "name"]);
      if (!serverId) {
        continue;
      }
      states.set(serverId, {
        authEnabled: yesNoFromValue(row.auth_enabled),
        authenticated: String(row.token ?? "").trim().toLowerCase() === "present"
      });
    }
  } catch {
    return states;
  }
  return states;
}

function manageItemContextValue(
  domain: ManageDomain,
  row: ManageRow,
  bridge: AlysisBridgeClient,
  workspaceTrusted: boolean,
  remoteName?: string
): string {
  const tokens = [domain.contextValue];
  const actions = MANAGE_DOMAIN_ACTIONS[domain.domainId] ?? {};
  for (const [actionKey, actionId] of Object.entries(actions) as Array<[ManageActionKey, string]>) {
    const action = backendActionById(actionId);
    if (action && canShowActionForItem(action, actionKey, row.state, bridge, workspaceTrusted, remoteName)) {
      tokens.push(actionContextToken(actionKey));
    }
  }
  return tokens.join(" ");
}

export function canShowActionForItem(
  action: BackendActionMetadata,
  actionKey: ManageActionKey,
  state: ManageRowState,
  bridge: AlysisBridgeClient,
  workspaceTrusted: boolean,
  remoteName?: string
): boolean {
  if (!isBackendActionSupportedByCapabilities(action, {
    supportsMethod: (method) =>
      bridge.supportsMethod(method as Parameters<AlysisBridgeClient["supportsMethod"]>[0]),
    featureValue: (path) => bridge.featureValue(path)
  })) {
    return false;
  }
  if ((action.mutates || action.workspaceTrustRequired) && !workspaceTrusted) {
    return false;
  }
  switch (actionKey) {
    case "trust":
      return state.trusted === false;
    case "untrust":
      return state.trusted === true;
    case "enable":
      return state.enabled === false;
    case "disable":
      return state.enabled === true;
    case "remove":
      return state.removable === true;
    case "mcpLogin":
      return !mcpOAuthRemoteUnavailableReason(remoteName)
        && state.authEnabled === true
        && state.authenticated !== true;
    case "mcpLogout":
      return state.authEnabled === true && state.authenticated === true;
    case "info":
      return true;
  }
}

function actionContextToken(actionKey: ManageActionKey): string {
  switch (actionKey) {
    case "info":
      return "alysisManageActionInfo";
    case "trust":
      return "alysisManageActionTrust";
    case "untrust":
      return "alysisManageActionUntrust";
    case "enable":
      return "alysisManageActionEnable";
    case "disable":
      return "alysisManageActionDisable";
    case "remove":
      return "alysisManageActionRemove";
    case "mcpLogin":
      return "alysisManageActionMcpLogin";
    case "mcpLogout":
      return "alysisManageActionMcpLogout";
  }
}

function records(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value)
    ? (value.filter((entry) => entry && typeof entry === "object" && !Array.isArray(entry)) as Record<string, unknown>[])
    : [];
}

function compactState(state: ManageRowState): ManageRowState {
  return Object.fromEntries(
    Object.entries(state).filter(([, value]) => value !== undefined)
  ) as ManageRowState;
}

function firstString(record: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.length > 0) {
      return value;
    }
  }
  return "";
}

function booleanFromValue(value: unknown): boolean | undefined {
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value !== "string") {
    return undefined;
  }
  const normalized = value.trim().toLowerCase();
  if (["true", "yes", "enabled", "present", "active"].includes(normalized)) {
    return true;
  }
  if (["false", "no", "disabled", "absent", "inactive"].includes(normalized)) {
    return false;
  }
  return undefined;
}

function yesNoFromValue(value: unknown): boolean | undefined {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (normalized === "yes") {
    return true;
  }
  if (normalized === "no") {
    return false;
  }
  return booleanFromValue(value);
}

function trustedFromValue(value: unknown): boolean | undefined {
  if (typeof value === "boolean") {
    return value;
  }
  const normalized = String(value ?? "").trim().toLowerCase();
  if (normalized === "trusted" || normalized === "yes" || normalized === "true") {
    return true;
  }
  if (normalized === "untrusted" || normalized === "no" || normalized === "false") {
    return false;
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function workspaceRoot(): string {
  return activeWorkspaceRoot() ?? "";
}
