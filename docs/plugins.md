# Plugins

An Alysis Code plugin is a declarative bundle that contributes one or more skills, custom tools, MCP servers, and hooks through a single `alysis-plugin.toml` file at the plugin root.

Plugins are intentionally explicit. They are installed from pinned sources, validated before use, and enabled only after trust is recorded for the relevant user or workspace. Cloning a repository or copying a directory onto disk does not activate a plugin by itself.

## Quickstart

Create a plugin root directory, place a `alysis-plugin.toml` file at the top level, and add the component files or directories referenced by the manifest.

```toml
schema_version = 1

[plugin]
id = "acme.demo"
name = "Demo Plugin"
version = "1.0.0"
description = "Example Alysis Code plugin"
author = "Acme Inc"
license = "Apache-2.0"

[compatibility]
alysis = ">=0.1"
```

This manifest is valid, but it declares no components. Alysis Code accepts metadata-only plugins and emits a warning until you add at least one skill, tool, MCP server, or hook.

## Installing Plugins

Install sources must be pinned. You can install from a curated registry id, from a direct `git+https://...git@<40-char-commit>` source, or from a bare HTTPS git URL with a `#<40-char-commit>` fragment.

```bash
alysis ext install acme.demo
alysis ext install git+https://github.com/acme/demo-plugin.git@0123456789abcdef0123456789abcdef01234567
alysis ext install https://github.com/acme/demo-plugin.git#0123456789abcdef0123456789abcdef01234567
```

Plugins install globally by default. Use `--project` to install into the current repository's `.alysis/` directory instead:

```bash
alysis ext install acme.demo --project
```

Before writing anything, Alysis Code clones the pinned commit into a temporary staging directory, checks out exactly that commit, validates `alysis-plugin.toml`, computes the manifest SHA-256, and shows a trust prompt. The prompt includes the plugin id, name, version, source URL, commit, manifest hash, component counts, requested environment variables, MCP scopes, hook events, network access, filesystem write access, and security contact when present.

The default prompt answer is no. Pass `--yes` to accept the displayed trust prompt without an interactive confirmation. Pass `--ci`, or set `ALYSIS_CI=1`, only in automation; this accepts trust silently and is never the default. Reinstalling the same plugin id at the same commit with the same manifest hash is a no-op. Reinstalling the same plugin id at a different commit prompts for trust again.

After a successful install and trust acceptance, the installed plugin is enabled by default in the install scope. Disable it explicitly if you want to keep it installed but inert.

## Uninstalling Plugins

Use `ext uninstall` with the same scope used during install:

```bash
alysis ext uninstall acme.demo
alysis ext uninstall acme.demo --project
```

Uninstall prompts before removing anything unless `--yes` is supplied. Alysis Code removes tracked components in reverse install order: hooks, MCP servers, custom tools, then skills. It then removes the installed plugin root and deletes the installed record from extension state.

Uninstall is best effort for component cleanup. If one component cannot be removed, Alysis Code continues removing the rest and then reports a partial failure with the individual cleanup errors. The installed state entry is still removed after cleanup attempts complete.

## Enabling and Disabling Plugins

Use `ext enable` and `ext disable` to change activation state without reinstalling:

```bash
alysis ext enable acme.demo
alysis ext disable acme.demo
alysis ext enable acme.demo --project
alysis ext disable acme.demo --project
```

User-scope enablement is global. Project-scope enablement writes `.alysis/extensions.json` in the workspace and affects only sessions started in that workspace. A plugin must be installed in at least one scope before it can be enabled or disabled.

The effective-enabled set is computed at session start. Alysis Code starts with globally enabled plugins, applies project `disabled[]` removals, then applies project `enabled[]` additions. Because project enables are applied last, a plugin listed in both project arrays ends up enabled. A project disable can turn off a globally enabled plugin for that workspace only.

## Workspace Trust

