from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code.agent.blast_radius import (
    BlastRadiusScope,
    BlastRadiusStatus,
    ScopeEntry,
    ScopeLanguage,
    ScopeTier,
    build_blast_radius_scope_advisory,
    command_path_selectors,
    selection_covers,
)
from alysis_code.agent.verification import TurnExecutionState, _record_tool_effect


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for directory in ("tests", "tests/nested", "other"):
        folder = tmp_path / directory
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "__init__.py").write_text("")
    for path in (
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/nested/test_child.py",
        "other/test_outside.py",
        "tests/alternate_test.py",
    ):
        (tmp_path / path).write_text(
            "import unittest\nclass Checks(unittest.TestCase):\n"
            "    def test_ok(self):\n        self.assertTrue(True)\n"
        )
    return tmp_path


def _selection(repo: Path, suffix: str):
    return command_path_selectors(f"python3 -m unittest {suffix}", workspace_root=repo)


@pytest.mark.parametrize("target", ["tests.test_alpha", "tests/test_alpha.py"])
def test_explicit_module_and_file_cover_only_that_file(repo: Path, target: str):
    selectors = _selection(repo, f"{target} -v")
    assert selectors == ("tests/test_alpha.py",)
    assert selection_covers(selectors, "tests/test_alpha.py")
    assert not selection_covers(selectors, "tests/test_beta.py")


@pytest.mark.parametrize(
    "suffix",
    [
        "tests.test_alpha.Checks",
        "tests.test_alpha.Checks.test_ok",
        "tests.test_alpha -k test_ok",
        "tests.test_missing",
        "unknown.module.Checks",
        "missing.py",
        "tests.test_alpha tests.test_missing",
        "--unknown-option",
        "discover -s absent",
        "discover -s tests -k test_ok",
    ],
)
def test_partial_or_unresolved_selection_is_not_whole_suite(repo: Path, suffix: str):
    selectors = _selection(repo, suffix)
    assert selectors is None
    assert not selection_covers(selectors, "tests/test_alpha.py")


def test_resolution_does_not_import_project_modules(repo: Path):
    (repo / "tests/__init__.py").write_text("raise RuntimeError('must not import')\n")
    (repo / "tests/test_alpha.py").write_text("raise RuntimeError('must not import')\n")
    assert _selection(repo, "tests.test_alpha") == ("tests/test_alpha.py",)


def test_ambiguous_file_package_and_unresolved_src_layout_are_inconclusive(repo: Path):
    (repo / "tests/test_alpha").mkdir()
    (repo / "tests/test_alpha/__init__.py").write_text("")
    assert _selection(repo, "tests.test_alpha") is None
    (repo / "src/pkg").mkdir(parents=True)
    (repo / "src/pkg/test_extra.py").write_text("")
    assert _selection(repo, "pkg.test_extra") is None


