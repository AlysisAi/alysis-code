# Skills

This package implements Alysis Code skills: reusable instruction bundles rooted
at a `SKILL.md` file.

## Contents

- `discovery.py` finds skills in approved project and user roots.
- `loader.py`, `models.py`, and `validation.py` parse and validate bundles.
- `scaffold.py`, `install.py`, `state.py`, and `transactions.py` support the
  local skill lifecycle.
- `prompting.py` prepares concise skill context for sessions.
- `matching.py` and `evals.py` support deterministic matching and evaluation.
- `conventions.py` handles repo convention files, which are separate from
  skills.

## Scope

Skills are instruction text, not host policy or executable capability. They
should be advertised compactly and read on demand rather than copied wholesale
into every prompt.

Repo convention files such as `AGENTS.md`, `CLAUDE.md`, and `CONVENTIONS.md`
must remain separate from the skill registry.

## Development

Changes to discovery, validation, or prompt rendering should include regression
coverage for both project-local and user-global skill roots.

## See Also

- [Skills](../../../docs/skills.md)
- [Skills lifecycle](../../../docs/skills_lifecycle.md)
