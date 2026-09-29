export type BackendActionGroupId =
  | "session"
  | "profiles"
  | "toolsSkills"
  | "mcpHooks"
  | "extensions"
  | "healthDiagnostics"
  | "forgeAssets";

export interface BackendActionGroup {
  id: BackendActionGroupId;
  commandId: string;
  title: string;
  description: string;
}

export interface BackendActionMetadata {
  id: string;
  commandId: string;
  group: BackendActionGroupId;
  title: string;
  description: string;
  slashAliases: readonly string[];
  requiredMethods: readonly string[];
  capabilityPath?: readonly string[];
  mutates: boolean;
  mayMutate: boolean;
  mutationDeterminedByParams: boolean;
  mutationRequiresWorkspaceTrust: boolean;
  workspaceTrustRequired: boolean;
  workspaceTrustExemptionRationale?: string;
  workspaceRequired: boolean;
  requiresActiveSession: boolean;
  requiresActivePlan: boolean;
  parameterStrategy: string;
  confirmationRequired?: boolean;
  credentialsRequired?: boolean;
}

export interface BackendActionCapabilitySource {
  supportsMethod(method: string): boolean;
  featureValue(path: readonly string[]): unknown;
}

export const BACKEND_ACTION_GROUPS: readonly BackendActionGroup[] = [
  {
    id: "session",
    commandId: "alysis.sessionActions",
    title: "Current Task Tools",
    description: "View or adjust the current Alysis Code task."
  },
  {
    id: "profiles",
    commandId: "alysis.manageProfiles",
    title: "Model and Provider Settings",
    description: "View configuration and manage model profiles."
  },
  {
    id: "toolsSkills",
    commandId: "alysis.manageToolsSkills",
    title: "Manage Tools and Skills",
    description: "View and manage the tools and skills Alysis Code can use."
  },
  {
    id: "mcpHooks",
    commandId: "alysis.manageMcpHooks",
    title: "Manage MCP, Hooks, and Conventions",
    description: "View advanced integrations and project rules."
  },
  {
    id: "extensions",
    commandId: "alysis.manageExtensions",
    title: "Manage Alysis Code Extensions",
    description: "Search, inspect, install, and toggle Alysis Code extension packages."
  },
  {
    id: "healthDiagnostics",
    commandId: "alysis.healthDiagnostics",
    title: "Troubleshooting",
    description: "Check setup, safety, updates, and diagnostic information."
  },
  {
    id: "forgeAssets",
    commandId: "alysis.manageForgeAssets",
    title: "Plan Files and References",
    description: "View or update files and references attached to the current plan."
  }
] as const;

