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

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

BUILD_INFO="src/alysis_code/_build_info.py"
REQUIRE_CLEAN=1
for arg in "$@"; do
  case "$arg" in
    --allow-dirty) REQUIRE_CLEAN=0 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

# Always put the dev-default stamp back, even on failure or interrupt: the
# repository must never carry a stamp belonging to an earlier build.
restore_build_info() {
  git checkout -- "$BUILD_INFO" 2>/dev/null || true
}
trap restore_build_info EXIT INT TERM

echo "==> stamping build identity"
if [ "$REQUIRE_CLEAN" = "1" ]; then
  python3 scripts/generate_build_info.py --require-clean
else
  python3 scripts/generate_build_info.py
fi

echo "==> building wheel"
rm -rf dist/*.whl 2>/dev/null || true
if command -v uv >/dev/null 2>&1; then
  uv build --wheel
else
  python3 -m build --wheel
fi

WHEEL="$(ls -t dist/*.whl 2>/dev/null | head -1)"
if [ -z "${WHEEL:-}" ]; then
  echo "ERROR: no wheel produced in dist/" >&2
  exit 1
fi

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
echo "wheel: $REPO_ROOT/$WHEEL"
echo "export ALYSIS_WHEEL=\"$REPO_ROOT/$WHEEL\""
