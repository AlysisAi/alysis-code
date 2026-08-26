# Reference

This page is a compact reference for the main Alysis Code CLI surface,
configuration model, runtime modes, and extension points. For deeper subsystem
details, follow the linked guides.

## What Alysis Code Does

Alysis Code runs local coding sessions from a terminal. A session binds to a
workspace, sends user turns to the configured model provider, exposes a
controlled set of tools, and stores local logs and artifacts for review.

Core capabilities include:

- interactive chat and one-shot commands
- filesystem, search, git, shell, web, and verification tools
- workspace-aware execution modes
- optional MCP, skills, plugins, hooks, custom tools, and subagents
- Forge planning and execution workflows
- local session logs and feedback bundles

## Commands

Common entrypoints:

```bash
alysis
alysis chat
alysis run "Explain this repository."
alysis setup
alysis tools
alysis update check
```

Workspace selection:

```bash
alysis chat --path /path/to/project
alysis run --path /path/to/project "Summarize the codebase."
alysis run --path ./new-app --create-path "Scaffold a minimal project."
```

Provider and credentials:

```bash
alysis config set base_url "https://api.openai.com/v1"
alysis config set model "gpt-4.1-mini"
alysis config set-api-key
alysis run --api-key-stdin "Hello"
alysis run --api-key-env OTHER_API_KEY --base-url "https://example.com/v1" --model "your-model" "Hello"
```

Auth state for a supervising application (one JSON object on stdout; exit code 0
means the command ran, and the payload carries `authenticated`):

```bash
alysis auth status --json
alysis auth list --json
alysis whoami --json
alysis doctor auth --json
```

See [Account Runtimes](account-runtimes.md) for the payload contract.

Forge:

```bash
alysis forge plan --path .
alysis forge show --path .
alysis forge status --path .
alysis forge run --path . --mode auto --verify strict
alysis forge exec T01 --path . --mode review
alysis forge swarm --path . --parallel 3 --mode auto --verify warn
alysis forge resume --path .
alysis forge unlock --path .
```

## Execution Modes

Execution mode controls the default approval posture.

- `readonly`: inspect-only mode. Write tools, shell commands, and verification
  are not available.
- `review`: preview or ask before writes and shell commands.
- `auto`: allow routine edits with fewer prompts while still blocking dangerous
  operations.
- `fullaccess`: remove mode-level write and shell prompts for trusted
  workspaces.

Set the default mode:

```bash
alysis config set default_mode review
```

Override per command:

```bash
alysis run --mode readonly "Explain this repository."
alysis chat --mode auto
alysis run --benchmark --deadline-seconds 855 --require-deadline -- "Fix the failing task."
```

Session policy also records a `runtime_kind` such as `interactive_chat`,
`one_shot`, or `forge_exec`; extension systems use that runtime kind when
deciding which tools or catalogs may be exposed.

`alysis run --benchmark` applies the raw autonomy profile for benchmark and
managed one-shot hosts. It uses auto execution posture, code-only routing, a
longer fixed step budget, and disables optional capability layers such as
subagents, skills, custom tools, and web search by default. The
profile is also selected when `ALYSIS_RUN_PROFILE` is set to a recognized raw
benchmark alias such as `benchmark`, `raw-agent`, or `raw-benchmark`.

## Workspace Binding

`alysis chat` and `alysis run` bind a workspace before the session starts.
The requested path is the current directory or `--path`.

- Inside a Git repository, Alysis Code binds to the repository root and keeps the
  starting directory as the focus directory.
- Plain directories bind to the requested directory.
- Missing paths require `--create-path`.
- Broad paths such as a home directory require an explicit override.
- The filesystem root is blocked as a workspace root.

In chat, relative file, search, and shell paths default to the active workdir
inside the bound workspace. Use `/pwd` to inspect the current workspace root,
focus directory, and active workdir.

## Chat Slash Commands

Common interactive commands:

- `/help`: show chat commands
- `/status`: show mode, model, workspace, and runtime state
- `/pwd`: show workspace and active workdir
- `/mode`: inspect or change execution mode
- `/config`: open the configuration menu
- `/usage`: show token and cost usage
- `/stream`: open the live-output picker (On by default)
- `/stream on|off|status`: control or inspect live answer and reasoning output for this session
- `/trace off|compact|full`: control reasoning/tool progress detail
- `/image <path>`: attach an image to the next turn
- `/subagents`: open the active-subagent picker and select a run for the live pane
- `/skill`: list discovered skills
- `/plan <task>`: draft a plan for review and approval
- `/forge [resume]`: enter or resume Forge for the workspace
- `/report [text]`: create a local feedback bundle
- `/exit`: quit chat

While an agent turn is running, press Enter to send the current text as a
mid-turn message. Alysis Code delivers it at the next safe step boundary and
keeps it in conversation history. Press Ctrl+Q instead to queue the text as a
separate follow-up turn. Esc interrupts the active turn and discards pending
mid-turn messages, queued follow-ups, and staged commands.

