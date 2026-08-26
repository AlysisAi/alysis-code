<p align="center">
  <img src="https://raw.githubusercontent.com/AlysisAi/alysis-code/main/docs/assets/alysis-demo.gif" alt="Alysis Code owl logo" width="192" height="192">
</p>

<h1 align="center">ALYSIS</h1>

<p align="center">
  <strong>Local CLI coding agent that turns plans into reviewed, PR-ready code.</strong>
</p>

<p align="center">
  Bring your own model. Sandboxed by default.
</p>

<p align="center">
  <a href="https://alysiscode.com/">Website</a> ·
  <a href="https://github.com/AlysisAi/alysis-code/tree/main/docs">Docs</a> ·
  <a href="https://github.com/AlysisAi/alysis-code/blob/main/CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://github.com/sponsors/AlysisAi"><img src="https://img.shields.io/github/sponsors/AlysisAi?label=Sponsor&logo=GitHub" alt="GitHub Sponsors"></a>
</p>

<p align="center">
  <a href="https://github.com/AlysisAi/alysis-code/actions/workflows/ci.yml"><img src="https://github.com/AlysisAi/alysis-code/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/alysis-code/"><img src="https://img.shields.io/pypi/v/alysis-code.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/alysis-code/"><img src="https://img.shields.io/pypi/pyversions/alysis-code.svg" alt="Python versions"></a>
  <a href="https://github.com/AlysisAi/alysis-code/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/alysis-code.svg" alt="License"></a>
</p>

---

## Get started

```bash
pipx install alysis-code
alysis chat       # pick a provider, paste your API key, start building
```

Bring your own key from any supported provider — or point Alysis Code at a local endpoint (Ollama,
LM Studio, vLLM) and run fully offline. The `/config` picker lists every preset.

---

## Why Alysis Code

- **Forge** — Plan, dispatch parallel workers, verify each task, ship.
- **Orchestrated subagents** — Run isolated workers in parallel, pin verification to their exact
  candidates, and apply only after host-side conflict checks.
- **Cross-run memory** — Failures become structured issues the next run avoids.
- **Bring your own model** — OpenAI, Anthropic, DeepSeek, Qwen, Gemini, Mistral, Xiaomi MiMo, OpenRouter, xAI, and more — plus local endpoints.
- **Sandboxed by default** — Docker or Bubblewrap. An always-on denylist refuses `rm -rf /`, `curl | sh`, and `sudo` — even in `fullaccess`.

## How Forge Works

Type `/forge` in chat (or run `alysis forge plan`), describe what you want, and Forge:

1. Asks 1–3 clarifying questions if the ask is vague.
2. Writes `plan.json` with explicit tasks and runnable file scope.
3. On `/execute plan`, dispatches a swarm of workers that run tasks in parallel.
4. Verifies each task before marking it done. Failures become `issue` entries the next attempt sees.
5. Merges to `main` when everything passes, then reports what changed, where the files landed, and
   how to try the result.

All plans, traces, and per-task artifacts persist under `.alysis/runs/<run_id>/`. Resume any time with `/forge resume`.


## Install

Alysis Code requires Python 3.11 or newer.

```bash
pipx install alysis-code
```

If your default `python3` is older than 3.11:

```bash
pipx install --python python3.12 alysis-code
```

`pip` also works inside a virtual environment:

```bash
python -m pip install alysis-code
```

## Quick Start

```bash
pipx install alysis-code
export ALYSIS_API_KEY="YOUR_KEY"
alysis config set model "your-model"
alysis chat
```

On a fresh install, running `alysis` opens a guided setup wizard. Under
**Connection Method**, choose **Use an API key** or **Use an AI subscription**.
The subscription choice then shows the supported provider connections. Re-run
the wizard anytime:

```bash
alysis setup
```

### Use an AI subscription

Subscription connections use provider sign-in while keeping Alysis Code's native
TUI, agent loop, tools, skills, MCP, subagents, and Forge. Alysis Code
stores refreshable credentials in its encrypted provider vault, never in
`config.json`. The built-in connection currently supports ChatGPT Codex sign-in:

```bash
alysis auth login openai-codex
alysis run --mode readonly "Explain this repository."
alysis chat --mode auto
```

