from __future__ import annotations

import pytest

from alysis_code.agent.verification_commands import (
    _matching_effective_verification_commands,
    _normalize_shell_command_for_match,
    _verify_run_commands_match_effective_contract,
)
from alysis_code.agent.verification_evidence import classify_verification_evidence
from alysis_code.verification_command_analysis import analyze_verification_command


@pytest.mark.parametrize(
    ("observed", "required"),
    [
        ("pytest tests/test_a.py -q", "pytest tests/test_b.py -q"),
        ("pytest tests/test_a.py -q", "pytest -q"),
        ("pytest -q", "pytest tests/test_a.py -q"),
        ("pytest tests/test_a.py -k smoke -q", "pytest tests/test_a.py -q"),
        ("pytest -m smoke -q", "pytest -q"),
        ("pytest --deselect tests/test_a.py::test_edge -q", "pytest -q"),
        ("pytest --ignore=tests/test_a.py -q", "pytest -q"),
        ("pytest tests/test_a.py::test_one -q", "pytest tests/test_a.py -q"),
        ("pytest tests/test_a.py -x -q", "pytest tests/test_a.py -q"),
        ("pytest tests/test_a.py --maxfail=1 -q", "pytest tests/test_a.py -q"),
        ("pytest tests/test_a.py -q", "pytest tests/Test_a.py -q"),
        ("pytest -k 'FastCase' -q", "pytest -k 'fastcase' -q"),
        ("pytest -k 'two  spaces' -q", "pytest -k 'two spaces' -q"),
        ("pytest -k '-q'", "pytest -k '-v'"),
        ("pytest --plugin-switch -k '-q'", "pytest --plugin-switch -k '-v'"),
        ("pytest -- '-q'", "pytest -- '-v'"),
        ("PYTHONPATH=other pytest -q", "PYTHONPATH=src pytest -q"),
        ("PYTHONPATH=src pytest -q", "pytest -q"),
        ("MODE=fast pytest -q", "MODE=FAST pytest -q"),
        ("MODE='two  spaces' pytest -q", "MODE='two spaces' pytest -q"),
        ("env MODE=fast pytest -q", "env MODE=slow pytest -q"),
        ("'MODE=fast' pytest -q", "MODE=fast pytest -q"),
        ('MO"DE"=fast pytest -q', "MODE=fast pytest -q"),
        ("MODE\\=fast pytest -q", "MODE=fast pytest -q"),
        ("cd package_a && pytest -q", "cd package_b && pytest -q"),
        ("cd Package && pytest -q", "cd package && pytest -q"),
        ("cd package && pytest -q", "pytest -q"),
        ("uv run pytest -q", "pytest -q"),
        ("poetry run pytest -q", "uv run pytest -q"),
        ("bash -lc 'pytest -q'", "pytest -q"),
        ("bash -lc 'MODE=fast pytest -q'", "bash -lc 'MODE=slow pytest -q'"),
        (".venv/bin/python -m pytest -q", "other/bin/python -m pytest -q"),
        ("python -m PyTest -q", "pytest -q"),
        ("pytest 'tests/test_*.py' -q", "pytest tests/test_*.py -q"),
        ("pytest -k '$FILTER' -q", 'pytest -k "$FILTER" -q'),
        ("ruff check src/a.py", "ruff check ."),
        ("go test ./pkg/...", "go test ./..."),
        ("cargo test one_case", "cargo test"),
        ("npm test -- one_case", "npm test"),
        ("make verify TARGET=unit", "make verify TARGET=all"),
    ],
)
def test_different_execution_scope_never_covers_a_required_command(
    observed: str, required: str
) -> None:
    assert not _matching_effective_verification_commands(
        observed_command=observed, effective_verification_commands=[required]
    )
    assert _verify_run_commands_match_effective_contract(
        requested_commands=[observed], effective_verification_commands=[required]
    ) == [observed]
    evidence = classify_verification_evidence(
        observed,
        known_verification_commands=[required],
        authoritative=True,
        exit_code=0,
        output="1 passed\n",
        real_execution=True,
    )
    assert evidence.allowed_to_satisfy_contract is False
    assert evidence.covered_verification_commands == ()