Read-only commands remain available during a running turn: `/help` (and `/`),
`/status`, `/subagents`, `/pwd`, `/context` or `/ctx`, `/usage`, `/model-info`, `/trace`,
`/toolbar`, `/images`, `/image`, `/paste-image`, `/clear-images`, `/terminals`,
and bare `/skill`. `/mode`, `/persona`, `/model`, and typed `/config` commands
are accepted and staged; they run when the active turn finishes and before a
queued follow-up starts. They never alter the running turn. In particular, a
staged model change does not retroactively switch the model handling that turn.

Bare `/config` opens its overlay immediately so settings
can be browsed and saved while output continues. A save reaches disk at once,
but the live session reload waits until the running turn finishes. Other
state-changing commands and unknown commands remain blocked while a turn is in
flight; the mid-turn allowlist fails closed.

Forge mode has its own command surface for goal, task, plan, review, and
execution actions. See [Forge](forge.md).

Interactive chat opens in the full-screen TUI. The transcript owns mouse-wheel
scrolling and shows a persistent scrollbar while the input and footer remain
pinned. It scrolls three rows per wheel event by default. Set
`ALYSIS_SCROLL_SPEED` to an integer from `1` through `20` to tune the distance
for a high-resolution trackpad or a physical mouse wheel.

Dragging across transcript text selects it inside the TUI and leaves the
clipboard unchanged. While text is selected, the status line displays
`ctrl+c to copy`; pressing Ctrl+C copies the selection and clears the highlight.
Without a selection, Ctrl+C keeps its normal interrupt/exit behavior. Copied text
omits Alysis Code's visual message and reasoning markers. When stdin is redirected
on WSL or another POSIX host, the TUI reads interaction from the controlling
terminal so wheel events are not lost in the pipe.

When subagents are running, the TUI can show a short live-tail pane above the
input without covering the transcript. Press Ctrl+N to move forward and Ctrl+B
to move backward through child runs in spawn order; navigation wraps within the
children. Esc is the only way back to the main view: it closes an open subagent
pane before it acts as the active-turn interrupt. The pane works with `/trace off`, `compact`, and
`full`: trace level does not control its visibility. It retains only a bounded
recent tail and incrementally reads the selected child. `/subagents` opens a
picker for currently active children and selects one into the same pane. A
terminal, collected run leaves the live rotation after it is closed or another
run is selected.

## Configuration

Configuration is stored in the platform-specific Alysis Code config directory.
Use `alysis config menu` or `/config` in chat for interactive edits.

For an AI-subscription profile, open **Default Model** to choose an
account-scoped model and its supported reasoning effort together. Raw `model`,
`llm_reasoning_effort`, and `base_url` setters are intentionally rejected while
that profile is active, and legacy reasoning-effort environment overrides do not
replace the paired subscription selection.

The ChatGPT Codex subscription also manages sampling temperature, so its
unsupported per-role temperature fields are hidden. API-key profiles continue to
offer temperature overrides for models that accept them.

Common keys:

- `base_url`
- `model`
- `default_mode`

### Autonomous execution and optional limits

Normal chat, one-shot, Forge, conflict-resolution, and subagent work uses the
`autonomous` execution policy. It has no default step ceiling and continues
until completion, cancellation, a genuine blocker, or a fatal error.

`--max-steps N` is an optional per-invocation safety limit. The advanced
`step_budget_policy=limited` setting enables persisted caps through `max_steps`,
`task_max_steps`, and `subagent_max_steps`; these values are ignored by the
default autonomous policy. Existing `adaptive` and `fixed` configuration values
load as the backwards-compatible aliases `autonomous` and `limited`.

Setup and `/config` do not ask users to choose step budgets. Per-provider LLM
timeouts and optional one-shot `--deadline-seconds` remain separate operational
limits.

- `max_steps`
- `run_deadline_seconds`
- `run_deadline_unlimited`
- `run_deadline_degradation_enabled`
- `run_deadline_convergence_fraction`
- `run_deadline_wrap_up_fraction`
- `task_max_steps`
- `stream`
- `routing_mode`
- `route_arbitration_enabled`
- `semantic_turn_contract_enabled`
- `unified_turn_path_enabled`
- `reply_language`
- `evidence_v2_enabled`
- `regression_baseline_enabled`
- `turn_contract_v2_enabled`
- `reproduction_first_enabled`
- `blast_radius_gate_enabled`
- `blast_radius_max_scope_files`
- `blast_radius_scope_seconds_cap`
- `blast_radius_over_broad_threshold`
- `process_reaping_enabled`
- `workspace_provisioning_enabled`
- `role_models.router`
- `subagents_enabled`
- `custom_tools_enabled`
- `web_search_policy`
- `web_search_mode`
- `web_search_adapter`
- `web_search_base_url`
- `web_search_model`
- `web_search_timeout_s`
- `session_log_dir`
- `crash_diagnostic_log_path`
- `verify_commands`
- `update_check_enabled`
- `update_check_interval_hours`
- `update_check_timeout_s`
- `update_prompt_enabled`

`semantic_turn_contract_enabled` defaults to `true`. The environment variable
`ALYSIS_SEMANTIC_TURN_CONTRACT` overrides it with
`on/1/true/yes/enabled` or `off/0/false/no/disabled`. Turning it off keeps the
router's legacy posture-only path; it does not restore language-specific intent
matching.