export const BACKEND_ACTIONS: readonly BackendActionMetadata[] = [
  action("session.status", "session", "Session Status", "Show live session status.", ["/pwd", "/status"], ["session.status"], false, false, true, false, "active-session"),
  action("session.usage", "session", "Session Usage", "Show usage for a live or retained session.", ["/usage"], ["session.usage"], false, false, false, false, "selected-or-active-session"),
  action("session.history", "session", "Search Session History", "Search live session history.", ["/history"], ["session.history"], false, false, true, false, "query"),
  action("session.context", "session", "Session Context", "Show limited live context metadata.", ["/context", "/ctx"], ["session.context"], false, false, true, false, "active-session"),
  action("session.modelInfo", "session", "Model Info", "Show redacted model and provider metadata for the active session.", ["/model-info"], ["session.modelInfo"], false, false, true, false, "optional-model"),
  action("session.subagents.status", "session", "Subagent Status", "Show or toggle safe subagent status for the active session.", ["/subagents"], ["session.subagents.status"], false, false, true, false, "subagent-status-or-toggle"),
  action("session.subagents.setEnabled", "session", "Toggle Subagents", "Toggle subagents for subsequent live session turns.", [], ["session.subagents.setEnabled"], true, true, true, false, "subagent-on-off", true),
  action("session.trace.status", "session", "Trace Status", "Show bounded redacted trace status or events for the active session.", ["/trace"], ["session.trace.status"], false, false, true, false, "trace-status-or-level"),
  action("session.trace.setLevel", "session", "Set Trace Level", "Set active session trace detail to off, compact, or full.", [], ["session.trace.setLevel"], true, false, true, false, "trace-level"),
  action("session.trace.listEvents", "session", "List Trace Events", "List bounded redacted structured trace events.", [], ["session.trace.listEvents"], false, false, true, false, "active-session"),
  action("session.trace.readArtifact", "session", "Read Trace Artifact", "Read a bounded redacted trace artifact.", [], ["session.trace.readArtifact"], false, false, true, false, "artifact-id"),
  action("session.trace.clear", "session", "Clear Trace Events", "Clear retained IDE trace events for the active session.", [], ["session.trace.clear"], true, false, true, false, "active-session", true),
  action("session.terminals.list", "session", "List Terminals", "List existing background terminals for the active session.", ["/terminals"], ["session.terminals.list"], false, false, true, false, "terminals-command"),
  action("session.terminals.show", "session", "Show Terminal", "Show bounded redacted output for an existing background terminal.", [], ["session.terminals.show"], false, false, true, false, "terminal-id"),
  action("session.terminals.kill", "session", "Kill Terminal", "Kill an existing managed background terminal.", [], ["session.terminals.kill"], true, true, true, false, "terminal-id", true),
  action("session.terminals.clear", "session", "Clear Terminal Output", "Clear retained output for a managed background terminal if supported.", [], ["session.terminals.clear"], true, true, true, false, "terminal-id", true),
  action("session.compact", "session", "Compact Session", "Request structured compaction metadata.", ["/compact"], ["session.compact"], true, true, true, false, "optional-focus", true),
  action("session.resume", "session", "Resume Session", "Replay a retained session into a live IDE session.", ["/resume"], ["session.resume"], true, true, false, false, "target-session-id", true),
  action("session.images.list", "session", "List Session Images", "List pending images for the next chat turn.", ["/images"], ["session.images.list"], false, false, true, false, "active-session"),
  action("session.images.add", "session", "Add Session Image", "Attach a workspace-scoped image for the next chat turn.", ["/image", "/paste-image"], ["session.images.add"], true, false, true, false, "image-path"),
  action("session.images.clear", "session", "Clear Session Images", "Clear pending images for the next chat turn.", ["/clear-images"], ["session.images.clear"], true, false, true, false, "active-session"),
  // /permissions is owned by the core router because it must update the active session when present and
  // otherwise update the extension default. Advertising the backend alias made two owners for the
  // same token even though the core parser always won.
  action("session.setMode", "session", "Set Session Permissions", "Change what the active session may do.", [], ["session.setMode"], true, false, true, false, "mode"),
  action("session.setModel", "session", "Set Session Model", "Change the active session model.", ["/model"], ["session.setModel"], true, false, true, false, "model"),
  action("session.setStream", "session", "Set Session Streaming", "Toggle active session streaming.", ["/stream"], ["session.setStream"], true, false, true, false, "stream-on-off"),
  // Moving the agent's working directory re-scopes every later file operation, so it requires trust.
  action("session.setActiveWorkdir", "session", "Set Active Workdir", "Change the active session workdir.", ["/cd"], ["session.setActiveWorkdir"], true, true, true, false, "workspace-relative-path"),
  action("session.clear", "session", "Clear Session", "Clear live session messages.", ["/clear"], ["session.clear"], true, true, true, false, "active-session", true),
  action("session.show", "session", "Show Retained Session", "Show retained session metadata.", [], ["session.show"], false, false, false, false, "session-id"),
  action("session.score", "session", "Score Sessions", "Show selected or latest retained session scoring metadata.", [], ["session.score"], false, false, false, false, "session-id-or-latest"),
  action("session.search", "session", "Search Past Tasks", "Search bounded, redacted history from this workspace.", [], ["session.search"], false, false, true, false, "session-search-query"),
  action("chat.queue.list", "session", "Queued Messages", "Show pending and recently processed messages for the current task.", [], ["chat.queue.list"], false, false, true, false, "active-session"),
  action("chat.queue.get", "session", "Queued Message Details", "Show safe metadata and the bounded preview for one queued message.", [], ["chat.queue.get"], false, false, true, false, "prompt-id"),
  action("chat.queue.delete", "session", "Remove Queued Message", "Cancel a pending queued message before Alysis Code starts it.", [], ["chat.queue.delete"], true, false, true, false, "prompt-id", true),
  action("checkpoint.list", "session", "Checkpoints", "Show recoverable workspace checkpoints for the current task.", [], ["checkpoint.list"], false, false, true, false, "active-session"),
  action("checkpoint.diff", "session", "Checkpoint Changes", "Preview the bounded changes recorded by a checkpoint.", [], ["checkpoint.diff"], false, false, true, false, "checkpoint-id"),
  action("checkpoint.revert", "session", "Revert to Checkpoint", "Restore workspace files to a selected checkpoint with explicit confirmation.", [], ["checkpoint.revert"], true, true, true, false, "checkpoint-id", true),
  action("checkpoint.redo", "session", "Redo Checkpoint", "Reapply the most recently reverted checkpoint with explicit confirmation.", [], ["checkpoint.redo"], true, true, true, false, "active-session", true),
  action("checkpoint.branch", "session", "Branch from Checkpoint", "Create a Git reference for a selected checkpoint without changing the working tree.", [], ["checkpoint.branch"], true, true, true, false, "checkpoint-id-and-branch", true),
  {
    ...action("code.review.start", "session", "Start Code Review", "Start a structured review of working-tree, branch, commit, or revision-range changes.", [], ["code.review.start", "code.review.result"], true, true, true, false, "code-review-scope", true),
    credentialsRequired: true
  },
  action("code.review.result", "session", "Code Review Results", "Show bounded, redacted findings for a structured code review job.", [], ["code.review.result"], false, false, true, false, "review-job-id"),
  action("report.create", "session", "Create Report", "Create an Alysis Code feedback report.", ["/report", "/feedback"], ["report.create"], true, true, false, false, "feedback", true),

  action("config.get", "profiles", "Show Config", "Show sanitized Alysis Code config.", [], ["config.get"], false, false, false, false, "none"),
  action("config.schema", "profiles", "Show Config Schema", "Show config schema metadata.", [], ["config.schema"], false, false, false, false, "none"),
  action("config.validate", "profiles", "Validate Config", "Validate Alysis Code config.", [], ["config.validate"], false, false, false, false, "none"),
  action("config.set", "profiles", "Set Config Value", "Set a non-secret Alysis Code config key.", ["/config set"], ["config.set", "config.schema"], true, true, false, false, "config-key-value", true),
  action("profile.list", "profiles", "List Profiles", "List configured profiles.", ["/profiles"], ["profile.list"], false, false, false, false, "none"),
  action("profile.show", "profiles", "Show Profile", "Show one profile.", ["/profile"], ["profile.show"], false, false, false, false, "profile-name"),
  action("profile.use", "profiles", "Use Profile", "Switch active profile.", ["/profile use"], ["profile.use"], true, true, false, false, "profile-name", true),
  action("profile.presets", "profiles", "List Profile Presets", "List provider profile presets.", [], ["profile.presets"], false, false, false, false, "none"),
  action("profile.preset", "profiles", "Create Profile From Preset", "Create a profile from a preset.", [], ["profile.preset"], true, true, false, false, "profile-preset", true),
  action("profile.convert", "profiles", "Convert Profile To Preset", "Convert a profile to a preset.", [], ["profile.convert"], true, true, false, false, "profile-convert", true),
  action("profile.add", "profiles", "Add Profile", "Add a profile without collecting secrets.", [], ["profile.add"], true, true, false, false, "profile-add", true),
  action("profile.remove", "profiles", "Remove Profile", "Remove a profile.", [], ["profile.remove"], true, true, false, false, "profile-name", true),
  action("profile.rename", "profiles", "Rename Profile", "Rename a profile.", [], ["profile.rename"], true, true, false, false, "profile-rename", true),

  action("tools.catalog", "toolsSkills", "Tools Catalog", "Show built-in tool catalog.", ["/tools"], ["tools.catalog"], false, false, false, false, "none"),
  action("tool.list", "toolsSkills", "List Custom Tools", "List workspace tools.", [], ["tool.list"], false, false, false, false, "workspace"),
  action("tool.info", "toolsSkills", "Tool Info", "Show custom tool info.", [], ["tool.info"], false, false, false, false, "tool-name"),
  action("tool.trust", "toolsSkills", "Trust Tool", "Trust a project tool.", [], ["tool.trust"], true, true, false, false, "tool-name", true),
  action("tool.untrust", "toolsSkills", "Untrust Tool", "Remove trust for a project tool.", [], ["tool.untrust"], true, true, false, false, "tool-name", true),
  action("skill.list", "toolsSkills", "List Skills", "List available skills.", ["/skills"], ["skill.list"], false, false, false, false, "workspace"),
  action("skill.info", "toolsSkills", "Skill Info", "Show skill info.", ["/skill"], ["skill.info"], false, false, false, false, "skill-name"),
  action("skill.validate", "toolsSkills", "Validate Skill", "Validate a skill by name or bundle path.", [], ["skill.validate"], false, false, false, false, "skill-validate"),
  action("skill.init", "toolsSkills", "Initialize Skill", "Create a skill scaffold.", [], ["skill.init"], true, true, false, false, "skill-init", true),
  action("skill.install", "toolsSkills", "Install Skill", "Install a skill bundle.", [], ["skill.install"], true, true, false, false, "skill-install", true),
  action("skill.enable", "toolsSkills", "Enable Skill", "Enable a skill.", [], ["skill.enable"], true, true, false, false, "skill-name", true),
  action("skill.disable", "toolsSkills", "Disable Skill", "Disable a skill.", [], ["skill.disable"], true, true, false, false, "skill-name", true),
  action("skill.remove", "toolsSkills", "Remove Skill", "Remove a managed skill.", [], ["skill.remove"], true, true, false, false, "skill-name", true),
  action("permission.rules.list", "toolsSkills", "Permission Rules", "Inspect persistent allow, ask, and deny rules without exposing command patterns.", [], ["permission.rules.list"], false, false, false, false, "none"),
  action("permission.rules.grant", "toolsSkills", "Add Permission Rule", "Add a scoped permission rule after explicit review.", [], ["permission.rules.grant"], true, true, false, false, "permission-rule", true),
  action("permission.rules.revoke", "toolsSkills", "Remove Permission Rule", "Remove one persistent permission rule after explicit confirmation.", [], ["permission.rules.revoke"], true, true, false, false, "permission-rule-id", true),
  action("permission.evaluate", "toolsSkills", "Test Permission Decision", "Explain how current policy would handle a tool, path, or command without running it.", [], ["permission.evaluate"], false, false, false, false, "permission-evaluation"),
  action("permission.session.list", "toolsSkills", "Task Permission Grants", "Inspect temporary permission grants for the current task.", [], ["permission.session.list"], false, false, true, false, "active-session"),
  action("permission.session.revoke", "toolsSkills", "Revoke Task Permission", "Revoke one temporary permission grant for the current task.", [], ["permission.session.revoke"], true, false, true, false, "permission-grant-id", true),

  action("mcp.status", "mcpHooks", "MCP Status", "Show MCP server status.", ["/mcp"], ["mcp.status"], false, false, false, false, "workspace"),
  action("mcp.prompts.list", "mcpHooks", "List MCP Prompts", "List MCP prompts.", [], ["mcp.prompts.list"], false, false, false, false, "workspace"),
  action("mcp.prompts.get", "mcpHooks", "Get MCP Prompt", "Render one MCP prompt.", [], ["mcp.prompts.get"], false, false, false, false, "mcp-prompt"),
  action("mcp.auth.status", "mcpHooks", "MCP Auth Status", "Show MCP auth status.", [], ["mcp.auth.status"], false, false, false, false, "workspace"),
  action("mcp.auth.logout", "mcpHooks", "MCP Auth Logout", "Remove MCP OAuth token state.", [], ["mcp.auth.logout"], true, true, false, false, "mcp-server", true),
  action("mcp.auth.login.start", "mcpHooks", "MCP Auth Login", "Start an OAuth login for an MCP server.", [], ["mcp.auth.login.start", "mcp.auth.login.status", "mcp.auth.login.cancel"], true, true, false, false, "mcp-server", true),
  action("hooks.list", "mcpHooks", "List Hooks", "List hooks.", ["/hooks"], ["hooks.list"], false, false, false, false, "workspace"),
  action("hooks.doctor", "mcpHooks", "Hooks Doctor", "Run hooks diagnostics.", [], ["hooks.doctor"], false, false, false, false, "workspace"),
  action("hooks.effective", "mcpHooks", "Effective Hooks", "Show effective hooks.", [], ["hooks.effective"], false, false, false, false, "hook-event"),
  action("hooks.test", "mcpHooks", "Test Hooks", "Test hook matching.", [], ["hooks.test"], false, false, false, false, "hook-event"),
  action("hooks.trace", "mcpHooks", "Hook Trace", "Show hook trace events.", [], ["hooks.trace"], false, false, false, false, "optional-session"),
  action("hooks.trust", "mcpHooks", "Trust Hooks Config", "Trust project hooks config.", [], ["hooks.trust"], true, true, false, false, "project-config", true),
  action("hooks.untrust", "mcpHooks", "Untrust Hooks Config", "Untrust project hooks config.", [], ["hooks.untrust"], true, true, false, false, "project-config", true),
  action("hooks.init", "mcpHooks", "Initialize Hooks", "Create hooks config.", [], ["hooks.init"], true, true, false, false, "none", true),
  action("hooks.enable", "mcpHooks", "Enable Hook", "Enable a hook.", [], ["hooks.enable"], true, true, false, false, "hook-id", true),
  action("hooks.disable", "mcpHooks", "Disable Hook", "Disable a hook.", [], ["hooks.disable"], true, true, false, false, "hook-id", true),
  action("conventions.list", "mcpHooks", "List Conventions", "List convention docs.", [], ["conventions.list"], false, false, false, false, "workspace"),
  action("conventions.render", "mcpHooks", "Render Conventions", "Render convention docs.", [], ["conventions.render"], false, false, false, false, "workspace"),

  action("ext.search", "extensions", "Search Extensions", "Search Alysis Code extension packages.", [], ["ext.search"], false, false, false, false, "query"),
  action("ext.list", "extensions", "List Extensions", "List installed extension packages.", [], ["ext.list"], false, false, false, false, "workspace"),
  action("ext.info", "extensions", "Extension Info", "Show extension package info.", [], ["ext.info"], false, false, false, false, "extension-id"),
  action("ext.install", "extensions", "Install Extension", "Install an extension package through trust review.", [], ["ext.install"], true, true, false, false, "extension-source", true),
  action("ext.uninstall", "extensions", "Uninstall Extension", "Uninstall an extension package.", [], ["ext.uninstall"], true, true, false, false, "extension-id", true),
  action("ext.enable", "extensions", "Enable Extension", "Enable an extension package.", [], ["ext.enable"], true, true, false, false, "extension-id", true),
  action("ext.disable", "extensions", "Disable Extension", "Disable an extension package.", [], ["ext.disable"], true, true, false, false, "extension-id", true),

  action("doctor.summary", "healthDiagnostics", "Doctor Summary", "Show structured Alysis Code doctor summary.", [], ["doctor.summary"], false, false, false, false, "none"),
  action("doctor.providers", "healthDiagnostics", "Provider Diagnostics", "Show provider diagnostics without exposing secrets.", [], ["doctor.providers"], false, false, false, false, "none"),
  action("doctor.bundle", "healthDiagnostics", "Doctor Bundle", "Create a redacted diagnostic bundle payload.", [], ["doctor.bundle"], false, false, false, false, "none"),
  action("sandbox.doctor", "healthDiagnostics", "Sandbox Doctor", "Run structured sandbox diagnostics.", [], ["sandbox.doctor"], false, false, false, false, "sandbox-doctor"),
  action("sandbox.setup", "healthDiagnostics", "Sandbox Setup", "Set up the configured sandbox runtime after explicit confirmation.", [], ["sandbox.setup"], true, true, false, false, "sandbox-setup", true),
  action("sandbox.pull", "healthDiagnostics", "Pull Sandbox Images", "Pull sandbox images after explicit confirmation.", [], ["sandbox.pull"], true, true, false, false, "sandbox-pull", true),
  action("update.check", "healthDiagnostics", "Check for Updates", "Check cached update status, with online checks only by explicit selection.", ["/update"], ["update.check"], false, false, false, false, "update-check"),

  action("forge.show", "forgeAssets", "Show Forge Plan", "Show structured details for the active or selected Forge plan.", ["/forge show", "/forge plan show"], ["forge.show"], false, false, false, false, "forge-plan-id"),
  action("forge.plan.getState", "forgeAssets", "Forge Plan State", "Show typed Forge plan state, goal, assistant instruction, and validation metadata.", ["/forge plan state"], ["forge.plan.getState"], false, false, false, true, "active-plan"),
  dynamicMutation(action("forge.plan.setAssistant", "forgeAssets", "Forge Assistant", "Read or update the active Forge plan assistant instruction.", ["/assistant"], ["forge.plan.getState", "forge.plan.setAssistant"], false, false, false, true, "forge-assistant"), true),
  dynamicMutation(action("forge.plan.setGoal", "forgeAssets", "Forge Goal", "Read or update the active Forge plan goal.", ["/goal"], ["forge.plan.getState", "forge.plan.setGoal"], false, false, false, true, "forge-goal"), true),
  dynamicMutation(action("forge.plan.updateTask", "forgeAssets", "Forge Task", "Show or safely update an existing Forge plan task.", ["/task"], ["forge.plan.updateTask"], false, false, false, true, "forge-task"), true),
  action("forge.plan.validate", "forgeAssets", "Validate Forge Plan", "Validate active Forge plan quality metadata.", ["/forge plan validate"], ["forge.plan.validate"], false, false, false, true, "active-plan"),
  {
    ...action("forge.plan.regenerate", "forgeAssets", "Regenerate Forge Plan", "Regenerate the active Forge plan through typed IDE protocol with optimistic revision support.", ["/forge plan regenerate"], ["forge.plan.regenerate"], true, true, false, true, "forge-plan-regenerate", true),
    credentialsRequired: true
  },
  action("forge.assets.list", "forgeAssets", "List Forge Assets", "List assets for the active Forge plan.", ["/assets", "/asset list"], ["forge.assets.list"], false, false, false, true, "active-plan"),
  action("forge.assets.show", "forgeAssets", "Show Forge Asset", "Show an asset from the active Forge plan.", ["/asset show"], ["forge.assets.show"], false, false, false, true, "asset-id"),
  action("forge.assets.add", "forgeAssets", "Add Forge Asset", "Add a workspace-scoped asset to the active Forge plan.", ["/asset add"], ["forge.assets.add"], true, true, false, true, "asset-source", true),
  action("forge.assets.delete", "forgeAssets", "Delete Forge Asset", "Delete an asset from the active Forge plan.", ["/asset delete"], ["forge.assets.delete"], true, true, false, true, "asset-id", true),
  action("forge.assets.edit", "forgeAssets", "Edit Forge Asset", "Edit asset metadata.", ["/asset edit"], ["forge.assets.edit"], true, true, false, true, "asset-edit", true),
  action("forge.assets.refresh", "forgeAssets", "Refresh Forge Asset", "Refresh asset comprehension.", ["/asset refresh"], ["forge.assets.refresh"], true, true, false, true, "asset-id", true),
  action("forge.assets.cancelPending", "forgeAssets", "Cancel Pending Asset Work", "Cancel pending asset work.", ["/asset cancel-pending"], ["forge.assets.cancelPending"], false, false, false, true, "active-plan"),
  action("forge.assets.checkPlan", "forgeAssets", "Check Forge Asset Plan Links", "Check plan references against assets.", ["/asset check"], ["forge.assets.checkPlan"], false, false, false, true, "active-plan"),
  action("forge.assets.pruneLegacy", "forgeAssets", "Prune Legacy Forge Assets", "Prune legacy plan assets.", ["/asset prune"], ["forge.assets.pruneLegacy"], true, true, false, true, "active-plan", true),
  action("forge.attach", "forgeAssets", "Attach Forge Asset", "Attach a source path to the active Forge plan.", [], ["forge.attach"], true, true, false, true, "asset-source", true),
  {
    ...action("forge.review", "forgeAssets", "Review Forge Task", "Run structured review for one task in the active Forge plan.", ["/forge review", "/review"], ["forge.review"], true, true, false, true, "forge-task-id", true),
    credentialsRequired: true
  }
] as const;

