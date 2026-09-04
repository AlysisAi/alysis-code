# Alysis Code provider catalog gap check — 2026-09-02

Audit of every hosted preset in `src/alysis_code/profile_presets.py` and the hand-maintained
layers in `src/alysis_code/model_registry.py` (`_OFFICIAL_PROVIDER_MODEL_METADATA`,
`_CANONICAL_MODEL_METADATA`, `_BUILT_IN_MODEL_METADATA`) against official provider docs as of
2026-09-02. Previous full pass: `MODEL_CATALOG_REFRESH_2026-07-19.md`.

**Method.** One research agent per provider group, official docs only (no models.dev / LiteLLM
inference). Every id below appears verbatim on a fetched official page; ids that could not be
confirmed are listed under `unverifiable` and must not be shipped. Time-critical claims
(Perplexity sunset, Together removals, Gemini 3.8) were re-fetched independently.

**Status.** Applied the same day. `profile_presets.py`, `model_registry.py`,
`reasoning_contracts.py`, the Perplexity web-search adapter, tests, `docs/web-search.md`, and the
CHANGELOG were updated; see the CHANGELOG entry for the summary. Two decisions deviate from the
suggestions below and are worth knowing:

- **NVIDIA keeps no aliases.** `nvidia/nemotron-3-nano-30b-a3b` and the undated DeepSeek ids are
  "Free Endpoint: Deprecated" but still appear in NVIDIA's live `/v1/models` inventory, and the
  catalog never rewrites a live id. They only left the offline recommendation set.
- **Perplexity was migrated, not deprecated.** The preset now uses the `openai_responses`
  protocol against `https://api.perplexity.ai/v1` (Agent API) with Perplexity-hosted routes, and
  the search adapter posts to `/v1/agent`. This has been verified against the documented request
  and response shapes only — a live smoke test with a real key is still owed before 2026-09-27.

A new registry test (`test_every_suggested_hosted_model_resolves_real_capacity`) guards the
second-order finding of this pass: `qwen3.7-plus`, `command-a-plus-05-2026`,
`anthropic/claude-sonnet-5`, `openai/gpt-5.6-terra/-luna`, `groq/compound`, `gemma-4-31b`,
`mistral-small-2603`, `grok-build-0.1`, the `grok-4.20-0309-*` snapshots, `glm-4.7-flash(x)` and
`MiniMaxAI/MiniMax-M3` — several of them preset **defaults** — were resolving to the unknown
128K/8K fallback shape on every run because the pinned LiteLLM snapshot lacks them. All now carry
official-layer capacity.

**2026-09-05 addendum — GPT-6 Astra.** OpenAI released `gpt-6-astra` on 2026-09-03
(developers.openai.com/api/docs/models/gpt-6-astra): 1.05M context / 922K max input / 128K
output, $10 / $50, cached input $1, cache writes $12.50, reasoning `low|medium|high|xhigh|max`
(no `none` → always-on contract), Chat Completions + Responses, image input, knowledge cutoff
2026-04-30. It leads the `openai` and `openai-responses` presets and is on OpenRouter as
`openai/gpt-6-astra` (same prices, verified against the live model list). API rollout is staged
("in the coming days" from 2026-09-03); the ChatGPT-subscription surface lists it live once the
plan is enabled (Pro/Business/Enterprise first). `openai/gpt-6-astra-pro` exists on OpenRouter but
has no OpenAI API listing and was not added. Live-probed OK on both OpenAI surfaces.

Not refreshed: the bundled LiteLLM snapshot (`litellm_model_prices_snapshot.json`, upstream commit
3d63eda, fetched 2026-07-11) and the ChatGPT Codex snapshot (2026-07-20). Both are
`manual_reviewed_only`; every post-snapshot model lives in the official-provider layer per the
registry's existing policy.

---

## Summary table