`unified_turn_path_enabled` defaults to `true`. The environment variable
`ALYSIS_UNIFIED_TURN_PATH` overrides it with the same on/off vocabulary.
On the unified turn path no router model call runs before a turn: every text
turn goes straight to the main model with the full per-mode agent surface, and
execution posture derives from the execution mode (`readonly` stays advisory;
all other modes keep the full execution contract). Setting it off selects the
legacy pre-turn semantic-router path, which remains available for one release
while the router is removed.

`reply_language` (default empty) fixes the reply language for turns that run
without the router, for example `reply_language = "Greek"`. When set, the host
injects a reply-language directive and keys the final-summary language rewrite
to it; when empty, the model answers in the user's language naturally.

Two chat commands accompany the unified turn path. `/ask <question>` runs one
read-only turn: the session switches to the readonly execution mode for
exactly that turn and the previous mode is restored afterwards, even on
errors. `/chat <message>` produces one bounded conversational reply from the
main model with a minimal prompt, no tools, and no workspace context.

Useful environment overrides:

- `ALYSIS_API_KEY`
- `ALYSIS_CONFIG_DIR`: overrides the user config directory used for `config.json`, `credentials.json`, and the MCP OAuth token store.
- `ALYSIS_BASE_URL`
- `ALYSIS_MODEL`
- `ALYSIS_MODEL_ROUTER`
- `ALYSIS_LLM_TIMEOUT_S`
- `ALYSIS_RUN_DEADLINE_SECONDS`
- `ALYSIS_ROUTING_MODE`
- `ALYSIS_SEMANTIC_TURN_CONTRACT`
- `ALYSIS_UNIFIED_TURN_PATH`
- `ALYSIS_CRASH_DIAGNOSTIC_LOG_PATH`
- `ALYSIS_WEB_SEARCH_POLICY`
- `ALYSIS_WEB_SEARCH_ADAPTER`
- `ALYSIS_WEB_SEARCH_API_KEY`
- `ALYSIS_WEB_SEARCH_BASE_URL`
- `ALYSIS_WEB_SEARCH_MODEL`
- `ALYSIS_WEB_SEARCH_TIMEOUT_S`
- `ALYSIS_BLAST_RADIUS`
- `TAVILY_API_KEY`

The blast-radius gate measures what a change broke beyond its own verification. It
selects the tests nearest the touched paths (name mirrors, static importers, the
surrounding package), compares a clean-tree run of that scope against a post-fix
run, and blocks on tests that passed before the change and fail after it.
`blast_radius_max_scope_files` caps how many test files one scope names (default
40); `blast_radius_scope_seconds_cap` is the wall-clock ceiling per scope run
(default 300) — exceeding it shrinks the scope to its nearest tests rather than
skipping the check; `blast_radius_over_broad_threshold` (default 20) is the count
of newly-broken tests past which the change is treated as over-broad and rewritten
narrowly instead of repaired test by test. `ALYSIS_BLAST_RADIUS=off` keeps the
telemetry and reverts the directives and gate policy.

`role_models.router` overrides the model used for lightweight routing. Leave it
unset to inherit `model`, or set it to a smaller/cheaper model while keeping the
main coding model stronger.

## AgentBox Telemetry

AgentBox telemetry is opt-in, metadata-only, and built into Alysis Code. No
AgentBox package or sibling source checkout is required:

```bash
python -m pip install alysis-code
```

From a contributor checkout, the normal `python -m pip install -e ".[dev]"`
installation includes the integration.

On an enrolled computer, Alysis Code reads the machine URL, token, identity, and
privacy detail setting from `~/.agentbox/config.toml`. Set only the opt-in switch:

```bash
export AGENTBOX_ENABLED=1
```

Use `AGENTBOX_CONFIG` to select another enrollment config. Environment
variables override config values when an explicit per-process override is
needed:

```bash
export AGENTBOX_ENABLED=1
export AGENTBOX_PLANE_URL=https://agentbox.example.com
export AGENTBOX_TOKEN="<this machine's ingest token>"
export AGENTBOX_MACHINE_ID="<this machine's ID>"
# optional overrides:
export AGENTBOX_AGENT_ID="<unique Alysis Code agent ID>"
export AGENTBOX_TASK_DETAIL=category
export AGENTBOX_QUEUE_DIR=~/.agentbox/alysis-sdk-queue
```

Each computer must be enrolled separately. Do not copy another machine's token
or identity. By default, the enrolled Alysis Code agent ID is
`<machine-id>_alysis`, so computers appear as distinct agents.

Alysis Code uses its dedicated `~/.agentbox/sdk-queue` rather than the
connector shipper's `~/.agentbox/queue`. Set `AGENTBOX_QUEUE_DIR` only when a
separate Alysis Code-specific queue location is required.

Network failures leave events in that queue for replay and do not fail the
Alysis Code run. Scrubbed client errors are written to
`~/.agentbox/alysis-agentbox.log`; URLs and filesystem paths are removed.

When disabled or missing required configuration, Alysis Code does not create the
AgentBox client and emits nothing. When enabled, it sends session lifecycle,
short sanitized task objectives, turn token/cost totals, and tool activity
categories only. Prompts, diffs, tool arguments, outputs, file contents, and
full paths are never sent.

## Profiles

Profiles group provider settings such as protocol, base URL, API key source,
default model, and provider notes.

Useful commands:

