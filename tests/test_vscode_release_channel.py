from __future__ import annotations

import pytest

from scripts.release.vscode_release_channel import (
    validate_release_version,
    validate_stable_vsix,
    validate_vsix_channel,
)


def test_candidate_version_channel_mismatch_is_rejected_before_signing() -> None:
    validate_release_version("0.3.0", channel="beta")
    validate_release_version("0.4.0", channel="stable")
    with pytest.raises(ValueError, match="odd minor"):
        validate_release_version("0.3.0", channel="stable")
    with pytest.raises(ValueError, match="odd minor"):
        validate_release_version("0.4.0", channel="beta")


def test_beta_is_explicit_and_cannot_be_promoted_as_stable() -> None:
    manifest = '<PackageManifest><Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="true"/></PackageManifest>'
    package = {"version": "0.3.0", "preview": False}
    validate_vsix_channel(manifest, package, channel="beta")
    with pytest.raises(ValueError, match="pre-release marker"):
        validate_stable_vsix(manifest, package)
    with pytest.raises(ValueError, match="pre-release marker"):
        validate_vsix_channel("<PackageManifest/>", package, channel="beta")


@pytest.mark.parametrize("version", ["0.2.0", "0.3.0-beta", "03.3.0", "0.3"])
def test_beta_requires_a_distinct_numeric_version(version: str) -> None:
    manifest = '<PackageManifest><Property Id="Microsoft.VisualStudio.Code.PreRelease" Value="true"/></PackageManifest>'
    with pytest.raises(ValueError, match="version"):
        validate_vsix_channel(manifest, {"version": version}, channel="beta")


@pytest.mark.parametrize(
    "marker", ["", '<Property Value="false" Id="Microsoft.VisualStudio.Code.PreRelease"/>']
)
def test_stable_channel_accepts_absent_or_false_marker(marker: str) -> None:
    validate_stable_vsix(
        f'<PackageManifest xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">{marker}</PackageManifest>',
        {"preview": False},
    )


@pytest.mark.parametrize(
    "marker",
    [
        '<Property Value="true" Id="Microsoft.VisualStudio.Code.PreRelease"/>',
        '<Property Id="Microsoft.VisualStudio.Code.PreRelease"/>',
        '<Property Value="false" Id="Microsoft.VisualStudio.Code.PreRelease"/>' * 2,
    ],
)
def test_stable_channel_rejects_prerelease_and_ambiguous_metadata(marker: str) -> None:
    with pytest.raises(ValueError, match="pre-release marker"):
        validate_stable_vsix(f"<PackageManifest>{marker}</PackageManifest>", {})


@pytest.mark.parametrize("preview", [True, "false", None, 0])
def test_stable_channel_rejects_preview_and_malformed_preview(preview: object) -> None:
    with pytest.raises(ValueError, match="marked preview"):
        validate_stable_vsix("<PackageManifest/>", {"preview": preview})
