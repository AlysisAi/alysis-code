# Prompt caching with Alysis hosted models

Alysis hosted connections use **Auto** prompt caching by default. Sonnet sends
five-minute cache controls. DeepSeek, GLM and Luna use provider-managed caching;
the client does not send unsupported cache settings to those routes.

The first eligible Sonnet request writes a cache entry. Later requests can read
matching prefixes before expiry. A changed system prompt, tool catalog or other
cache-sensitive request setting can require a new write. Auto enables the cache
mechanism; it does not promise a hit on every turn. Check the reported cache-read
and cache-write token counts to see what the provider actually reused.

For Sonnet, five-minute writes cost 1.25 times ordinary input and reads cost
0.1 times ordinary input. Repeated coding turns can therefore spend fewer
credits. Output tokens keep their normal price. See
[Anthropic's prompt-caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

## Existing configurations

The updated client migrates an Alysis hosted configuration from the old Manual
default to Auto when its cache settings otherwise match the old defaults. Old
versions saved Manual even when the user had never selected it, so these cases
cannot be distinguished retrospectively.

Explicit Off, custom cache keys/retention, enabled manual Anthropic caching,
non-default TTLs, profile capability overrides, disabled cache-affinity keys and
enabled keepalive are preserved. A migration marker records that the decision
has been made. Subsequent Manual or Off choices are retained across upgrades.
Other provider connections retain their existing defaults.

Migration happens when loading settings or selecting the hosted connection;
ordinary configuration saves persist the marker. Install the updated client and
restart an existing TUI to use the new behavior.

## Controls and limits

Use `/config` → **Context & Cache** to select Auto, Manual or Off. From a terminal:

```sh
alysis config set prompt_cache_mode auto
```

Restart a running TUI after changing its config externally. The
`ALYSIS_PROMPT_CACHE_MODE` environment variable takes precedence. Off suppresses
optional client cache controls; provider-managed implicit caching can still
occur.

Hosted Sonnet uses five minutes even if a saved one-hour Anthropic preference
exists; that saved preference remains available to other providers. The hosted
gateway does not support one-hour caching.

Caching changes token charges, not request admission. Cached requests still count
toward the shared **20 requests per minute per user** and **four concurrent
requests per user**. Existing credit allowances and rolling caps still apply.