```bash
alysis profile presets
alysis profile preset <provider-preset>
alysis profile set-key <profile> --stdin
alysis profile use <profile>
alysis profile list
```

OpenAI, Anthropic, and Gemini profiles can use their native API protocols.
Other provider and gateway profiles use the OpenAI-compatible protocol.

Presets are convenience templates, not hard constraints. Custom profiles can
point at model provider endpoints.

## Built-In Tools

The built-in tool surface depends on mode, runtime kind, workspace binding,
sandbox readiness, and configuration.

Common tool families:

- filesystem reads, writes, edits, moves, copies, and deletes
- repository text search and symbol lookup
- git history inspection
- shell command execution
- verification command execution
- web fetch and optional web search
- session history and local artifacts

Use:

```bash
alysis tools
```

for the current built-in tool catalog and configuration-dependent availability.

## Web Access

`web_fetch` retrieves one specific HTTP(S) URL. It is for targeted page or
document retrieval.

`web_search` is a host-owned discovery tool exposed to the active model. The
model decides whether a request needs current or source-backed web evidence and
calls the tool through its normal tool-calling path. Search results remain
untrusted external data. Alysis Code does not classify phrases or execute a search
before the model call.

`web_search_policy=auto` exposes the tool. `off` hides it. The former `always`
value is accepted as a compatibility alias for `auto`. System instructions tell
the model to search when external evidence is needed and to respect an explicit
request to stay offline.

`web_search_mode` controls backend selection:

- `auto`: use a ready provider-native adapter, then a configured external adapter.
- `native`: use only search hosted by the active model provider.
- `external`: use only a model-independent adapter such as Tavily.
- `off`: register no search backend.

For a direct provider without a hosted adapter, configure external search once
for the installation:

```bash
alysis config set web_search_policy auto
alysis config set web_search_mode external
alysis config set web_search_adapter tavily
export ALYSIS_WEB_SEARCH_API_KEY=<tavily-key>
alysis tools
```

`TAVILY_API_KEY` is also accepted. In a managed deployment, provision the search
key in the service environment rather than requiring each user or chat provider
to supply one. If no backend is ready, the tool is not registered and provider
diagnostics report the required setup. See [Web Search](web-search.md) for the
provider-hosted adapter and model coverage matrix.

`web_fetch` requires provenance before it retrieves a URL. Accepted provenance
classes are an explicit user-provided URL, a URL returned by `web_search`, an
observed canonical redirect from either source, and a bounded same-origin or
search-mediated recovery URL. URLs found inside trusted fetched pages, trusted
local file reads, and registered tool or shell output are recorded with their
source event and parent URL when applicable, then treated as source-linked
derived provenance rather than broad domain permission. URL comparison
canonicalizes scheme, host, default ports, fragments, and harmless
trailing-slash variants. Recovery still runs through the same SSRF, credential,
scheme, redirect, and byte-cap checks as a normal fetch. In deadline
finalization, missing provenance returns a structured recovery message instead
of starting optional web search.

## Verification

Verification commands can be inferred from the workspace, provided by config, or
passed explicitly:

```bash
alysis run --verify-cmd "pytest -q" "Fix the failing test."
alysis chat --verify-cmd "npm test"
```

For Node projects, discovery includes safe `package.json` scripts named `check`,
`lint`, `typecheck`, and `test:*`; each script body must pass the same verifier
command analysis before its package-manager command is authorized.

For Python projects, discovery distinguishes unittest suites from pytest and
also authorizes configured pytest, Ruff, and mypy checks declared by
`pyproject.toml`, `pytest.ini`, or `setup.cfg`. Every inferred command passes
the same verifier safety analysis before entering the authoritative contract.

When enabled, the `verify_run` tool lets the agent run the selected verification
commands and return a compact result while retaining full output in local
artifacts.

Selected commands are also serialized as typed verification specs backed by the
canonical command analyzer. A spec keeps a stable command ID, original and
display text, normalized primary command, execution mode, provenance, trust
level, required/advisory status, working directory, timeout policy, criterion
IDs, parse/validation state, shell-control classification, pipeline policy,
checker entrypoints, and evidentiary capability. Execution modes distinguish
ordinary argv-style commands, exact trusted shell wrappers, interpreter
snippets, and invalid diagnostic entries.

Trusted source and evidentiary capability are separate. `NON_ASSERTIVE`
commands are invalid as verifiers, `UNKNOWN` commands can be diagnostic but
cannot satisfy a contract by default, and `ASSERTIVE` commands still need fresh
host-observed execution. Alysis Code rejects vacuous, non-assertive, and
failure-masking verification commands such as `true`, `echo ok`,
`python -c 'pass'`, and `pytest -q || true` before they can satisfy
authoritative completion. Legitimate checks such as `pytest -q`, `npm run
build`, pre-existing validation scripts, `curl -fsS`, `test -f`, `diff`,
`cmp`, and `grep -q` keep their verification status when they actually
execute. Plain `curl -s` is observational because HTTP 4xx/5xx can still exit
zero.

Verification evidence is classified into an explicit hierarchy. Authoritative
managed or effective commands take precedence, repo-native commands can satisfy
ordinary repository tasks, and task-specific acceptance checks can satisfy only
tasks without a known verification contract. Observation commands, dry runs,
metadata inspection, and commands that mutate verification-relevant material
files are recorded but do not satisfy the gate.

