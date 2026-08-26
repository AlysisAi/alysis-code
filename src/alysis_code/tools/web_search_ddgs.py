from __future__ import annotations

import importlib.util
import threading
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from ..branding import env_get


class DdgsSearchError(RuntimeError):
    pass


# Keyless metasearch (DuckDuckGo and sibling engines via the `ddgs` package).
# This backend needs no API key, so it is the guaranteed last-resort external
# adapter: it keeps web_search available for chat providers with no hosted
# search (DeepSeek, Together, Fireworks, local models) and no external key.
KEYLESS_WEB_SEARCH_ENV = "ALYSIS_WEB_SEARCH_KEYLESS"
_KEYLESS_DISABLED_VALUES = frozenset({"0", "false", "off", "no", "disabled"})
_MAX_SNIPPET_CHARS = 500
_MAX_OVERFETCH_RESULTS = 50


def keyless_web_search_enabled() -> bool:
    raw = str(env_get(KEYLESS_WEB_SEARCH_ENV) or "").strip().lower()
    return raw not in _KEYLESS_DISABLED_VALUES


def ddgs_package_available() -> bool:
    try:
        return importlib.util.find_spec("ddgs") is not None
    except Exception:  # noqa: BLE001
        return False


def _truncate_snippet(raw_value: Any) -> str | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    return text[: _MAX_SNIPPET_CHARS - 3].rstrip() + "..."


def _source_url(row: dict[str, Any]) -> str:
    for key in ("href", "url", "link"):
        url = str(row.get(key) or "").strip()
        if url:
            return url
    return ""


def _source_snippet(row: dict[str, Any]) -> str | None:
    for key in ("body", "snippet", "description", "content"):
        snippet = _truncate_snippet(row.get(key))
        if snippet is not None:
            return snippet
    return None


def _url_matches_domains(url: str, domains: list[str]) -> bool:
    if not domains:
        return True
    try:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return False
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _dedupe_sources(raw_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in raw_sources:
        url = str(source.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _default_text_search(
    query: str,
    *,
    max_results: int,
    timeout_s: float,
) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except Exception as e:  # noqa: BLE001
        raise DdgsSearchError(
            "keyless web search requires the 'ddgs' package (pip install ddgs)"
        ) from e
    try:
        client = DDGS(timeout=max(1, int(timeout_s)))
        rows = client.text(query, max_results=max_results)
    except Exception as e:  # noqa: BLE001
        raise DdgsSearchError(f"keyless (ddgs) search failed: {e}") from e
    return [row for row in rows or [] if isinstance(row, dict)]


def _run_with_deadline(
    fn: Callable[[], list[dict[str, Any]]],
    *,
    timeout_s: float,
) -> list[dict[str, Any]]:
    # The ddgs client's timeout bounds each engine HTTP request, not the whole
    # metasearch fan-out, so enforce the overall web_search budget here.
    outcome: dict[str, Any] = {}

    def _target() -> None:
        try:
            outcome["value"] = fn()
        except BaseException as e:  # noqa: BLE001
            outcome["error"] = e

    worker = threading.Thread(target=_target, daemon=True, name="alysis-ddgs-search")
    worker.start()
    worker.join(timeout=max(1.0, float(timeout_s)))
    if worker.is_alive():
        raise DdgsSearchError(f"keyless (ddgs) search timed out after {timeout_s:.0f}s")
    if "error" in outcome:
        error = outcome["error"]
        if isinstance(error, DdgsSearchError):
            raise error
        raise DdgsSearchError(f"keyless (ddgs) search failed: {error}") from error
    value = outcome.get("value")
    return value if isinstance(value, list) else []


def ddgs_search(
    *,
    query: str,
    max_results: int = 5,
    include_domains: list[str] | None = None,
    timeout_s: float = 45.0,
    text_search_fn: Callable[..., list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    validated_query = str(query or "").strip()
    if not validated_query:
        raise DdgsSearchError("query must be a non-empty string.")

    try:
        validated_max_results = int(max_results)
    except (TypeError, ValueError) as e:
        raise DdgsSearchError("max_results must be an integer between 1 and 20.") from e
    if validated_max_results < 1 or validated_max_results > 20:
        raise DdgsSearchError("max_results must be an integer between 1 and 20.")

    validated_domains = [str(item or "").strip().lower() for item in include_domains or []]
    validated_domains = [item for item in validated_domains if item]

    # Domain filtering happens client-side, so over-fetch when a filter is set to
    # keep enough matching results after the cut.
    requested_results = validated_max_results
    if validated_domains:
        requested_results = min(max(validated_max_results * 5, 20), _MAX_OVERFETCH_RESULTS)

    search_fn = text_search_fn or _default_text_search
    rows = _run_with_deadline(
        lambda: search_fn(
            validated_query,
            max_results=requested_results,
            timeout_s=timeout_s,
        ),
        timeout_s=timeout_s,
    )

    raw_sources: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = _source_url(row)
        if not url or not _url_matches_domains(url, validated_domains):
            continue
        source: dict[str, Any] = {
            "url": url,
            "title": str(row.get("title") or "").strip(),
        }
        snippet = _source_snippet(row)
        if snippet is not None:
            source["snippet"] = snippet
        raw_sources.append(source)

    deduped_sources = _dedupe_sources(raw_sources)
    sources_truncated = len(deduped_sources) > validated_max_results
    final_sources = deduped_sources[:validated_max_results]

    # Zero surviving sources is a valid outcome the model can act on (broaden the
    # query, drop the domain filter) — mirror the Tavily contract and return an
    # empty result instead of raising, so a no-hit query never reads as a backend
    # failure that disables web tools for the turn.
    citations = [
        {
            "title": str(source.get("title") or "").strip(),
            "url": str(source.get("url") or "").strip(),
            "start_index": None,
            "end_index": None,
        }
        for source in final_sources
        if str(source.get("url") or "").strip()
    ]

    return {
        "query": validated_query,
        "answer": "",
        "citations": citations,
        "sources": final_sources,
        "queries": [validated_query],
        "model": None,
        "backend": "ddgs",
        "allowed_domains": validated_domains,
        "external_web_access": True,
        "response_id": None,
        "sources_truncated": sources_truncated,
    }
