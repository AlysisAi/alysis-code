from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

from alysis_code.agent_loop import build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.repo_map import repo_map


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="repo-map-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(tmp_path: Path):
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        cfg=AppConfig(model="test-model"),
        non_interactive=True,
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_repo_map_collects_imports_tests_and_commands(tmp_path: Path) -> None:
    _write(tmp_path / "pyproject.toml", "[tool.pytest.ini_options]\n")
    _write(
        tmp_path / "src" / "pkg" / "core.py",
        "from pkg.helper import helper\n\n\ndef run() -> str:\n    return helper()\n",
    )
    _write(tmp_path / "src" / "pkg" / "helper.py", "def helper() -> str:\n    return 'ok'\n")
    _write(
        tmp_path / "tests" / "test_core.py",
        "from pkg.core import run\n\n\ndef test_run():\n    assert run() == 'ok'\n",
    )

    result = repo_map(root=tmp_path, paths=["src/pkg/core.py"], max_items=20)

    assert "src/pkg/core.py" in result["paths"]
    assert {
        "from": "src/pkg/core.py",
        "to": "src/pkg/helper.py",
        "import": "pkg.helper",
        "depth": 0,
    } in result["import_edges"]
    assert any(item["path"] == "src/pkg/helper.py" for item in result["related_files"])
    assert any(item["path"] == "tests/test_core.py" for item in result["candidate_tests"])
    assert any(
        item["command"] == "python -m pytest tests/test_core.py -q"
        for item in result["candidate_commands"]
    )
    assert any("pytest" in command and "-q" in command for command in result["broad_commands"])


def test_repo_map_normalizes_input_paths_under_root(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "pkg" / "core.py", "VALUE = 1\n")
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "name: ci\n")

    result = repo_map(
        root=tmp_path,
        paths=[
            str(tmp_path / "src" / "pkg" / "core.py"),
            "./.github/workflows/ci.yml",
            "../outside.py",
        ],
        include_tests=False,
        include_imports=False,
    )

    assert result["paths"] == ["src/pkg/core.py", ".github/workflows/ci.yml"]
    assert all(not path.startswith("..") for path in result["paths"])
    assert any(item["path"] == ".github/workflows/ci.yml" for item in result["related_files"])


def test_build_tools_registers_repo_map(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    assert "repo_map" in tools
    schema = tools["repo_map"].as_openai_tool()["function"]["parameters"]
    assert schema["properties"]["depth"]["default"] == 2
    assert schema["properties"]["max_items"]["maximum"] == 200
