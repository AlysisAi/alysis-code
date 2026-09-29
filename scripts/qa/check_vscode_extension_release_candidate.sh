#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
EXT_DIR="${ROOT_DIR}/extensions/vscode-alysis"

if [[ -n "${PYTHON:-}" ]]; then
  PYTHON_BIN="${PYTHON}"
elif [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
  PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
else
  PYTHON_BIN="python"
fi
DOGFOOD_MODE="${ALYSIS_RELEASE_DOGFOOD:-mock}"
STRICT_MODE="${ALYSIS_RELEASE_STRICT:-0}"
NPM_BIN="$(command -v npm || true)"
USE_WINDOWS_NPM=0
EXT_DIR_WIN=""
if [[ "${NPM_BIN}" == /mnt/* ]]; then
  USE_WINDOWS_NPM=1
  EXT_DIR_WIN="$(wslpath -w "${EXT_DIR}")"
fi

section() {
  printf '\n==> %s\n' "$1"
}

run_root() {
  section "$1"
  shift
  (cd "${ROOT_DIR}" && "$@")
}

run_ext() {
  section "$1"
  shift
  (cd "${EXT_DIR}" && "$@")
}

run_ext_npm() {
  section "$1"
  shift
  # Executes the release gate npm commands: npm ci, npm test, npm run lint,
  # npm audit --audit-level=high, and npm run package:pre-release.
  if [[ "${USE_WINDOWS_NPM}" == "1" ]]; then
    local encoded
    encoded="$(
      EXT_DIR_WIN="${EXT_DIR_WIN}" NPM_ARGS_JSON="$(
        "${PYTHON_BIN}" -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@"
      )" "${PYTHON_BIN}" - <<'PY'
import base64
import json
import os
import subprocess

path = os.environ["EXT_DIR_WIN"]
args = json.loads(os.environ["NPM_ARGS_JSON"])
cmd_line = (
    f"pushd {subprocess.list2cmdline([path])} && "
    f"{subprocess.list2cmdline(['npm', *[str(arg) for arg in args]])} && "
    "popd"
)
cmd_line = cmd_line.replace("'", "''")
script = (
    "$ErrorActionPreference = 'Stop'\n"
    f"& cmd.exe /d /s /c '{cmd_line}'\n"
    "exit $LASTEXITCODE\n"
)
print(base64.b64encode(script.encode("utf-16le")).decode("ascii"))
PY
    )"
    (cd /mnt/c && powershell.exe -NoProfile -ExecutionPolicy Bypass -EncodedCommand "${encoded}")
    return
  fi
  (cd "${EXT_DIR}" && npm "$@")
}

package_script() {
  EXT_PACKAGE_JSON="${EXT_DIR}/package.json" "${PYTHON_BIN}" - <<'PY'
import json
import os
from pathlib import Path

scripts = json.loads(Path(os.environ["EXT_PACKAGE_JSON"]).read_text(encoding="utf-8")).get(
    "scripts", {}
)
for name in ("package:dev-vsix",):
    if scripts.get(name):
        print(name)
        raise SystemExit(0)
PY
}

write_dogfood_summary_artifact() {
  section "Write dogfood summary artifact"
  ALYSIS_REPO_ROOT="${ROOT_DIR}" "${PYTHON_BIN}" - <<'PY'
import json
import os
from pathlib import Path

from scripts.qa import vscode_extension_dogfood as dogfood

root = Path(os.environ["ALYSIS_REPO_ROOT"])
report_root = root / "qa_reports" / "vscode_dogfood"
summaries = sorted(
    report_root.glob("*/summary.json"),
    key=lambda path: path.stat().st_mtime,
    reverse=True,
)
if not summaries:
    raise SystemExit("No dogfood summary.json was produced.")
latest = summaries[0]
payload = json.loads(latest.read_text(encoding="utf-8"))
payload["source_summary"] = str(latest)
dogfood.validate_component_valid_summary(payload)
target = report_root / "component_candidate_summary.json"
target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"Wrote {target}")
print(f"Dogfood status: {payload.get('status')}")
print(f"Component valid: {payload.get('component_valid')}")
print("Release valid: false (a generic VSIX has no signed managed runtime)")
PY
}

