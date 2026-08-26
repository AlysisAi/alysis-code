from __future__ import annotations

import httpx
import pytest

import alysis_code.tools.web as web_mod


def test_web_fetch_html_extracts_title_and_readable_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://docs.example.com/spec"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Spec Title</title></head><body>"
                "<h1>API</h1><p>Important details.</p>"
                "<script>ignored()</script></body></html>"
            ),
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url="https://docs.example.com/spec",
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == "https://docs.example.com/spec"
    assert out["final_url"] == "https://docs.example.com/spec"
    assert out["status_code"] == 200
    assert out["content_type"] == "text/html"
    assert out["title"] == "Spec Title"
    assert "API" in out["content"]
    assert "Important details." in out["content"]
    assert "ignored()" not in out["content"]
    assert out["truncated"] is False
    assert out["backend"] == "httpx"


def test_web_fetch_preserves_valid_parenthesized_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://docs.example.com/Function_(mathematics)"
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="balanced path content",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url="https://docs.example.com/Function_(mathematics)",
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == "https://docs.example.com/Function_(mathematics)"
    assert out["final_url"] == "https://docs.example.com/Function_(mathematics)"
    assert out["content"] == "balanced path content"
    assert "raw_input_url" not in out


def test_web_fetch_preserves_legal_trailing_parenthesis_in_structured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_url = "https://docs.example.com/path?x=a)"

    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == clean_url
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="trailing parenthesis content",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url=clean_url,
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == clean_url
    assert out["final_url"] == clean_url
    assert out["content"] == "trailing parenthesis content"
    assert "raw_input_url" not in out


def test_web_fetch_preserves_bracket_query_params_in_structured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_url = "https://docs.example.com/path?foo[bar]=1"

    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == clean_url
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="bracket query content",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url=clean_url,
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == clean_url
    assert out["final_url"] == clean_url
    assert out["content"] == "bracket query content"
    assert "raw_input_url" not in out


@pytest.mark.parametrize("suffix", [":", ";", ".", ","])
def test_web_fetch_preserves_structured_trailing_punctuation_in_network_request(
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
) -> None:
    clean_url = f"https://docs.example.com/path{suffix}"

    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == clean_url
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="structured punctuation content",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url=clean_url,
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == clean_url
    assert out["final_url"] == clean_url
    assert out["content"] == "structured punctuation content"
    assert "raw_input_url" not in out


def test_web_fetch_preserves_legal_trailing_exclamation_in_structured_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clean_url = "https://docs.example.com/Yahoo!"

    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == clean_url
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="exclamation content",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url=clean_url,
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == clean_url
    assert out["final_url"] == clean_url
    assert out["content"] == "exclamation content"
    assert "raw_input_url" not in out


@pytest.mark.parametrize(
    ("raw_url", "expected_url"),
    [
        ("(https://docs.example.com/spec)", "https://docs.example.com/spec"),
        ('https://docs.example.com/spec"', "https://docs.example.com/spec"),
        ("`https://docs.example.com/spec`", "https://docs.example.com/spec"),
        ("*https://docs.example.com/spec*", "https://docs.example.com/spec"),
        ("**https://docs.example.com/spec**", "https://docs.example.com/spec"),
        ("_https://docs.example.com/spec_", "https://docs.example.com/spec"),
        ("__https://docs.example.com/spec__", "https://docs.example.com/spec"),
    ],
)
def test_web_fetch_canonicalizes_wrappers_and_trailing_punctuation_before_network_request(
    monkeypatch: pytest.MonkeyPatch,
    raw_url: str,
    expected_url: str,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == expected_url
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="canonical content",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url=raw_url,
        transport=httpx.MockTransport(handler),
    )

    assert out["url"] == expected_url
    assert out["final_url"] == expected_url
    assert out["content"] == "canonical content"
    assert out["raw_input_url"] == raw_url


def test_web_fetch_sends_browser_like_text_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Mozilla/5.0" in request.headers["user-agent"]
        assert "text/html" in request.headers["accept"]
        assert "application/json" in request.headers["accept"]
        assert request.headers["accept-language"].startswith("en-US")
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            text="ok",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url="https://docs.example.com/spec",
        transport=httpx.MockTransport(handler),
    )

    assert out["content"] == "ok"


def test_web_fetch_json_returns_readable_decoded_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json; charset=utf-8"},
            content=b'{"ok":true,"items":[1,2]}',
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url="https://api.example.com/data",
        transport=httpx.MockTransport(handler),
    )

    assert out["status_code"] == 200
    assert out["content_type"] == "application/json"
    assert out["title"] == ""
    assert '"ok": true' in out["content"]
    assert '"items": [' in out["content"]
    assert out["backend"] == "httpx"