Use `alysis auth login openai-codex --device-code` when a browser callback is
not practical. Then choose the account model and its supported reasoning effort
in **`/config` → Default Model**; setup and login do not choose them implicitly.
If the selected subscription is not connected, plain `alysis` still opens
the native TUI: type **`/login`** and choose Alysis Code or an AI subscription.
Model prompts remain blocked until authentication succeeds, and the TUI then
opens **Default Model** so the model and reasoning effort can be selected
together. Non-interactive `alysis run` commands continue to fail fast while
disconnected. The provider-specific `auth login` command remains available for
shell scripts and non-interactive use.
See [AI subscription connections](docs/account-runtimes.md) for the
credential, compatibility, configuration, and adapter extension boundaries.

Configure a provider endpoint and model:

```bash
alysis config set base_url "your-base-url"
alysis config set model "your-model"
```

Per-command key, endpoint, and model overrides:

```bash
alysis run --api-key-env OTHER_API_KEY --base-url "your-base-url" --model "your-model" "Summarize this project."
```

## Core Commands

| Command | Use |
| --- | --- |
| `alysis` | Start setup or interactive chat. |
| `alysis setup` | Choose API-key or AI-subscription model access and connect an account. |
| `/login` | Choose and connect an Alysis Code account or supported AI subscription inside the TUI. |
| `alysis auth list` | Show supported subscription connections and sign-in state. |
| `alysis run "..."` | Run a one-shot task in the current workspace. |
| `alysis chat` | Start an interactive coding session. |
| `alysis forge plan` | Create or update a Forge plan from the CLI. |
| `alysis forge exec` | Execute a Forge task non-interactively. |
| `alysis forge swarm` | Run Forge tasks across parallel workers. |
| `alysis tools` | Show built-in tools and readiness. |
| `alysis sandbox doctor --smoke` | Check sandbox readiness. |

Useful chat commands include `/help`, `/status`, `/mode`, `/config`, `/plan`, `/forge`, `/skill`, `/subagent`, `/terminals`, `/resume`.

## Execution Modes

Choose a mode per command with `--mode`, change it in chat with `/mode`, or set the default with `alysis config set default_mode <mode>`.

| Mode | Behavior |
| --- | --- |
| `readonly` | Inspection-only. No file writes, shell, MCP, or subagent delegation. |
| `review` | Default safe mode. Previews and asks before file writes and shell commands. |
| `auto` | Applies changes with fewer prompts. Hard denylist still applies. |
| `fullaccess` | No mode-level approval prompts. Denylist + audit log still active. |

```bash
alysis run --mode readonly "Find risky areas in this codebase."
alysis run --mode review "Implement the failing test fix."
alysis chat --mode auto
```

### Personas

Personas layer named postures on top of the execution-mode gate: `code` (implementation),
`architect` (planning; may write markdown documents only), `ask` (read-only questions), and
`debug` (reproduce-before-fix), plus custom personas from `.alysis_personas/*.md`. Switch
with `/persona`, cycle with **Tab** in the TUI, or let the model propose a switch through the
approval-gated `switch_mode` tool. A persona is a convention, never a permission: the clamp
rule means it can keep or lower your execution mode but can never raise it, and each persona
can bind its own model role (`persona_models.<persona>`). See
[Personas](docs/personas.md).

```bash
alysis run --persona architect "Design the auth flow and write it to PLAN.md"
alysis chat --persona ask
```

For execution-style `alysis run` tasks, a final answer is accepted only after the runtime sees
the applicable material-work and verification evidence, or an accepted concrete blocker. Text-only
plans/progress updates and invalid final claims get bounded continuation nudges; repeated
no-progress finalization stops as completion-gate stagnation, which is reported separately from
actual step-budget exhaustion. The default gate policy permits two targeted nudges for an
unchanged evidence episode before the third unchanged invalid finalization terminates; duplicate
nudge detection is telemetry, not an independent stop path. Empty model responses are treated as a
recoverable control anomaly first, and generic clarification questions are rejected for actionable
one-shot tasks unless safety, credentials, external inputs, or destructive choices genuinely require
the user.