| preset | verdict | missing | retiring / broken | biggest change |
|---|---|---|---|---|
| openai / openai-responses | partially-stale | 0 | `gpt-5.3-codex` is Responses-only | 5.6 prices cut (Sol $4/$20, Terra $2/$12, Luna $0.20/$1.20); alias targets differ from OpenAI's |
| anthropic (+compat/native) | partially-stale | **1** | — | `claude-fable-5-1` shipped 2026-09-01; `claude-fable-5` now Legacy |
| gemini (+compat/native) | partially-stale | **1** | — | `gemini-3.8-flash` shipped 2026-09-02; "2.5 shutdown 2026-10-16" not on official page |
| deepseek | **current** | 0 | — | peak/off-peak pricing since 2026-08-16; effort levels low/high/max |
| nvidia | partially-stale | **4** | validation model's free endpoint deprecated | dated DeepSeek snapshots supersede bare ids; Nemotron 3.5 Lightning |
| qwen-intl / -us / -cn | **stale** | **2–3** | `qwen3-coder-plus`, `qwen3-coder-next` **shut down 2026-10-10** | `qwen3.8-flash`, `qwen3.7-flash`; `qwen3.7-max` now legacy and pricier than 3.8-max |
| zhipu | partially-stale | **3** | — | `glm-5.3` is the flagship at glm-5.2 price; `glm-5.3-flash` 1M multimodal |
| zai-coding-plan | **stale** | **1** | `glm-4.7`, `glm-5-turbo` no longer plan models (server-routed) | `glm-5.3-flash`; "5.3 exclusive to plan" claim is wrong |
| moonshot / moonshot-cn | partially-stale | 0 | all 13 alias sources now 404 | ids correct; web-search rationale outdated |
| kimi-code | partially-stale | **1** | — | `k3-256k`; default effort on this endpoint is `high`, not `max` |
| minimax | partially-stale | 0 | — | M3 is $0.30/$1.20 (permanent 50% off); `MiniMax-M2` alias is wrong (still live) |
| xiaomi-mimo | **stale** | 1 (gated) | `mimo-v2-flash` **gone since 2026-06-30** | catalog prices 2–7x too high |
| bytedance | **stale** | **5** | — | whole default set is now "previous generation"; `doubao-seed-evolving` / 2.1 family |
| groq | partially-stale | **1** | llama ids Enterprise-only since 2026-08-16 | `qwen/qwen3.8-27b` (preview) |
| cerebras | **stale** | 0 | `zai-glm-4.7` **deprecated 2026-08-17** (coding slot dead) | only `gpt-oss-120b` + `gemma-4-31b` remain public |
| mistral | partially-stale | **1** | — | `zai-glm-5-2` (1M, preview); "retired 2026-07-31" note unsupported |
| xai | **current** | 1 (niche) | — | all ids verified; `grok-4.20-multi-agent-0309` optional |
| cohere | **current** | 0 | — | no successor to `command-a-plus-05-2026` |
| openrouter | partially-stale | **8** | — | DeepSeek prices roughly halved; Opus 5 / Fable 5.1 / GLM-5.3 / Kimi K3 / Grok 4.6 / Gemini 3.8 |
| perplexity | **stale** | 3 | `sonar`, `sonar-pro` **shut down 2026-09-27** | whole Chat Completions surface sunsets; Agent API is Responses-format |
| together | **stale** | **7** | `Kimi-K2.7-Code` **removed 2026-08-27**; `gpt-oss-20b` **removed 2026-09-14** | coding + validation models dead/dying |
| fireworks | partially-stale | **7** | — | `glm-5p3`, `kimi-k3`; `deepseek-v4-flash-0731` price wrong in every field |

Totals: **~50 distinct new ids** across presets (many are the same model on different gateways);
**6 hard deadlines** inside the next 6 weeks.

### Act-now deadlines

| date | what breaks |
|---|---|
| **already** | cerebras `zai-glm-4.7` (coding slot) deprecated 2026-08-17; xiaomi `mimo-v2-flash` gone; together `moonshotai/Kimi-K2.7-Code` gone; moonshot alias sources 404 |
| **2026-09-08** | nvidia `minimaxai/minimax-m3` shuts down (not in catalog — do not add) |
| **2026-09-09** | z.ai `glm-5.3-flash` 50% launch promo ends |
| **2026-09-14** | together `openai/gpt-oss-20b` removed → `validation_model` and fallback fail |
| **2026-09-27** | perplexity `sonar` / `sonar-pro` Chat Completions sunset → entire preset fails |
| **2026-10-10** | qwen `qwen3-coder-plus` + `qwen3-coder-next` shut down, all regions (Coding Plan copies too) |
| **2026-10-15** | anthropic `claude-haiku-4-5-20251001` earliest-retirement date (no notice yet; only Haiku) |
| **2026-11-21** | openai Sol promo price ($4/$20) guaranteed through at least this date |
| **2026-12-11** | openai `gpt-5-nano` (alias source) shutdown |
| **2026-12-31** | gemini 3.6/3.7/3.8-flash intro pricing ends → $1.50 / $7.50 / $0.15 |

---

## New model ids missing from the catalog

Only ids verified on official pages. Prices $/MTok in / out / cache-read unless noted.

### First-party

