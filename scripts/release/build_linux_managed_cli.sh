#!/usr/bin/env bash
set -euo pipefail
uv_executable="$(command -v uv)"
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --env HOME=/tmp \
  --env UV_CACHE_DIR=/tmp/uv-cache \
  --env UV_PROJECT_ENVIRONMENT=/tmp/alysis-venv \
  --env UV_PYTHON=3.11.15 \
  --env UV_PYTHON_INSTALL_DIR=/tmp/alysis-python \
  --env UV_MANAGED_PYTHON=true \
  --env OUTPUT_NAME="$OUTPUT_NAME" \
  --volume "$PWD:/workspace" \
  --volume "$uv_executable:/usr/local/bin/uv:ro" \
  --workdir /workspace \
  "$MANYLINUX_IMAGE" \
  bash -euxo pipefail -c '
    # The image Python is static-only. Pinned uv verifies its bundled
    # checksum for the exact shared-library-capable Astral distribution.
    uv python install 3.11.15
    uv sync --frozen --no-editable --extra managed-build
    uv run --frozen --no-sync python -m PyInstaller \
      --noconfirm \
      --clean \
      --onefile \
      --name alysis \
      --collect-all alysis_code \
      scripts/release/managed_cli_entry.py
    mkdir -p managed-cli-out
    mv dist/alysis "managed-cli-out/${OUTPUT_NAME}"
  '