Verification evidence is typed before it can satisfy the gate. Effective or managed verification
commands are authoritative, repo-native test commands can satisfy normal tasks, and task-specific
acceptance checks are accepted only when no known verification contract needs precedence.
Verification commands themselves are represented internally by one canonical command analysis with
stable IDs, provenance, trust level, execution mode, timeout policy, validation status, shell-control
classification, pipeline policy, and evidentiary capability. Ordinary commands use argv-compatible
execution. The only shell compound form that can satisfy verification by default is a safely
analyzed wrapper such as `cd <workspace> && <single assertive command>` or `bash -lc '<single
assertive command>'`. Pipelines such as `pytest -q | cat`, `tail`, or `tee` are rejected as
authoritative verification unless a future runner can guarantee pipefail semantics across every
backend. Explicit interpreter snippets such as Python assertions are converted to interpreter
invocations instead of being passed to `/bin/sh`, and malformed fragments stay diagnostic/advisory
rather than hard requirements.
Execute-intent turns also build an acceptance contract from the user instruction, trusted task
context, planning constraints, pre-turn workspace scan, and host verification commands. The gate
tracks concrete required artifacts, exact user commands, preservation constraints, durable-service
requests, thresholds, and repo-native check surfaces. Evidence records whether it came from a host
command, explicit user check, pre-existing repo or task checker, direct black-box command,
self-authored test, or ad hoc observation; self-authored tests are supplemental when independent
acceptance evidence is still missing or contradicted.
Path requirements are root-aware: `/app/out.txt` inside a workspace rooted at `/app` is treated as
`out.txt`, while external absolute paths stay external and are not probed as repo mutations. If a
request asks to save, write, create, export, move, or put an answer or result in a local path,
Alysis Code routes it through the repo-capable execution loop even when web or MCP research is needed
first; web-assisted execution remains able to call web tools before writing the requested artifact.
Explain-only, plan-only, advice-only, and no-modification requests keep their non-execution
precedence.
Mentioned paths carry roles such as required output, existing input, preservation target,
verification checker, or advisory reference. Criteria also carry confidence and enforcement:
explicit user and host requirements are hard, while weak parser inferences and extension-only
format hints are advisory. Finalization is decided by a completion certificate over hard criteria,
current-generation verification, and known failures; a green generic test run does not override a
missed explicit threshold or preservation violation. The same shared gate path is used for
interactive execute turns and one-shot runs.
Generic `pytest -q` is not universal authority: it is selected only when a trustworthy pre-existing
Python test surface exists, while explicit user commands, host commands, and discovered repo-native
checks take precedence. If the agent already ran the exact trusted task command through
`shell_run`, that successful clean run can satisfy the same verification contract without repeating
it through `verify_run`; a command that mutates source or requested outputs requires a later clean
rerun.
Trusted provenance is not enough by itself: vacuous or non-assertive commands such as `true`,
`echo ok`, `python -c 'pass'`, plain `curl -s`, and failure-masking forms such as
`pytest -q || true` are rejected or marked inconclusive before they can satisfy authoritative
verification. Process success is separate from contract success: `verify_run` reports each command
as `passed`, `failed`, `inconclusive`, `not_executed`, or `stale`, and `all_passed` is true only
when every command both exits successfully and has confirmed meaningful real execution. Pre-existing
workspace checker entrypoints are fingerprinted by resolved path, regular-file state, size, and
SHA-256 content hash; if the agent modifies, replaces, deletes, or retargets one, the evidence is
downgraded to supplemental `mutable_authoritative_checker`/self-authored evidence. A missing
managed-host verifier is represented as verifier unavailable, not as a successful host check.
Persistent-service requests require explicit durable-service evidence. `shell_background` remains
session-owned and is reaped on session close; `shell_service_start` creates a durable service with
detached logs/metadata, a stable `service_id`, and a bounded readiness probe that is rechecked before
finalization can pass.
In one-shot execute-intent runs, a visible plan is not a stopping point: after read-only
exploration the next step should normally implement, create the requested deliverable, verify
existing work, or report a concrete evidence-backed blocker. Explicit plan-only, advice-only,
review-only, or "do not modify files" requests remain non-execution requests.