Project overrides are workspace-trusted. If `.alysis/extensions.json` contains `enabled[]` or `disabled[]` entries and the workspace has not been trusted, Alysis Code prompts before applying those overrides. The prompt shows the workspace root, the SHA-256 of the overrides file, the plugins the project wants to enable, and the plugins it wants to disable.

Trust is stored per user at the workspace trust state path under the extensions data directory. Entries are keyed by the SHA-256 of the canonical absolute workspace path and include the hash of the overrides file at the time trust was granted. If the overrides file changes later, Alysis Code prompts again because the project gained or changed plugin power.

In non-interactive sessions, untrusted project overrides are ignored and Alysis Code logs a one-line warning. Revoke workspace trust by manually editing the workspace trust JSON file.

## Default Activation Behavior

Install plus trust means enabled. This avoids a second consent step after the user has already reviewed the manifest hash, components, permissions, and source commit. Use `alysis ext disable <plugin_id>` if you want to keep a plugin installed but inactive.

At session bootstrap, Alysis Code discovers skills, custom tools, MCP servers, and hooks normally, resolves the effective-enabled plugin set once, then filters plugin-scoped components whose plugin id is not active. Built-in components and user-authored components without a plugin marker are always retained.

## Manifest Reference

### Top level

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `schema_version` | `1` | Yes | Manifest schema version. v1 manifests must set this to the integer `1`. |
| `plugin` | table | Yes | Plugin metadata block. |
| `compatibility` | table | Yes | Alysis Code version and platform compatibility block. |
| `components` | table | No | Component container. Defaults to empty lists for every component type. |
| `security` | table | No | Security contact and disclosure metadata. |

### `[plugin]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `id` | `str` | Yes | Stable plugin identifier. Must match `^[a-z][a-z0-9_]*\.[a-z][a-z0-9_-]*$` and be at most 64 characters. |
| `name` | `str` | Yes | Human-readable plugin name. Length: 1 to 60 characters. |
| `version` | `str` | Yes | Plugin version string. Must parse as a valid PEP 440 version. Plain SemVer strings such as `1.2.3` are valid. |
| `description` | `str` | Yes | Short plugin summary. Length: 1 to 200 characters. |
| `author` | `str` | Yes | Publisher or maintainer string. Length: 1 to 200 characters. |
| `license` | `str` | Yes | Non-empty license identifier or label. Common SPDX identifiers are recommended. Unknown values warn but do not fail validation. |
| `homepage` | `HttpUrl` | No | Plugin home page URL. |
| `repository` | `HttpUrl` | No | Plugin source repository URL. |
| `keywords` | `list[str]` | No | Search keywords. Maximum 8 entries. Each keyword must be lowercase, match `^[a-z0-9_-]+$`, and be at most 32 characters. |

### `[compatibility]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `alysis` | `str` | Yes | Alysis Code version constraint. Must parse as a valid PEP 440 specifier set such as `>=0.1,<0.2`. |
| `platforms` | `list["linux" \| "darwin" \| "windows"]` | No | Supported operating systems. Defaults to `["linux", "darwin", "windows"]`. |

### `[components]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `skill` | `list[SkillComponent]` | No | Declared skill components. Defaults to `[]`. |
| `tool` | `list[ToolComponent]` | No | Declared custom tool components. Defaults to `[]`. |
| `mcp_server` | `list[McpServerComponent]` | No | Declared MCP server components. Defaults to `[]`. |
| `hook` | `list[HookComponent]` | No | Declared hook components. Defaults to `[]`. |

### `[[components.skill]]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `path` | `str` | Yes | Relative path from the plugin root to the skill directory or bundle location. Must stay inside the plugin root after resolution. |
| `id` | `str` | No | Optional explicit skill id. If omitted, the effective id defaults to `basename(path)`. |
| `enabled` | `bool` | No | Whether the component is enabled by default. Defaults to `true`. |

