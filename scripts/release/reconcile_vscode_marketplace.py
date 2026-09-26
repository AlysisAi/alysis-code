#!/usr/bin/env python3
"""Plan and verify idempotent VS Code Marketplace target publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.vscode_extension_dogfood import SUPPORTED_PLATFORM_TARGETS  # noqa: E402
from scripts.release.vscode_release_channel import validate_vsix_channel  # noqa: E402

SCHEMA_NAME = "vscode-marketplace-publication-plan"
SCHEMA_VERSION = 2
EXTENSION_ID = "alysisai.vscode-alysis"
VSIX_ASSET_TYPE = "Microsoft.VisualStudio.Services.VSIXPackage"
PRE_RELEASE_PROPERTY = "Microsoft.VisualStudio.Code.PreRelease"


class MarketplaceReconciliationError(RuntimeError):
    """Marketplace state is ambiguous, conflicting, or not bound to candidates."""


def build_publication_plan(
    candidate_dir: Path,
    marketplace_payload: dict[str, Any],
    *,
    version: str,
    channel: str = "stable",
) -> dict[str, Any]:
    candidates = _candidate_inventory(candidate_dir, version=version, channel=channel)
    extension = _marketplace_extension(marketplace_payload)
    existing: dict[str, dict[str, str]] = {}
    if extension is not None:
        publisher = extension.get("publisher")
        publisher_name = publisher.get("publisherName") if isinstance(publisher, dict) else None
        if publisher_name != "alysisai" or extension.get("extensionName") != "vscode-alysis":
            raise MarketplaceReconciliationError(
                "Marketplace extension identity is not Alysis Code."
            )
        versions = extension.get("versions")
        if not isinstance(versions, list):
            raise MarketplaceReconciliationError("Marketplace version inventory is missing.")
        matching = [
            entry
            for entry in versions
            if isinstance(entry, dict) and entry.get("version") == version
        ]
        for entry in matching:
            target = entry.get("targetPlatform")
            if target not in SUPPORTED_PLATFORM_TARGETS:
                raise MarketplaceReconciliationError(
                    "Marketplace version contains an unsupported or generic target."
                )
            if target in existing:
                raise MarketplaceReconciliationError(
                    f"Marketplace version contains duplicate target {target}."
                )
            properties = entry.get("properties")
            pre_release = (
                any(
                    isinstance(prop, dict)
                    and prop.get("key") == PRE_RELEASE_PROPERTY
                    and str(prop.get("value", "true")).casefold() != "false"
                    for prop in properties
                )
                if isinstance(properties, list)
                else False
            )
            if pre_release != (channel == "beta"):
                raise MarketplaceReconciliationError(
                    f"Marketplace target {target} has a pre-release channel collision."
                )
            existing[target] = {
                "candidate": candidates[target]["filename"],
                "download_url": _vsix_download_url(entry),
                "sha256": candidates[target]["sha256"],
            }
    missing = [
        {"target": target, "candidate": candidates[target]["filename"]}
        for target in sorted(set(candidates) - set(existing))
    ]
    return {
        "schema_name": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "extension_id": EXTENSION_ID,
        "channel": channel,
        "version": version,
        "candidates": candidates,
        "existing": dict(sorted(existing.items())),
        "missing": missing,
    }


def verify_downloaded_packages(
    plan: dict[str, Any],
    downloaded_dir: Path,
    *,
    require_complete: bool = False,
    channel: str = "stable",
) -> None:
    if plan.get("schema_name") != SCHEMA_NAME or plan.get("schema_version") != SCHEMA_VERSION:
        raise MarketplaceReconciliationError("Marketplace publication plan schema is unsupported.")
    if channel not in {"stable", "beta"} or plan.get("channel") != channel:
        raise MarketplaceReconciliationError("Marketplace publication plan channel does not match.")
    candidates = plan.get("candidates")
    existing = plan.get("existing")
    missing = plan.get("missing")
    if (
        not isinstance(candidates, dict)
        or not isinstance(existing, dict)
        or not isinstance(missing, list)
    ):
        raise MarketplaceReconciliationError("Marketplace publication plan is malformed.")
    if require_complete and missing:
        raise MarketplaceReconciliationError("Marketplace publication is still missing targets.")
    expected_names = {
        value.get("candidate") for value in existing.values() if isinstance(value, dict)
    }
    if None in expected_names:
        raise MarketplaceReconciliationError("Marketplace publication plan has invalid filenames.")
    inventory = _regular_inventory(downloaded_dir)
    if set(inventory) != expected_names:
        raise MarketplaceReconciliationError(
            "Downloaded Marketplace package inventory does not match the observed targets."
        )
    for target, observed in existing.items():
        if not isinstance(observed, dict):
            raise MarketplaceReconciliationError("Marketplace existing-target record is invalid.")
        candidate = candidates.get(target)
        if not isinstance(candidate, dict):
            raise MarketplaceReconciliationError("Marketplace plan target has no candidate.")
        filename = observed.get("candidate")
        if not isinstance(filename, str) or hashlib.sha256(
            inventory[filename].read_bytes()
        ).hexdigest() != candidate.get("sha256"):
            raise MarketplaceReconciliationError(
                f"Published Marketplace target {target} differs from the exact candidate bytes."
            )


def _candidate_inventory(
    candidate_dir: Path, *, version: str, channel: str = "stable"
) -> dict[str, dict[str, str]]:
    paths = sorted(candidate_dir.glob("*.vsix"), key=lambda path: path.name)
    if len(paths) != len(SUPPORTED_PLATFORM_TARGETS):
        raise MarketplaceReconciliationError(
            "Candidate directory must contain exactly six VSIX files."
        )
    candidates: dict[str, dict[str, str]] = {}
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise MarketplaceReconciliationError(
                "Candidate VSIX inventory must contain regular files."
            )
        try:
            with ZipFile(path) as archive:
                package = json.loads(archive.read("extension/package.json"))
                manifest = archive.read("extension.vsixmanifest").decode("utf-8")
        except (BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MarketplaceReconciliationError(f"Candidate VSIX is invalid: {path.name}") from exc
        if not isinstance(package, dict) or (
            package.get("publisher"),
            package.get("name"),
            package.get("version"),
        ) != ("alysisai", "vscode-alysis", version):
            raise MarketplaceReconciliationError(
                f"Candidate VSIX identity/version is invalid: {path.name}"
            )
        target_match = re.search(r'<Identity\b[^>]*\bTargetPlatform="([^"]+)"', manifest)
        target = target_match.group(1) if target_match else ""
        if target not in SUPPORTED_PLATFORM_TARGETS or target in candidates:
            raise MarketplaceReconciliationError(
                f"Candidate VSIX target is unsupported or duplicated: {path.name}"
            )
        try:
            validate_vsix_channel(manifest, package, channel=channel)
        except ValueError as exc:
            raise MarketplaceReconciliationError(str(exc)) from exc
        expected_name = f"vscode-alysis-{target}.vsix"
        if path.name != expected_name:
            raise MarketplaceReconciliationError(
                f"Candidate VSIX filename does not match target {target}."
            )
        candidates[target] = {
            "filename": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    if set(candidates) != set(SUPPORTED_PLATFORM_TARGETS):
        raise MarketplaceReconciliationError("Candidate VSIX target coverage is incomplete.")
    return dict(sorted(candidates.items()))


def _marketplace_extension(payload: dict[str, Any]) -> dict[str, Any] | None:
    if "results" not in payload:
        return payload if payload else None
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise MarketplaceReconciliationError("Marketplace query response has invalid results.")
    extensions = results[0].get("extensions")
    if extensions == []:
        return None
    if (
        not isinstance(extensions, list)
        or len(extensions) != 1
        or not isinstance(extensions[0], dict)
    ):
        raise MarketplaceReconciliationError(
            "Marketplace query must resolve exactly one Alysis Code extension."
        )
    return extensions[0]


def _vsix_download_url(version: dict[str, Any]) -> str:
    files = version.get("files")
    source = None
    if isinstance(files, list):
        matches = [
            entry
            for entry in files
            if isinstance(entry, dict) and entry.get("assetType") == VSIX_ASSET_TYPE
        ]
        if len(matches) == 1 and isinstance(matches[0].get("source"), str):
            source = matches[0]["source"]
    if not source:
        asset_uri = version.get("assetUri")
        if isinstance(asset_uri, str) and asset_uri.strip():
            source = asset_uri.rstrip("/") + "/" + VSIX_ASSET_TYPE
    if not isinstance(source, str):
        raise MarketplaceReconciliationError("Marketplace target has no VSIX download asset.")
    parsed = urlsplit(source)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or (parsed.port not in (None, 443))
        or parsed.fragment
        or not (hostname == "marketplace.visualstudio.com" or hostname.endswith(".vsassets.io"))
    ):
        raise MarketplaceReconciliationError("Marketplace VSIX download URL is untrusted.")
    return source


def _regular_inventory(directory: Path) -> dict[str, Path]:
    try:
        entries = list(directory.iterdir())
    except OSError as exc:
        raise MarketplaceReconciliationError(
            "Downloaded Marketplace directory is unavailable."
        ) from exc
    if any(not entry.is_file() or entry.is_symlink() for entry in entries):
        raise MarketplaceReconciliationError(
            "Downloaded Marketplace inventory must contain regular files only."
        )
    return {entry.name: entry for entry in entries}


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarketplaceReconciliationError(f"{label} is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise MarketplaceReconciliationError(f"{label} must be a JSON object.")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("candidate_dir", type=Path)
    plan_parser.add_argument("marketplace_json", type=Path)
    plan_parser.add_argument("--version", required=True)
    plan_parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    plan_parser.add_argument("--output", type=Path, required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("plan", type=Path)
    verify_parser.add_argument("downloaded_dir", type=Path)
    verify_parser.add_argument("--require-complete", action="store_true")
    verify_parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            plan = build_publication_plan(
                args.candidate_dir,
                _load_json(args.marketplace_json, "Marketplace query response"),
                version=args.version,
                channel=args.channel,
            )
            args.output.write_text(
                json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"Marketplace publication plan has {len(plan['missing'])} missing target(s).")
        else:
            verify_downloaded_packages(
                _load_json(args.plan, "Marketplace publication plan"),
                args.downloaded_dir,
                require_complete=args.require_complete,
                channel=args.channel,
            )
            print("Marketplace package bytes match the exact candidates.")
    except MarketplaceReconciliationError as exc:
        print(f"VS Code Marketplace reconciliation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
