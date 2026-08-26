# Built-In Tools

This package contains Alysis Code's built-in tool implementations. These are
registered by the host at session startup after mode, workspace, sandbox, and
configuration checks have been applied.

## Contents

- `fs.py`, `search.py`, `symbols.py`, `git.py`, and `history.py` cover local
  workspace inspection and edits.
- `shell.py` delegates command execution to the configured shell runner.
- `web.py` and `web_search.py` provide bounded web access when configured.
- `registry.py` keeps the tool metadata, schemas, previews, and result summaries
  used by the session runtime.
- `availability.py` tracks optional tools without making missing optional
  backends a startup failure.

## Scope

Tool exposure is decided in `../agent/tools_assembly.py`; defining a tool here
does not make it available in every runtime or mode.

Shell execution policy is owned by the session and sandbox layers. Networked
tools should continue to use the safe HTTP path where applicable.

## Development

Keep changes narrow and run the tests for the tool family you touch. If a change
affects which tools are exposed, also cover the session assembly path.

## See Also

- [Security model](../../../docs/security_model.md)
- [Shell sandbox](../../../docs/shell_sandbox.md)