export function backendActionById(id: string): BackendActionMetadata | undefined {
  return BACKEND_ACTIONS.find((action) => action.id === id);
}

export function backendActionGroupByCommandId(commandId: string): BackendActionGroup | undefined {
  return BACKEND_ACTION_GROUPS.find((group) => group.commandId === commandId);
}

export function backendActionsForGroup(group: BackendActionGroupId): BackendActionMetadata[] {
  return BACKEND_ACTIONS.filter((action) => action.group === group);
}

export function isBackendActionSupportedByCapabilities(
  action: BackendActionMetadata,
  capabilities: BackendActionCapabilitySource
): boolean {
  const methodsSupported = action.requiredMethods.every((method) => capabilities.supportsMethod(method));
  if (action.capabilityPath) {
    const featureValue = capabilities.featureValue(action.capabilityPath);
    if (featureValue === false) {
      return false;
    }
    if (featureValue === true) {
      return methodsSupported;
    }
  }
  return methodsSupported;
}

export function backendActionMutationLabel(action: BackendActionMetadata): "Can make changes" | "View or update" | "View only" {
  if (action.mutationDeterminedByParams) {
    return "View or update";
  }
  return action.mutates ? "Can make changes" : "View only";
}

export function backendActionMutatesWithParams(
  action: BackendActionMetadata,
  params: Record<string, unknown>
): boolean {
  if (!action.mutationDeterminedByParams) {
    return action.mutates;
  }
  switch (action.id) {
    case "forge.plan.setAssistant":
      return typeof params.instruction === "string" && params.instruction.trim().length > 0;
    case "forge.plan.setGoal":
      return typeof params.goal === "string" && params.goal.trim().length > 0;
    case "forge.plan.updateTask":
      return (
        typeof params.status === "string" ||
        typeof params.title === "string" ||
        typeof params.body === "string"
      );
    default:
      return action.mutates;
  }
}