| preset | id | ctx | out | price | reasoning | vision | suggested role | note |
|---|---|---|---|---|---|---|---|---|
| anthropic | `claude-fable-5-1` | 1M | 128K | 10 / 50 / 0.25 (5m write 12.50, 1h 20) | always-on adaptive | yes | reasoning (replace `claude-fable-5`) | Released 2026-09-01. Forced `tool_choice` returns an error; thinking blocks incompatible with earlier models. Cache read 0.025x. |
| gemini | `gemini-3.8-flash` | 1,048,576 | 65,536 | 0.75 / 3.75 / 0.075 (→ 1.50 / 7.50 / 0.15 from 2027-01-01) | yes (`thinking_level`; `minimal` errors) | yes | default (demote 3.7 → fallback) | Stable, released 2026-09-02. Same price as 3.7. |
| qwen-intl/-us/-cn | `qwen3.8-flash` | 1M | 131,072 | intl 0.15 / 0.47 / 0.016; us 0.113 / 0.382 / 0.014; cn ¥0.8 / 2.7 / 0.1 | optional | yes | fast (replace `qwen3.6-flash`) | Released 2026-08-26. Alibaba's stated low-cost tier. |
| qwen-intl/-us/-cn | `qwen3.7-flash` | 1M | 131,072 | intl 0.03 / 0.13 / 0.006 (≤32K); us 0.028 / 0.11; cn ¥0.2 / 0.8 | optional | yes | economy + validation (replace `qwen-flash`) | Released 2026-07-21. Fully featured; `qwen-flash` lacks function calling outside Beijing. |
| qwen-intl/-cn | `qwen3.8-2.4t-a95b` | 1M | 131,072 | USD not published (cn ¥12 / 36 / 1.5) | yes | no | optional | Open-weight 3.8 flagship, 2026-08-12. Beijing + Singapore only. |
| zhipu | `glm-5.3` | 1M | 131,072 | ¥8 / 28 / 2 | forced (`thinking.type=disabled` errors; effort low/high/max) | no | default (replace `glm-5.2`) | 2026-08-19. Same price as 5.2. |
| zhipu | `glm-5.3-flash` | 1M | 131,072 | ¥0.8 / 2.8 / 0.23 (promo ½) | forced | yes (image/video/file) | fast | 2026-08-26. Only paid flash with 1M. |
| zhipu | `glm-5v-turbo` | 200K | 131,072 | ¥5 / 22 / 1.2 | optional | yes | optional | 2026-04-02; superseded for vision by 5.3-flash. |
| zai-coding-plan | `glm-5.3-flash` | 1M | 131,072 | plan credits: 2.3 / 0.56 / 8 per 10K (3x quota of 5.3); PAYG 0.15 / 0.50 / 0.03 | forced | yes | fast + fallback (replace `glm-5-turbo`, `glm-4.7`) | On every plan tier. |
| kimi-code | `k3-256k` | 262,144 | — | quota (~½ of `k3`) | yes (default effort **high**) | image only (no video) | economy | Official "recommended launch"; Moderato+. Switching from `k3` fails if history has video. |
| bytedance | `doubao-seed-evolving` | 1,048,576 | 262,144 | ¥6 / 30 / 1.20 | optional (default high) | yes | default | Rolling always-latest coding/agent id; return `encrypted_content` in history. |
| bytedance | `doubao-seed-2-1-pro-260628` | 262,144 | 262,144 | ¥6 / 30 / 1.20 | optional | yes | advanced | Flat pricing. |
| bytedance | `doubao-seed-2-1-turbo-260628` | 262,144 | 262,144 | ¥3 / 15 / 0.60 | optional | yes | fast | Half of 2.1-pro. |
| bytedance | `doubao-seed-2-0-lite-260428` | 262,144 (224K in) | 131,072 | ¥0.6 / 3.6 / 0.12 (≤32K) | optional | yes | economy | Newer snapshot than `-260215`. |
| bytedance | `doubao-seed-2-0-mini-260428` | 262,144 (224K in) | 131,072 | ¥0.2 / 2.0 / 0.04 (≤32K) | optional | yes | validation | Newer snapshot than `-260215`. |
| xai | `grok-4.20-multi-agent-0309` | 1M | — | 1.25 / 2.50 / 0.20 (2x ≥200K) | yes | yes | optional | Live since 2026-03-10; deep-research multi-agent. Niche. |

### Hosted / gateway

