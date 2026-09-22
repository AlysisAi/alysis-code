export type ManageActionKey =
  | "info"
  | "trust"
  | "untrust"
  | "enable"
  | "disable"
  | "remove"
  | "mcpLogin"
  | "mcpLogout";

export const MANAGE_DOMAIN_ACTIONS: Record<string, Partial<Record<ManageActionKey, string>>> = {
  tools: { info: "tool.info", trust: "tool.trust", untrust: "tool.untrust" },
  skills: { info: "skill.info", enable: "skill.enable", disable: "skill.disable", remove: "skill.remove" },
  mcp: { mcpLogin: "mcp.auth.login.start", mcpLogout: "mcp.auth.logout" },
  hooks: { disable: "hooks.disable" },
  conventions: { info: "conventions.render" },
  extensions: { info: "ext.info", enable: "ext.enable", disable: "ext.disable", remove: "ext.uninstall" }
};

export const MANAGE_ITEM_COMMANDS: ReadonlyArray<{ commandId: string; actionKey: ManageActionKey }> = [
  { commandId: "alysis.manage.info", actionKey: "info" },
  { commandId: "alysis.manage.trust", actionKey: "trust" },
  { commandId: "alysis.manage.untrust", actionKey: "untrust" },
  { commandId: "alysis.manage.enable", actionKey: "enable" },
  { commandId: "alysis.manage.disable", actionKey: "disable" },
  { commandId: "alysis.manage.remove", actionKey: "remove" },
  { commandId: "alysis.manage.mcpLogin", actionKey: "mcpLogin" },
  { commandId: "alysis.manage.mcpLogout", actionKey: "mcpLogout" }
];
