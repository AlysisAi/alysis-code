# Sandbox Image

This directory contains the Dockerfile used to build Alysis Code sandbox images
for shell execution, verification, and server workers when Docker is selected.

## Contents

- `Dockerfile` builds the supported `base`, `dev`, and `server` variants through
  the `VARIANT` build argument.

## Scope

The image is one execution backend, not the whole security model. Execution
modes, workspace binding, safe HTTP checks, MCP policy, hook trust, and tool
validation still apply outside the container.

Production deployments should pin images by digest and verify signatures and
attestations as described in the sandbox guide.

## Supply-chain contract

- Debian repositories use a dated snapshot, and the Python base and uv helper
  images are digest-pinned.
- Node, Go, and rustup downloads are version-pinned and SHA-256 verified for
  both `linux/amd64` and `linux/arm64`; the Rust toolchain is an exact version.
- The server variant installs from `uv.lock` with network downloads of a
  replacement Python interpreter disabled.
- Release candidates are scanned and runtime-smoked on both architectures
  before any moving or release alias is promoted.

Changing a pinned version requires changing its matching digest in the same
review and passing the sandbox release-contract tests.

## Development

Image changes should be checked with `alysis sandbox doctor --smoke` against
a locally built image when Docker is available.

## See Also

- [Shell sandbox](../docs/shell_sandbox.md)
- [Server mode](../docs/server.md)
