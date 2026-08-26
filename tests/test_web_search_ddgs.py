from __future__ import annotations

from typing import Any

import pytest

from alysis_code.tools.web_search_ddgs import (
    DdgsSearchError,
    ddgs_package_available,
    ddgs_search,
    keyless_web_search_enabled,
)


def _fake_rows(*urls: str) -> list[dict[str, Any]]:
    return [
        {"title": f"Result {index}", "href": url, "body": f"Snippet for {url}"}
        for index, url in enumerate(urls)
    ]


def test_ddgs_search_returns_stable_result_contract() -> None:
    def _search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        assert query == "python release notes"
        assert max_results == 2
        assert timeout_s == 30.0
        return _fake_rows(
            "https://python.org/downloads",
            "https://docs.python.org/3/whatsnew/",
        )

    result = ddgs_search(
        query="python release notes",
        max_results=2,
        timeout_s=30.0,
        text_search_fn=_search,
    )

    assert result == {
        "query": "python release notes",
        "answer": "",
        "citations": [
            {
                "title": "Result 0",
                "url": "https://python.org/downloads",
                "start_index": None,
                "end_index": None,
            },
            {
                "title": "Result 1",
                "url": "https://docs.python.org/3/whatsnew/",
                "start_index": None,
                "end_index": None,
            },
        ],
        "sources": [
            {
                "url": "https://python.org/downloads",
                "title": "Result 0",
                "snippet": "Snippet for https://python.org/downloads",
            },
            {
                "url": "https://docs.python.org/3/whatsnew/",
                "title": "Result 1",
                "snippet": "Snippet for https://docs.python.org/3/whatsnew/",
            },
        ],
        "queries": ["python release notes"],
        "model": None,
        "backend": "ddgs",
        "allowed_domains": [],
        "external_web_access": True,
        "response_id": None,
        "sources_truncated": False,
    }


def test_ddgs_search_filters_to_allowed_domains_and_overfetches() -> None:
    seen: dict[str, Any] = {}

    def _search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        seen["max_results"] = max_results
        return _fake_rows(
            "https://spam.example.net/one",
            "https://docs.python.org/3/library/",
            "https://sub.python.org/nested",
            "https://python.org.evil.com/phish",
        )

    result = ddgs_search(
        query="python docs",
        max_results=5,
        include_domains=["python.org"],
        text_search_fn=_search,
    )

    assert seen["max_results"] == 25
    urls = [source["url"] for source in result["sources"]]
    assert urls == [
        "https://docs.python.org/3/library/",
        "https://sub.python.org/nested",
    ]
    assert result["allowed_domains"] == ["python.org"]


def test_ddgs_search_dedupes_and_truncates_sources() -> None:
    def _search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        return _fake_rows(
            "https://a.example.com/",
            "https://a.example.com/",
            "https://b.example.com/",
            "https://c.example.com/",
        )

    result = ddgs_search(query="dupes", max_results=2, text_search_fn=_search)

    urls = [source["url"] for source in result["sources"]]
    assert urls == ["https://a.example.com/", "https://b.example.com/"]
    assert result["sources_truncated"] is True


def test_ddgs_search_rejects_invalid_inputs() -> None:
    def _search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        raise AssertionError("search must not run on invalid input")

    with pytest.raises(DdgsSearchError, match="query must be a non-empty string"):
        ddgs_search(query="   ", text_search_fn=_search)
    with pytest.raises(DdgsSearchError, match="max_results must be an integer between 1 and 20"):
        ddgs_search(query="ok", max_results=0, text_search_fn=_search)
    with pytest.raises(DdgsSearchError, match="max_results must be an integer between 1 and 20"):
        ddgs_search(query="ok", max_results=21, text_search_fn=_search)


def test_ddgs_search_returns_empty_result_when_no_sources_survive() -> None:
    """Zero hits is a model-fixable outcome, not a backend failure (Tavily parity)."""

    def _search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        return []

    result = ddgs_search(query="nothing", text_search_fn=_search)

    assert result["sources"] == []
    assert result["citations"] == []
    assert result["sources_truncated"] is False
    assert result["backend"] == "ddgs"


def test_ddgs_search_enforces_overall_deadline() -> None:
    import time

    def _slow_search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        time.sleep(5.0)
        return _fake_rows("https://late.example.com/")

    with pytest.raises(DdgsSearchError, match="timed out after 1s"):
        ddgs_search(query="slow", timeout_s=1.0, text_search_fn=_slow_search)


def test_ddgs_search_accepts_alternate_row_keys() -> None:
    def _search(query: str, *, max_results: int, timeout_s: float) -> list[dict[str, Any]]:
        return [
            {"title": "Alt", "url": "https://alt.example.com/", "snippet": "alt snippet"},
            {"title": "Link", "link": "https://link.example.com/", "description": "desc"},
        ]

    result = ddgs_search(query="alt keys", max_results=5, text_search_fn=_search)

    assert [source["url"] for source in result["sources"]] == [
        "https://alt.example.com/",
        "https://link.example.com/",
    ]
    assert result["sources"][0]["snippet"] == "alt snippet"
    assert result["sources"][1]["snippet"] == "desc"


def test_keyless_toggle_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_KEYLESS", raising=False)
    assert keyless_web_search_enabled() is True
    for disabled in ("0", "false", "off", "no", "disabled"):
        monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", disabled)
        assert keyless_web_search_enabled() is False
    monkeypatch.setenv("ALYSIS_WEB_SEARCH_KEYLESS", "1")
    assert keyless_web_search_enabled() is True


def test_ddgs_package_is_installed() -> None:
    assert ddgs_package_available() is True
