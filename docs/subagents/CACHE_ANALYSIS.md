# Prompt-cache prefix analysis

Date: 2026-08-20

This note records the evidence gathered before changing request construction. The
source runs used GPT 5.6 Luna through the OpenAI Responses protocol:

- `relaydesk`: session `20260820T082949Z_a3bce180`
- `relaydesk-existing-fresh`: session `20260820T095837Z_12c1ae42`

The provider telemetry is under each session's
`diagnostics/provider_telemetry.jsonl`. The matching session JSONL supplied the
usage role for each provider call. Counts below use only the two root session
logs; child usage replayed into a root log is counted once.

## Summary

| Run | Calls | Prompt tokens | Cached | Uncached | Hit ratio |
| --- | ---: | ---: | ---: | ---: | ---: |
| relaydesk | 151 | 8,520,681 | 7,257,856 | 1,262,825 | 85.18% |
| relaydesk-existing-fresh | 124 | 9,501,823 | 8,375,168 | 1,126,655 | 88.14% |
| combined | 275 | 18,022,504 | 15,633,024 | 2,389,480 | 86.74% |

| Cause | Attributed uncached tokens | Evidence | Convicted? |
| --- | ---: | --- | --- |
| Cold first requests | 45,289 | First request for each usage role | No; expected |
| New transcript frontier | 610,864 | Positive prompt growth since the prior request for that role, capped by the call's uncached count | No; expected |
| Mutable/turn-local context before stable history | 79,922 | Second interactive turn changed the main stable-prefix hash; 81,507 uncached minus 1,585 new frontier tokens | Yes |
| New child with a different task | 3,925 | A second explorer session changed the role stream's prefix hash but had no prior shared child transcript | No; expected cold child |
| Same-prefix provider miss/expiry | 1,649,480 | Stable-prefix hash and tool-schema hash unchanged; controlled payloads remained append-only | No code cause established |
| Tool-output offload or pruning rewrite | 0 | Offload happens before the tool message is first appended; no applied compaction or empty-response recovery occurred | No |
| Nondeterministic serialization | 0 | Repeated mock requests were byte-identical; tool order and schemas were stable | No |

The attribution method is deliberately conservative. For each usage-role stream,
the first call is cold. On later calls, positive prompt growth is the new
frontier. Remaining uncached tokens are assigned to a changed stable-prefix hash
or a same-hash provider miss. This does not claim that every same-hash miss is a
provider defect: the telemetry records only a bounded stable-prefix hash rather
than every request byte. Controlled full-payload captures are the additional
check for application-side mutation.

The single convicted rewrite is material but cannot explain the full gap to a
98% comparison run. Recovering all 79,922 tokens would move the measured combined
ratio from 86.74% to about 87.18%. Most residual misses occurred after long child
runs or at cache re-warm points and retained the same application hashes. A
real-provider rerun is required to distinguish cache routing/retention behavior
from any unobserved provider normalization.

## 1. Retroactive mutation

Normal tool-output offloading is forward-only. In
`agent/turn/core.py`, `ToolOutputOffloader.maybe_offload` runs before the new tool
message is appended. The shaped stub is therefore the only version ever sent in
persistent history. The two evaluation runs recorded offload events but no
corresponding prefix-hash transition.

`compact_recent_tool_output` can replace earlier tool messages, but only on the
explicit empty-response recovery path. Neither evaluation contained that stall
or an applied conversation compaction. It caused zero attributable tokens in
these runs and already occurs at a recovery boundary where replay is expected.

Three host contexts do replace messages that may already have been sent:

- `refresh_session_environment_context_message` replaces
  `<environment_context>` after verification selection changes.
- `refresh_session_workspace_binding_context_message` replaces
  `<workspace_binding_context>` after focus/workspace changes.
- `refresh_session_task_brief_message` and
  `refresh_session_task_brief_from_observed_turn` replace `<task_brief>` at turn
  boundaries.

The relaydesk session's last first-turn main call used prefix hash `38f58f49...`
with 81,280 cached of 83,378 prompt tokens. The first call of turn two used
`b91df05d...`, cached only 3,456 of 84,963, and incurred 81,507 uncached. Only
1,585 tokens were new prompt growth, leaving 79,922 attributable to the prefix
transition. Moving these mutable contexts to a request-only volatile suffix
preserves their current content without rewriting the reusable leading history.

## 2. Volatile-before-stable inventory

The controlled request capture found the following model-facing classes:

| Message class | Persistent position before fix | Varies | Finding |
| --- | --- | --- | --- |
| workspace binding | Startup prefix | On focus/workspace changes | Mutable before stable history |
| task brief | Startup prefix | At observed turn boundaries | Mutable before stable history |
| environment context | Startup prefix | On verification/workdir changes | Mutable before stable history |
| reproduction-first directive | Inserted at turn boundary before the user message | Present per eligible turn | Disappears/reorders at the next turn |
| blast-radius directive | Inserted at turn boundary before the user message | Policy/eligibility dependent | Disappears/reorders at the next turn |
| hook/caller turn system messages | Turn boundary | Per turn | Disappears/reorders at the next turn |
| subagent turn context and caller/hook user context | Immediately after the current user message | Per turn and background state | Disappears/reorders at the next turn |
| step-budget and phase prompts | Request suffix | Per step | Already suffix-local |
| deadline prompts | Request suffix | Per step and remaining time | Already suffix-local |
| parent inbox message | Request suffix | Per delivery | Already suffix-local |
| controller nudge | Durable append | Per intervention | Safe append; not retroactive |
| TUI HUD/status | Surface only | Per refresh | Not model-facing |

In a two-turn mock session, the final request of turn one and first request of
turn two shared only the five startup items. At the first differing item, turn
one had the reproduction-first system directive while turn two had the prior
user task. Within an ordinary multi-step turn with no dynamic prompt, the first
request was an exact item prefix of the second. With step-pressure enabled, the
common prefix ended immediately before the old suffix, as intended.

The fix should therefore establish one request-construction rule: stable
persistent history first; mutable host contexts and all ephemeral turn/step
messages afterward. It must reorder only a request-local copy, leaving persistent
history and compaction semantics unchanged.

## 3. Child prefix composition

Controlled first requests for explorer, code-reviewer, verifier, and implementer
all begin with the same base Alysis Code system-prompt bytes. The pairwise common
prefix is 10,365 bytes. Role guidance is appended after that base within the same
system message; it is not interleaved before the base. First system-message sizes
were 13,364, 13,345, 13,952, and 13,166 bytes respectively.

Live first child calls were generally cold, but launches were separated in time
and used role-specific tool schemas and tasks. One implementer first call reused
3,456 tokens, proving that the existing leading byte prefix can be reused. There
is no measured request-prefix defect to justify splitting or reordering the child
system prompt. This candidate fix is not implemented.

## 4. Serialization stability

Two identical `OpenAIResponsesClient.chat` calls captured through an HTTP mock
produced identical 394-byte request bodies. Dict insertion order is preserved,
and repeated tool and property ordering was stable. In live telemetry the tool
schema hash stayed constant for every continuing role. Sorting would change
request bytes without evidence of instability, so no serialization change is
justified.

## Controlled proof required after the fix

A permanent mock-provider regression must run multiple steps across two turns,
capture consecutive serialized requests, and verify:

1. stable transcript items form a byte-identical leading prefix up to the append
   frontier;
2. mutable host and ephemeral turn/step messages occur only after that stable
   prefix; and
3. request reordering neither drops nor rewrites any model-facing content.

After merge, the real-model evaluation should record parent-only cached and
uncached input, hit ratio, largest uncached call and call position, stable-prefix
hash transitions, tool-schema hash transitions, and gaps between calls. The
target is at least 95% parent-session cache hit ratio. If the same-prefix misses
remain dominant, the next investigation should test provider cache affinity and
retention explicitly rather than attributing them to transcript mutation.

## Cache-shard affinity follow-up

The follow-up transport audit found no installed OpenAI SDK: Alysis Code builds raw
HTTP payloads. Both its Responses and OpenAI-compatible Chat Completions clients
already accept and serialize `prompt_cache_key`; the public OpenAI Responses API
also documents that request field. Capability policy still removes it for a
provider or protocol that does not declare support, and the compatibility client
retains its unsupported-parameter retry fallback.

Session traffic now supplies an affinity key by default. A parent uses its stable
session id. Every child of that parent, including children with different roles,
uses the same separately hashed `<parent session id>:children` key. This keeps
child traffic off the parent's cache shard while allowing sequential children to
reuse their common base and role prefixes. `cache.prompt_cache_key_enabled=false`
is the kill switch. Provider telemetry records only the SHA-256 hash of an
actually emitted key under `cache_policy.prompt_cache_key_hash`, never the key.

The current protocol layer also exposes `prompt_cache_retention`; the OpenAI
Responses API exposes retention controls, including the newer
`prompt_cache_options.ttl` form. Alysis Code does not enable extended retention by
default, and this change does not add or select a retention value because that
can change cache cost. The newer options object remains a follow-up compatibility
knob rather than part of this affinity change.