| preset | id | ctx | out | price | reasoning | vision | suggested role | note |
|---|---|---|---|---|---|---|---|---|
| nvidia | `nvidia/nemotron-3.5-lightning-30b-a3b` | 1M | — | free endpoint | yes (`enable_thinking`, `reasoning_budget`) | no | fast + **validation** (replace nano) | 2026-08-11. Nano's free endpoint is deprecated. |
| nvidia | `deepseek-ai/deepseek-v4-pro-0813` | 1M | — | free endpoint | yes (low/high/max) | no | reasoning (replace bare id) | 2026-08-24; "supersedes the preview model". |
| nvidia | `deepseek-ai/deepseek-v4-flash-0731` | 1M | — | free endpoint | yes | no | fast (replace bare id) | 2026-08-17. |
| nvidia | `moonshotai/kimi-k3` | 1M | — | free endpoint | yes (up to max) | yes | coding | Updated 2026-08-27. |
| groq | `qwen/qwen3.8-27b` | 131,042 (sic, page literal) | 16,384 | 0.80 / 4.00 | optional | yes (≤3 images) | coding (alongside 3.6-27b) | Preview. |
| mistral | `zai-glm-5-2` | 1M | 128K | 1.40 / 4.40 / 0.14 | unknown | no | coding (long-context) | Public preview, 2026-08-06; 1-month deprecation policy; not on Agents/Conversations. |
| openrouter | `anthropic/claude-opus-5` | 1M | 128K | 5 / 25 / 0.50 | yes | yes | advanced (replace `claude-opus-4.8`) | Same price as 4.8. |
| openrouter | `anthropic/claude-fable-5.1` | 1M | 128K | 10 / 50 / 0.25 | yes | yes | reasoning | Added 2026-09-01. Note dot, not dash. |
| openrouter | `z-ai/glm-5.3` | 1,310,720 | 131,072 | 1.40 / 4.40 / 0.26 | yes | no | default candidate | 2026-08-18. |
| openrouter | `z-ai/glm-5.3-flash` | 1,310,720 | 131,072 | 0.075 / 0.25 / 0.015 | yes | yes | economy (replace `z-ai/glm-5.2`) | 2026-08-26. |
| openrouter | `moonshotai/kimi-k3` | 1,048,576 | 943,718 | 3 / 15 / 0.30 | yes | yes | coding | 2026-07-16. |
| openrouter | `x-ai/grok-4.6` | 500K | 450K | 2 / 6 / 0.50 | yes | yes | agentic | 2026-08-12. |
| openrouter | `google/gemini-3.8-flash` | 1,048,576 | 65,536 | 0.75 / 3.75 / 0.075 | yes | yes | fast | 2026-09-02. |
| openrouter | `qwen/qwen3.8-flash` | 1M | 131,072 | 0.15 / 0.47 / 0.016 | yes | yes | economy | 2026-08-26. |
| together | `zai-org/GLM-5.3` | 1M | — | 1.40 / 4.40 / 0.26 | yes | no | default (replace GLM-5.2) | FP4. |
| together | `zai-org/GLM-5.3-Flash` | 1M | — | 0.15 / 0.50 / 0.03 | yes | no | economy | Together's stated small-model replacement. |
| together | `moonshotai/Kimi-K3` | 1,048,576 | — | 3 / 15 / 0.30 | yes | yes | coding (replace K2.7-Code) | Only Kimi left on serverless. |
| together | `Qwen/Qwen3.5-9B` | 262,144 | — | 0.17 / 0.25 | optional | yes | **validation + fallback** (replace gpt-oss-20b) | Together's stated replacement for gpt-oss-20b. |
| together | `Qwen/Qwen3.7-Plus` | 1M | — | 0.32 / 1.28 | yes | no | fast | — |
| together | `Qwen/Qwen3.8-Flash` | 1M | — | 0.15 / 0.47 | yes | no | economy | function-calling column blank on page. |
| together | `Qwen/Qwen3.8-2.4T-A95B` | — | — | 2.50 / 6.25 / 0.50 | yes | no | optional | ctx/tool columns blank on page. |
| fireworks | `accounts/fireworks/models/glm-5p3` | 1,040,000 | — | 1.40 / 4.40 / 0.26 | yes | no | default (replace glm-5p2) | 2026-08-28. |
| fireworks | `accounts/fireworks/models/glm-5p3-flash` | 1,040,000 | — | 0.15 / 0.50 / 0.03 | yes | yes | economy | 2026-08-26. |
| fireworks | `accounts/fireworks/models/kimi-k3` | 1,040,000 | — | 3 / 15 / 0.30 | yes | yes | coding | 2026-07-19; Fireworks' top code/agentic pick. Fast router `routers/kimi-k3-fast`. |
| fireworks | `accounts/fireworks/models/nemotron-lightning-3p5-30b-a3b` | 262,000 | — | 0.05 / 0.20 / 0.01 | yes | no | **validation** | 2026-08-07; cheapest model. |
| fireworks | `accounts/fireworks/models/deepseek-v4-flash-vision-exp` | — | — | 0.22 / 0.66 / 0.007 | yes | yes | vision | On pricing page; model page empty. |
| fireworks | `accounts/fireworks/models/gpt-oss-120b` | 131,000 | — | 0.15 / 0.60 / 0.015 | yes | no | fallback | Migration target for gpt-oss-20b. |
| fireworks | `accounts/fireworks/models/qwen3p8-2p4t-a95b` | 262,000 | — | 2 / 6 / 0.25 | yes | no | optional | The "Qwen 3.8 Max" pricing link resolves here; no `qwen3p8-max` id observed. |
| perplexity | `perplexity/sonar`, `perplexity/kimi-k2.7-code`, `perplexity/glm-5.3` | — | — | 0.25/2.50; 0.95/4.00; 1.40/4.40 | — | — | see Perplexity section | **Agent API only** (Responses format, base `https://api.perplexity.ai/v1`). Not usable on the current `openai_compat` preset. |

### Do NOT add (gated / retiring)

| id | why |
|---|---|
| `claude-mythos-5-1`, `claude-mythos-5` | Invite-only (Glasswing / CVP / LSVP). Requests from unenrolled orgs fail. |
| `gpt-5.6-cyber` | Daybreak Red program, separate approval, Responses-only, $12.50 / $75. |
| `chat-latest` (OpenAI) | Rolling ChatGPT snapshot; OpenAI says use `gpt-5.6-sol` for production. |
| `mimo-v2.5-pro-ultraspeed` | Closed beta by application; not returned by `/v1/models`; offline date TBA. |
| `minimaxai/minimax-m2.7` (Groq) | Enterprise / Contact Sales only. |
| `minimaxai/minimax-m3` (NVIDIA) | Shuts down 2026-09-08. |
| `z-ai/glm-5.2` (NVIDIA) | Free endpoint deprecated. |
| `~z-ai/glm-latest` etc. (OpenRouter) | Rolling aliases; catalog policy avoids floating ids. |
| Mistral "Ministral 3 14B", Cohere "North Mini Code" | No API id published / HF weights only. |
| Perplexity Router API ids | Private preview by email. |