/**
 * Some read-only actions reach a confirmation-gated mutation once their parameters are known
 * (`/subagents on` performs session.subagents.setEnabled, `/trace clear` performs session.trace.clear).
 * Return the confirmation of the action whose gate applies, or undefined when none does.
 */
export function backendActionConfirmation(
  action: BackendActionMetadata,
  params: Record<string, unknown>
): { title: string; mutates: boolean } | undefined {
  if (action.confirmationRequired) {
    return { title: action.title, mutates: backendActionMutatesWithParams(action, params) };
  }
  const delegated = delegatedConfirmationActionId(action, params);
  if (!delegated) {
    return undefined;
  }
  const target = backendActionById(delegated);
  return { title: target?.title ?? action.title, mutates: target?.mutates ?? true };
}

function delegatedConfirmationActionId(
  action: BackendActionMetadata,
  params: Record<string, unknown>
): string | undefined {
  switch (action.id) {
    case "session.subagents.status":
      return typeof params.enabled === "boolean" ? "session.subagents.setEnabled" : undefined;
    case "session.trace.status":
      return params.operation === "clear" ? "session.trace.clear" : undefined;
    default:
      return undefined;
  }
}

function action(
  id: string,
  group: BackendActionGroupId,
  title: string,
  description: string,
  slashAliases: readonly string[],
  requiredMethods: readonly string[],
  mutates: boolean,
  workspaceTrustRequired: boolean,
  requiresActiveSession: boolean,
  requiresActivePlan: boolean,
  parameterStrategy: string,
  confirmationRequired = false,
  capabilityPath?: readonly string[]
): BackendActionMetadata {
  return {
    id,
    commandId: `alysis.backend.${id}`,
    group,
    title,
    description,
    slashAliases,
    requiredMethods,
    capabilityPath: capabilityPath ?? defaultCapabilityPath(id, group),
    mutates,
    mayMutate: mutates,
    mutationDeterminedByParams: false,
    mutationRequiresWorkspaceTrust: mutates && workspaceTrustRequired,
    workspaceTrustRequired,
    workspaceTrustExemptionRationale: mutates && !workspaceTrustRequired
      ? defaultWorkspaceTrustExemptionRationale(id)
      : undefined,
    workspaceRequired: defaultWorkspaceRequired(id),
    requiresActiveSession,
    requiresActivePlan,
    parameterStrategy,
    confirmationRequired
  };
}

