from __future__ import annotations

import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from scripts.qa.vscode_extension_dogfood import SUPPORTED_PLATFORM_TARGETS
from scripts.release.reconcile_vscode_marketplace import (
    MarketplaceReconciliationError,
    build_publication_plan,
    verify_downloaded_packages,
)

VERSION = "1.2.3"


def _candidates(tmp_path: Path, *, beta: bool = False) -> Path:
    root = tmp_path / "candidates"
    root.mkdir()
    for target in sorted(SUPPORTED_PLATFORM_TARGETS):
        with ZipFile(root / f"vscode-alysis-{target}.vsix", "w") as archive:
            archive.writestr(
                "extension/package.json",
                json.dumps(
                    {
                        "publisher": "alysisai",
                        "name": "vscode-alysis",
                        "version": "0.3.0" if beta else VERSION,
                    }
                ),
            )
            archive.writestr(
                "extension.vsixmanifest",
                (
                    f'<PackageManifest><Identity TargetPlatform="{target}" />'
                    + (
                        '<Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="true"/>'
                        if beta
                        else ""
                    )
                    + "</PackageManifest>"
                ),
            )
    return root


def _version(target: str, *, pre_release: bool = False) -> dict[str, object]:
    return {
        "version": VERSION,
        "targetPlatform": target,
        "properties": (
            [{"key": "Microsoft.VisualStudio.Code.PreRelease", "value": "true"}]
            if pre_release
            else []
        ),
        "files": [
            {
                "assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
                "source": f"https://alysisai.gallerycdn.vsassets.io/{target}/package",
            }
        ],
    }


def _payload(*versions: dict[str, object]) -> dict[str, object]:
    return {
        "results": [
            {
                "extensions": [
                    {
                        "publisher": {"publisherName": "alysisai"},
                        "extensionName": "vscode-alysis",
                        "versions": list(versions),
                    }
                ]
            }
        ]
    }


def test_marketplace_plan_starts_with_all_targets_missing(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    plan = build_publication_plan(candidates, {"results": [{"extensions": []}]}, version=VERSION)

    assert {entry["target"] for entry in plan["missing"]} == set(SUPPORTED_PLATFORM_TARGETS)
    assert plan["existing"] == {}


def test_beta_plan_requires_beta_markers_and_exact_published_bytes(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path, beta=True)
    entry = _version("win32-x64", pre_release=True)
    entry["version"] = "0.3.0"
    plan = build_publication_plan(candidates, _payload(entry), version="0.3.0", channel="beta")
    assert plan["channel"] == "beta"
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    filename = "vscode-alysis-win32-x64.vsix"
    (downloaded / filename).write_bytes((candidates / filename).read_bytes())
    verify_downloaded_packages(plan, downloaded, channel="beta")
    with pytest.raises(MarketplaceReconciliationError, match="channel"):
        verify_downloaded_packages(plan, downloaded)
    entry["properties"] = []
    with pytest.raises(MarketplaceReconciliationError, match="channel collision"):
        build_publication_plan(candidates, _payload(entry), version="0.3.0", channel="beta")
    (downloaded / filename).write_bytes(b"tampered beta")
    with pytest.raises(MarketplaceReconciliationError, match="exact candidate"):
        verify_downloaded_packages(plan, downloaded, channel="beta")


def test_marketplace_partial_retry_accepts_only_exact_existing_bytes(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    existing_target = "linux-x64"
    plan = build_publication_plan(candidates, _payload(_version(existing_target)), version=VERSION)
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    candidate_name = f"vscode-alysis-{existing_target}.vsix"
    (downloaded / candidate_name).write_bytes((candidates / candidate_name).read_bytes())

    verify_downloaded_packages(plan, downloaded)
    assert existing_target not in {entry["target"] for entry in plan["missing"]}


def test_marketplace_partial_retry_rejects_conflicting_existing_bytes(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    target = "darwin-arm64"
    plan = build_publication_plan(candidates, _payload(_version(target)), version=VERSION)
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    (downloaded / f"vscode-alysis-{target}.vsix").write_bytes(b"substituted")

    with pytest.raises(MarketplaceReconciliationError, match="differs from the exact candidate"):
        verify_downloaded_packages(plan, downloaded)


def test_marketplace_plan_rejects_prerelease_target_collision(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)

    with pytest.raises(MarketplaceReconciliationError, match="pre-release channel collision"):
        build_publication_plan(
            candidates,
            _payload(_version("win32-x64", pre_release=True)),
            version=VERSION,
        )


def test_marketplace_plan_rejects_duplicate_target(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    entry = _version("linux-x64")

    with pytest.raises(MarketplaceReconciliationError, match="duplicate target"):
        build_publication_plan(candidates, _payload(entry, entry), version=VERSION)


def test_marketplace_post_publish_requires_all_targets(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    plan = build_publication_plan(candidates, _payload(_version("linux-x64")), version=VERSION)
    downloaded = tmp_path / "downloaded"
    downloaded.mkdir()
    filename = "vscode-alysis-linux-x64.vsix"
    (downloaded / filename).write_bytes((candidates / filename).read_bytes())

    with pytest.raises(MarketplaceReconciliationError, match="still missing targets"):
        verify_downloaded_packages(plan, downloaded, require_complete=True)


def test_marketplace_plan_rejects_untrusted_download_host(tmp_path: Path) -> None:
    candidates = _candidates(tmp_path)
    entry = _version("linux-x64")
    entry["files"][0]["source"] = "https://example.com/package"

    with pytest.raises(MarketplaceReconciliationError, match="untrusted"):
        build_publication_plan(candidates, _payload(entry), version=VERSION)