def test_web_fetch_follows_redirects_and_reports_final_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/start":
            return httpx.Response(302, headers={"location": "/next"})
        if url == "https://example.com/next":
            return httpx.Response(301, headers={"location": "https://docs.example.com/final"})
        if url == "https://docs.example.com/final":
            return httpx.Response(
                200,
                headers={"content-type": "text/plain; charset=utf-8"},
                text="final content",
            )
        raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url="https://example.com/start",
        transport=httpx.MockTransport(handler),
    )

    assert out["status_code"] == 200
    assert out["final_url"] == "https://docs.example.com/final"
    assert out["content_type"] == "text/plain"
    assert "final content" in out["content"]


def test_web_fetch_revalidates_redirect_target(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        assert str(request.url) == "https://example.com/start"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/internal"})

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="Blocked URL host"):
        web_mod.web_fetch(
            url="https://example.com/start",
            transport=httpx.MockTransport(handler),
        )

    assert call_count == 1


def test_web_fetch_max_chars_truncation_sets_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            text="0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    out = web_mod.web_fetch(
        url="https://docs.example.com/long",
        max_chars=12,
        transport=httpx.MockTransport(handler),
    )

    assert len(out["content"]) == 12
    assert out["truncated"] is True


def test_web_fetch_rejects_non_http_scheme() -> None:
    with pytest.raises(web_mod.WebFetchError, match="Unsupported URL scheme"):
        web_mod.web_fetch(url="ftp://example.com/spec")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://127.0.0.1/",
        "http://10.0.0.7/",
        "http://169.254.1.2/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
        "http://240.0.0.1/",
        "http://169.254.169.254/",
        "http://100.100.100.200/",
    ],
)
def test_web_fetch_rejects_local_and_special_hosts(url: str) -> None:
    with pytest.raises(web_mod.WebFetchError, match="Blocked URL host"):
        web_mod.web_fetch(url=url)


def test_web_fetch_rejects_hostname_resolving_only_to_blocked_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["127.0.0.1", "10.0.0.5"]

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="resolved to blocked/local address"):
        web_mod.web_fetch(
            url="https://internal.example/spec",
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, text="ok")),
        )


def test_web_fetch_rejects_unsupported_binary_content_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"\x00\x01\x02\x03",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="Unsupported content type"):
        web_mod.web_fetch(
            url="https://example.com/blob.bin",
            transport=httpx.MockTransport(handler),
        )


def test_web_fetch_http_error_status_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            headers={"content-type": "text/plain"},
            text="not found",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="HTTP error 404") as excinfo:
        web_mod.web_fetch(
            url="https://docs.example.com/missing",
            transport=httpx.MockTransport(handler),
        )
    # The site answered, so the failure is URL-specific: the model must be able
    # to recover by picking a different source instead of losing web tools for
    # the rest of the turn.
    assert excinfo.value.recoverable is True


def test_web_fetch_read_timeout_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("The read operation timed out")

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(
        web_mod.WebFetchError,
        match=r"timed out during response read .*overall=10s",
    ) as excinfo:
        web_mod.web_fetch(
            url="https://docs.example.com/slow",
            transport=httpx.MockTransport(handler),
        )
    # No response arrived — a genuine connectivity signal stays unrecoverable.
    assert excinfo.value.recoverable is False


def test_web_fetch_oversized_body_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"x" * 1_100_000,
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="exceeded max_bytes") as excinfo:
        web_mod.web_fetch(
            url="https://docs.example.com/huge",
            transport=httpx.MockTransport(handler),
        )
    # The site responded; an oversized page is URL-specific and must not
    # disable web tools for the rest of the turn.
    assert excinfo.value.recoverable is True


def test_web_fetch_cloudflare_challenge_status_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["104.18.32.47"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={
                "content-type": "text/html; charset=utf-8",
                "cf-mitigated": "challenge",
                "server": "cloudflare",
            },
            text="<html><title>Just a moment...</title><body>Cloudflare challenge</body></html>",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="anti-bot/challenge protection") as excinfo:
        web_mod.web_fetch(
            url="https://chatgpt.com/",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is True


def test_web_fetch_rejects_hostname_with_mixed_safe_and_blocked_dns_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_count = 0

    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34", "127.0.0.1"]

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="resolved to blocked/local address"):
        web_mod.web_fetch(
            url="https://mixed.example/spec",
            transport=httpx.MockTransport(handler),
        )

    assert request_count == 0


def test_web_fetch_rejects_too_many_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/start"})

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="Too many redirects"):
        web_mod.web_fetch(
            url="https://example.com/start",
            transport=httpx.MockTransport(handler),
        )


def test_web_fetch_rejects_embedded_credentials() -> None:
    with pytest.raises(web_mod.WebFetchError, match="Embedded URL credentials"):
        web_mod.web_fetch(url="https://user:pass@example.com/spec")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:70000/",
        "https://example.com:abc/",
    ],
)
def test_web_fetch_rejects_invalid_port_values(url: str) -> None:
    with pytest.raises(web_mod.WebFetchError, match="invalid port value"):
        web_mod.web_fetch(url=url)


def test_web_fetch_rejects_max_chars_above_cap() -> None:
    with pytest.raises(web_mod.WebFetchError, match="between 1 and 50000"):
        web_mod.web_fetch(url="https://docs.example.com/spec", max_chars=50_001)