---

## Deprecated, retired, or broken entries currently in the catalog

| preset | entry | status | official replacement |
|---|---|---|---|
| openai | `gpt-5.3-codex` (suggested, coding) | Active on API but **Chat Completions: Not supported** — broken on the `openai` preset; deprecated in Codex-with-ChatGPT | `gpt-5.6-sol` |
| openai | aliases → `gpt-5.5` | `gpt-5.5` is Active, but OpenAI's official replacement for the retired codex/chat-latest ids is `gpt-5.6-sol` (cheaper: $4/$20 vs $5/$30) | `gpt-5.6-sol`; `gpt-5.1-codex-mini` → `gpt-5.6-terra`; `gpt-5-nano` → `gpt-5.6-luna` |
| anthropic | `claude-fable-5` (suggested, reasoning) | Active (legacy) since 2026-09-01 | `claude-fable-5-1` |
| anthropic | `claude-opus-4-8` labelled fallback, `-4-7` legacy | both are "Active (legacy)"; `claude-opus-4-6` is also still Active (alias to 4-8 is a catalog choice) | — |
| gemini | `gemini-3.7-flash` "newest GA" | superseded by 3.8 | `gemini-3.8-flash` |
| gemini | alias `gemini-2.0-flash-lite` → `gemini-3.1-flash-lite` | target shuts down 2027-05-07 | `gemini-3.5-flash-lite` |
| qwen (all regions) | `qwen3-coder-plus`, `qwen3-coder-next` | **shutdown 2026-10-10** | `qwen3.7-plus` (coding), `qwen3.8-max` (strongest) |
| qwen (all regions) | `qwen3.7-max` (fallback) | Legacy table; text-only; costs *more* than 3.8-max in Singapore (2.5/7.5 vs 2.0/6.0) | `qwen3.8-max` / `qwen3.7-plus` |
| qwen (all regions) | `qwen3.6-flash`, `qwen-flash` | Legacy (no date); `qwen-flash` lacks function calling outside Beijing, 32K out | `qwen3.8-flash`, `qwen3.7-flash` |
| zai-coding-plan | `glm-4.7`, `glm-5-turbo` | not plan models — server routes both to `glm-5.3-flash` | `glm-5.3-flash` |
| moonshot / -cn | all 13 alias sources | all 404 (kimi-k2.5 + moonshot-v1-* since 2026-08-31; k2 family 2026-05-25) | Moonshot's stated target is `kimi-k3` for all (catalog maps to k2.6 — works, differs) |
| minimax | alias `MiniMax-M2` → `MiniMax-M2.7` | `MiniMax-M2` is still live and separately priced; alias silently swaps a billed model | remove alias |
| xiaomi-mimo | `mimo-v2-flash` (suggested) | **shut down 2026-06-30** (routed to `mimo-v2.5` from 06-18) | `mimo-v2.5` |
| bytedance | all four suggested ids | now "往期" (previous-gen) table; not retiring | `doubao-seed-evolving`, 2.1 family |
| groq | `llama-3.3-70b-versatile`, `llama-3.1-8b-instant` (alias sources) | Enterprise/Contact-Sales only since 2026-08-16 | mapping correct for non-enterprise |
| cerebras | `zai-glm-4.7` (suggested, coding) | **deprecated 2026-08-17**, removed from public catalog; aliases `qwen-3-coder-480b`, `zai-glm-4.6` now point at a dead id | `gpt-oss-120b` (nothing else public) |
| mistral | `devstral-2512`, `magistral-*`, `mistral-medium-2508` | Deprecated 2026-05-22, still accessible (6-month GA policy → ~2026-11-22); "retired 2026-07-31" is not on any official page | mapping correct |
| mistral | alias source `mistral-medium-2604` | not on the Medium 3.5 page (only `mistral-medium-3-5`, `-latest`, `-3`) | unverified — consider dropping |
| nvidia | `nvidia/nemotron-3-nano-30b-a3b` (validation), `deepseek-ai/deepseek-v4-pro`, `deepseek-ai/deepseek-v4-flash` | free hosted endpoint marked Deprecated (no date) | `nemotron-3.5-lightning-30b-a3b`, `-0813`, `-0731` |
| perplexity | `sonar`, `sonar-pro` | **shutdown 2026-09-27** (also sonar-reasoning-pro, sonar-deep-research) | Agent API `/v1/agent` (Responses format) — needs a client Alysis Code does not ship on this preset |
| together | `moonshotai/Kimi-K2.7-Code` (coding) | **removed from serverless 2026-08-27**; the two Qwen-Coder aliases now target a dead id | `moonshotai/Kimi-K3` |
| together | `openai/gpt-oss-20b` (validation + fallback) | **removed 2026-09-14** | `Qwen/Qwen3.5-9B` |
| together | alias source `zai-org/GLM-5.1` | removed 2026-07-10 | mapping correct |
| fireworks | alias `kimi-k2p6` → `kimi-k2p7-code` | `kimi-k2p6` is still serverless ($0.95/$4) — alias unnecessary | remove alias |
| fireworks | `routers/kimi-k2p7-code-fast`, bare `deepseek-v4-pro`, `deepseek-v4-flash`, `gpt-oss-20b`, `minimax-m2p7` | retired Aug 2026 (none are catalog ids; listed so they are not added) | — |