verify_prerelease_vsix_marker() {
  section "Verify pre-release VSIX marker and contents"
  ALYSIS_EXT_DIR="${EXT_DIR}" "${PYTHON_BIN}" - <<'PY'
import json
import os
from pathlib import Path
from zipfile import ZipFile

vsix_dir = Path(os.environ["ALYSIS_EXT_DIR"])
candidates = sorted(
    vsix_dir.glob("*.vsix"),
    key=lambda path: (path.stat().st_mtime, path.name),
    reverse=True,
)
if not candidates:
    raise SystemExit("No VSIX package found; strict release mode requires package:pre-release output.")
vsix = candidates[0]
with ZipFile(vsix) as archive:
    names = set(archive.namelist())
    manifest = archive.read("extension.vsixmanifest").decode("utf-8")
    package = json.loads(archive.read("extension/package.json"))
marker = 'Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="true"'
if marker not in manifest:
    raise SystemExit(f"{vsix} is missing the Marketplace pre-release marker.")
required = {
    "extension/package.json",
    "extension/dist/extension.js",
    "extension/media/startView.css",
    "extension/media/startView.js",
    "extension/resources/icon.png",
    "extension/resources/alysis-logo.svg",
}
missing = sorted(required - names)
if missing:
    raise SystemExit(f"{vsix} is missing required runtime files: {missing}")
main = "extension/" + package["main"].removeprefix("./")
if main not in names:
    raise SystemExit(f"{vsix} manifest main is not packaged: {main}")
forbidden_prefixes = (
    "extension/node_modules/",
    "extension/src/",
    "extension/test/",
    "extension/.vscode-test/",
    "extension/design/",
)
forbidden = sorted(
    name for name in names
    if name.startswith(forbidden_prefixes)
    or name.endswith(".map")
    or name in {
        "extension/package-lock.json",
        "extension/tsconfig.json",
        "extension/tsconfig.webview.json",
    }
)
if forbidden:
    raise SystemExit(f"{vsix} contains development-only files: {forbidden}")
if "extension/resources/managed-cli/manifest.json" in names:
    raise SystemExit("Generic component gate must not package a production-trusted managed runtime.")
print(f"Verified pre-release marker and {len(names)} packaged entries in {vsix}")
PY
}

section "VS Code extension component-candidate verification"
printf 'Repository: %s\n' "${ROOT_DIR}"
printf 'Python: %s\n' "${PYTHON_BIN}"
printf 'Network use: npm ci and npm audit may contact the registry; no LLM/provider credentials are required.\n'
printf 'Strict release mode: %s\n' "${STRICT_MODE}"

if [[ "${STRICT_MODE}" == "1" ]]; then
  case "${DOGFOOD_MODE}" in
    extension-host|run|require-cached)
      ;;
    *)
      printf 'ALYSIS_RELEASE_STRICT=1 requires ALYSIS_RELEASE_DOGFOOD=run, extension-host, or require-cached; got %s.\n' "${DOGFOOD_MODE}" >&2
      printf 'Mock or skipped Extension Host dogfood cannot satisfy strict component validation.\n' >&2
      exit 1
      ;;
  esac
  if [[ "${ALYSIS_RELEASE_SKIP_PACKAGE:-0}" == "1" ]]; then
    printf 'ALYSIS_RELEASE_STRICT=1 requires package generation; unset ALYSIS_RELEASE_SKIP_PACKAGE.\n' >&2
    exit 1
  fi
  if [[ "${ALYSIS_RELEASE_SKIP_NPM_CI:-0}" == "1" ]]; then
    printf 'ALYSIS_RELEASE_STRICT=1 requires npm ci; unset ALYSIS_RELEASE_SKIP_NPM_CI.\n' >&2
    exit 1
  fi
  if [[ "${ALYSIS_RELEASE_SKIP_NPM_AUDIT:-0}" == "1" ]]; then
    printf 'ALYSIS_RELEASE_STRICT=1 requires npm audit; unset ALYSIS_RELEASE_SKIP_NPM_AUDIT.\n' >&2
    exit 1
  fi
