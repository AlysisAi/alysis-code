#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SOURCE = "bundled_chatgpt_codex_subscription_snapshot"
_DEFAULT_ENDPOINT = "https://chatgpt.com/backend-api/codex/models"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the reviewed ChatGPT Codex subscription metadata snapshot from a local "
            "model-discovery response. The snapshot is a capacity fallback only and never "
            "grants model entitlement."
        )
    )
    parser.add_argument("--input", required=True, help="Local ChatGPT Codex models JSON response.")
    parser.add_argument(
        "--client-version", required=True, help="Codex client compatibility version."
    )
    parser.add_argument(
        "--fetched-at",
        default=None,
        help="UTC timestamp recorded in the snapshot (default: current UTC time).",
    )
    parser.add_argument("--endpoint", default=_DEFAULT_ENDPOINT)
    return parser.parse_args(argv)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _snapshot_path() -> Path:
    return (
        _repo_root()
        / "src"
        / "alysis_code"
        / "model_catalog"
        / "chatgpt_codex_subscription_snapshot.json"
    )


def _clean_string(value: Any) -> str:
    return str(value or "").strip()


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, float):
            if not value.is_integer():
                return None
            parsed = int(value)
        else:
            parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _strict_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    clean = value.strip()
    try:
        parsed = datetime.strptime(clean, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise SystemExit("--fetched-at must use UTC format YYYY-MM-DDTHH:MM:SSZ.") from exc
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _input_modalities(raw: dict[str, Any]) -> list[str]:
    advertised = raw.get("input_modalities")
    if isinstance(advertised, list):
        values = list(
            dict.fromkeys(
                _clean_string(item).casefold() for item in advertised if _clean_string(item)
            )
        )
        if values:
            return values
    if raw.get("supports_image_input") is True:
        return ["text", "image"]
    return ["text"]


def _reasoning_efforts(raw: dict[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    advertised = raw.get("supported_reasoning_levels")
    if not isinstance(advertised, list):
        return result
    for item in advertised:
        if not isinstance(item, dict):
            continue
        effort_id = _clean_string(item.get("effort") or item.get("id"))
        if not effort_id:
            continue
        result.append(
            {
                "description": _clean_string(item.get("description")),
                "id": effort_id,
            }
        )
    return result


def _normalize_model(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    model_id = _clean_string(raw.get("slug") or raw.get("id"))
    visibility = _clean_string(raw.get("visibility") or "list").casefold()
    if not model_id or visibility in {"hide", "hidden"}:
        return None
    try:
        priority = int(raw.get("priority") or 9999)
    except (TypeError, ValueError):
        priority = 9999
    return {
        "context_window_tokens": _positive_int(raw.get("context_window")),
        "default_reasoning_effort": (_clean_string(raw.get("default_reasoning_level")) or None),
        "description": _clean_string(raw.get("description")),
        "id": model_id,
        "input_modalities": _input_modalities(raw),
        "label": _clean_string(raw.get("display_name")) or model_id,
        "max_output_tokens": _positive_int(raw.get("max_output_tokens")),
        "priority": priority,
        "reasoning_efforts": _reasoning_efforts(raw),
    }


def _normalize_catalog(raw: Any) -> list[dict[str, Any]]:
    raw_models = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(raw_models, list):
        raise SystemExit("Input JSON must contain a models array.")
    models = [model for item in raw_models if (model := _normalize_model(item)) is not None]
    if not models:
        raise SystemExit("Input JSON did not contain any visible models.")
    seen: set[str] = set()
    for model in models:
        folded = str(model["id"]).casefold()
        if folded in seen:
            raise SystemExit(f"Input JSON contains duplicate model id {model['id']!r}.")
        seen.add(folded)
    return sorted(
        models, key=lambda model: (model["priority"], model["label"].casefold(), model["id"])
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    input_path = Path(args.input).expanduser().resolve()
    try:
        input_bytes = input_path.read_bytes()
        raw = json.loads(input_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit("Could not read a valid ChatGPT Codex model catalog JSON file.") from exc
    payload = {
        "client_version": _clean_string(args.client_version),
        "endpoint": _clean_string(args.endpoint),
        "fetched_at_utc": _strict_timestamp(args.fetched_at),
        "input_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "models": _normalize_catalog(raw),
        "refresh_policy": "manual_reviewed_only",
        "schema_version": 1,
        "source": _SOURCE,
        "usage": "capacity_and_capability_fallback_only_not_entitlement",
    }
    if not payload["client_version"]:
        raise SystemExit("--client-version cannot be empty.")
    snapshot_path = _snapshot_path()
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {snapshot_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
