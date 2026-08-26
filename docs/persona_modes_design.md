# Persona modes — design proposal

Status: v1 landed on `feat/persona-modes-registry` — PR A `3ae8f13e` (registry/config/state),
PR B `ca3aa43c` (/mode switching, pickers, badge), PR C `fa02fb50` (switch_mode tool),
PR D `9db140cd` (overlays, /chat retirement) · Owner: AL · Follows: router removal (PRs #5–#12)

Landed post-v1: Tab persona cycling (silent, badge-driven) and **persona write scoping** —
Architect now runs at a clamped `review` with `allow_write_globs = ("*.md", "**/*.md")`, so it
writes markdown plans through the gate and nothing else (Kilo's fileRegex, host-enforced;
persona and user scopes are independent constraints and a write must satisfy both).

Also landed post-v1: custom personas from markdown, `alysis run --persona`, and
per-persona model binding in the main chat turn. Still open from §8 (phase 2+): VS Code
extension persona support and the one-release deletion of the retired `chat_only` plumbing.

Kilo-Code-style persona modes for interactive chat: **Code, Architect, Ask, Debug**, switchable
by the user and proposable by the model with explicit approval. Personas layer *on top of* the
execution-mode gate; they never replace it.

## 1. Goals

- Named personas the user switches between, Kilo-style: one `/mode` mental namespace, TUI picker,
  footer badge, `alysis chat --mode <persona>`.
- A `switch_mode` tool so the model can *propose* a persona change ("this looks like read-only
  advice — switch to Ask?") that the user approves or declines. Visible, deterministic, fail-safe.
- Per-persona defaults: prompt overlay, model role, default execution mode.
- Zero behavior change for users who never switch (default persona = Code = today's behavior).

## Non-goals

- **No pre-turn classification returns.** Nothing runs before the turn; nothing guesses. The
  router stays dead.
- **Personas never weaken the gate.** Execution modes, approvals (`guard_write`/`guard_shell`),
  write-scope, and the sandbox remain the sole enforcement layer. A persona is a *convention*
  (prompt + defaults); the gate is the *law*. This is where we deliberately diverge from Kilo,
  whose mode restrictions are prompt/toolset conventions only.
- No silent switches. Every persona change is user-initiated or user-approved, and surfaced.

## 2. Concept: persona ≠ permission

| Axis | What it is | Authority |
|---|---|---|
| Persona (new) | Prompt overlay, model role, *default* execution mode, UI badge | Suggests |
| Execution mode (existing) | `readonly / review / auto / fullaccess` tool filtering + approval guards | Enforces |

A persona *selects* an execution mode through the existing single mutation primitive
(`_apply_chat_effective_mode`, `cli_impl/chat/loop.py:1016`); it never bypasses it. The
invariant, mirroring `_clamp_subagent_mode` (`agent/tools_assembly.py:1627`):

> **Clamp rule:** a persona switch may freely *lower* the effective execution mode, but may never
> raise it above the user's session mode. If the user set `readonly`, switching to Code still
> yields `readonly`. `switch_mode` can therefore never be an escalation path.

## 3. Built-in personas

| Persona | Default exec mode | Model role (`model_router.py`) | Prompt overlay | Notes |
|---|---|---|---|---|
| `code` | session default (`review`) | `coding` | none | Today's behavior; the default persona |
| `architect` | `readonly` | `planner` | plan-mode-style overlay | Drafts plans; hands off to Code via the existing approved-plan pipeline |
| `ask` | `readonly` | `comprehension` (falls back to `coding`) | brief advisory overlay | Read-only Q&A with inspection tools; absorbs `/ask` and replaces the removed `/chat` |
| `debug` | session default (`review`) | `coding` | repro-first overlay | Leans on the existing `reproduction_first.py` conditional directive |

Details:

- **Code** is deliberately empty: no overlay, no model change. Migration safety comes from this.
- **Architect** generalizes interactive plan mode. It enters `readonly` exactly the way
  `/plan mode` does today (`_ChatPlanModeState` → `_apply_chat_effective_mode(next_mode="readonly")`,
  `cli_impl/chat/commands.py:1392`), reuses `plan_mode.py` drafting
  (`generate_plan_draft`, `instruction_with_approved_plan`), and its overlay text mirrors the
  contract line from `INTERACTIVE_PLAN_MODE_SYSTEM_PROMPT`: *the host owns state transitions and
  execution gating*. Approving a plan offers the switch to Code.
  Open question for review: does Architect *subsume* interactive plan mode (aliasing
  `/plan mode` → `/mode architect`) in v1, or coexist? Recommendation: coexist in v1, alias in
  v2 after the persona machinery has a release of soak time.
- **Ask** is a *sticky* readonly persona. `/ask <q>` (one-turn override via
  `mode_override`/`restore_mode_after` on `_ChatExecutionRequest`, `cli_impl/chat/state.py:112`)
  stays as the one-shot form; `/mode ask` is the persistent form. Read-only is enforced by tool
  removal and guards as today — never by the overlay text.
- **Debug**'s overlay stresses reproduce-before-fix; the *binding* of the reproduction gate stays
  observed-facts (`fcbf2025` semantics) — the persona only makes the model try to produce those
  facts sooner.

## 4. Switching UX

One namespace, like Kilo. `/mode` accepts both vocabularies; names don't collide:

- `/mode architect` → set persona `architect` (which applies its default exec mode via the clamp).
- `/mode readonly` → set execution mode only (today's behavior, unchanged).
- `/mode` (bare) → picker with execution modes only. Personas are deliberately NOT picker rows
  (revised after v1 review): in the TUI, **Tab on an empty input cycles the persona**
  (code → architect → ask → debug), Kilo/OpenCode-style, via `next_persona` and the same
  apply primitive. Tab with text in the buffer keeps its completion behavior.
- `alysis chat --mode <persona|exec-mode>` at startup. (`alysis run` personas: phase 2.)
- Plan-mode interlock: while `/plan mode` is on, persona switches are refused the same way
  `/mode` is refused today (`cli_impl/chat/commands.py:1270`).

State and visibility:

- New `session.persona` field + `_ChatTuiState.persona` (`cli_impl/tui/state.py:35` sibling of
  `exec_mode`); footer badge shows both: `arch·read`, `code·review` (`_MODE_SHORT` pattern,
  `cli_impl/tui/footer.py:26`).
- The model sees the persona via the `<environment_context>` block: add `persona: architect`
  next to the existing `mode:` line (`_environment_context_message`,
  `agent/prompt_context.py:832`). `refresh_session_environment_context_message` is already called
  by `_apply_chat_effective_mode`, so it stays correct on every transition for free.
- Built-in overlays are injected as per-turn ephemeral system messages
  (`run_turn(ephemeral_system_messages=...)` → `_request_messages_with_ephemeral_system_prompts`,
  `agent/llm_calls.py`). Custom persona bodies are untrusted project/user content and travel as
  ephemeral user messages instead. Neither path rebuilds the session prompt or changes its
  `system_prompt_sha256`.

## 5. The `switch_mode` tool (model-proposed, user-approved)

Registration:

- `BuiltinToolMetadata` entry in `tools/registry.py` (`_BUILTIN_TOOL_METADATA`, :709) with
  `built_in_subagent_exposure="hidden"` — top-level interactive chat only.
- Registered in `build_tools` through `_append_builtin_tool` only when
  `runtime_kind == INTERACTIVE_CHAT` and not `non_interactive`. One-shot, forge, swarm, subagent,
  and conflict runtimes never see it: **no silent switches in automation.**

Contract:

```
switch_mode(persona: "code"|"architect"|"ask"|"debug", reason: str)
```

1. Tool raises an approval through the existing `ApprovalRequest` surface
   (`surface/types.py:83`, same object as `guard_write`): kind `persona_switch`, preview
   "Model proposes switching to Ask — {reason}".
2. **Approve** → `_apply_chat_persona(...)` applies persona + clamped exec mode; sticky until the
   next switch (Kilo semantics). Tool returns `{"applied": true, "persona": ..., "effective_mode": ...}`.
3. **Decline** → tool returns `{"applied": false}` with a short "continue in current persona"
   note. Not an error; the turn continues. Mirrors `ApprovalDeclinedError` handling *without*
   aborting the turn, because nothing was blocked — only proposed.
4. Consecutive identical proposals in the same session are deduplicated host-side (the second
   identical ask auto-returns the prior decline) to prevent approval fatigue.

Events (both channels, following `mode_changed` precedent, `surface/events.py:162`):

- Session log: `persona_switch_proposed`, `persona_switch_applied`, `persona_switch_declined`.
- Surface dataclass: `PersonaChanged {persona, effective_mode, source: "user"|"model"}` +
  `emit_persona_changed` on every surface implementation + docstring inventory line.

## 6. Configuration

- `default_persona: str = "code"` — real `AppConfig` field (`config.py:474`), `_SETTABLE_KEYS`
  entry, validation branch mirroring `default_mode` (:1665).
- `persona_models.<persona> = <role>` — dotted-key namespace in `extra_fields`, parser modeled on
  `_role_model_key_parts` (:2591), e.g. `alysis config set persona_models.architect planner`.
  Resolution goes through the existing `resolve_model_for_role(...)` chain untouched — personas
  map to *roles*; no new precedence machinery.
- Kill switch, house-style pair: `persona_modes_enabled = true` config key +
  `ALYSIS_PERSONA_MODES=off` env. Off → `/mode` accepts only exec modes, `switch_mode` not
  registered, `/ask` behaves exactly as shipped today.

## 7. What happens to `/ask` and `/chat`

- `/ask` — kept, unchanged contract (one-turn readonly override). It becomes the one-shot form
  of the Ask persona; same producer, same `test_ask_and_chat_turns.py` pins.
- `/chat` — **removed** (per decision). Move to `_CHAT_RETIRED_COMMANDS`
  (`cli_impl/commands/cli_common.py:1049`) with the notice "use `/mode ask`". Remove from `SPECS`,
  visible commands, help panels. The internal `chat_only` plumbing
  (`CHAT_ONLY_SYSTEM_PROMPT`, the `chat_only` branch in `agent/turn/core.py:1912`, the
  `_ChatExecutionRequest.chat_only` field) stays for one release with no producer, then is deleted.

## 8. Out of scope for v1 (phase 2+)

- Custom personas from `.alysis_modes/*.md` — reuse the subagent frontmatter pipeline
  (`subagents.py`: allowlist parse via `parse_frontmatter_yaml`, fail-closed
  `normalize_subagent_mode`-style defaulting to `readonly`, body marked `prompt_trust="untrusted"`
  and wrapped by the untrusted-prelude convention from `prompt_context.py:1926`).
- VS Code extension persona support: `SlashCommandRegistry.ts`, `SlashCommandRouter.ts`
  (currently validates `readonly|review|auto` only), `BackendActionController.ts::SessionMode`,
  `ide/health.py::SUPPORTED_MODES`, `stdio_bridge.py`. Until then the IDE bridge simply keeps its
  exec-mode-only vocabulary; persona switching is a CLI/TUI feature.
- `alysis run --mode <persona>` for one-shot; Architect⇄plan-mode aliasing.

## 9. Test plan

New suite `tests/test_persona_modes.py` plus targeted extensions:

- Persona switch applies overlay + clamped exec mode and restores on `/mode code`
  (pattern: `test_one_turn_mode_override_applies_and_restores`).
- Clamp rule: user `readonly` + `/mode code` → effective `readonly`; model `switch_mode` can
  never raise above session mode (pattern: subagent clamp tests).
- `switch_mode` approval flow: approve applies + events; decline continues turn; dedupe of
  repeated identical proposals; tool absent in `one_shot`/`forge_exec`/`swarm_worker`/`subagent`
  runtimes (extend `test_tool_exposure_by_mode.py`).
- Rebuild integrity on persona switch (extend `test_chat_mode_rebuild.py` — MCP bindings,
  verification commands, custom shell runner survive).
- Completer/handler parity and visible-command lists — `test_chat_slash_completer.py`
  source-grep test and `test_cli_ux.py:770` literal list will force consistency; `/chat`
  moves to the retired assertions.
- Config: `default_persona` validation, `persona_models.*` dotted keys, kill-switch off-state
  (extend `test_config.py`).
- Surface events: `PersonaChanged` shape + dispatch (extend `test_surface_events.py`).
- Env-context: `persona:` line present and updated after switches (prompt-context suite).

## 10. Rollout

1. **PR A — registry + state:** persona dataclass/registry, `session.persona`, config keys,
   env-context line, events. No UX change (default `code`).
2. **PR B — switching UX:** `/mode` persona names, TUI picker + footer, classic terminal rows,
   `chat --mode`, plan-mode interlock.
3. **PR C — `switch_mode` tool** + approval flow + dedupe + runtime-kind gating.
4. **PR D — Ask/Architect/Debug overlays** + `/chat` retirement.
5. Changelog entries follow the house format; the compatibility sentence to reuse verbatim:
   "`/chat` is retired; `chat_only` internals remain accepted-and-ignored for one release."

## 11. The honest trade

- Every persona feature here is *convention plus enforcement we already have*. The new moving
  part count is deliberately tiny: one registry, one tool, one event pair, one config namespace.
- The `/mode` namespace now carries two vocabularies. Mitigation: disjoint names, grouped picker,
  and the retirement-notice pattern for anything ambiguous. If review disagrees, the fallback is
  a separate `/persona` command — one-line change in this design.
- Sticky model-proposed switches add an approval interaction. The dedupe rule and the
  interactive-chat-only registration keep it from becoming noise.
- We are *not* copying Kilo's enforcement model, only its UX. In Kilo a mode's restrictions live
  in the prompt and toolset config; here they compile down to the same gate that survived the
  router removal. If a persona prompt and the gate ever disagree, the gate wins — by
  construction, not by convention.
