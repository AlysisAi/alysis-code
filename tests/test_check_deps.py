from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_check_deps_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "qa" / "check_deps.py"
    spec = importlib.util.spec_from_file_location("alysis_check_deps_test_module", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load dependency checker from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check_deps = _load_check_deps_module()


def _write_project(tmp_path: Path, *, dependencies: list[str]) -> Path:
    root = tmp_path / "project"
    package = root / "src" / "alysis_code"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    deps = "\n".join(f'  "{dependency}",' for dependency in dependencies)
    (root / "pyproject.toml").write_text(
        f"""
[project]
name = "demo"
dependencies = [
{deps}
]

[project.optional-dependencies]
server = [
  "python-multipart>=0.0.9",
]
""".lstrip(),
        encoding="utf-8",
    )
    return root


def test_find_missing_dependencies_reports_distribution_and_location(
    tmp_path: Path, monkeypatch
) -> None:
    root = _write_project(tmp_path, dependencies=["httpx>=0.27.0"])
    source = root / "src" / "alysis_code"
    (source / "demo.py").write_text(
        "import httpx\nfrom packaging.version import Version\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_deps, "PROJECT_ROOT", root)
    monkeypatch.setattr(check_deps, "SOURCE_ROOT", source)
    monkeypatch.setattr(check_deps, "PYPROJECT", root / "pyproject.toml")

    assert check_deps.find_missing_dependencies() == [("packaging", source / "demo.py", 2)]


def test_main_reports_missing_dependencies_in_release_check_format(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    root = _write_project(tmp_path, dependencies=["httpx>=0.27.0"])
    source = root / "src" / "alysis_code"
    (source / "demo.py").write_text(
        "import httpx\nfrom packaging.version import Version\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_deps, "PROJECT_ROOT", root)
    monkeypatch.setattr(check_deps, "SOURCE_ROOT", source)
    monkeypatch.setattr(check_deps, "PYPROJECT", root / "pyproject.toml")

    assert check_deps.main() == 1
    assert capsys.readouterr().out == "packaging imported in src/alysis_code/demo.py:2\n"


def test_find_missing_dependencies_accepts_declared_and_server_optional_deps(
    tmp_path: Path, monkeypatch
) -> None:
    root = _write_project(
        tmp_path,
        dependencies=["httpx>=0.27.0", "packaging>=23.0", "pillow>=12.3.0"],
    )
    source = root / "src" / "alysis_code"
    (source / "demo.py").write_text(
        "import httpx\nfrom packaging.version import Version\nfrom PIL import Image\nimport multipart\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(check_deps, "PROJECT_ROOT", root)
    monkeypatch.setattr(check_deps, "SOURCE_ROOT", source)
    monkeypatch.setattr(check_deps, "PYPROJECT", root / "pyproject.toml")

    assert check_deps.find_missing_dependencies() == []
