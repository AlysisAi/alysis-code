from __future__ import annotations

import pytest

from alysis_code.agent.acceptance_contract import _commands_equivalent
from alysis_code.agent.verification_commands import (
    _effective_verification_command_matches,
    _matching_effective_verification_commands,
)
from alysis_code.agent.verification_evidence import classify_verification_evidence


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("uv run python -m pytest tests/Case.py -v", "pytest tests/Case.py -q"),
        ("poetry run pytest tests/Case.py -v", "pytest tests/Case.py -q"),
        ("bash -lc 'pytest tests/Case.py -v'", "pytest tests/Case.py -q"),
        ("env PYTHONPATH=src pytest tests -v", "PYTHONPATH=src pytest tests -q"),
        ("bash -lc 'cd unit && pytest tests -v'", "cd unit && pytest tests -q"),
        ("pytest tests/unit.py", "pytest tests/integration.py"),
        ("pytest tests/unit.py -q", "pytest -q"),
        ("pytest tests/unit", "pytest tests"),
        ("pytest tests -k unit", "pytest tests -k integration"),
        ("pytest tests -k Unit", "pytest tests -k unit"),
        ("pytest tests -m unit", "pytest tests -m integration"),
        ("pytest tests -k unit", "pytest tests"),
        (
            "pytest tests --deselect=tests/a.py::test_a",
            "pytest tests --deselect=tests/a.py::test_b",
        ),
        ("pytest tests/Case.py", "pytest tests/case.py"),
        ("pytest tests/δοκιμή.py", "pytest tests/Δοκιμή.py"),
        (
            "pytest 'tests/a.py::test_value[two  words]'",
            "pytest 'tests/a.py::test_value[two words]'",
        ),
        ("pytest tests -- -v", "pytest tests"),
        ("pytest tests --maxfail=1", "pytest tests --maxfail=2"),
        ("python -m unittest discover -s unit", "python -m unittest discover -s integration"),
        (
            "python -m unittest discover -p unit*.py",
            "python -m unittest discover -p integration*.py",
        ),
        ("python -m unittest discover -s tests", "python -m unittest discover"),
        ("go test ./unit/...", "go test ./integration/..."),
        ("go test -run Unit ./...", "go test -run Integration ./..."),
        ("cargo test unit", "cargo test integration"),
        ("npm test -- unit", "npm test -- integration"),
        ("ruff check src/a.py", "ruff check src"),
        ("make verify TARGET=unit", "make verify TARGET=integration"),
        ("PYTHONPATH=unit pytest tests", "PYTHONPATH=integration pytest tests"),
        ("PYTHONPATH=unit pytest tests", "pytest tests"),
        ("cd unit && pytest tests", "cd integration && pytest tests"),
        ("cd unit && pytest tests", "pytest tests"),
    ],
)
def test_distinct_selections_do_not_satisfy_each_other(observed: str, expected: str) -> None:
    assert not _matching_effective_verification_commands(
        observed_command=observed, effective_verification_commands=[expected]
    )
    assert not _effective_verification_command_matches(
        normalized_cmd=observed, known_verification_commands=[expected]
    )
    assert not _commands_equivalent(observed, expected)
    evidence = classify_verification_evidence(
        observed,
        known_verification_commands=[expected],
        authoritative=True,
        exit_code=0,
        real_execution=True,
        output="1 passed\n",
    )
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.covered_verification_commands == ()


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("pytest tests/Case.py -v", "pytest tests/Case.py -q"),
        ("pytest -q tests/Case.py -vv", "pytest tests/Case.py --verbose"),
        ("python3 -m pytest tests/Case.py -v", "pytest tests/Case.py -q"),
        ("uv run python -m pytest tests/Case.py -v", "uv run pytest tests/Case.py -q"),
        ("poetry run pytest tests/Case.py -v", "poetry run pytest tests/Case.py -q"),
        ("bash -lc 'pytest tests/Case.py -v'", "bash -lc 'pytest tests/Case.py -q'"),
        ("env PYTHONPATH=src pytest tests -v", "env PYTHONPATH=src pytest tests -q"),
        ("bash -lc 'cd unit && pytest tests -v'", "bash -lc 'cd unit && pytest tests -q'"),
        ("python3 -m unittest discover -s tests -v", "python -m unittest discover -s tests -v"),
        ("pytest   tests -k 'Case and not Other'", 'pytest tests -k "Case and not Other"'),
        (
            "pytest 'tests/a.py::test_value[two  words]'",
            "pytest 'tests/a.py::test_value[two  words]'",
        ),
    ],
)
def test_same_selection_keeps_existing_launcher_and_reporting_equivalences(
    observed: str, expected: str
) -> None:
    assert _matching_effective_verification_commands(
        observed_command=observed, effective_verification_commands=[expected]
    ) == {expected}
    assert _commands_equivalent(observed, expected)


def test_actual_working_directory_is_part_of_observed_selection(tmp_path) -> None:
    evidence = classify_verification_evidence(
        "pytest tests",
        known_verification_commands=["pytest tests"],
        exit_code=0,
        real_execution=True,
        root=tmp_path,
        working_directory="unit",
    )
    assert evidence.covered_verification_commands == ()
    assert evidence.allowed_to_satisfy_contract is False
