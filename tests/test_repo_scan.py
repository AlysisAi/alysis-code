from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import alysis_code.repo_scan as repo_scan_mod
from alysis_code.repo_scan import (
    render_repo_scan_markdown,
    render_repo_scan_summary_lines,
    scan_workspace,
)
from alysis_code.workspace_context import resolve_workspace_context


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")


def _commit(repo: Path, relative_path: str = "README.md") -> None:
    target = repo / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("hello\n", encoding="utf-8")
    _git(repo, "add", relative_path)
    _git(repo, "commit", "-m", "init")


def test_repo_scan_plain_dir_infers_python_signals(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Project\n\nSmall repo.\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text(
        "def test_smoke():\n    assert True\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.workspace_kind == "plain_dir"
    assert scan.focus_relpath == "."
    assert any(item["path"] == "pyproject.toml" for item in scan.manifests)
    assert scan.readme_paths == ["README.md"]
    assert scan.language_hints == ["python"]
    assert scan.likely_test_commands == ["pytest -q"]


def test_repo_scan_plain_dir_with_manifest_but_no_real_python_tests_stays_conservative(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.language_hints == ["python"]
    assert scan.likely_test_commands == []


def test_repo_scan_plain_python_layout_without_manifest_infers_pytest(tmp_path: Path) -> None:
    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.workspace_kind == "plain_dir"
    assert scan.language_hints == []
    assert scan.likely_test_commands == ["pytest -q"]


def test_repo_scan_plain_repo_with_non_python_tests_stays_conservative(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "app.test.js").write_text("test('ok', () => {})\n", encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == []


def test_repo_scan_readme_pycon_examples_never_infer_doctest_commands(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Mathlet\n\n```pycon\n>>> from mathlet import double\n>>> double(3)\n6\n```\n",
        encoding="utf-8",
    )
    (tmp_path / "mathlet.py").write_text(
        "def double(value: int) -> int:\n    return value * 2\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == []


def test_repo_scan_readme_doctest_never_shadows_a_real_test_surface(tmp_path: Path) -> None:
    (tmp_path / "README.rst").write_text(
        "Mathlet\n=======\n\n>>> from mathlet import double\n>>> double(3)\n6\n",
        encoding="utf-8",
    )
    (tmp_path / "test_mathlet.py").write_text("def test_double() -> None:\n    pass\n", "utf-8")

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == ["pytest -q"]


def test_repo_scan_tests_helper_py_without_python_test_signals_stays_conservative(
    tmp_path: Path,
) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == []


def test_repo_scan_git_repo_records_branch_and_git_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit(repo)
    (repo / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(repo))

    assert scan.workspace_kind == "git_repo"
    assert scan.git_root == os.fspath(repo.resolve())
    assert scan.has_head_commit is True
    assert scan.current_branch
    assert "npm test" in scan.likely_test_commands


def test_repo_scan_git_repo_without_head_reports_no_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    (repo / "Cargo.toml").write_text("[package]\nname='demo'\nversion='0.1.0'\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "lib.rs").write_text("pub fn parse() -> bool { true }\n", encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(repo))

    assert scan.workspace_kind == "git_repo_no_head"
    assert scan.git_root == os.fspath(repo.resolve())
    assert scan.has_head_commit is False
    assert scan.current_branch
    assert scan.likely_test_commands == ["cargo test"]
    assert "src/lib.rs" in scan.observed_paths
    assert "- `src/lib.rs`" in render_repo_scan_markdown(scan)


def test_repo_scan_prefers_focus_conventions_and_collects_readmes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit(repo)
    (repo / "README.md").write_text("# Root\n\nRoot readme.\n", encoding="utf-8")
    focus_dir = repo / "packages" / "api"
    focus_dir.mkdir(parents=True)
    (focus_dir / "README.md").write_text("# API\n\nFocus readme.\n", encoding="utf-8")
    (focus_dir / "CONVENTIONS.md").write_text("Keep handlers small.\n", encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(focus_dir))

    assert scan.focus_relpath == "packages/api"
    assert scan.readme_paths == ["README.md", "packages/api/README.md"]
    assert scan.conventions_path == "packages/api/CONVENTIONS.md"
    assert any("Focus readme" in item["excerpt"] for item in scan.readme_excerpts)
    assert scan.conventions_excerpt == "Keep handlers small."


def test_repo_scan_uses_focus_pythonpath_for_subdir_python_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit(repo)
    service = repo / "service"
    (service / "app").mkdir(parents=True)
    (service / "tests").mkdir()
    (service / "app" / "config.py").write_text(
        "def load_mode():\n    return 'dev'\n",
        encoding="utf-8",
    )
    (service / "tests" / "test_config.py").write_text(
        "from app.config import load_mode\n\n\ndef test_load_mode():\n    assert load_mode() == 'dev'\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(service))

    assert scan.focus_relpath == "service"
    assert scan.likely_test_commands == ["PYTHONPATH=service pytest -q service"]


def test_repo_scan_prefers_focus_python_tests_over_root_python_tests(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit(repo)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_root.py").write_text(
        "def test_root():\n    assert True\n",
        encoding="utf-8",
    )
    service = repo / "service"
    (service / "app").mkdir(parents=True)
    (service / "tests").mkdir()
    (service / "app" / "config.py").write_text("VALUE = 'service'\n", encoding="utf-8")
    (service / "tests" / "test_config.py").write_text(
        "from app.config import VALUE\n\n\ndef test_config():\n    assert VALUE == 'service'\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(service))

    assert scan.focus_relpath == "service"
    assert scan.likely_test_commands == ["PYTHONPATH=service pytest -q service"]


def test_repo_scan_likely_test_commands_are_conservative_and_deterministic(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"packageManager":"pnpm@9.0.0","scripts":{"test":"echo \\"Error: no test specified\\" && exit 1"}}\n',
        encoding="utf-8",
    )
    (tmp_path / "Makefile").write_text("test:\n\tpytest -q\n", encoding="utf-8")
    (tmp_path / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == ["make test", "go test ./..."]
    summary_lines = render_repo_scan_summary_lines(scan)
    assert any("Likely verify: make test, go test ./..." in line for line in summary_lines)


def test_repo_scan_authorizes_safe_package_verification_scripts(tmp_path: Path) -> None:
    (tmp_path / "typecheck.js").write_text("export const value = 1;\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "node --test",
                    "check": "node --test",
                    "lint": "node --test",
                    "typecheck": "node --check typecheck.js",
                    "test:unit": "node --test",
                    "test:unsafe": "rm -rf build",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == [
        "npm test",
        "npm run check",
        "npm run lint",
        "npm run typecheck",
        "npm run test:unit",
    ]


def test_repo_scan_authorizes_python_native_declared_checks(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n\n[tool.ruff]\n\n[tool.mypy]\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == ["pytest -q", "ruff check .", "mypy ."]


def test_repo_scan_prefers_declared_unittest_suite(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text(
        "import unittest\n\nclass DemoTest(unittest.TestCase):\n    pass\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == ["python -m unittest discover"]


def test_repo_scan_authorizes_setup_cfg_python_checks(tmp_path: Path) -> None:
    (tmp_path / "setup.cfg").write_text(
        "[tool:pytest]\n\n[ruff]\n\n[mypy]\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text(
        "def test_demo():\n    assert True\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == ["pytest -q", "ruff check .", "mypy ."]


def test_repo_scan_excludes_python_check_rejected_by_safety_analysis(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
    analyze = repo_scan_mod.analyze_verification_command

    def reject_ruff(command: str, **kwargs):  # type: ignore[no-untyped-def]
        candidate = "rm -rf build" if command == "ruff check ." else command
        return analyze(candidate, **kwargs)

    monkeypatch.setattr(repo_scan_mod, "analyze_verification_command", reject_ruff)

    scan = scan_workspace(context=resolve_workspace_context(tmp_path))

    assert scan.likely_test_commands == []


def test_repo_scan_discovers_nested_package_and_maven_manifests_for_monorepos(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit(repo)

    web_dir = repo / "packages" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text(
        '{"packageManager":"pnpm@9.0.0","scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    (web_dir / "tsconfig.json").write_text(
        '{"compilerOptions":{"target":"ES2022"}}\n', encoding="utf-8"
    )

    service_dir = repo / "services" / "orders"
    service_dir.mkdir(parents=True)
    (service_dir / "pom.xml").write_text(
        "<project><modelVersion>4.0.0</modelVersion></project>\n",
        encoding="utf-8",
    )

    scan = scan_workspace(context=resolve_workspace_context(repo))

    assert any(item["path"] == "packages/web/package.json" for item in scan.manifests)
    assert any(item["path"] == "packages/web/tsconfig.json" for item in scan.manifests)
    assert any(item["path"] == "services/orders/pom.xml" for item in scan.manifests)
    assert "javascript" in scan.language_hints
    assert "typescript" in scan.language_hints
    assert "java" in scan.language_hints
    assert "pnpm" in scan.package_hints
    assert "maven" in scan.package_hints
    assert scan.likely_test_commands == [
        "pnpm --dir packages/web test",
        "mvn -f services/orders/pom.xml test",
    ]


def test_repo_scan_prefers_nested_service_manifests_over_duplicate_root_manifests(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    _commit(repo)
    (repo / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")
    (repo / "pom.xml").write_text("<project></project>\n", encoding="utf-8")

    web_dir = repo / "packages" / "web"
    web_dir.mkdir(parents=True)
    (web_dir / "package.json").write_text('{"scripts":{"test":"vitest run"}}\n', encoding="utf-8")

    service_dir = repo / "services" / "orders"
    service_dir.mkdir(parents=True)
    (service_dir / "pom.xml").write_text("<project></project>\n", encoding="utf-8")

    scan = scan_workspace(context=resolve_workspace_context(repo))

    assert [item["path"] for item in scan.manifests] == [
        "package.json",
        "packages/web/package.json",
        "pom.xml",
        "services/orders/pom.xml",
    ]
    assert scan.likely_test_commands == [
        "npm --prefix packages/web test",
        "mvn -f services/orders/pom.xml test",
    ]


def test_repo_scan_manifest_walk_is_deterministic_under_directory_cap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(repo_scan_mod, "_MAX_MANIFEST_SCAN_DIRS", 3)
    repo = tmp_path / "repo"
    repo.mkdir()

    for name in ("z-last", "a-first", "b-second", "c-third"):
        service_dir = repo / "services" / name
        service_dir.mkdir(parents=True)
        (service_dir / "package.json").write_text(
            '{"scripts":{"test":"vitest run"}}\n', encoding="utf-8"
        )

    scan = scan_workspace(context=resolve_workspace_context(repo))

    assert any(item["path"] == "services/a-first/package.json" for item in scan.manifests)
    assert not any(item["path"] == "services/b-second/package.json" for item in scan.manifests)
    assert not any(item["path"] == "services/z-last/package.json" for item in scan.manifests)
