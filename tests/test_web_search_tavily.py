from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.tools.web_search_tavily import TavilySearchError, tavily_search


def _public_resolver(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def test_tavily_search_returns_unified_contract_and_passes_include_domains() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.tavily.com/search"
        body = json.loads(request.content.decode("utf-8"))
        assert body["query"] == "httpx migration guide"
        assert body["include_answer"] is True
        assert body["max_results"] == 2
        assert body["include_domains"] == ["python-httpx.org", "github.com"]
        return httpx.Response(
            200,
            json={
                "request_id": "tavily_req_1",
                "answer": "Use the official migration guide first.",
                "results": [
                    {
                        "title": "HTTPX",
                        "url": "https://www.python-httpx.org/compatibility/",
                        "content": "Migration guide",
                    },
                    {
                        "title": "HTTPX duplicate",
                        "url": "https://www.python-httpx.org/compatibility/",
                        "content": "Duplicate source",
                    },
                    {
                        "title": "Release notes",
                        "url": "https://github.com/encode/httpx/releases",
                        "content": "Release notes",
                    },
                ],
            },
        )

    result = tavily_search(
        query="httpx migration guide",
        api_key="tavily-key",
        max_results=2,
        include_domains=["python-httpx.org", "github.com"],
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["backend"] == "tavily"
    assert result["model"] is None
    assert result["response_id"] == "tavily_req_1"
    assert result["external_web_access"] is True
    assert result["allowed_domains"] == ["python-httpx.org", "github.com"]
    assert result["queries"] == ["httpx migration guide"]
    assert result["answer"] == "Use the official migration guide first."
    assert result["sources_truncated"] is False
    assert result["sources"] == [
        {
            "url": "https://www.python-httpx.org/compatibility/",
            "title": "HTTPX",
            "snippet": "Migration guide",
        },
        {
            "url": "https://github.com/encode/httpx/releases",
            "title": "Release notes",
            "snippet": "Release notes",
        },
    ]
    assert result["citations"] == [
        {
            "title": "HTTPX",
            "url": "https://www.python-httpx.org/compatibility/",
            "start_index": None,
            "end_index": None,
        },
        {
            "title": "Release notes",
            "url": "https://github.com/encode/httpx/releases",
            "start_index": None,
            "end_index": None,
        },
    ]


def test_tavily_search_truncates_snippets_and_marks_source_truncation() -> None:
    long_snippet = "x" * 800

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "Many results",
                "results": [
                    {"title": "A", "url": "https://a.example.com", "content": long_snippet},
                    {"title": "B", "url": "https://b.example.com", "content": "short"},
                    {"title": "C", "url": "https://c.example.com", "content": "short"},
                ],
            },
        )

    result = tavily_search(
        query="many docs",
        api_key="tavily-key",
        max_results=2,
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["sources_truncated"] is True
    assert len(result["sources"]) == 2
    assert result["sources"][0]["snippet"].endswith("...")
    assert len(result["sources"][0]["snippet"]) <= 500


def test_tavily_search_handles_missing_or_partial_fields_gracefully() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [
                    {},
                    {"url": "https://docs.example.com/guide", "content": ""},
                ],
            },
        )

    result = tavily_search(
        query="example guide",
        api_key="tavily-key",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )

    assert result["answer"] == ""
    assert result["response_id"] is None
    assert result["queries"] == ["example guide"]
    assert result["sources"] == [{"url": "https://docs.example.com/guide", "title": ""}]
    assert result["citations"] == [
        {
            "title": "",
            "url": "https://docs.example.com/guide",
            "start_index": None,
            "end_index": None,
        }
    ]


def test_tavily_search_surfaces_http_errors_cleanly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "Invalid API key"})

    with pytest.raises(TavilySearchError, match="Tavily error 401: Invalid API key"):
        tavily_search(
            query="httpx docs",
            api_key="bad-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_tavily_search_surfaces_invalid_json_cleanly() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with pytest.raises(TavilySearchError, match="non-JSON"):
        tavily_search(
            query="httpx docs",
            api_key="tavily-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_tavily_search_surfaces_timeout_with_configured_limit() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out")

    with pytest.raises(
        TavilySearchError,
        match=r"Tavily search request timed out during response read .*overall=45s",
    ):
        tavily_search(
            query="httpx docs",
            api_key="tavily-key",
            transport=httpx.MockTransport(handler),
            resolver=_public_resolver,
        )


def test_tavily_search_requires_query_and_api_key() -> None:
    with pytest.raises(TavilySearchError, match="query must be a non-empty string"):
        tavily_search(query="   ", api_key="tavily-key")
    with pytest.raises(TavilySearchError, match="TAVILY_API_KEY is required"):
        tavily_search(query="httpx docs", api_key="")
