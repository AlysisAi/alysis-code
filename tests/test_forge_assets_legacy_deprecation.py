from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.forge import attach_asset, create_plan_run, load_plan


def test_attach_asset_emits_deprecation_warning(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    create_plan_run(repo)
    source = repo / "legacy.txt"
    source.write_text("legacy\n", encoding="utf-8")

    with pytest.warns(DeprecationWarning):
        _paths, metadata = attach_asset(repo, source)

    assert metadata["stored_path"]
    plan = load_plan(_paths, migrate_legacy=False)
    assert plan["schema_version"] == 1