function dynamicMutation(
  metadata: BackendActionMetadata,
  mutationRequiresWorkspaceTrust: boolean
): BackendActionMetadata {
  return {
    ...metadata,
    mayMutate: true,
    mutationDeterminedByParams: true,
    mutationRequiresWorkspaceTrust,
    workspaceTrustExemptionRationale: mutationRequiresWorkspaceTrust
      ? metadata.workspaceTrustExemptionRationale
      : metadata.workspaceTrustExemptionRationale ?? defaultWorkspaceTrustExemptionRationale(metadata.id)
  };
}

function defaultCapabilityPath(
  id: string,
  group: BackendActionGroupId
): readonly string[] | undefined {
  if (group === "forgeAssets") {
    if (id === "forge.show") {
      return ["forge", "show", "supported"];
    }
    if (id.startsWith("forge.plan.")) {
      return ["forge", "plan_editing", "methods", id, "supported"];
    }
    if (id === "forge.attach") {
      return ["forge", "attach", "supported"];
    }
    if (id.startsWith("forge.assets.")) {
      return ["forge", "assets", "supported"];
    }
    if (id === "forge.review") {
      return ["forge", "review", "supported"];
    }
  }
  if (isManagementAction(id)) {
    return ["management", "methods", id, "supported"];
  }
  if (id === "session.context" || id === "session.compact" || id === "session.resume") {
    return ["run_chat_options", "session_method_capabilities", id, "supported"];
  }
  if (id === "session.modelInfo") {
    return ["run_chat_options", "session_method_capabilities", "session.modelInfo", "supported"];
  }
  if (id.startsWith("session.subagents.")) {
    return ["run_chat_options", "session_method_capabilities", "session.subagents", "supported"];
  }
  if (id.startsWith("session.trace.")) {
    return ["run_chat_options", "session_method_capabilities", "session.trace", "supported"];
  }
  if (id.startsWith("session.terminals.")) {
    return ["run_chat_options", "session_method_capabilities", "session.terminals", "supported"];
  }
  if (id.startsWith("session.images.")) {
    return ["run_chat_options", "session_method_capabilities", "session.images", "supported"];
  }
  return undefined;
}

