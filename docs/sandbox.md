# Sandbox

Canonical sandbox documentation lives in [SANDBOX.md](SANDBOX.md). This lowercase entry remains
for historical links and tests that reference `docs/sandbox.md`.

Alysis Code shell and verification runs default to strict sandboxing too. Verification sandbox mode is
(default `strict`) and does not fall back to host shell when the selected sandbox runtime cannot
enforce the requested network policy.

Default shell/verification constraints include:

- `network=off`
- strict mode with `bwrap` on supported Linux hosts
- warn mode with `docker` when explicitly configured

Server workers use `ALYSIS_SERVER_WORKER_SANDBOX_MODE`; supported operator choices include
`strict` and `warn`, with `bwrap` or `docker` selected by the resolved sandbox backend. Deployment
policy decides the effective server worker mode.
