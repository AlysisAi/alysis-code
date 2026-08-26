from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.test_discovery import test_discover


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="test-discover",
        cwd=str(root),
        repo_root=str(root),
    )


def test_test_discover_finds_python_mirrored_test_and_command(tmp_path: Path) -> None:
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "src/pkg/calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "tests/test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n",
        encoding="utf-8",
    )

    result = test_discover(root=tmp_path, paths=["src/pkg/calc.py"])

    assert "pytest" in result["frameworks"]
    assert result["candidate_tests"][0]["path"] == "tests/test_calc.py"
    assert result["candidate_commands"][0]["command"] == "python -m pytest tests/test_calc.py -q"
    assert result["broad_commands"]


def test_test_discover_include_commands_false_suppresses_all_command_suggestions(
    tmp_path: Path,
) -> None:
    (tmp_path / "src/pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "src/pkg/calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (tmp_path / "tests/test_calc.py").write_text(
        "from pkg.calc import add\n\n\ndef test_add():\n    assert add(1, 1) == 2\n",
        encoding="utf-8",
    )

    result = test_discover(root=tmp_path, paths=["src/pkg/calc.py"], include_commands=False)

    assert result["candidate_tests"]
    assert result["candidate_commands"] == []
    assert result["broad_commands"] == []
    assert "pytest" in result["frameworks"]


def test_test_discover_rejects_escaping_paths_and_preserves_dot_paths(tmp_path: Path) -> None:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "src").mkdir()
    (tmp_path / ".github/workflows/check.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "src/app.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = test_discover(
        root=tmp_path,
        paths=["../outside.py", "./.github/workflows/check.py"],
        failure_summary={
            "failing_tests": [
                {"id": "../outside.py::test_secret", "path": "../outside.py"},
                {"id": "../outside::test_secret"},
                {"id": "src/app.py::test_app"},
            ],
            "likely_next_files": ["../secret.py", "./src/app.py"],
            "stack_frames": [{"path": "../frame.py"}, {"path": ".github/workflows/check.py"}],
        },
    )

    assert result["paths"] == [".github/workflows/check.py", "src/app.py"]
    assert all("outside" not in str(item) for item in result["paths"])
    assert all(
        "outside" not in str(test.get("id", "")) and test.get("path") != "outside.py"
        for test in result["candidate_tests"]
    )
    assert all("outside" not in item["command"] for item in result["candidate_commands"])


def test_test_discover_uses_failure_summary_nodeid(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_calc.py").write_text(
        "def test_add():\n    assert False\n", encoding="utf-8"
    )

    result = test_discover(
        root=tmp_path,
        failure_summary={
            "framework": "pytest",
            "failing_tests": [
                {
                    "id": "tests/test_calc.py::test_add",
                    "path": "tests/test_calc.py",
                    "message": "AssertionError",
                }
            ],
            "likely_next_files": ["src/calc.py"],
        },
    )

    assert result["candidate_tests"][0]["id"] == "tests/test_calc.py::test_add"
    assert result["candidate_commands"][0]["command"] == (
        "python -m pytest tests/test_calc.py::test_add -q"
    )
    assert "src/calc.py" in result["paths"]


def test_build_tools_registers_test_discover(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )

    assert "test_discover" in tools
    schema = tools["test_discover"].as_openai_tool()["function"]["parameters"]
    assert "paths" in schema["properties"]
    assert "failure_summary" in schema["properties"]