One-shot runs can also take an invocation-wide wall-clock deadline:
`alysis run --deadline-seconds 600 "..."`. This is separate from the
per-provider LLM timeout: the run deadline stops new LLM, tool, verification,
and subagent work once the monotonic deadline is exhausted, while LLM timeout
controls a single model request.
Managed benchmark hosts should invoke `alysis run` with both
`--benchmark`, `--deadline-seconds N`, and `--require-deadline`, passing the
instruction after a positional `--` separator. `--benchmark` selects the raw
autonomy profile: auto posture, code-only routing, a longer fixed step budget,
and no optional subagents, skills, custom tools, or web search by
default. The host deadline should be derived from the final effective agent
timeout, not verifier or environment-build timeouts, and should leave host-side
reserve for process collection and artifact flushing. This host reserve is
separate from Alysis Code's internal finalization reserve.
The Terminal-Bench adapter enforces this contract by requiring
`managed_host_agent_timeout_sec` through `--agent-kwarg`; with current
Terminal-Bench runners, pass the same value as `--global-agent-timeout-sec`
because the runner applies per-task timeout multipliers inside the harness and
does not expose that computed value to imported agents. The host shutdown
reserve is configured by `managed_host_shutdown_reserve_sec`, then
`ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC`, then the conservative default of
30 seconds. When an agent logging directory is available, the adapter writes a
sanitized `managed-host-deadline.json` artifact with the timeout source, elapsed
pre-launch time, host reserve, computed Alysis Code deadline, and validation
status. The Terminal-Bench adapter does not pass a default `--verify-cmd`; when
no explicit host verifier is supplied, setup clears managed-profile explicit
verifier commands, marks the host verifier unavailable, lets repo-native
verification discovery run normally, and records only sanitized verifier
status/count/hash metadata.
Transient provider transport failures such as stream truncation, incomplete
chunked reads, peer-closed connections, and provider-side throttling are retried
only when the remaining deadline budget can safely absorb the retry. During the
soft finalization window, provider retries are limited to one bounded restart so
the agent preserves time to materialize or truthfully summarize the best result.

## Model-Independent Web Search

Alysis Code exposes `web_search` to the active model and lets the model decide when
current external evidence is needed. Native protocol clients map that capability
to the provider's server-side tool; other models call the same Alysis Code tool and
use the provider adapter selected for their profile.

Configure one search backend for the installation:

```bash
alysis config set web_search_policy auto
alysis config set web_search_mode auto
export ALYSIS_WEB_SEARCH_API_KEY=<tavily-key>
alysis tools
```

`web_search_policy=auto` makes search available to the model; `off` hides it.
`web_search_mode` independently selects `auto`, provider-hosted, external-only,
or disabled backend behavior. Direct providers without hosted search, including
DeepSeek, use the configured external adapter. See the
[provider coverage matrix](docs/web-search.md) for the exact API and model support.

## Sandbox & Safety

Shell and verification execution run inside a hardened Docker or Bubblewrap sandbox by default.
Shell commands and verification commands default to strict sandboxing. To deliberately disable
verification sandboxing for a trusted local setup, set `verify_sandbox.mode="off"` or
`ALYSIS_VERIFY_SANDBOX_MODE=off`.

```bash
docker pull ghcr.io/alysisai/alysis-sandbox:dev
docker pull ghcr.io/alysisai/alysis-sandbox:server
```

Prepare or diagnose:

```bash
alysis sandbox setup
alysis sandbox doctor --smoke
alysis sandbox pull
```

The denylist is always-on across every mode. It refuses `rm -rf /`, `curl ... | sh`, `sudo`, force-push to `main` / `master`, raw disk writes, fork-bombs, recursive `chmod 777 /`, and direct `> /dev/sd*` redirects. In `fullaccess`, every successful shell command additionally writes a JSONL audit event.

Outbound HTTP from web tools and MCP OAuth goes through `safe_http_request` with SSRF guards: rejects non-HTTP schemes, loopback / link-local / private / multicast targets across IPv4 and IPv6, validates redirects, and enforces a streamed byte cap.
`web_fetch` is additionally provenance-gated: fetch targets must be explicitly
provided by the user, returned by `web_search`, reached through an observed
canonical redirect, or recovered through a bounded same-origin/search-mediated
path. URLs discovered inside trusted fetched pages, local file reads, and
registered tool or shell output are tracked as source-linked derived URLs rather
than broad same-domain permission. Finalization does not start optional web
research just to repair missing provenance.

See [Shell sandbox](docs/sandbox.md) for backend requirements, image cosign signatures, SLSA provenance, and production pinning. See [Security model](docs/security_model.md) for the full threat boundary.

## Extend Alysis Code

Six capability surfaces. Four of them — skills, custom tools, MCP servers, hooks — bundle into a single declarative `.toml` plugin manifest.