---

## Registry metadata corrections (`model_registry.py`)

| layer / key | current | official | source |
|---|---|---|---|
| anthropic `claude-opus-5` `context_window_tokens` | 1,128,000 | docs say "1M" + 128K out; 1,128,000 appears on no official page (may be an intentional input+output sum — decide and comment) | platform.claude.com/docs/en/models/opus-5/overview |
| anthropic — add `claude-fable-5-1` | — | 1M / 128K / 10 / 50 / cache-read **0.25** / 5m 12.50 / 1h 20 | platform.claude.com/docs/en/models/fable-5-1/overview |
| gemini — add `gemini-3.8-flash` | — | same shape as 3.7 (1,048,576 / 65,536 / 0.75 / 3.75 / 0.075); intro until 2026-12-31 | ai.google.dev/gemini-api/docs/pricing |
| gemini comment "2.5 shut down 2026-10-16" | — | deprecations page: "No shutdown date announced" for all three 2.5 models | ai.google.dev/gemini-api/docs/deprecations |
| xai comment "retired slugs fully shut down 2026-08-15" | — | not on any official page; retirement guide says slugs "continue to resolve" | docs.x.ai/developers/migration/may-15-retirement |
| xai `grok-4.5` (if added) | — | cache read $0.30, not $0.50 | docs.x.ai/developers/pricing |
| xai region note | build-0.1 only | `grok-4.3` is us-east-1 **only**; 4.6/4.5/4.20 are us-east-1 + us-west-2 | docs.x.ai/developers/models |
| deepseek pricing comment | "half price outside two daily peak windows" | correct; peak = 01:00–04:00 & 06:00–10:00 UTC Mon–Fri since 2026-08-16; max out 393,216 | api-docs.deepseek.com/quick_start/pricing |
| zai_coding_plan comment on `glm-5.3` | "exclusive to Coding Plan; PAYG coming soon" | wrong — on Z.AI PAYG price list since 2026-08-18 ($1.40 / $4.40 / $0.26) | docs.z.ai/guides/overview/pricing |
| zai_coding_plan `glm-5-turbo`, `glm-4.7` | 200K entries | not served natively by the plan (routed to 5.3-flash) | docs.z.ai/devpack/overview |
| moonshot web-search comment | "only k2.6 accepts thinking-off, which $web_search needs" | docs now say `$web_search` works with `kimi-k3` and recommend it; pricing pages flag web search as "being updated, not recommended" | platform.kimi.ai/docs/guide/use-web-search.md |
| moonshot `kimi-k2.7-code*` `max_output_tokens` | 32,768 | not published | — |
| built_in `mimo-v2.5-pro` | $1 / $3 | **$0.435 / $0.87 / cache 0.0036**; 1M / 131,072 | mimo.mi.com/docs/en-US/price/pay-as-you-go |
| built_in `mimo-v2.5` | $0.40 / $2 | **$0.14 / $0.28 / 0.0028** | same |
| built_in `mimo-v2-flash`, `mimo` | present | model retired 2026-06-30 | mimo.mi.com/docs/en-US/updates/deprecate |
| openrouter `deepseek/deepseek-v4-pro-0813` | 1.188 / 3.564 | **0.66 / 1.98 / 0.022**; max out 384,000 | openrouter.ai/api/v1/models |
| openrouter `deepseek/deepseek-v4-flash-0731` | 0.08 / 0.18 / 0.016 | **0.065 / 0.18 / 0.016**; max out 943,718 | same |
| openrouter `deepseek/deepseek-v4-flash-vision-exp` | 0.44 / 1.32 / 0.014 | **0.22 / 0.66 / 0.007** | same |
| together `zai-org/GLM-5.2` (preset desc) | 256K | 1,000,000 | docs.together.ai/docs/serverless-models |
| fireworks `deepseek-v4-flash-0731` | 0.14 / 0.28 / 0.028 | **0.22 / 0.66 / 0.007** — every field wrong | docs.fireworks.ai/serverless/pricing.md |
| fireworks `kimi-k2p7-code` | no vision flag | image input Supported | fireworks.ai/models/fireworks/kimi-k2p7-code |
| nvidia `nemotron-3-super` `max_output_tokens` 32,768 | 32,768 | not stated; sample uses 16,384; docs say "up to 1M" | build.nvidia.com/nvidia/nemotron-3-super-120b-a12b |
| nvidia `deepseek-ai/deepseek-v4-flash` `max_output_tokens` 16,384 | 16,384 | only the code sample's `max_tokens` | docs.api.nvidia.com |
| OpenAI (LiteLLM snapshot) 5.6 prices | launch prices | Sol 4 / 20 / 0.40 (write 5.00); Terra 2 / 12 / 0.20 (write 2.50); Luna 0.20 / 1.20 / 0.02 (write 0.25). 5.4-mini/nano + 5.3-codex are 400K ctx / 272K max input, not 1M | developers.openai.com/api/docs/pricing.md |
| minimax `MiniMax-M3` (if added to registry) | — | 1M / 524,288 out; 0.30 / 1.20 / 0.06 (2x above 512K in); thinking `adaptive|disabled` | platform.minimax.io/docs/guides/pricing-paygo.md |
| minimax `MiniMax-M2.7` | 200K | 204,800; thinking cannot be disabled | same |
| groq `qwen/qwen3.8-27b` ctx | — | page literally says 131,042 (likely typo for 131,072) | console.groq.com/docs/model/qwen/qwen3.8-27b |
| cerebras `gemma-4-31b` price | — | official page self-contradicts ($2.15/$2.70 prose vs $0.99/$1.49 spec block); leave cost unknown | inference-docs.cerebras.ai/models/gemma-4-31b |

