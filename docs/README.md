# Alysis Code Docs

Public documentation for setup, operation, extension points, and security.

Start with the [root README](../README.md) for installation and first use. Use this index for deeper guides.

## Start Here

- [Quickstart](quickstart.md): configure a provider, bind a workspace, and run the first task.
- [AI subscription connections](account-runtimes.md): use provider sign-in with Alysis Code's native agent and `/config` model/effort selection.
- [Credentials](credentials.md): understand API-key precedence and persisted credential storage.
- [Reference](reference.md): review commands, modes, configuration, updates, sessions, and troubleshooting.
- [Web search](web-search.md): review model-led search behavior, provider coverage, and fallback configuration.

## Core Guides

- [Architecture](architecture.md): understand the session loop, provider layer, tools, verification, and trust boundaries.
- [Shell sandbox](shell_sandbox.md): configure Docker or Bubblewrap isolation for shell and verification commands.
- [Security model](security_model.md): review trust boundaries, HTTP protections, MCP boundaries, hooks, plugins, and fullaccess mode.
- [Server mode](server.md): start the HTTP API and configure authentication, uploads, job queues, and workers.
- [Forge](forge.md): plan, execute, verify, and review larger coding tasks.
- [Subagents](subagents.md): delegate focused read-only exploration, review, and testing strategy work.
- [Release checklist](release_checklist.md): regression gates for one-shot completion safety, deadlines, diagnostics, and compatibility.

## Extension Guides

- [MCP](mcp.md): connect stdio or Streamable HTTP MCP servers with explicit policy.
- [Skills](skills.md): discover and use reusable instruction bundles.
- [Skills lifecycle](skills_lifecycle.md): scaffold, validate, install, enable, disable, and remove skills.
- [Plugins](plugins.md): package skills, tools, MCP servers, and hooks into trusted bundles.
- [Custom tools](custom_tools.md): add trusted Python tools with manifests, validation, and subprocess execution.
- [Lifecycle hooks](hooks.md): run deterministic command hooks around sessions and tool calls.

## Project

- [Contributing](../.github/CONTRIBUTING.md): local development and pull request guidance.
- [Release process](RELEASING.md): package and sandbox-image release steps.
- [Security policy](../.github/SECURITY.md): private vulnerability reporting.
- [Code of Conduct](../.github/CODE_OF_CONDUCT.md): community participation expectations.