fi

run_root "Validate IDE/CLI parity matrix" "${PYTHON_BIN}" scripts/qa/check_ide_cli_parity.py
run_root "Run targeted Python IDE parity/protocol/bridge tests" \
  "${PYTHON_BIN}" -m pytest -q \
  tests/test_ide_cli_parity_matrix.py \
  tests/test_ide_protocol.py \
  tests/test_ide_stdio_bridge.py \
  tests/test_ide_protocol_contract.py \
  tests/test_vscode_extension_dogfood.py

if [[ "${ALYSIS_RELEASE_SKIP_NPM_CI:-0}" == "1" ]]; then
  section "Install VS Code extension dependencies"
  printf 'Skipped because ALYSIS_RELEASE_SKIP_NPM_CI=1.\n'
else
  run_ext_npm "Install VS Code extension dependencies" ci
fi

run_ext_npm "Run VS Code extension unit tests" test
run_ext_npm "Lint VS Code extension" run lint

if [[ "${ALYSIS_RELEASE_SKIP_NPM_AUDIT:-0}" == "1" ]]; then
  section "Audit VS Code extension dependencies"
  printf 'Skipped because ALYSIS_RELEASE_SKIP_NPM_AUDIT=1.\n'
else
  run_ext_npm "Audit VS Code extension dependencies" audit --audit-level=high
fi

if [[ "${ALYSIS_RELEASE_SKIP_PACKAGE:-0}" == "1" ]]; then
  section "Package VS Code extension"
  printf 'Skipped because ALYSIS_RELEASE_SKIP_PACKAGE=1.\n'
else
  PACKAGE_SCRIPT="$(package_script)"
  if [[ -n "${PACKAGE_SCRIPT}" ]]; then
    run_ext_npm "Package VS Code extension (${PACKAGE_SCRIPT})" run "${PACKAGE_SCRIPT}" -- --pre-release
  else
    section "Package VS Code extension"
    printf 'No package script found; skipping package generation.\n'
  fi
fi

if [[ "${STRICT_MODE}" == "1" ]]; then
  verify_prerelease_vsix_marker
fi

case "${DOGFOOD_MODE}" in
  skip)
    section "Run VS Code extension dogfood smoke"
    printf 'Skipped because ALYSIS_RELEASE_DOGFOOD=skip.\n'
    ;;
  mock)
    run_root "Run VS Code extension mock dogfood smoke" \
      "${PYTHON_BIN}" scripts/qa/vscode_extension_dogfood.py \
      --mode mock \
      --package-mode require \
      --extension-host skip
    write_dogfood_summary_artifact
    ;;
  extension-host|run)
    run_root "Run VS Code extension-host component dogfood" \
      "${PYTHON_BIN}" scripts/qa/vscode_extension_dogfood.py \
      --mode mock \
      --package-mode require \
      --extension-host run
    write_dogfood_summary_artifact
    ;;
  require-cached)
    run_root "Run VS Code extension cached component dogfood" \
      "${PYTHON_BIN}" scripts/qa/vscode_extension_dogfood.py \
      --mode mock \
      --package-mode require \
      --extension-host require-cached
    write_dogfood_summary_artifact
    ;;
  *)
    section "Run VS Code extension dogfood smoke"
    printf 'Unknown ALYSIS_RELEASE_DOGFOOD=%s. Use skip, mock, extension-host, run, or require-cached.\n' "${DOGFOOD_MODE}" >&2
    exit 1
    ;;
esac

section "Component-candidate verification completed (not releasable)"
printf 'Use managed-cli-vsix-release.yml for target-specific signed production candidates.\n'