Safe compound handling is intentionally narrow. Leading environment
assignments, approved runner prefixes, shell `-c`/`-lc` wrappers, and
`cd <path> && <single assertive command>` can be unwrapped when the inner
command is fully analyzable. `cd <path> && true`, arbitrary `&&`, `||`, `;`,
and pipelines are rejected. Pipelines are not authoritative verification under
the default `/bin/sh` runners because upstream failures can be masked by
`cat`, `tail`, or `tee`; they can only be reintroduced through a controlled
pipefail-capable backend with identical policy across host, bwrap, and Docker.
Explicit Python or similar validation snippets are converted to an interpreter
invocation when the language and syntax are clear; malformed backtick prose
remains invalid or advisory and is never sent to `/bin/sh`.

`verify_run` separates process success from contract satisfaction. Each command
has a `status` of `passed`, `failed`, `inconclusive`, `not_executed`, or
`stale`; legacy `ok` and `all_passed` are derived from that stronger status.
Exit code zero plus unknown real execution is inconclusive, not passed.

For execute-intent turns, Alysis Code also creates an acceptance contract from
trusted inputs: the user instruction, task brief, planning constraints,
pre-turn workspace scan, pre-existing checks, and host verification commands.
The contract records required artifacts, exact command or I/O checks,
format/schema hints, protocol or service requirements, dependencies,
preservation rules, thresholds, host checks, and repo-native check surfaces.
Path references are normalized against the workspace root and keep their role:
required output, existing input, preservation target, verification checker, or
advisory reference. Absolute paths inside the workspace become canonical
workspace-relative paths; external absolute paths stay external and are never
joined under the workspace. Format criteria are scoped to the output clause:
explicit format/schema language is hard, while extension-only inference is
advisory.
Evidence provenance is tracked separately from verification category:
host-authoritative, user-explicit, pre-existing repo-native, pre-existing task
checker, direct black-box, self-authored, or ad hoc observation. Tests created
or materially changed during the turn can support debugging, but they are
supplemental unless independent evidence covers the required criterion.
Pre-existing checker entrypoints inside the workspace are fingerprinted by
resolved path, regular-file state, size, and SHA-256 hash before the turn. If a
checker is created, modified, replaced, deleted, or symlink-retargeted by the
agent, Alysis Code emits a mutable-authoritative-checker downgrade and treats the
evidence as supplemental instead of host-authoritative.

Forge can make verification authoritative for task gates. See [Forge](forge.md).

## Extensions

Alysis Code supports several extension points:

- [MCP](mcp.md): connect external Model Context Protocol servers.
- [Skills](skills.md): use reusable instruction bundles rooted at `SKILL.md`.
- [Skills lifecycle](skills_lifecycle.md): scaffold, validate, install, enable,
  disable, and remove skills.
- [Plugins](plugins.md): package skills, tools, MCP servers, and hooks.
- [Custom tools](custom_tools.md): add trusted Python tools with manifests.
- [Lifecycle hooks](hooks.md): run deterministic command hooks around sessions
  and tool calls.
- [Subagents](subagents.md): orchestrate focused synchronous or background work,
  including retained isolated candidates and pinned verification.

Each extension type has its own trust boundary. Project-local executable
extension points generally require explicit trust before they affect execution.

## One-Shot Runs

`alysis run` is optimized for a single bounded instruction. It includes
guardrails for execution-style repository tasks, but it is not meant to replace
interactive refinement.

Use `alysis run` for focused tasks such as:

- explain this repository
- summarize a file or module
- make a small targeted change
- run a specific verification command

Prefer `alysis chat` or Forge for ambiguous, exploratory, or highly
iterative work.

For execution-style repository tasks, the one-shot completion gate accepts a
final answer only when the runtime has evidence for the requested work. The
gate requires a non-empty final response, material-work evidence unless a
concrete blocker is accepted, and verification evidence when the touched paths
or effective verification command contract require it. It also checks required
acceptance criteria: missing output paths, failed exact user checks,
unverified host commands, failed preservation constraints, and missed
thresholds keep the gate closed. A completion certificate reduces the current
state to `SUFFICIENT`, `INSUFFICIENT`, or `CONTRADICTED`; only hard criteria can
create gate problems, and unverified advisory criteria cannot block a sufficient
certificate. Known explicit failures take precedence over unrelated green
verification. The same certificate-backed gate is used for interactive execute
turns in `alysis chat`.
Local materialization has precedence over non-repo routing. If the user asks to
save, write, create, export, move, or put an answer/result in a local file or
directory, the turn is treated as repo-capable execution even when the first
step is web or MCP research. The repo loop keeps those web/MCP tools available,
then requires the requested artifact and evidence before finalization. Explicit
explain-only, answer-only, plan-only, advice-only, review-only, and
no-modification requests remain non-execution requests when they genuinely
conflict with material work.
Generic configured pytest fallback is refined
for one-shot execute turns, so non-Python or artifact-only tasks do not inherit
pytest when no trustworthy Python test surface exists; explicit user commands
and discovered repo-native checks remain preferred.
The same task-aware selection applies to interactive execute turns. Generic
`pytest -q` requires a pre-existing Python test surface such as real tests,
Python project metadata plus test directories, a repo-native pytest command, or
an explicit user pytest instruction. A Python output file, plain directory, or
agent-created test file is not enough. Exact successful trusted commands run
through `shell_run` can cover the contract directly; if they mutate material
source or requested outputs, a later clean rerun is still required.