### `[[components.tool]]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `path` | `str` | Yes | Relative path from the plugin root to the custom tool file. |
| `id` | `str` | No | Optional explicit tool id. If omitted, the effective id defaults to `basename(path)`. |
| `enabled` | `bool` | No | Whether the component is enabled by default. Defaults to `true`. |
| `description` | `str` | Yes | Human-readable tool description. Length: 1 to 200 characters. |
| `required_env` | `list[str]` | No | Required environment variable names. Each entry must match `^[A-Z][A-Z0-9_]*$`. Defaults to `[]`. |
| `network` | `bool` | Yes | Whether the tool requires network access. |
| `filesystem` | `"none" \| "read" \| "write"` | Yes | Filesystem access level requested by the tool. |
| `timeout_sec` | `int` | No | Tool timeout in seconds. Range: 1 to 600. Defaults to `60`. |

### `[[components.mcp_server]]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `id` | `str` | Yes | MCP server identifier. Must be non-empty. |
| `enabled` | `bool` | No | Whether the component is enabled by default. Defaults to `true`. |
| `transport` | `"stdio" \| "http"` | Yes | MCP transport kind. |
| `command` | `list[str]` | Conditionally | Required when `transport = "stdio"`. Forbidden when `transport = "http"`. Each element must be non-empty. |
| `url` | `HttpUrl` | Conditionally | Required when `transport = "http"`. Forbidden when `transport = "stdio"`. URLs must use `https`, except `http://localhost...` and `http://127.0.0.1...` are allowed for local development. |
| `env` | `list[str]` | No | Environment variable names exposed to the server process. Each entry must match `^[A-Z][A-Z0-9_]*$`. Defaults to `[]`. |
| `scopes` | `list[str]` | Yes | Declared MCP scopes. Must contain at least 1 and at most 32 non-empty entries. |
| `oauth` | `dict` | No | Pass-through OAuth configuration. v1 manifest validation does not validate the shape of this object; existing MCP OAuth code validates it later at registration time. |

### `[[components.hook]]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `event` | `"PreToolUse" \| "PostToolUse" \| "PostWrite" \| "SessionStart" \| "SessionStop"` | Yes | Hook lifecycle event. |
| `path` | `str` | Yes | Relative path from the plugin root to the hook file. |
| `id` | `str` | No | Optional explicit hook id. If omitted, the effective id defaults to `basename(path)`. |
| `enabled` | `bool` | No | Whether the component is enabled by default. Defaults to `true`. |

### `[security]`

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `contact` | `str` | Yes | Security contact. Must be either an email address or an `https://` URL. |
| `policy_url` | `HttpUrl` | No | Security policy URL. |
| `disclosure_days` | `int` | No | Disclosure target in days. Range: 1 to 365. |

## Validation Rules

Alysis Code rejects a plugin manifest when any of the following conditions are true:

1. The manifest file is larger than 64 KB before parsing.
2. The TOML document is syntactically invalid.
3. `schema_version` is not the integer `1`.
4. Any field violates the declared schema: unknown keys, wrong types, invalid ids, invalid version strings, invalid specifier sets, invalid env names, invalid URLs, invalid enum values, or out-of-range integers.
5. The manifest declares more than 32 total components across skills, tools, MCP servers, and hooks.
6. A component `path` is absolute, contains a `..` segment, or resolves outside the plugin root.
7. A component `path` does not exist on disk inside the plugin root.
8. Two components of the same type resolve to the same effective id, or an MCP server violates the transport contract by omitting its required `command` or `url` or by supplying the wrong one.

A metadata-only plugin with zero components does not fail validation, but it emits a warning because it contributes nothing.

## Forward Compatibility

`schema_version` is a hard compatibility boundary. Alysis Code only accepts manifests whose schema version it explicitly understands. A future breaking manifest format will increment `schema_version`, and older Alysis Code builds will reject that manifest instead of guessing how to interpret it.

This means plugin authors should treat schema upgrades as explicit migrations, not silent extensions. If you need to support multiple Alysis Code generations, publish separate manifests or release lines that target the correct schema version rather than trying to write one manifest that relies on undefined fallback behavior.
