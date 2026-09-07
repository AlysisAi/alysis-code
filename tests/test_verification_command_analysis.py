from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from alysis_code.agent.acceptance_contract import capture_acceptance_workspace_snapshot
from alysis_code.verification_command_analysis import analyze_verification_command


def test_long_inline_python_verifier_is_not_treated_as_checker_path(tmp_path: Path) -> None:
    snippet = "; ".join(["assert True"] * 40)
    command = "python -c " + shlex.quote(snippet)

    analysis = analyze_verification_command(command, trusted=True, workspace_root=tmp_path)
    snapshot = capture_acceptance_workspace_snapshot(
        root=tmp_path,
        effective_verification_commands=[command],
    )

    assert len(snippet.encode()) > 255
    assert analysis.uses_opaque_inline_code is True
    assert analysis.checker_entrypoint_paths == ()
    assert snapshot.preexisting_checker_paths == frozenset()


@pytest.mark.parametrize("options", ["-ec", "-euc"])
def test_long_inline_shell_verifier_is_not_treated_as_checker_path(
    tmp_path: Path,
    options: str,
) -> None:
    snippet = "true " * 80
    command = f"sh {options} " + shlex.quote(snippet)

    analysis = analyze_verification_command(command, trusted=True, workspace_root=tmp_path)
    snapshot = capture_acceptance_workspace_snapshot(
        root=tmp_path,
        effective_verification_commands=[command],
    )

    assert len(snippet.encode()) > 255
    assert analysis.uses_opaque_inline_code is False
    assert analysis.checker_entrypoint_paths == ()
    assert snapshot.preexisting_checker_paths == frozenset()


@pytest.mark.parametrize(
    "command",
    [
        "python -I -c 'assert True'",
        "python3.12 -c 'assert True'",
        "python -W ignore -c 'assert True'",
        "node --eval 'process.exit(0)'",
        "ruby -w -e 'exit 0'",
        "ruby -we 'exit 0'",
        "Rscript --expression 'stopifnot(TRUE)'",
    ],
)
def test_interpreter_inline_code_has_no_checker_entrypoint(command: str) -> None:
    analysis = analyze_verification_command(command, trusted=True)

    assert analysis.uses_opaque_inline_code is True
    assert analysis.checker_entrypoint_paths == ()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("python checks/verify.py", ("checks/verify.py",)),
        ("node --check scripts/check.js", ("scripts/check.js",)),
        ("ruby -w scripts/check.rb", ("scripts/check.rb",)),
        ("ruby -Ke scripts/check.rb", ("scripts/check.rb",)),
        ("Rscript scripts/check.R", ("scripts/check.R",)),
        ("sh -eu checks/verify.sh", ("checks/verify.sh",)),
    ],
)
def test_interpreter_script_keeps_checker_entrypoint(
    command: str,
    expected: tuple[str, ...],
) -> None:
    analysis = analyze_verification_command(command, trusted=True)

    assert analysis.uses_opaque_inline_code is False
    assert analysis.checker_entrypoint_paths == expected


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -c 'assert True'",
        "env MODE=test node --eval 'process.exit(0)'",
        "env PYTHON=/usr/bin/python python -c 'assert True'",
        "timeout 5 ruby -e 'exit 0'",
        "sh -ec \"python -c 'assert True'\"",
        "bash -o pipefail -c \"python -c 'assert True'\"",
        "bash -O extglob -c \"python -c 'assert True'\"",
        "sh -o errexit -c \"python -c 'assert True'\"",
        "zsh -o errexit -c \"python -c 'assert True'\"",
    ],
)
def test_opaque_inline_code_property_handles_versions_and_wrappers(command: str) -> None:
    analysis = analyze_verification_command(command, trusted=True)

    assert analysis.uses_opaque_inline_code is True


@pytest.mark.parametrize(
    "command",
    [
        "sh -c 'python repro.py'",
        "bash -lc 'pytest tests/repro.py::test_case -q'",
        "bash -o pipefail checks/verify.sh",
        "bash -O extglob checks/verify.sh",
        "sh -o errexit checks/verify.sh",
        "zsh -o errexit checks/verify.sh",
        "python3.12 checks/verify.py",
        "timeout 5 python3.12 repro.py",
    ],
)
def test_shell_and_unknown_wrappers_around_scripts_are_not_opaque(command: str) -> None:
    analysis = analyze_verification_command(command, trusted=True)

    assert analysis.uses_opaque_inline_code is False
