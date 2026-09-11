import subprocess
from pathlib import Path

import pytest

from scripts.qa import check_public_repo
from scripts.qa.check_public_repo import (
    collect_violations,
    markdown_link_violations,
    path_violation,
)


def test_checks_working_tree_after_removing_tracked_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ci.log").write_text("synthetic CI output\n", encoding="utf-8")
    subprocess.run(["git", "add", "ci.log"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    monkeypatch.setattr(
        check_public_repo, "__file__", str(tmp_path / "scripts/qa/check_public_repo.py")
    )

    assert check_public_repo.main() == 1
    assert "ci.log" in capsys.readouterr().err

    (tmp_path / "ci.log").rename(tmp_path / "ci-output.txt")

    assert check_public_repo.main() == 0
    assert "passed" in capsys.readouterr().out

    (tmp_path / "new.log").write_text("synthetic untracked output\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")

    assert check_public_repo.main() == 1
    assert "new.log" in capsys.readouterr().err


def test_rejects_local_and_credential_bearing_paths() -> None:
    assert path_violation("src/pkg/__pycache__/module.pyc") == "generated cache directory"
    assert path_violation(".alysis/runs/session.json") == "local runtime or build-output directory"
    assert path_violation("config/.env") == "environment file"
    assert path_violation("certificates/release.pem") == "credential-bearing or generated file type"


def test_allows_source_documentation_and_environment_examples() -> None:
    assert path_violation("src/alysis_code/cli.py") is None
    assert path_violation("docs/security_model.md") is None
    assert path_violation(".env.example") is None


@pytest.mark.parametrize(
    "path",
    ["docs/internal/notes.md", "docs/internal/reviews/catalog.md", "Docs/Internal/notes.md"],
)
def test_rejects_internal_documentation(path: str) -> None:
    assert path_violation(path) == "internal documentation directory"
    assert path_violation("docs/internals.md") is None
    assert path_violation("src/pkg/internal/parser.py") is None


def test_rejects_large_tracked_files(tmp_path: Path) -> None:
    large_file = tmp_path / "large.bin"
    large_file.write_bytes(b"x" * (5 * 1024 * 1024 + 1))

    assert collect_violations(["large.bin"], tmp_path) == ["large.bin: tracked file exceeds 5 MiB"]


def test_reports_broken_relative_markdown_links(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "[existing](../README.md) [missing](missing.md) "
        "[web](https://example.com) [host route](../../../issues)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")

    assert markdown_link_violations(["README.md", "docs/guide.md"], tmp_path) == [
        "docs/guide.md:1: broken relative link: missing.md"
    ]


def test_relative_links_require_exact_file_and_directory_case(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (docs / "οδηγός.md").write_text("# Οδηγός\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[correct](docs/guide.md#setup)\n"
        "[wrong filename](docs/GUIDE.md)\n"
        "[wrong directory](Docs/guide.md)\n"
        "[directory](docs/)\n"
        "[wrong directory link](Docs/)\n"
        "[Unicode](docs/οδηγός.md)\n"
        "[encoded filename](docs/%CE%BF%CE%B4%CE%B7%CE%B3%CF%8C%CF%82.md)\n"
        "[wrong Unicode case](docs/ΟΔΗΓΌΣ.md)\n",
        encoding="utf-8",
    )

    assert markdown_link_violations(["README.md"], tmp_path) == [
        "README.md:2: broken relative link: docs/GUIDE.md",
        "README.md:3: broken relative link: Docs/guide.md",
        "README.md:5: broken relative link: Docs/",
        "README.md:8: broken relative link: docs/ΟΔΗΓΌΣ.md",
    ]