---

## Per-preset notes that affect more than the id list

**openai / openai-responses.** `gpt-5.3-codex` must leave the `openai` (Chat Completions) preset or
the preset ships a model that 400s. Consider `gpt-5.6-luna` for economy + validation (same $0.20
input as `gpt-5.4-nano`, 1.05M vs 400K ctx). Codex subscription snapshot: `gpt-5.4` and
`gpt-5.4-mini` were removed from Codex-with-ChatGPT 2026-08-31; the five ids in the bundled
snapshot are still correct. "Astra" was announced 2026-09-01 with no API id — nothing to add.

**anthropic.** Fable 5.1 forbids forced `tool_choice` (`any` / specific tool) — check
`anthropic_messages` client paths that force tools before making it a suggested model. Sonnet 5
$2/$10 is permanent (2026-08-10); drop any "intro pricing until Aug 31" wording. `temperature` /
`top_p` / `top_k` non-default values 400 on Claude 4.7+.

**gemini.** `thinking_budget` replaced by `thinking_level`; `temperature` / `top_p` / `top_k`
deprecated 2026-07-21; `candidate_count` unsupported on 3.x. No GA Pro model exists;
`gemini-3.1-pro-preview` is still served with no shutdown date.

**qwen.** The "coder models not served from US" warning is wrong for `qwen3-coder-plus` /
`qwen3-coder-flash` (both have a Virginia block) — but moot after 2026-10-10. US "Global" prices are
~17–30 % below Singapore. New regions: Hong Kong, Tokyo. `qwen3.8-max-preview` was retired
2026-08-05 and routes to `qwen3.8-max`; current snapshot is `qwen3.8-max-0902`.

**zhipu / zai-coding-plan.** `glm-5.3` and `glm-5.3-flash` reject `thinking.type=disabled`; the
client must send `enabled` + `reasoning_effort` (the plan proxy converts `disabled` → `low`; the raw
API rejects it). `glm-5-turbo` is documented as an OpenClaw/agent model, not a coding model. Coding
Plan moved to a credits quota 2026-07-30 (off-peak incl. weekends = 50 %).

**moonshot / kimi-code.** Sampling params are fixed and error if passed (`temperature=1.0`,
`top_p=0.95`, `n=1`, penalties 0). `tool_choice="required"` is k3-only. K3 needs a prior top-up
(≥$1). On the Kimi Code endpoint the default effort is `high`; `none` disables thinking and
silently routes to K2.6; unknown effort strings 400. Wrong highspeed id silently falls back to
`kimi-for-coding`.

**minimax.** Only M3 has a thinking toggle (`adaptive` / `disabled`); M2.x cannot disable — so
`validation_model: MiniMax-M2.5` never exercises thinking-off. Docs recommend the Anthropic-compat
base `https://api.minimax.io/anthropic` as primary.

**bytedance.** All Doubao ids support `thinking.type enabled|disabled` and 7 effort levels
(`xhigh` / `max` map down to `high`). 2.0 family: 256K window but 224K max **input**. `evolving` /
2.1 / 2.0-lite-260428 emit thinking summaries + `encrypted_content` instead of raw CoT — return it
in history. Prices are CNY.

**cerebras.** Public endpoints are now exactly `gpt-oss-120b` and `gemma-4-31b`; everything else
(GLM 5.x, Kimi, Qwen-Coder, MiniMax, DeepSeek) is dedicated-endpoint only with no public id. API v2
became default 2026-07-22 (stricter multi-turn tool-call validation). Paid tier: 131K ctx / 40K out.

**mistral.** No newer first-party generation exists (no medium-3-6, large-26xx, devstral 3,
codestral-26xx). `zai-glm-5-2` is the only 1M-context option; not eligible for `-latest` aliases,
not on Agents/Conversations (so not usable as `web_search_model`).

**perplexity.** The preset as written dies 2026-09-27. Options: (a) mark the preset deprecated and
surface the date in `setup_warning`; (b) add a Responses-format client for `/v1/agent` (the
`openai_responses` protocol already exists for OpenAI — check whether it can target
`https://api.perplexity.ai/v1` with `perplexity/sonar` and the `fast` / `low` presets). Router API
(Chat Completions) is private preview.

**together.** Two catalog ids are dead or dying (`Kimi-K2.7-Code` gone, `gpt-oss-20b` 09-14).
The alias table's two cross-vendor Qwen-Coder → Kimi remaps now target the removed id.

