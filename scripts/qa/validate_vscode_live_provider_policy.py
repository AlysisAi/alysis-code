#!/usr/bin/env python3
"""Validate the source-controlled credential-destination policy for live VS Code QA."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.qa.validate_vscode_installed_live_provider import (  # noqa: E402
    InstalledLiveProviderEvidenceError,
    _provider_origin,
)

SCHEMA_NAME = "vscode-live-provider-origin-policy"
SCHEMA_VERSION = 1
ROOT_KEYS = {"schema_name", "schema_version", "entries"}
ENTRY_KEYS = {"provider", "origins"}
PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class LiveProviderPolicyError(ValueError):
    """Raised when the committed QA destination policy is unsafe or does not authorize a run."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_policy(path: Path) -> tuple[dict[str, tuple[str, ...]], str]:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise LiveProviderPolicyError(
            "Live-provider policy must be an existing absolute regular file."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveProviderPolicyError("Live-provider policy is not valid JSON.") from exc
    if not isinstance(payload, dict) or set(payload) != ROOT_KEYS:
        raise LiveProviderPolicyError("Live-provider policy fields are not exact.")
    if payload.get("schema_name") != SCHEMA_NAME or payload.get("schema_version") != SCHEMA_VERSION:
        raise LiveProviderPolicyError("Live-provider policy schema is unsupported.")
    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        raise LiveProviderPolicyError("Live-provider policy entries must be a non-empty list.")

    result: dict[str, tuple[str, ...]] = {}
    rendered_providers: list[str] = []
    for value in entries:
        if not isinstance(value, dict) or set(value) != ENTRY_KEYS:
            raise LiveProviderPolicyError("Live-provider policy entry fields are not exact.")
        provider = value.get("provider")
        if not isinstance(provider, str) or PROVIDER_RE.fullmatch(provider) is None:
            raise LiveProviderPolicyError("Live-provider policy provider is invalid.")
        if provider in result:
            raise LiveProviderPolicyError("Live-provider policy providers must be unique.")
        raw_origins = value.get("origins")
        if not isinstance(raw_origins, list) or not raw_origins:
            raise LiveProviderPolicyError("Live-provider policy origins must be non-empty lists.")
        origins: list[str] = []
        for origin in raw_origins:
            try:
                origins.append(_provider_origin(origin))
            except InstalledLiveProviderEvidenceError as exc:
                raise LiveProviderPolicyError(str(exc)) from exc
        if origins != sorted(set(origins)):
            raise LiveProviderPolicyError("Live-provider policy origins must be sorted and unique.")
        result[provider] = tuple(origins)
        rendered_providers.append(provider)
    if rendered_providers != sorted(rendered_providers):
        raise LiveProviderPolicyError("Live-provider policy entries must be sorted by provider.")
    return result, sha256_file(path)


def authorize(path: Path, *, provider: str, origin: str) -> str:
    policy, digest = load_policy(path)
    if provider not in policy or origin not in policy[provider]:
        raise LiveProviderPolicyError(
            "Provider/origin is not authorized by the source-controlled live-provider policy."
        )
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("policy", type=Path)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args(argv)
    try:
        digest = authorize(
            args.policy.absolute(),
            provider=args.provider,
            origin=args.origin,
        )
    except LiveProviderPolicyError as exc:
        parser.error(str(exc))
    print(json.dumps({"policy_sha256": digest}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
