#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_STRICT_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_NON_MODEL_TOP_LEVEL_KEYS = {"sample_spec"}
_POSITIVE_INT_FIELDS = ("max_tokens", "max_input_tokens", "max_output_tokens")
_NON_NEGATIVE_FLOAT_FIELDS = ("input_cost_per_token", "output_cost_per_token")
_BOOL_FIELDS = ("supports_vision",)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Refresh the bundled LiteLLM model catalog snapshot from a local JSON file. "
            "This script never runs automatically."
        )
    )
    parser.add_argument(
        "--input", required=True, help="Path to model_prices_and_context_window.json"
    )
    parser.add_argument(
        "--upstream-repo",
        default="https://github.com/BerriAI/litellm",
        help="Upstream repository URL recorded in the provenance file.",
    )
    parser.add_argument(
        "--upstream-path",
        default="model_prices_and_context_window.json",
        help="Upstream file path recorded in the provenance file.",
    )
    parser.add_argument(
        "--upstream-ref",
        default=None,
        help=(
            "Optional reviewer note for the upstream ref. Commit-specific refs are accepted as-is; "
            "floating refs require --allow-floating-ref."
        ),
    )
    parser.add_argument(
        "--upstream-commit-sha",
        required=True,
        help="Exact upstream commit SHA for the snapshot input.",
    )
    parser.add_argument(
        "--fetched-at",
        default=None,
        help="Override the fetched-at UTC timestamp (default: current UTC time).",
    )
    parser.add_argument(
        "--allow-floating-ref",
        action="store_true",
        help="Allow a non-commit-specific --upstream-ref for reviewer context.",
    )
    return parser.parse_args(argv)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _snapshot_paths() -> tuple[Path, Path]:
    catalog_dir = _repo_root() / "src" / "alysis_code" / "model_catalog"
    return (
        catalog_dir / "litellm_model_prices_snapshot.json",
        catalog_dir / "litellm_model_prices_snapshot.meta.json",
    )


def _parse_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float):
            if not value.is_integer():
                return None
            parsed = int(value)
        else:
            text = str(value).strip()
            try:
                parsed = int(text)
            except ValueError:
                as_float = float(text)
                if not as_float.is_integer():
                    return None
                parsed = int(as_float)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _parse_non_negative_float(value: Any) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return None


def _require_full_commit_sha(value: Any) -> str:
    clean = str(value or "").strip().lower()
    if not _FULL_SHA_RE.fullmatch(clean):
        raise SystemExit("--upstream-commit-sha must be a full 40-character hex SHA.")
    return clean


