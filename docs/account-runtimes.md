# Connect an AI subscription

Alysis Code supports two model-access methods:

- **Use an API key** sends requests with a provider API key.
- **Use an AI subscription** signs in through a provider-auth adapter and uses
  the models available to that account.

Both methods keep Alysis Code's native TUI, agent loop, tools, skills, MCP,
subagents, Forge, safety policy, and session history. A subscription
adapter supplies authentication, the account-scoped model catalog, and any
provider request-dialect adjustments; it does not replace Alysis Code with another
coding agent.

## First-run setup

Run Alysis Code from the workspace you want to use:

```bash
cd /path/to/project
alysis
```

Under **Connection Method**, choose **Use an AI subscription**, then select a
supported connection. Setup checks its account state and, when needed, offers
browser sign-in. Device-code sign-in is also available from the CLI.

Setup deliberately does not choose a subscription model or reasoning effort.
After sign-in, choose both in **`/config` → Default Model**. The picker reads the
connected account's live catalog and only shows the reasoning efforts advertised
for the selected model. Alysis Code saves the model and effort together in the
active profile.

If sign-in is skipped or fails, the connection is saved in a pending state. The
native TUI still opens so configuration, help, and other local UI remain
available, but model prompts are blocked. Type `/login` in that TUI and choose the
provider connection; after a successful sign-in, Alysis Code returns to the
TUI and opens **Default Model** automatically. The equivalent shell flow is:

```bash
alysis auth login openai-codex
alysis config menu
```

Non-interactive model calls such as `alysis run` fail fast until the account
is connected and a model/effort pair has been selected.

Existing API-key profiles and their keys remain saved when a subscription is
selected.

## ChatGPT Codex subscription

The built-in `openai-codex` adapter signs in to ChatGPT and uses Codex models
available to that account directly from Alysis Code:

```bash
alysis auth login openai-codex
alysis auth login openai-codex --device-code
```

Inside the TUI, `/login` is the single account connection entry point and offers
both Alysis Code and ChatGPT Codex. Provider-specific shell commands, including
account switching, are:

```bash
alysis auth login openai-codex
alysis auth login openai-codex --device-code
alysis auth login openai-codex --switch-account
```

Under WSL, browser login opens the Windows default browser through WSL interop.
Use `--device-code` if Windows-to-WSL localhost callback forwarding is disabled.

Inspect or remove connection state with:

```bash
alysis auth list
alysis auth status openai-codex
alysis auth logout openai-codex
```

The same account actions are available interactively under **`/config` → Model
Access → Use an AI subscription**. Select the provider to connect, reconnect or
switch accounts, or disconnect without leaving the TUI.

This does not require the Codex CLI. Inside the TUI, `/login` unifies this flow
with Alysis Code account login behind one connection picker. Legacy shell commands
remain provider-specific for compatibility.

The ChatGPT Codex transport is a compatibility integration, not ordinary OpenAI
API usage. Availability depends on the account's plan, workspace permissions,
administrator controls, and current provider behavior. The provider may change
this surface independently, so Alysis Code fails closed on unexpected destinations
or incompatible authentication responses. This transport requires streamed
Responses calls; the adapter enforces streaming even for internal routing and
other callers that consume the completed response as a buffer.

ChatGPT subscription models and similarly named OpenAI API models have separate
capacity metadata. Alysis Code uses the account's live ChatGPT catalog first and a
reviewed subscription-only snapshot only when fields are missing or discovery is
temporarily unavailable. It never substitutes LiteLLM's API context window for a
subscription model. The snapshot is metadata fallback only: the live account
catalog remains authoritative for model availability and supported reasoning
efforts.

## Credentials

Alysis Code stores refreshable subscription credentials in its encrypted provider
vault, separate from `config.json`. It refreshes short-lived access tokens,
rotates refresh tokens when supplied, and only attaches authorization headers to
the adapter's exact allowlisted destinations. Tokens are never written into a
profile, runtime settings, logs, or prompts.

Logout always removes the local credential. Remote revocation is attempted and a
warning is shown if the provider is temporarily unavailable.

The vault's master key comes from the OS keyring when one is usable. When it is
not — a headless container, a CI runner, or a process spawned by a GUI that has
no session keyring — Alysis Code falls back to DPAPI on Windows and to a private
random key file elsewhere, and says so instead of degrading silently. Placeholder
backends are treated as unavailable: `keyring.backends.null` accepts a write and
discards it, so trusting it would produce a vault no later process could decrypt.

