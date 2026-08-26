from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ..safety import SafeHttpError, safe_http_request
from ..safety.safe_http import Resolver
from .http_timeout import build_http_timeout_budget, format_http_timeout_error


class TavilySearchError(RuntimeError):
    pass


_TAVILY_SEARCH_URL = "https://api.tavily.com/search"
_MAX_SNIPPET_CHARS = 500


def _extract_error_message(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    for key in ("error", "message", "detail"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = str(value.get("message") or value.get("detail") or "").strip()
            if nested:
                return nested
    return None


def _truncate_snippet(raw_value: Any) -> str | None:
    text = str(raw_value or "").strip()
    if not text:
        return None
    if len(text) <= _MAX_SNIPPET_CHARS:
        return text
    return text[: _MAX_SNIPPET_CHARS - 3].rstrip() + "..."


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


def tavily_search(
    *,
    query: str,
    api_key: str,
    max_results: int = 5,
    include_domains: list[str] | None = None,
    timeout_s: float = 45.0,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    validated_query = str(query or "").strip()
    if not validated_query:
        raise TavilySearchError("query must be a non-empty string.")

    validated_api_key = str(api_key or "").strip()
    if not validated_api_key:
        raise TavilySearchError("TAVILY_API_KEY is required for Tavily web_search.")

    try:
        validated_max_results = int(max_results)
    except (TypeError, ValueError) as e:
        raise TavilySearchError("max_results must be an integer between 1 and 20.") from e
    if validated_max_results < 1 or validated_max_results > 20:
        raise TavilySearchError("max_results must be an integer between 1 and 20.")

    validated_domains = [str(item or "").strip().lower() for item in include_domains or []]
    validated_domains = [item for item in validated_domains if item]

    payload: dict[str, Any] = {
        "api_key": validated_api_key,
        "query": validated_query,
        "max_results": validated_max_results,
        "include_answer": True,
    }
    if validated_domains:
        payload["include_domains"] = list(validated_domains)

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "alysis-code",
    }
    timeout_budget = build_http_timeout_budget(timeout_s, profile="search")

    try:
        response = asyncio.run(
            safe_http_request(
                "POST",
                _TAVILY_SEARCH_URL,
                timeout=timeout_s,
                headers=headers,
                json=payload,
                _transport=transport,  # type: ignore[arg-type]
                _resolver=resolver,
            )
        )
    except httpx.TimeoutException as e:
        raise TavilySearchError(
            format_http_timeout_error(
                operation="Tavily search request",
                budget=timeout_budget,
                error=e,
            )
        ) from e
    except SafeHttpError as e:
        raise TavilySearchError(f"Tavily request blocked: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise TavilySearchError(f"Tavily request failed: {e}") from e

    try:
        data = response.json()
    except Exception as e:  # noqa: BLE001
        if response.status_code >= 400:
            body = response.text
            if len(body) > 1000:
                body = body[:1000] + "...(truncated)"
            raise TavilySearchError(f"Tavily error {response.status_code}: {body}") from e
        raise TavilySearchError("Tavily API returned non-JSON response") from e

    if not isinstance(data, dict):
        raise TavilySearchError("Unexpected Tavily payload: expected JSON object")

    if response.status_code >= 400:
        message = _extract_error_message(data)
        if message:
            raise TavilySearchError(f"Tavily error {response.status_code}: {message}")
        raise TavilySearchError(f"Tavily error {response.status_code}: {data!r}")

    raw_results = data.get("results")
    normalized_results = raw_results if isinstance(raw_results, list) else []

    raw_sources: list[dict[str, Any]] = []
    for result in normalized_results:
        if not isinstance(result, dict):
            continue
        url = str(result.get("url") or "").strip()
        if not url:
            continue
        source: dict[str, Any] = {
            "url": url,
            "title": str(result.get("title") or "").strip(),
        }
        snippet = _truncate_snippet(
            result.get("content") or result.get("snippet") or result.get("raw_content")
        )
        if snippet is not None:
            source["snippet"] = snippet
        raw_sources.append(source)

    deduped_sources = _dedupe_sources(raw_sources)
    sources_truncated = len(deduped_sources) > validated_max_results
    final_sources = deduped_sources[:validated_max_results]

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

    response_id = str(data.get("request_id") or data.get("id") or "").strip() or None
    answer = str(data.get("answer") or "").strip()

    return {
        "query": validated_query,
        "answer": answer,
        "citations": citations,
        "sources": final_sources,
        "queries": [validated_query],
        "model": None,
        "backend": "tavily",
        "allowed_domains": validated_domains,
        "external_web_access": True,
        "response_id": response_id,
        "sources_truncated": sources_truncated,
    }