function isManagementAction(id: string): boolean {
  return (
    id.startsWith("config.") ||
    id.startsWith("profile.") ||
    id.startsWith("tool.") ||
    id.startsWith("skill.") ||
    id.startsWith("mcp.") ||
    id.startsWith("hooks.") ||
    id.startsWith("conventions.") ||
    id.startsWith("ext.") ||
    id.startsWith("doctor.") ||
    id.startsWith("sandbox.") ||
    id.startsWith("update.") ||
    id === "tools.catalog" ||
    id === "session.show" ||
    id === "session.score" ||
    id === "report.create"
  );
}

function defaultWorkspaceRequired(id: string): boolean {
  if (id === "report.create") {
    return true;
  }
  if (id.startsWith("tool.") || id.startsWith("skill.") || id.startsWith("mcp.") || id.startsWith("conventions.")) {
    return true;
  }
  if (id.startsWith("hooks.") && id !== "hooks.trace") {
    return true;
  }
  if (id.startsWith("ext.") && id !== "ext.search") {
    return true;
  }
  if (id.startsWith("checkpoint.") || id.startsWith("code.review.")) {
    return true;
  }
  return false;
}

function defaultWorkspaceTrustExemptionRationale(id: string): string | undefined {
  if (id === "chat.queue.delete" || id === "permission.session.revoke") {
    return "Removes pending work or narrows a temporary permission grant; it cannot add authority or mutate workspace files.";
  }
  if (id === "session.compact") {
    return "Mutates only the active in-memory IDE session context; the backend remains responsible for provider and session safety.";
  }
  if (id.startsWith("session.images.")) {
    return "Mutates only the active IDE session image basket; backend path validation remains workspace-scoped.";
  }
  if (
    id === "session.setMode" ||
    id === "session.setModel" ||
    id === "session.setStream" ||
    id === "session.clear" ||
    id.startsWith("session.trace.")
  ) {
    return "Mutates only live IDE session settings or memory, not workspace files or persistent trust state.";
  }
  return undefined;
}
