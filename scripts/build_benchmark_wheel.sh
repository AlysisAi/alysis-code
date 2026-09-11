#!/usr/bin/env bash
# Build a provenance-stamped wheel for a benchmark campaign.
#
# Why this script exists: benchmark runs set ALYSIS_REQUIRE_CLEAN_BUILD=1
# (box_harbor_agent.py defaults it on), and an unstamped wheel is refused during
# container setup — correctly, but the failure lands after the campaign has been
# launched. Building through this script makes the stamp automatic, so the gate
# only ever fires on a genuinely unidentifiable build.
#
# Usage:
#   bash scripts/build_benchmark_wheel.sh            # refuse a dirty tree
#   bash scripts/build_benchmark_wheel.sh --allow-dirty
#
# On success it prints the wheel path; export it as ALYSIS_WHEEL for Harbor.
# Each build uses a unique directory beneath dist/, preserving older wheels.
# The original local build metadata is restored on exit, including failures.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BUILD_INFO="src/alysis_code/_build_info.py"
REQUIRE_CLEAN=1
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) REQUIRE_CLEAN=0 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

if [[ ! -f "$BUILD_INFO" || -L "$BUILD_INFO" ]]; then
  echo "ERROR: build metadata must be an existing regular file: $BUILD_INFO" >&2
  exit 1
fi

# Preserve local edits as well as the committed dev-default stamp.
BUILD_INFO_BACKUP="$(mktemp "${TMPDIR:-/tmp}/alysis-build-info.XXXXXX")"
cp -p "$BUILD_INFO" "$BUILD_INFO_BACKUP"
restore_build_info() {
  local build_status=$?
  if ! cp -p "$BUILD_INFO_BACKUP" "$BUILD_INFO"; then
    echo "ERROR: could not restore build metadata; backup retained at $BUILD_INFO_BACKUP" >&2
    exit 1
  fi
  rm -f "$BUILD_INFO_BACKUP"
  return "$build_status"
}
trap restore_build_info EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "==> stamping build identity"
if [ "$REQUIRE_CLEAN" = "1" ]; then
  python3 scripts/generate_build_info.py --require-clean
else
  python3 scripts/generate_build_info.py
fi

echo "==> building wheel"
mkdir -p dist
WHEEL_DIR="$(mktemp -d "$REPO_ROOT/dist/benchmark.XXXXXX")"
if command -v uv >/dev/null 2>&1; then
  uv build --wheel --out-dir "$WHEEL_DIR"
else
  python3 -m build --wheel --outdir "$WHEEL_DIR"
fi

shopt -s nullglob
wheels=("$WHEEL_DIR"/*.whl)
if [ "${#wheels[@]}" -ne 1 ]; then
  echo "ERROR: expected one wheel in $WHEEL_DIR, found ${#wheels[@]}" >&2
  exit 1
fi
WHEEL="${wheels[0]}"

echo "==> verifying the stamp survived into the wheel"
python3 - "$WHEEL" <<'PY'
import sys, zipfile, re
wheel = sys.argv[1]
with zipfile.ZipFile(wheel) as zf:
    name = next(n for n in zf.namelist() if n.endswith("alysis_code/_build_info.py"))
    text = zf.read(name).decode("utf-8")
commit = re.search(r'BUILD_COMMIT\s*=\s*"([^"]*)"', text)
dirty = re.search(r'BUILD_DIRTY\s*=\s*(True|False)', text)
built = re.search(r'BUILD_TIMESTAMP\s*=\s*"([^"]*)"', text)
source = re.search(r'BUILD_SOURCE\s*=\s*"([^"]*)"', text)
commit_v = commit.group(1) if commit else ""
source_v = source.group(1) if source else ""
if not commit_v or commit_v == "unknown" or source_v == "dev-default":
    sys.exit(
        "ERROR: wheel carries no commit stamp - ALYSIS_REQUIRE_CLEAN_BUILD "
        f"would refuse it during container setup: {wheel}"
    )
print(f"    commit={commit_v[:12]} built={built.group(1) if built else '?'} dirty={dirty.group(1) if dirty else '?'}")
PY

echo
echo "wheel: $WHEEL"
printf 'export ALYSIS_WHEEL=%q\n' "$WHEEL"
