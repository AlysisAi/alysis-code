from __future__ import annotations

import socket
import ssl
from typing import Any

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
    # The connection was up (a stalled connect raises ConnectTimeout), so this
    # host is slow or stalling bots: one such site must not cost web tools for
    # the rest of the turn.
    assert excinfo.value.recoverable is True
    assert excinfo.value.remote_site_reason == "site didn't respond"
    assert excinfo.value.blocked_by_remote_site is False
    assert "do not retry this host" in str(excinfo.value)


def test_web_fetch_connect_timeout_stays_unrecoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="connection setup") as excinfo:
        web_mod.web_fetch(
            url="https://docs.example.com/slow",
            transport=httpx.MockTransport(handler),
        )
    # A connect that never completes looks the same as a network outage.
    assert excinfo.value.recoverable is False
    assert excinfo.value.remote_site_reason == ""


def _raise_chained(outer: type[httpx.TransportError], message: str, cause: BaseException):
    def handler(request: httpx.Request) -> httpx.Response:
        try:
            raise cause
        except BaseException as exc:
            raise outer(message, request=request) from exc

    return handler


@pytest.mark.parametrize(
    ("handler", "expected_reason"),
    [
        pytest.param(
            _raise_chained(
                httpx.ConnectError,
                "certificate verify failed: unable to get local issuer certificate",
                ssl.SSLCertVerificationError(1, "certificate verify failed"),
            ),
            "site's certificate couldn't be verified",
            id="untrusted-certificate",
        ),
        pytest.param(
            _raise_chained(
                httpx.ConnectError,
                "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure",
                ssl.SSLError(1, "sslv3 alert handshake failure"),
            ),
            "secure connection to the site failed",
            id="tls-handshake-failure",
        ),
        pytest.param(
            _raise_chained(
                httpx.ReadError,
                "[Errno 104] Connection reset by peer",
                ConnectionResetError(104, "Connection reset by peer"),
            ),
            "site closed the connection",
            id="connection-reset",
        ),
        pytest.param(
            _raise_chained(
                httpx.RemoteProtocolError,
                "Server disconnected without sending a response.",
                EOFError(),
            ),
            "site closed the connection",
            id="server-disconnected",
        ),
        pytest.param(
            _raise_chained(
                httpx.RemoteProtocolError,
                "illegal status line: bytearray(b'garbage')",
                ValueError("bad status line"),
            ),
            "site sent an invalid response",
            id="invalid-response",
        ),
    ],
)
def test_web_fetch_transport_failures_after_reaching_the_host_are_the_sites(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    expected_reason: str,
) -> None:
    monkeypatch.setattr(web_mod, "_resolve_host_addresses", lambda _h, _p: ["93.184.216.34"])
    with pytest.raises(web_mod.WebFetchError, match="HTTP request failed") as excinfo:
        web_mod.web_fetch(
            url="https://legacy.example.com/page",
            transport=httpx.MockTransport(handler),
        )
    # The host was reached and then failed on its own side: recoverable (web
    # tools stay available), named for the trace, and not a refusal.
    assert excinfo.value.recoverable is True
    assert excinfo.value.remote_site_reason == expected_reason
    assert excinfo.value.blocked_by_remote_site is False
    assert "use a different source" in str(excinfo.value)