def test_unittest_file_importing_same_named_package_cannot_baseline_file(repo: Path):
    (repo / "tests/test_alpha.py").write_text("raise RuntimeError('file did not run')\n")
    package = repo / "tests/test_alpha"
    package.mkdir()
    (package / "__init__.py").write_text(
        "import unittest\nclass ActualPackage(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests/test_alpha.py", "-v"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ActualPackage" in result.stderr
    assert _selection(repo, "tests/test_alpha.py -v") is None


def test_foreign_regular_package_shadowing_local_namespace_is_inconclusive(repo: Path):
    (repo / "tests/__init__.py").unlink()
    (repo / "tests/test_alpha.py").write_text("raise RuntimeError('local file did not run')\n")
    foreign = repo.parent / "foreign"
    (foreign / "tests").mkdir(parents=True)
    (foreign / "tests/__init__.py").write_text("")
    (foreign / "tests/test_alpha.py").write_text(
        "import unittest\nclass ActualForeign(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_alpha", "-v"],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(foreign)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ActualForeign" in result.stderr
    assert _selection(repo, "tests.test_alpha -v") is None
    assert _selection(repo, "tests/test_alpha.py -v") is None


def test_dotted_discovery_uses_same_namespace_guard_as_explicit_modules(repo: Path):
    assert _selection(repo, "discover -s tests.nested -v") == ("tests/nested/test_child.py",)
    (repo / "tests/__init__.py").unlink()
    (repo / "tests/nested/test_child.py").write_text("raise RuntimeError('local did not run')\n")
    foreign = repo.parent / "foreign"
    (foreign / "tests/nested").mkdir(parents=True)
    (foreign / "tests/__init__.py").write_text("")
    (foreign / "tests/nested/__init__.py").write_text("")
    (foreign / "tests/nested/test_elsewhere.py").write_text(
        "import unittest\nclass ForeignDiscovery(unittest.TestCase):\n"
        "    def test_ok(self):\n        self.assertTrue(True)\n"
    )
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests.nested", "-v"],
        cwd=repo,
        env={**os.environ, "PYTHONPATH": str(foreign)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ForeignDiscovery" in result.stderr
    assert _selection(repo, "discover -s tests.nested -v") is None
    # Actual directory discovery does not import the dotted start name.
    assert _selection(repo, "discover -s tests/nested -v") == ("tests/nested/test_child.py",)


def test_quoted_absolute_environment_and_cd_paths_use_existing_command_parser(repo: Path):
    for command in (
        'env MODE=test /opt/venv/bin/python -m unittest "tests.test_alpha" -v',
        f'python3 -m unittest "{repo}/tests/test_alpha.py" -v',
        "cd tests && python3 -m unittest test_alpha -v",
    ):
        assert command_path_selectors(command, workspace_root=repo) == ("tests/test_alpha.py",)
    outside = repo.parent / "test_outside_root.py"
    outside.write_text("")
    assert _selection(repo, str(outside)) is None


@pytest.mark.parametrize("suffix", ["discover", "", "-v"])
def test_default_discovery_covers_matching_existing_files(repo: Path, suffix: str):
    selected = _selection(repo, suffix)
    assert set(selected or ()) == {
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/nested/test_child.py",
        "other/test_outside.py",
    }
    assert not selection_covers(selected, "tests/alternate_test.py")
    assert not selection_covers(selected, "tests/test_later.py")


@pytest.mark.parametrize(
    "suffix",
    [
        "discover -s tests -t .",
        "discover tests test*.py .",
        "discover --start-directory=tests --top-level-directory=.",
    ],
)
def test_discovery_honors_start_directory_and_pattern(repo: Path, suffix: str):
    selected = _selection(repo, suffix)
    assert set(selected or ()) == {
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/nested/test_child.py",
    }
    assert not selection_covers(selected, "other/test_outside.py")
    assert _selection(repo, "discover -s tests -p '*_test.py'") == ("tests/alternate_test.py",)


def test_discovery_respects_package_boundary_and_explicit_cd(repo: Path):
    (repo / "tests/nested/__init__.py").unlink()
    selected = command_path_selectors(
        "cd tests && python3 -m unittest discover", workspace_root=repo
    )
    assert set(selected or ()) == {"tests/test_alpha.py", "tests/test_beta.py"}
    assert not selection_covers(selected, "tests/nested/test_child.py")


def test_symlink_loop_or_outside_discovery_is_inconclusive(repo: Path):
    loop = repo / "loop"
    try:
        loop.symlink_to(loop)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert _selection(repo, "discover -s loop") is None
    assert _selection(repo, "discover -s ..") is None


def _record(state: TurnExecutionState, repo: Path, commands: list[str], tool: str):
    results = []
    for command in commands:
        import shlex

        args = shlex.split(command)
        completed = subprocess.run(
            [sys.executable, *args[1:]], cwd=repo, text=True, capture_output=True, check=False
        )
        assert completed.returncode == 0, completed.stderr
        results.append(
            {
                "command": command,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            }
        )
    if tool == "verify_run":
        result = {"commands": commands, "all_passed": True, "command_results": results}
        arguments = {"commands": commands}
    else:
        assert len(commands) == 1
        result = {**results[0], "cmd": commands[0]}
        arguments = {"cmd": commands[0]}
    _record_tool_effect(
        root=repo,
        state=state,
        tool_name=tool,
        arguments=arguments,
        status="ok",
        result=result,
        known_verification_commands=[],
        verification_authoritative=True,
    )


def _state() -> TurnExecutionState:
    state = TurnExecutionState(execution_requested=True)
    state.blast_radius_scope = BlastRadiusScope(
        entries=tuple(
            ScopeEntry(path, ScopeTier.IMPORTER)
            for path in (
                "tests/test_alpha.py",
                "tests/test_beta.py",
            )
        ),
        language=ScopeLanguage.PYTHON,
    )
    return state


@pytest.mark.parametrize("tool", ["verify_run", "shell_run"])
def test_two_baselines_do_not_advertise_last_single_module_as_both(repo: Path, tool: str):
    state = _state()
    commands = ["python3 -m unittest tests.test_alpha -v", "python3 -m unittest tests.test_beta -v"]
    if tool == "verify_run":
        _record(state, repo, commands, tool)
    else:
        for command in commands:
            _record(state, repo, [command], tool)
    assert state.has_blast_radius_baseline()
    assert [run.command for run in state.blast_radius_runs] == commands
    assert state.blast_radius_baseline_command() == ""
    assert all(not run.whole_suite for run in state.blast_radius_runs)
    message = build_blast_radius_scope_advisory(
        state.blast_radius_scope,
        has_baseline=state.has_blast_radius_baseline(),
        baseline_covered_paths=state.blast_radius_baseline_covered_paths(),
        baseline_command=state.blast_radius_baseline_command(),
    )
    assert "tests/test_alpha.py, tests/test_beta.py" in message
    assert "for example" not in message


def test_single_module_gate_cannot_clear_missing_neighbour_verification(repo: Path):
    state = _state()
    command = "python3 -m unittest tests.test_alpha -v"
    _record(state, repo, [command], "verify_run")
    assert state.blast_radius_baseline_covered_paths() == ("tests/test_alpha.py",)
    assert not state.has_blast_radius_baseline()
    state.touched_repo_paths.add("existing.py")
    _record(state, repo, [command], "verify_run")
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").status
        == BlastRadiusStatus.GATE_MISSING
    )
    _record(state, repo, ["python3 -m unittest discover -s tests -v"], "verify_run")
    assert (
        state.compute_blast_radius_assessment(enabled=True, turn_intent="execute").gate_command
        == "python3 -m unittest discover -s tests -v"
    )


def test_unknown_selection_remains_explicit_in_event_and_cannot_prove_baseline(repo: Path):
    state = _state()
    _record(state, repo, ["python3 -m unittest tests.test_alpha.Checks.test_ok -v"], "verify_run")
    payload = state.blast_radius_runs[0].as_payload()
    assert payload["selectors"] is None
    assert payload["selection_known"] is False
    assert payload["whole_suite"] is False
    assert not state.has_blast_radius_baseline()
