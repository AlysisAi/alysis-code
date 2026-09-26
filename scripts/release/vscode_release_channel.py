"""Explicit channel contract shared by candidate and Marketplace validation."""

from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree

PRE_RELEASE_PROPERTY = "Microsoft.VisualStudio.Code.PreRelease"


def validate_release_version(version: object, *, channel: str) -> None:
    """Check a new candidate's channel before any signing credentials are used."""
    if channel not in {"stable", "beta"}:
        raise ValueError("Release channel must be stable or beta.")
    if not isinstance(version, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", version
    ):
        raise ValueError("Release requires a numeric major.minor.patch version.")
    if (int(version.split(".")[1]) % 2 == 1) != (channel == "beta"):
        raise ValueError("Use an odd minor version for beta and an even minor version for stable.")


def validate_stable_vsix(manifest: str, package: dict[str, Any]) -> None:
    """Reject preview packages, pre-release markers, and ambiguous channel metadata."""
    validate_vsix_channel(manifest, package, channel="stable")


def validate_vsix_channel(
    manifest: str, package: dict[str, Any], *, channel: str = "stable"
) -> None:
    if channel not in {"stable", "beta"}:
        raise ValueError("Release channel must be stable or beta.")
    try:
        root = ElementTree.fromstring(manifest)
    except ElementTree.ParseError as exc:
        raise ValueError("VSIX manifest is not valid XML.") from exc
    markers = [
        element.get("Value")
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Property"
        and element.get("Id") == PRE_RELEASE_PROPERTY
    ]
    if channel == "beta":
        if markers != ["true"]:
            raise ValueError("Beta VSIX must carry exactly one true pre-release marker.")
        validate_release_version(package.get("version"), channel=channel)
        return
    if len(markers) > 1 or (markers and markers != ["false"]):
        raise ValueError("Stable VSIX must not carry a pre-release marker.")
    if package.get("preview", False) is not False:
        raise ValueError("Stable VSIX must not be marked preview.")