For persistent-service tasks, the gate accepts only explicit durable-service
evidence. `shell_background` is session-owned and cannot satisfy persistence;
the agent must use `shell_service_start` with a readiness probe, then finalization
rechecks the stored `service_id` with `shell_service_status` semantics. If the
criterion names a port, the durable service must be ready through a matching TCP
readiness probe.

`alysis run --deadline-seconds N` sets an invocation-wide monotonic deadline
for one-shot execution. It is distinct from `llm_timeout_s`: the run deadline
has a soft finalization window plus a hard exhaustion point, while
`llm_timeout_s` bounds a single model request. During normal work, operation
launches are checked against hard remaining time and the reserved finalization
window. During finalization, Alysis Code blocks new routing, compaction,
subagents, background work, broad exploration, provider retry sleeps, and
speculative detours; it keeps bounded finishing actions such as edits,
foreground shell commands, and high-value verification available when enough
hard time remains. When the deadline is exhausted, Alysis Code reports
`deadline_exhausted` rather than step-budget exhaustion and uses a local
fallback summary if there is not enough time for a final model call. Deadline
telemetry records whether the source was explicit CLI, environment, config,
runtime default, inherited parent, or absent.

Non-interactive runs (`one_shot`, `forge_exec`, `swarm_worker`) resolve a
default budget of 3600 seconds when none is configured, so an unattended run
cannot grind indefinitely. Precedence is `--deadline-seconds`, then
`ALYSIS_RUN_DEADLINE_SECONDS`, then `run_deadline_seconds`, then that
default. To run unbounded, pass `--no-deadline`, or set the environment
variable or config key to `unlimited`, `never`, `off`, or `none`; choosing
unlimited explicitly stops the search instead of falling through to the
default. `0` remains invalid. Interactive `alysis chat` gets no default
budget, and `--require-deadline` is never satisfied by the default.

Within a finite budget the run degrades in stages rather than being cut off at
the end. At 75% elapsed it enters **convergence**: new subagents and background
processes are refused and the model is directed to drive existing work to a
verifiable state, while reads and edits stay available. At 90% it enters
**wrap-up**: file mutation and exploration are refused, and verification,
foreground shell commands, and the final model call stay available so the run
can verify what it has and report accurately. At 100% the run finalizes and
exits, keeping whatever landed in the working tree and exiting `0` when
material work was persisted. Stages are measured as fractions of elapsed
budget, so they are monotonic — a restriction from an earlier stage is never
lifted by a later one, including inside the finalization window. Refused
operations surface to the model as a tool error with
`failure_category: "deadline"` and the reason
`budget_degradation_disallows_operation`, never as a terminated turn. Stage
transitions are recorded in the session log as `deadline_phase_transition`.
Thresholds are configurable with `run_deadline_convergence_fraction` and
`run_deadline_wrap_up_fraction`, and `ALYSIS_RUN_BUDGET_DEGRADATION=off`
disables the stages while keeping the budget.

Managed hosts can make the deadline contract fail closed with
`--require-deadline`. When that option is present, Alysis Code resolves the
deadline from `--deadline-seconds`, `ALYSIS_RUN_DEADLINE_SECONDS`, or
`run_deadline_seconds` using the normal precedence, then exits with a
configuration error before session creation if no finite deadline exists.
Ordinary local `alysis run "..."` behavior is unchanged when
`--require-deadline` is omitted. For benchmark-style adapter runs, pair the
deadline contract with `--benchmark` so optional extension layers do not affect
the raw agent path under test. Host adapters should pass instructions after the
option separator, for example:

```bash
alysis run --benchmark --deadline-seconds 855 --require-deadline -- "--task can start with dash"
```

The deadline supplied by a benchmark adapter must come from the final effective
agent timeout after any host multiplier has already been applied. Do not use
verifier or environment-build timeouts, and do not multiply the timeout again.
The adapter should subtract a host-owned shutdown reserve for process cleanup and
artifact flushing; Alysis Code still keeps its own internal finalization reserve
inside the remaining invocation budget.

For the Terminal-Bench adapter, the authoritative timeout source is the
explicit `managed_host_agent_timeout_sec` agent kwarg. Current Terminal-Bench
runners compute the final timeout in the harness and do not expose it to
imported agents, so run with `--global-agent-timeout-sec N` and pass the same
already-effective value as
`--agent-kwarg managed_host_agent_timeout_sec=N`. The adapter computes:

```text
deadline_seconds = managed_host_agent_timeout_sec
  - monotonic_elapsed_before_alysis_launch
  - managed_host_shutdown_reserve_sec
```

The host shutdown reserve is configured by
`managed_host_shutdown_reserve_sec`, then
`ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC`, then default `30`. It covers
host process collection, terminal teardown, result serialization, and artifact
flushing; it is not the same as Alysis Code's internal finalization reserve, the
per-provider LLM timeout, the verifier timeout, or an optional explicit step
limit. Invalid or missing managed-host budgets fail closed before the Alysis Code
command is launched. When an agent logging directory is present, Terminal-Bench runs write
`agent-logs/managed-host-deadline.json` with sanitized metadata only.

