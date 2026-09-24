from __future__ import annotations

from pathlib import Path

import pytest

from scripts.release.reconcile_github_release_assets import (
    ReleaseAssetReconciliationError,
    reconcile_release_assets,
)


def _bundle(path: Path) -> None:
    path.mkdir()
    (path / "manifest.json").write_text("manifest", encoding="utf-8")
    (path / "alysis-linux-x64").write_bytes(b"runtime")


def test_release_asset_reconciliation_lists_only_missing_assets(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _bundle(local)
    remote.mkdir()
    (remote / "manifest.json").write_text("manifest", encoding="utf-8")

    assert reconcile_release_assets(local, remote) == ("alysis-linux-x64",)


def test_release_asset_reconciliation_accepts_exact_retry(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _bundle(local)
    _bundle(remote)

    assert reconcile_release_assets(local, remote, require_complete=True) == ()


def test_release_asset_reconciliation_refuses_conflicting_existing_asset(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _bundle(local)
    _bundle(remote)
    (remote / "manifest.json").write_text("substituted", encoding="utf-8")

    with pytest.raises(ReleaseAssetReconciliationError, match="will not be overwritten"):
        reconcile_release_assets(local, remote)


def test_release_asset_reconciliation_rejects_unexpected_download(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _bundle(local)
    remote.mkdir()
    (remote / "unexpected").write_text("x", encoding="utf-8")

    with pytest.raises(ReleaseAssetReconciliationError, match="unexpected assets"):
        reconcile_release_assets(local, remote)


def test_release_asset_reconciliation_requires_complete_post_upload(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _bundle(local)
    remote.mkdir()

    with pytest.raises(ReleaseAssetReconciliationError, match="publication is incomplete"):
        reconcile_release_assets(local, remote, require_complete=True)


def test_release_asset_reconciliation_rejects_symlinks(tmp_path: Path) -> None:
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    _bundle(local)
    remote.mkdir()
    target = remote / "target"
    target.write_text("manifest", encoding="utf-8")
    try:
        (remote / "manifest.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(ReleaseAssetReconciliationError, match="regular files"):
        reconcile_release_assets(local, remote)