## Reading auth state from another application

A supervising application (an IDE extension, a desktop GUI) cannot parse the
human console output reliably, and a spawned process does not necessarily see the
same keyring the user logged in from. Pass `--json` to get one JSON object on
stdout and nothing else:

```bash
alysis auth status openai-codex --json
alysis auth list --json
alysis whoami --json
```

Exit codes separate the command from its answer: **0 means the command ran**,
whatever the auth state, and the payload carries the result. A nonzero exit means
the command itself failed (2 for a usage error such as an unknown connection id).
Do not read `authenticated` from the exit code — read it from the JSON.

Each status object carries `connection`, `authenticated`, `account_label`,
`method`, `detail`, `transport`, and `error`, plus `keyring_available`,
`keyring_backend`, and `credential_fallback` so the caller can explain a
difference between contexts rather than guess at one. `auth list --json` returns
the same per-connection shape inside a `connections` array. When a stored session
needs re-authorization, `detail` is exactly `session expired` — a stable value to
branch on. A status probe never raises: an unreadable credential store comes back
as `authenticated: false` with the reason in `error` or `detail`.

For the environment behind a disagreement, use:

```bash
alysis doctor auth --json
```

That reports the resolved home directory and config dir, whether PATH looks
truncated the way a GUI-spawned process often sees it, the keyring backend with a
non-mutating availability probe, per-store credential health (which key source
each store was written with and which the next write would use), and whether the
current context looks interactive or spawned. Omit `--json` for a table.

## Choose model and reasoning effort

Open the same configuration flow from a shell or a chat session:

```bash
alysis config menu
# or type /config in chat
```

Open **Default Model**, choose an account model, then choose its reasoning effort.
This is the only supported mutation path for a subscription model/effort pair.
The following shortcuts are intentionally rejected for an active subscription
profile because they could create an incompatible pair:

```text
/model <id>
/config set model <id>
alysis config set model <id>
alysis config set llm_reasoning_effort <effort>
```

The provider-auth adapter owns the endpoint as well, so raw `base_url` overrides
are rejected while the subscription profile is active. Changing model access
closes and recreates the current session so clients from two protocols are never
mixed. Legacy `ALYSIS_LLM_REASONING_EFFORT` and
`ALYSIS_LLM_ENABLE_THINKING` environment overrides are also ignored for an
active subscription profile; the paired `/config` selection remains authoritative.

Sampling temperature is provider-managed for the ChatGPT Codex subscription
transport, so `/config` does not show per-role temperature controls while that
profile is active. API-key profiles keep those controls; if an API model rejects
the parameter, the Responses client retries without it and remembers that model's
capability for the process lifetime.

## Use the native Alysis Code agent

After the account is connected and `/config` has a model/effort selection, use
the normal commands:

```bash
alysis run --mode readonly "Explain this repository."
alysis run --mode review "Review the current diff."
alysis chat --mode auto
```

Subscription-backed sessions retain the normal Alysis Code command and tool
surface. Subagents, router/reviewer/compactor clients, native web search, and
session summaries use the same provider-auth connection without requiring a
static API key. Saved role-model overrides that are unavailable to the connected
subscription account are ignored in favor of the selected default model.

## Configuration shape

Secrets are not present in `config.json`. A configured subscription profile
looks like:

```json
{
  "execution": {
    "backend": "native",
    "runtime": null
  },
  "profiles": {
    "chatgpt-codex": {
      "protocol": "openai_responses",
      "base_url": "https://chatgpt.com/backend-api/codex",
      "auth_provider": "openai-codex",
      "default_model": "<chosen-in-config>",
      "reasoning_effort": "<chosen-in-config>"
    }
  },
  "active_profile": "chatgpt-codex"
}
```

Before `/config` selection, `default_model` and `reasoning_effort` may be absent
and Alysis Code will refuse to start chat.

## Add another subscription adapter

Setup and `/config` discover choices through the provider-auth registry. This
keeps provider names and OAuth details out of the UI flow, but it does not make
OAuth universal. Each provider still needs a trusted adapter defining its login,
refresh/revocation behavior, destination allowlist, account model catalog,
protocol, and request dialect.

Adding a profile entry alone cannot connect Claude, Grok, Gemini, or another
subscription. A real adapter and a provider-supported external-application flow
are required. Providers whose terms or technical controls do not allow this use
must remain unsupported.