The Terminal-Bench adapter has no fake verifier default. If no explicit
`verify_cmd` or `verify_cmds` agent kwarg is supplied, the host verifier state is
unavailable; setup clears managed-profile explicit verifier commands and
Alysis Code uses normal repo-native verification discovery. When commands are
supplied, the adapter rejects simultaneous `verify_cmd`/`verify_cmds`, empty
members, and unordered sequences, emits one `--verify-cmd` flag per command in
order, and records only verifier status/source/count plus stable command hashes
in diagnostics, never the raw command text.

Task-specific checks are intentionally narrow: they must execute a changed
script, a repo-local validation executable, or a concrete file comparison such as
`diff`/`cmp`. They are supplemental when authoritative or effective commands are
available, so a targeted smoke check cannot override a required contract.

The gate policy returns four telemetry-visible decisions: `ALLOW_FINAL`,
`NUDGE_AND_CONTINUE`, `TERMINATE_STAGNANT`, and
`TERMINATE_BUDGET_EXHAUSTED`. Non-final progress/planning text and invalid
final claims usually receive bounded continuation nudges instead of immediate
turn termination. By default an unchanged evidence episode receives two
targeted repair nudges; the third unchanged invalid finalization terminates as
completion-gate stagnation. Duplicate nudge detection is logged as telemetry
and does not independently terminate a turn.

Unknown tool calls return a structured recovery payload with the requested name,
currently available tool names, nearest suggestions, and alias guidance. Only
explicitly registered schema-compatible aliases are executed automatically;
ambiguous names such as generic read/write/search forms produce correction
guidance and count against the existing repeated-tool guard. Tool schema details
and secrets are not exposed in the recovery payload.

Subagent child sessions receive a separate exact catalog of the filtered tool
names they are allowed to call. That catalog is generated from the child session
tool registry after allow/deny filtering, so chat and one-shot subagents share
the same no-alias grounding and unknown-tool correction behavior.

Top-level sessions expose synchronous `subagent_run` plus background spawn,
status, wait, cancel, send, and resume operations. Shared exact-readonly and
isolated calls may run in parallel; shared writers stay sequential. Isolated
results remain available for explicit `subagent_apply` or `subagent_discard`,
and read-only roles can use `workspace_from_run` to verify the retained candidate
before it reaches the parent tree. Dependency chains, bounded writer helpers,
the grounded `dependency-scout`, and the `/subagents` active-run window all
share the session-owned child registry and the parent's absolute deadline.

Provider transport retries are typed and cause-chain aware. Throttling,
remote-protocol stream truncation, incomplete chunked reads, and peer-closed
connections can be retried when they are transient, while authentication and
permanent client errors do not retry. Streaming responses buffer deltas until a
complete stream is accepted, so a restarted stream does not leak partial tool
calls or duplicate visible text. Telemetry records stream restart count and
reason without recording raw model content.

For one-shot execute-intent turns, the prompt contract matches that runtime
behavior: Alysis Code must not emit a standalone text-only plan and wait for the
user. A short visible plan is allowed only as part of the same response that
also starts implementation-oriented tool work. After read-only exploration, the
next response should normally implement or create the requested deliverable, run
an implementation-producing command, verify already-existing work, or report a
concrete evidence-backed blocker. Explicit plan-only, advice-only, review-only,
or no-modification requests remain non-execution requests.

Empty assistant responses after tool results are recoverable model-control
anomalies with a separate bounded budget from success-claim and ordinary gate
repair. The runtime records recovery telemetry, appends a concise action
directive naming the missing next action, and retries. During the finalization
window, at most one compact anomaly recovery call is allowed. When the provider
explicitly supports forced tool choice and an appropriate schema is available,
Alysis Code may request a safe tool such as `fs_write` for an explicit output or
`verify_run` for missing verification; otherwise it falls back to ordinary
prompting. Repeated empty responses stop with a local truthful summary rather
than another forced-summary model call. For execute-intent one-shot and
interactive execute turns, generic clarification-only answers are rejected when
the instruction already gives enough direction for a safe best effort.
Clarification can still finalize when safety-critical scope, credentials,
unavailable external inputs, or destructive alternatives require user choice.

The controller tracks progress episodes from the current gate stage, material
edit state, touched paths, verification generations and coverage, missing
commands, normalized verification failure signatures, acceptance status counts
and failure signatures, and accepted blocker state. Meaningful progress includes
real edits, required artifact creation, accepted verification, improved
hard-criterion coverage, changed missing-command state, a changed failure after
an intervening edit, or concrete blocker evidence. Repeated read-only calls,
unknown-tool correction loops, repeated plan/progress prose, duplicate
clarifications or nudges, identical final claims, empty responses, and identical
verification failures without a relevant edit do not keep resetting stagnation.

If an unchanged episode repeats beyond the bounded nudge policy, the run exits
non-zero as completion-gate stagnation. Actual completion-gate repair
step-budget exhaustion instead reports `TERMINATE_BUDGET_EXHAUSTED`, and
forced-summary wording distinguishes that budget case from gate stagnation.
Release maintainers should keep the focused regression matrix in
[Release checklist](release_checklist.md) green when changing this behavior.