def test_web_fetch_connect_failure_stays_unrecoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 101] Network is unreachable", request=request)

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", lambda _h, _p: ["93.184.216.34"])
    with pytest.raises(web_mod.WebFetchError, match="Network is unreachable") as excinfo:
        web_mod.web_fetch(
            url="https://docs.example.com/page",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is False
    assert excinfo.value.remote_site_reason == ""


def test_web_fetch_nonexistent_host_is_recoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_such_host(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(web_mod.socket, "getaddrinfo", no_such_host)
    with pytest.raises(web_mod.WebFetchError, match="does not exist") as excinfo:
        web_mod.web_fetch(url="https://no-such-host.example.com/page")
    # A bad URL, not a DNS outage: the model can pick another source.
    assert excinfo.value.recoverable is True
    assert excinfo.value.remote_site_reason == ""


def test_web_fetch_resolver_outage_stays_unrecoverable(monkeypatch: pytest.MonkeyPatch) -> None:
    def resolver_down(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(web_mod.socket, "getaddrinfo", resolver_down)
    with pytest.raises(web_mod.WebFetchError, match="Failed to resolve host") as excinfo:
        web_mod.web_fetch(url="https://docs.example.com/page")
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
    assert excinfo.value.blocked_by_remote_site is True
    assert excinfo.value.remote_site_reason == "site doesn't allow automated access"


def test_web_fetch_plain_403_marks_remote_site_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bare 403 without challenge markers still means the server itself refused
    # the client: surfaces soften it, and the model is told to change source.
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"content-type": "text/html; charset=utf-8"},
            text="<html><body>Forbidden</body></html>",
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="declined automated access") as excinfo:
        web_mod.web_fetch(
            url="https://blocked.example.com/page",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is True
    assert excinfo.value.blocked_by_remote_site is True
    assert excinfo.value.remote_site_reason == "site doesn't allow automated access"
    assert "do not retry this host" in str(excinfo.value)


@pytest.mark.parametrize(
    ("status_code", "message_clause", "expected_reason"),
    [
        (401, "remote site requires sign-in", "site requires sign-in"),
        (429, "remote site is rate-limiting requests", "site is rate-limiting requests"),
        (
            451,
            "remote site is unavailable for legal reasons",
            "site is unavailable for legal reasons",
        ),
        (999, "remote site declined automated access", "site doesn't allow automated access"),
    ],
)
def test_web_fetch_refusal_statuses_mark_remote_site_block(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    message_clause: str,
    expected_reason: str,
) -> None:
    # Sign-in walls, rate limits, legal blocks, and LinkedIn's 999 are the
    # server refusing this client, exactly like a 403.
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, headers={"content-type": "text/html"}, text="<p>no</p>")

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", lambda _h, _p: ["93.184.216.34"])
    with pytest.raises(web_mod.WebFetchError, match=message_clause) as excinfo:
        web_mod.web_fetch(
            url="https://refusing.example.com/page",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is True
    assert excinfo.value.blocked_by_remote_site is True
    assert excinfo.value.remote_site_reason == expected_reason
    assert "do not retry this host" in str(excinfo.value)


@pytest.mark.parametrize(
    ("status_code", "headers", "body"),
    [
        pytest.param(
            401,
            {"server": "CloudFront", "x-datadome": "protected"},
            "<html><title>wsj.com</title><p id='cmsg'>Please enable JS and disable any ad "
            "blocker</p><script>var dd={'host':'geo.captcha-delivery.com'}</script></html>",
            id="datadome-401",
        ),
        pytest.param(
            503,
            {"server": "Server"},
            "<html><h4>Enter the characters you see below</h4><p>Sorry, we just need to make "
            "sure you're not a robot.</p><form action='/errors/validateCaptcha'></form></html>",
            id="amazon-captcha-503",
        ),
        pytest.param(
            405,
            {"x-amzn-waf-action": "captcha"},
            "<html><body><div id='captcha-container'></div></body></html>",
            id="aws-waf-405",
        ),
        pytest.param(
            503,
            {"server": "cloudflare"},
            "<html><title>Just a moment...</title><body>Checking your browser</body></html>",
            id="cloudflare-challenge-503",
        ),
    ],
)
def test_web_fetch_bot_wall_on_other_statuses_marks_remote_site_block(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    headers: dict[str, str],
    body: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, headers={"content-type": "text/html", **headers}, text=body
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", lambda _h, _p: ["93.184.216.34"])
    with pytest.raises(web_mod.WebFetchError, match="anti-bot/challenge protection") as excinfo:
        web_mod.web_fetch(
            url="https://walled.example.com/page",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is True
    assert excinfo.value.blocked_by_remote_site is True
    assert excinfo.value.remote_site_reason == "site doesn't allow automated access"


@pytest.mark.parametrize(
    ("status_code", "headers", "body"),
    [
        pytest.param(500, {}, "<h1>Internal Server Error</h1>", id="plain-500"),
        pytest.param(
            503,
            {"server": "nginx"},
            "<h1>Down for maintenance</h1><p>We'll be back in just a moment.</p>",
            id="maintenance-503",
        ),
        pytest.param(
            522,
            {"server": "cloudflare"},
            "<title>Connection timed out | Cloudflare</title><h1>Error 522</h1>",
            id="cloudflare-origin-down-522",
        ),
        pytest.param(
            404,
            {},
            "<h1>Page not found</h1><form class='g-recaptcha'>Search</form><p>Access denied?</p>",
            id="not-found-page-mentioning-captcha",
        ),
    ],
)
def test_web_fetch_server_errors_and_missing_pages_are_not_remote_site_blocks(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    headers: dict[str, str],
    body: str,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code, headers={"content-type": "text/html", **headers}, text=body
        )

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", lambda _h, _p: ["93.184.216.34"])
    with pytest.raises(web_mod.WebFetchError, match=f"HTTP error {status_code}") as excinfo:
        web_mod.web_fetch(
            url="https://broken.example.com/page",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is True
    assert excinfo.value.blocked_by_remote_site is False
    assert excinfo.value.remote_site_reason == ""


def test_web_fetch_error_block_flag_implies_a_trace_reason() -> None:
    error = web_mod.WebFetchError("blocked", recoverable=True, blocked_by_remote_site=True)
    assert error.remote_site_reason == "site doesn't allow automated access"
    assert web_mod.WebFetchError("plain failure").remote_site_reason == ""


def test_web_fetch_http_404_is_not_a_remote_site_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"}, text="nope")

    monkeypatch.setattr(web_mod, "_resolve_host_addresses", fake_resolver)
    with pytest.raises(web_mod.WebFetchError, match="HTTP error 404") as excinfo:
        web_mod.web_fetch(
            url="https://docs.example.com/missing",
            transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.recoverable is True
    assert excinfo.value.blocked_by_remote_site is False
    assert excinfo.value.remote_site_reason == ""


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
