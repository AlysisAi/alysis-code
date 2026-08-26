# Lifecycle Hooks

This package implements command-based lifecycle hooks around sessions, prompts,
and tool calls.

## Contents

- `config.py` loads user, project, and local hook configuration.
- `models.py` defines hook specs and event payloads.
- `dispatcher.py` executes hooks and interprets decisions.
- `trust.py` records trust for project hook configuration.
- `audit.py` writes hook audit artifacts.

## Scope

Project hook configuration is executable policy and is not trusted by default.
Edits to that configuration require a new trust decision.

Blocking events can stop prompts or tool calls. Non-blocking events record hook
output but continue execution. Keep that distinction explicit in code and docs.

Hooks receive a filtered environment by default; avoid broadening environment
inheritance unless the security impact is clear.

## Development

When changing event payloads or dispatcher behavior, update the hook protocol
tests and public hook documentation together.

## See Also

- [Lifecycle hooks](../../../docs/hooks.md)
- [Security model](../../../docs/security_model.md)