For the real-provider rerun, group calls by the recorded key hash and compare:

1. parent hit ratio and largest uncached call against the 85.18%/88.14% runs;
2. first-child cold cost versus later children sharing the child-stream hash;
3. same-prefix misses after long child runs; and
4. whether each parent has one hash, all of its children share another, and the
   two hashes are distinct.

The acceptance target remains at least 95% parent-session cache hit ratio. A
remaining same-hash miss bucket after this change points to retention/expiry or
provider-side normalization, not application routing ambiguity.

## Read-overlap and parent-idle controls

Three live evaluations showed a second, independent source of context cost:
sessions repeatedly requested unchanged line ranges after those ranges had already
been returned. One reviewer reconstructed most of a repository through overlapping
`fs_read_lines` calls, and one parent made about 25 overlapping reads between a
review handoff and its first edit. A per-session read ledger now records returned
line intervals with the file's SHA-256 content hash. An exact repeat returns a short
notice; a partial overlap returns only the unread intervals. A changed file or a
session write invalidates that path. `force=true` is the loss-safe bypass, and
`read_ledger_enabled=false` restores the previous behavior. Parent and child ledgers
are deliberately independent because each model must be able to acquire its own
evidence.

The largest measured cache loss followed a roughly ten-minute synchronous child:
the idle parent later paid about 3.4 million uncached input tokens to restore a
prefix it had already sent. `cache.keepalive_enabled` therefore adds an optional
affinity experiment. It is **off by default** pending real-provider cost evidence.
When enabled, a parent blocked on synchronous child work for
`cache.keepalive_idle_threshold_s` (default 240 seconds) sends this bounded request
at most once per threshold interval:

```text
messages=<deep copy of the parent's last request messages>
tools=<deep copy of the parent's last request tools>
tool_choice=<the same value, when present>
stream=false
temperature=0
max_tokens=16
```

The response is discarded and never enters the transcript or advances a turn
step. Its usage is recorded under the `<parent role>:cache_keepalive` role, the
session log receives `cache_keepalive`, and provider telemetry labels the call's
operation `cache_keepalive`. No ping starts in the finalization window, after
cancellation, or when the remaining deadline is shorter than the threshold. Child
completion cancels an in-flight ping through the ordinary cancellation token.

OpenAI Responses requires at least 16 output tokens, so the transport clamps any
smaller `max_output_tokens` value to 16 and logs the adjustment. Keepalive failures
are telemetry-visible as `status=failed` with their error class; two consecutive
failures disable further pings for that session and emit one warning without
delaying the child wait.

The expected-cost comparison is intentionally empirical rather than a fixed token
claim. For each enabled evaluation, sum the cached and uncached input plus output
cost of `operation=cache_keepalive`, then compare that amount with cold parent calls
that follow child-idle gaps of at least 240 seconds under the same prompt-cache-key
hash. The mechanism is beneficial only when avoided cold-read cost exceeds the
ping cost. The immediate rerun should report:

1. parent hit ratio and largest uncached parent call;
2. total keepalive calls, cached/uncached tokens, and dollar cost;
3. child duration and parent idle gap preceding every ping;
4. the first parent call after each child, including whether it remained warm; and
5. read-ledger skipped/partial results and the full-content tokens they replaced.

Keep the switch disabled if the provider does not retain the prefix, if pings are
mostly uncached, or if their aggregate cost is not lower than the cold rereads.

### Continuation-transport result

The live subscription-backed Responses rerun answered the keepalive experiment
negatively. Four pings consumed 227,235 prompt tokens: only 13,824 were cached
(the same 3,456-token prefix each time), while 213,411 were uncached. The gateway
also returned 595, 4,483, 208, and 379 output tokens despite the requested
16-token ceiling. In aggregate, the pings spent about 213k uncached input tokens
to protect a parent wake-up whose cold portion was about 49k tokens.

The reason is transport mismatch, not request-byte instability. The parent's real
Responses calls use server-side response continuation, while a keepalive replays
the visible message list as a stateless request. Those are different cache
streams, so replay cannot refresh the continuation entry. `ParentCacheKeepalive`
now refuses to arm for response-continuation clients and subscription/gateway
adapters, emitting one `keepalive_unsupported_transport` warning with the reason.
Keepalive remains available only for stateless full-request transports, where the
ping and the following parent call can address the same serialized prefix.