## Forge

Forge is the plan-driven workflow for larger tasks. It creates a structured
plan, executes scoped tasks, records verification/review evidence, and keeps run
artifacts under the workspace runtime directory.

Use Forge when a change benefits from:

- an explicit plan
- scoped task boundaries
- review or verification gates
- batch execution
- local PR-style task flow

See [Forge](forge.md).

## Sessions And Logs

Alysis Code stores session logs locally as JSONL. Session commands include:

```bash
alysis sessions list
alysis sessions list --all-owners
alysis sessions show <session_id>
alysis sessions score <session_id>
alysis sessions score --latest 5
```

Session logs are stamped with the account that recorded them, and listings show only the
local account's sessions by default. They are never filtered silently: whenever anything is
dropped, the surface prints how many sessions from other accounts were hidden, including when
that leaves the list empty. `--all-owners` (spelled `--all` on `sessions list` for
compatibility) includes them.

Feedback bundles can be created from retained session artifacts:

```bash
alysis report create --path .
alysis report create "expected X, got Y" --path . --latest
```

Alysis Code prepares local artifacts for review. It does not submit GitHub issues
or upload archives automatically.

For crash-resilient minimal diagnostics, pass `--diagnostic-log PATH` or set
`crash_diagnostic_log_path` / `ALYSIS_CRASH_DIAGNOSTIC_LOG_PATH`. This
append-only JSONL stream is opt-in and separate from normal session logs. It
still records lifecycle events when `--no-log` disables session logging, but it
only writes allowlisted metadata such as event type, run/session ID, runtime
kind, step, tool name, status, duration, and deadline state. It does not write
prompts, tool arguments, command output, source contents, API keys,
authorization headers, or environment dumps.

## Updates

Alysis Code checks for newer releases in a non-blocking, cache-backed way when
enabled. It never installs updates silently.

When a cached check knows about a newer release, interactive launches
(`alysis`, `alysis chat`) show a one-time popup before the setup and
workspace screens with three choices: update now (runs the detected installer
command after your confirmation, then asks you to restart), remind me later
(snoozes the prompt for 24 hours), or skip this version (never asks again for
that release). Editable/source installs and non-interactive launches are never
prompted. Disable the popup with `alysis config set update_prompt_enabled
false` or `ALYSIS_UPDATE_PROMPT_ENABLED=0`; disabling update checks
entirely (`update_check_enabled false`) also suppresses it.

```bash
alysis update check
alysis update
alysis update --dry-run
```

The update command detects common install styles such as `pipx`, `uv`, virtual
environments, and pip installs, then shows the exact upgrade command before
running it.

## Troubleshooting

- If `alysis chat` shows a network or model error, verify the API key, base
  URL, model name, and network access.
- If provider setup fails, run `alysis doctor providers` for redacted
  provider diagnostics.
- If a connection looks signed in from a terminal but not from an application
  that spawns the CLI, run `alysis doctor auth`. The spawned process usually
  resolves a different home directory or cannot reach the OS keyring holding the
  credential vault's master key.
- If a provider response ends with a truncated stream, incomplete chunked read,
  or peer-closed transport error, Alysis Code retries only when the error is
  transient and the current deadline budget permits the retry.
- If the model asks for an unavailable tool, inspect the structured correction
  payload. It lists available names and safe suggestions without exposing tool
  schema internals.
- If shell commands cannot run, check the selected execution mode and sandbox
  setup.
- If a one-shot run stops with `deadline_exhausted`, increase
  `--deadline-seconds` or remove `ALYSIS_RUN_DEADLINE_SECONDS`; this is
  separate from step-budget exhaustion and from `ALYSIS_LLM_TIMEOUT_S`.
- If normal session logs are disabled with `--no-log`, use
  `--diagnostic-log PATH` only when you need minimal crash-safe lifecycle
  diagnostics.
- If web search is unavailable, run `alysis tools` and check `web_search`
  readiness.
- If `web_fetch` refuses a URL, the URL was not supplied by the user, returned
  by `web_search`, or recovered through an allowed canonical/same-origin path.
  Provide the URL explicitly or enable a supported search backend.
- If the workspace is not what you expected, run `/pwd` in chat or start again
  with `--path`.
- If clipboard image paste does not work, install a supported clipboard backend
  for your platform.

## Detailed Guides

- [Architecture](architecture.md): high-level system structure.
- [Quickstart](quickstart.md): first setup and first run.
- [Credentials](credentials.md): API key precedence and storage.
- [Security model](security_model.md): trust boundaries and sandboxing.
- [Shell sandbox](shell_sandbox.md): Docker and Bubblewrap setup.
- [Server mode](server.md): HTTP API operation.
- [Forge](forge.md): plan-driven workflows.
- [MCP](mcp.md): external server integration.
- [Skills](skills.md): reusable instruction bundles.
- [Skills lifecycle](skills_lifecycle.md): skill authoring, validation, installation, and removal.
- [Subagents](subagents.md): synchronous and background specialists, isolated candidates, and pinned verification.
- [Plugins](plugins.md): trusted extension bundles.
- [Custom tools](custom_tools.md): trusted Python tool authoring.
- [Lifecycle hooks](hooks.md): command-based policy and automation.