@pytest.mark.parametrize(
    ("observed", "required"),
    [
        ("python -m pytest -v", "pytest -q"),
        ("python3 -m pytest tests/test_a.py -vv", "pytest tests/test_a.py --quiet"),
        ("py -m pytest -qq tests/test_a.py", "pytest --verbose tests/test_a.py"),
        ("py.test tests/test_a.py -q", "pytest tests/test_a.py -v"),
        ("pytest -q tests/test_a.py", "pytest tests/test_a.py -v"),
        ("pytest   tests/test_a.py -q", "pytest tests/test_a.py -v"),
        ('pytest "tests/test a.py" -q', "pytest 'tests/test a.py' -v"),
        ("pytest -k 'two  spaces' -q", "python -m pytest -k 'two  spaces' -v"),
        ("PYTHONPATH=src pytest -q", "PYTHONPATH=src python -m pytest -v"),
        ("env MODE=fast pytest -q", "env MODE=fast python -m pytest -v"),
        ("cd package && pytest -q", "cd package && python -m pytest -v"),
        ("uv run pytest -q", "uv run python -m pytest -v"),
        ("bash -lc 'pytest -q'", "bash -lc 'python -m pytest -v'"),
        (".venv/bin/python -m pytest -q", ".venv/bin/python -m pytest -v"),
        ("python -m ruff check src/a.py", "ruff check src/a.py"),
        ("pytest -k '$FILTER' -q", "pytest -k '$FILTER' -q"),
    ],
)
def test_equivalent_static_scope_and_reporter_variants_cover_the_requirement(
    observed: str, required: str
) -> None:
    assert _matching_effective_verification_commands(
        observed_command=observed, effective_verification_commands=[required]
    ) == {required}
    assert (
        _verify_run_commands_match_effective_contract(
            requested_commands=[observed], effective_verification_commands=[required]
        )
        == []
    )


def test_one_command_covers_only_its_own_requirement() -> None:
    required = ["pytest tests/test_a.py -q", "pytest tests/test_b.py -q", "pytest -q"]
    assert _matching_effective_verification_commands(
        observed_command="python -m pytest tests/test_a.py -v",
        effective_verification_commands=required,
    ) == {required[0]}


@pytest.mark.parametrize(
    "switch",
    [
        "-x",
        "--exitfirst",
        "--disable-warnings",
        "--disable-pytest-warnings",
        "--strict-config",
        "--strict-markers",
    ],
)
def test_no_argument_switch_preserves_selectors_and_allows_reporter_variants(switch: str) -> None:
    required = f"pytest {switch} -q"
    assert _matching_effective_verification_commands(
        observed_command=f"python -m pytest {switch} -v",
        effective_verification_commands=[required],
    ) == {required}
    assert not _matching_effective_verification_commands(
        observed_command=f"pytest {switch} -k '-q'",
        effective_verification_commands=[f"pytest {switch} -k '-v'"],
    )


def test_runner_capability_is_separate_from_contract_coverage() -> None:
    observed = "PYTHONPATH=src pytest tests/test_a.py -q"
    assert analyze_verification_command(observed).is_valid_verifier
    standalone = classify_verification_evidence(observed, exit_code=0, real_execution=True)
    scoped = classify_verification_evidence(
        observed, known_verification_commands=["pytest -q"], exit_code=0, real_execution=True
    )
    assert standalone.allowed_to_satisfy_contract is True
    assert scoped.allowed_to_satisfy_contract is False


def test_source_normalization_preserves_case_and_quoted_whitespace() -> None:
    source = "MODE='Two  Spaces' pytest Tests/Test_A.py -q"
    assert _normalize_shell_command_for_match(f"  {source}  ") == source
