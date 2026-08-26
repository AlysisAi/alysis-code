from __future__ import annotations

from types import SimpleNamespace

from alysis_code.cli_impl.commands import forge_asset_view


def test_forge_asset_view_retries_empty_index_after_legacy_migration(monkeypatch) -> None:
    calls = 0

    class FakeAssetIndex:
        def __init__(self, _paths) -> None:
            pass

        def records(self, *, include_deleted: bool = False):
            nonlocal calls
            assert include_deleted is False
            calls += 1
            if calls == 1:
                return []
            return [
                SimpleNamespace(
                    id="ast_requirements",
                    original_filename="requirements.txt",
                    stored_path=".alysis/runs/r/assets/raw/ast_requirements/requirements.txt",
                    size_bytes=14,
                    added_by={"legacy_stored_path": ".alysis/runs/r/plan/assets/requirements.txt"},
                )
            ]

    monkeypatch.setattr(forge_asset_view, "AssetIndex", FakeAssetIndex)
    monkeypatch.setattr(forge_asset_view.time, "sleep", lambda _seconds: None)

    entries = forge_asset_view.forge_asset_view_entries(
        SimpleNamespace(assets_index_path="index.json"),
        {
            "schema_version": 2,
            "legacy_assets_migrated_at": "2026-06-05T00:00:00+00:00",
            "assets": [],
        },
    )

    assert calls == 2
    assert [entry.display_name for entry in entries] == ["requirements.txt"]
