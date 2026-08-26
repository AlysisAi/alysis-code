from __future__ import annotations

import asyncio
import socket

import httpx
import pytest

from alysis_code.safety import SafeHttpError, safe_http_request
from alysis_code.safety import safe_http as safe_http_mod


def _run(coro: object) -> httpx.Response:
    return asyncio.run(coro)  # type: ignore[arg-type]


def _mock_getaddrinfo(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, list[str]]) -> None:
    def fake_getaddrinfo(host: str, port: int, *args: object, **kwargs: object) -> list[object]:
        del args, kwargs
        addresses = mapping[str(host)]
        family = socket.AF_INET6 if ":" in addresses[0] else socket.AF_INET
        return [(family, socket.SOCK_STREAM, 0, "", (address, port)) for address in addresses]

    monkeypatch.setattr(safe_http_mod.socket, "getaddrinfo", fake_getaddrinfo)


def _use_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(safe_http_mod.httpx, "AsyncClient", _Client)


@pytest.mark.parametrize(
    "scheme",
    ["file", "gopher", "data", "about", "javascript", "ftp", "ssh"],
)
def test_safe_http_request_rejects_unsupported_schemes(scheme: str) -> None:
    with pytest.raises(SafeHttpError, match="Unsupported URL scheme"):
        _run(safe_http_request("GET", f"{scheme}://example.com/resource"))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "169.254.169.254",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::",
        "ff00::1",
    ],
)
def test_safe_http_request_rejects_denied_address_ranges(
    monkeypatch: pytest.MonkeyPatch,
    address: str,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"blocked.test": [address]})

    with pytest.raises(SafeHttpError, match="denied address"):
        _run(safe_http_request("GET", "http://blocked.test/resource"))


def test_safe_http_request_rejects_when_any_resolved_address_is_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"mixed.test": ["93.184.216.34", "127.0.0.1"]})

    with pytest.raises(SafeHttpError, match="127.0.0.1"):
        _run(safe_http_request("GET", "http://mixed.test/resource"))


def test_safe_http_request_connects_to_resolved_ip_and_preserves_host_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        return httpx.Response(200, json={"ok": True})

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    response = _run(safe_http_request("GET", "http://example.com/path?q=1"))

    assert response.status_code == 200
    assert response.url == "http://example.com/path?q=1"
    assert response.json() == {"ok": True}


def test_safe_http_request_preserves_https_hostname_for_sni(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "example.com"
        assert request.headers["host"] == "example.com"
        return httpx.Response(200, json={"ok": True})

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    response = _run(safe_http_request("GET", "https://example.com/path?q=1"))

    assert response.status_code == 200
    assert response.url == "https://example.com/path?q=1"
    assert response.json() == {"ok": True}


def test_safe_http_request_defaults_accept_encoding_to_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, json={"ok": True})

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    response = _run(safe_http_request("GET", "https://example.com/path"))

    assert response.json() == {"ok": True}


def test_safe_http_request_preserves_explicit_accept_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["accept-encoding"] == "gzip"
        return httpx.Response(200, json={"ok": True})

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    response = _run(
        safe_http_request(
            "GET",
            "https://example.com/path",
            headers={"Accept-Encoding": "gzip"},
        )
    )

    assert response.json() == {"ok": True}


def test_safe_http_request_wraps_invalid_compressed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
            content=b'{"ok": true}',
        )

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(SafeHttpError, match="Response decompression failed"):
        _run(safe_http_request("GET", "https://example.com/path"))


def test_safe_http_request_validates_redirect_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_getaddrinfo(
        monkeypatch,
        {
            "example.com": ["93.184.216.34"],
            "next.test": ["93.184.216.35"],
        },
    )
    seen_hosts: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_hosts.append(request.headers["host"])
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"Location": "http://next.test/final"})
        return httpx.Response(200, text="done")

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    response = _run(safe_http_request("GET", "http://example.com/start"))

    assert response.text == "done"
    assert response.url == "http://next.test/final"
    assert seen_hosts == ["example.com", "next.test"]


def test_safe_http_request_enforces_redirect_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://example.com/again"})

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(SafeHttpError, match="Too many redirects"):
        _run(safe_http_request("GET", "http://example.com/start", max_redirects=1))


def test_safe_http_request_enforces_max_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"abcdef")

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(SafeHttpError, match="max_bytes=3"):
        _run(safe_http_request("GET", "http://example.com/resource", max_bytes=3))


def test_safe_http_request_preserves_httpx_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _mock_getaddrinfo(monkeypatch, {"example.com": ["93.184.216.34"]})

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    _use_transport(monkeypatch, httpx.MockTransport(handler))

    with pytest.raises(httpx.ReadTimeout):
        _run(safe_http_request("GET", "http://example.com/resource", timeout=0.01))
