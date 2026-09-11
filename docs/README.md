# Alysis Code documentation

The root [README](../README.md) covers installation and a first run. The guides below describe
configuration, operation, extension points, and security in more detail.

## Getting started

- [Quickstart](quickstart.md): configure a provider and run your first task.
- [Credentials](credentials.md): understand API-key precedence and storage.
- [AI subscription connections](account-runtimes.md): sign in through a supported provider.
- [Reference](reference.md): commands, modes, configuration, sessions, and troubleshooting.
- [Migration from Sylliptor](migration-alysis-code.md): update commands and configuration after
  the project rename.

## Core guides

- [Architecture](architecture.md): session loop, providers, tools, and verification.
- [Security model](security_model.md): trust boundaries and host-enforced controls.
- [Shell sandbox](shell_sandbox.md): Docker and Bubblewrap configuration.
- [Forge](forge.md): plan and execute larger tasks.
- [Background terminals](terminals.md): manage background commands and durable services.
- [Subagents](subagents.md): delegate focused work with constrained permissions.
- [Personas](personas.md): switch between implementation, planning, question, and debugging
  behavior.
- [Web search](web-search.md): configure hosted and external search backends.
- [Server mode](server.md): run the HTTP API and worker service.

## Extensions

- [MCP servers](mcp.md)
- [Skills](skills.md)
- [Skill lifecycle](skills_lifecycle.md)
- [Plugins](plugins.md)
- [Custom tools](custom_tools.md)
- [Lifecycle hooks](hooks.md)
- [Custom tool and hook examples](examples/README.md)
- [IDE protocol](ide_protocol.md): build editor integrations using the structured bridge API.

## Project information

- [Contributing](../.github/CONTRIBUTING.md): local development and pull request guidance.
- [Release process](RELEASING.md): package and sandbox-image release steps.
- [Security policy](../.github/SECURITY.md): private vulnerability reporting.
- [Code of Conduct](../.github/CODE_OF_CONDUCT.md): community participation expectations.
- [Changelog](CHANGELOG.md): user-facing release history.