def _require_strict_utc_timestamp(value: Any) -> str:
    clean = str(value or "").strip()
    if not _STRICT_UTC_TIMESTAMP_RE.fullmatch(clean):
        raise SystemExit("--fetched-at must use strict UTC ISO format YYYY-MM-DDTHH:MM:SSZ.")
    try:
        datetime.strptime(clean, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SystemExit(
            "--fetched-at must use strict UTC ISO format YYYY-MM-DDTHH:MM:SSZ."
        ) from exc
    return clean


def _normalize_upstream_ref(value: Any, *, allow_floating_ref: bool) -> str | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        raise SystemExit("--upstream-ref cannot be empty.")
    lowered = clean.casefold()
    if _FULL_SHA_RE.fullmatch(lowered):
        return lowered
    if re.fullmatch(r"commit/[0-9a-f]{40}", lowered):
        return lowered
    if not allow_floating_ref:
        raise SystemExit(
            "--upstream-ref must be commit-specific unless --allow-floating-ref is set."
        )
    return clean


def _normalize_github_repo_url(value: str) -> str | None:
    clean = value.strip().rstrip("/")
    if clean.endswith(".git"):
        clean = clean[:-4]
    slug: str | None = None
    if clean.startswith("git@github.com:"):
        slug = clean.split(":", 1)[1]
    elif clean.startswith("https://github.com/"):
        slug = clean.removeprefix("https://github.com/")
    elif clean.startswith("http://github.com/"):
        slug = clean.removeprefix("http://github.com/")
    if slug is None or "/" not in slug:
        return None
    owner, repo = slug.split("/", 1)
    if not owner or not repo:
        return None
    return f"https://github.com/{owner}/{repo}"


def _build_github_blob_url(*, repo_url: str, upstream_path: str, commit_sha: str) -> str | None:
    normalized_repo = _normalize_github_repo_url(repo_url)
    normalized_path = str(upstream_path or "").strip().lstrip("/")
    if normalized_repo is None or not normalized_path:
        return None
    return f"{normalized_repo}/blob/{commit_sha}/{normalized_path}"


def _validate_model_catalog(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        raise SystemExit("Input JSON must be a top-level object.")

    model_entries = 0
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise SystemExit("Input JSON keys must be strings.")
        if not isinstance(value, dict):
            raise SystemExit(f"Input JSON entry {key!r} must be an object.")
        normalized[key] = value
        if key.casefold() in _NON_MODEL_TOP_LEVEL_KEYS:
            continue
        model_entries += 1
        for field_name in _POSITIVE_INT_FIELDS:
            if field_name not in value or value[field_name] is None:
                continue
            if _parse_non_negative_int(value[field_name]) is None:
                raise SystemExit(
                    f"Input JSON entry {key!r} has invalid {field_name!r}; expected a non-negative integer."
                )
        for field_name in _NON_NEGATIVE_FLOAT_FIELDS:
            if field_name not in value or value[field_name] is None:
                continue
            if _parse_non_negative_float(value[field_name]) is None:
                raise SystemExit(
                    f"Input JSON entry {key!r} has invalid {field_name!r}; expected a non-negative number."
                )
        for field_name in _BOOL_FIELDS:
            if field_name not in value or value[field_name] is None:
                continue
            if _parse_bool(value[field_name]) is None:
                raise SystemExit(
                    f"Input JSON entry {key!r} has invalid {field_name!r}; expected a boolean."
                )

    if model_entries <= 0:
        raise SystemExit("Input JSON must contain at least one model entry.")
    return normalized


def _read_and_validate_json(path: Path) -> tuple[bytes, dict[str, dict[str, Any]]]:
    payload = path.read_bytes()
    raw = json.loads(payload)
    return payload, _validate_model_catalog(raw)


def _iso_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_provenance_metadata(
    *,
    upstream_repo: str,
    upstream_path: str,
    upstream_commit_sha: str,
    upstream_ref: str | None,
    fetched_at_utc: str,
    payload: bytes,
    raw: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    upstream: dict[str, Any] = {
        "commit_sha": upstream_commit_sha,
        "path": upstream_path,
        "repo": upstream_repo,
    }
    blob_url = _build_github_blob_url(
        repo_url=upstream_repo,
        upstream_path=upstream_path,
        commit_sha=upstream_commit_sha,
    )
    if blob_url is not None:
        upstream["blob_url"] = blob_url
    if upstream_ref is not None:
        upstream["ref"] = upstream_ref
    return {
        "refresh_policy": "manual_reviewed_only",
        "schema_version": 1,
        "snapshot": {
            "bundled_json_sha256": hashlib.sha256(payload).hexdigest(),
            "bundled_json_size_bytes": len(payload),
            "fetched_at_utc": fetched_at_utc,
            "top_level_entry_count": len(raw),
        },
        "source": "bundled_litellm_snapshot",
        "upstream": upstream,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    input_path = Path(args.input).expanduser().resolve()
    snapshot_path, meta_path = _snapshot_paths()

    payload, raw = _read_and_validate_json(input_path)
    upstream_commit_sha = _require_full_commit_sha(args.upstream_commit_sha)
    fetched_at = _require_strict_utc_timestamp(args.fetched_at or _iso_now())
    upstream_ref = _normalize_upstream_ref(
        args.upstream_ref,
        allow_floating_ref=bool(args.allow_floating_ref),
    )
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_bytes(payload)

    meta = _build_provenance_metadata(
        upstream_repo=str(args.upstream_repo).strip(),
        upstream_path=str(args.upstream_path).strip(),
        upstream_commit_sha=upstream_commit_sha,
        upstream_ref=upstream_ref,
        fetched_at_utc=fetched_at,
        payload=payload,
        raw=raw,
    )
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Updated {snapshot_path}")
    print(f"Updated {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