- [**MCP**](docs/mcp.md) — connect stdio or Streamable HTTP MCP servers, with OAuth, frozen catalogs, and narrowing-only project overrides.
- [**Custom tools**](docs/custom_tools.md) — drop Python scripts into `.alysis/tools/*.py`. AST-only discovery, trust-keyed by file hash.
- [**Skills**](docs/skills.md) — `SKILL.md` instruction bundles. Native + interop roots (`.alysis_skills/`, `.agents/skills/`, `.claude/skills/`, `.github/skills/`).
- [**Subagents**](docs/subagents.md) — focused delegation. Drop YAML+markdown into `.alysis_agents/*.md` for custom agents. Built-ins include `explorer`, `dependency-scout`, `implementer`, `frontend-engineer`, `debugger`, `verifier`, `code-reviewer`, `test-strategist`, and the opt-in `visual-designer` image generator.
- [**Hooks**](docs/hooks.md) — lifecycle policy across 11 events (`PreToolUse`, `PostToolUse`, `SessionStart`, ...). Three trust layers.
- [**Plugins**](docs/plugins.md) — declarative bundles of skills + custom tools + MCP servers + hooks. Pinned install (registry id or `git+https://...@<sha40>`).

Run as an HTTP service with [Server mode](docs/server.md) — worker jobs, uploads, queues, and authentication.

**Repo conventions.** Alysis Code reads `AGENTS.md`, `CLAUDE.md`, and `CONVENTIONS.md` from your repo root as read-only project context.

## Configuration & Credentials

API keys can come from per-command options, `ALYSIS_API_KEY` or persisted credentials.

```bash
alysis config show
alysis config set-api-key
alysis config clear-api-key
```

Provider profiles switch between configured endpoints:

```bash
alysis profile presets
alysis profile use openai
alysis profile list
```

AI-subscription credentials follow a separate boundary: Alysis Code encrypts them
in its provider vault and attaches them only to the selected adapter's allowlisted
destinations. `config.json` contains only the connection plus the model and
reasoning effort explicitly chosen in `/config`. See
[AI subscription connections](docs/account-runtimes.md).

See [Credentials](docs/credentials.md) for key resolution and storage details.

Reliability knobs:

- Normal chat, run, Forge, and subagent execution is autonomous and has no
  default step ceiling. `--max-steps N` adds an optional safety limit for a
  specific invocation; persisted legacy caps are used only by the opt-in
  `limited` policy. Setup and `/config` do not ask for step budgets.
- `run_deadline_seconds` or `ALYSIS_RUN_DEADLINE_SECONDS` sets a default
  one-shot wall-clock deadline. `--deadline-seconds` overrides it for one run.
  Near the end of a configured deadline, Alysis Code enters a soft finalization
  window: it stops new subagents and broad exploration, keeps bounded finishing
  actions available, and asks the agent once to materialize the best valid
  result before the hard deadline.
- `crash_diagnostic_log_path` or `ALYSIS_CRASH_DIAGNOSTIC_LOG_PATH` enables
  an opt-in minimal JSONL diagnostic log. `--diagnostic-log PATH` overrides it
  for a command and still works with `--no-log`.

The diagnostic log records lifecycle/status metadata only: event type, run and
session IDs, step/tool names, durations, deadline phase/source/reserve state,
and normalized status. It does not persist prompts, tool arguments, command
output, source text, environment dumps, or secrets.

## Workspace Behavior

`alysis run` and `alysis chat` bind a workspace before the session starts. The requested path is `--path` or the current directory. In a git repository, Alysis Code binds to the repository root while preserving the starting directory as the focus directory.

Missing paths require `--create-path`. Broad directories such as `~` require an explicit override. `/` is blocked as a workspace root.

Runtime artifacts such as logs, session files, and coverage output are classified separately from
material source, config, and deliverable changes. Verification commands that mutate material
source/config files or requested deliverables are not allowed to satisfy the completion gate until a
clean verification result follows.
Explicit unchanged or only-touch constraints are checked against material touched paths. Required
thresholds stay failed when measured output misses the requested target, and performance-style
thresholds require repeated samples instead of a single lucky observation.
After tool results, blank assistant responses use a separate model-control anomaly budget: the
runtime records recovery telemetry, appends one compact action-only directive, and may request a
safe forced tool call only when the provider advertises that capability and an appropriate tool
schema is present. Providers without explicit forced-tool support fall back to ordinary prompting.

## Project Links

- [Website](https://alysiscode.com/)
- [Docs index](docs/README.md)
- [Release checklist](docs/release_checklist.md)
- [Changelog](CHANGELOG.md)
- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [License](LICENSE)

Use Python 3.11 or newer for local development. See [CONTRIBUTING.md](CONTRIBUTING.md) for setup and PR expectations. Report vulnerabilities through [SECURITY.md](SECURITY.md), not public GitHub issues.

Alysis Code is distributed under the [Apache License 2.0](LICENSE).