**fireworks.** `kimi-k2p6` is still serverless, so its alias is a silent model swap. The
"Qwen 3.8 Max" pricing link resolves to `qwen3p8-2p4t-a95b`; no `qwen3p8-max` id exists.

**nvidia.** NIM publishes no per-token prices and no dated retirement schedule for deprecated free
endpoints; the `/v1/models` merge at setup time already handles live availability, but the
offline-safe list and `validation_model` should move to non-deprecated ids.

---

## Sources

OpenAI: developers.openai.com/api/docs/{models,pricing.md,deprecations.md,changelog.md},
per-model pages for gpt-5.6-{sol,terra,luna,cyber}, gpt-5.5, gpt-5.4{,-mini,-nano}, gpt-5.3-codex;
learn.chatgpt.com/docs/models.md; openai.com/index/path-to-astra.
Anthropic: platform.claude.com/docs/en/{models/overview,about-claude/pricing,about-claude/model-deprecations,about-claude/models/model-ids-and-versions,build-with-claude/context-windows}, per-model overview pages for fable-5-1, mythos-5-1, fable-5, opus-5, opus-4-8, opus-4-7, sonnet-5, haiku-4-5; anthropic.com/claude-fable-and-mythos-5-1.
Google: ai.google.dev/gemini-api/docs/{models,pricing,changelog,deprecations,latest-model}, per-model pages 3.8-flash, 3.7-flash, 3.6-flash, 3.5-flash-lite, 3.1-pro-preview.
xAI: docs.x.ai/developers/{models,pricing,release-notes,migration/may-15-retirement}, per-model pages.
DeepSeek: api-docs.deepseek.com/{quick_start/pricing,updates,news/news260813}.
Alibaba: alibabacloud.com/help/en/model-studio/{models,text-generation-model,qwen3-8-max,qwen3-8-flash,qwen3-7-plus,qwen3-7-flash,qwen3-7-max,qwen3-6-flash,qwen-flash,qwen3-coder-plus,qwen3-coder-next,qwen3-coder-flash}; help.aliyun.com/en/model-studio/{newly-released-models,model-depreciation}; alibabacloud.com/en/notice/detail?id={1949,1950,2000,2009,2074}; aliyun.com/notice/{118177,118344,118345,118434}.
Zhipu / Z.AI: open.bigmodel.cn/pricing; docs.bigmodel.cn/cn/guide/{start/model-overview,models/text/glm-5.3,models/vlm/glm-5.3-flash,start/migrate-to-glm-new}; docs.z.ai/{guides/overview/pricing,guides/llm/glm-5.3,guides/vlm/glm-5.3-flash,devpack/overview,devpack/latest-model,release-notes/new-released,devpack/notice/usage-revision}.
Moonshot: platform.kimi.ai/docs/{models.md,pricing/chat-k3,pricing/chat-k27-code,pricing/chat-k26,api/models-overview.md,guide/use-thinking-models.md,guide/kimi-k3-quickstart.md,guide/use-web-search.md,platform-changelog.md}; platform.kimi.com/docs/pricing/chat; kimi.com/code/docs/en/{kimi-code/models,kimi-code/whats-new.html}.
MiniMax: platform.minimax.io/docs/{guides/models-intro,guides/pricing-paygo.md,guides/text-generation.md,api-reference/text-chat-openai.md,release-notes/models.md}.
Xiaomi: mimo.mi.com/docs/en-US/{quick-start/summary/model,price/pay-as-you-go,updates/deprecate,updates/model,api/model/list-models}; mimo.mi.com/models/en-US/mimo-v2.5-pro-ultraspeed.
ByteDance: docs.volcengine.com/docs/82379/{1330310,1544106,2549861,1449737} (rendered in browser).
Groq: console.groq.com/docs/{models,deprecations,changelog,model/qwen/qwen3.8-27b}.
Cerebras: inference-docs.cerebras.ai/{models/overview,models/openai-oss,models/gemma-4-31b,support/deprecation,support/change-log,dedicated/overview}.
Mistral: docs.mistral.ai/{models,inference/pricing,inference/model-lifecycle}, per-model pages incl. zai-glm-5-2.
Cohere: docs.cohere.com/docs/{models,deprecations,command-a-plus,command-a-reasoning,command-a-vision,reasoning,north-mini-code-1.0,how-does-cohere-pricing-work}.
Perplexity: docs.perplexity.ai/{llms.txt,docs/sonar/models,docs/sonar/openai-compatibility,docs/agent-api/models,docs/agent-api/openai-compatibility,docs/agent-api/migrate-from-sonar/overview,docs/router/models,docs/getting-started/pricing}.
OpenRouter: openrouter.ai/api/v1/models (423 entries, queried in-browser).
Together: docs.together.ai/docs/{serverless-models,deprecations}.
Fireworks: docs.fireworks.ai/{serverless/pricing.md,guides/recommended-models.md,updates/changelog.md,serverless/overview.md}; fireworks.ai/models/fireworks/* pages.
NVIDIA: build.nvidia.com model + publisher pages; docs.api.nvidia.com/nim/reference/{llm-apis,per-model}.
