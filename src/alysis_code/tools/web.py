from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..safety import SafeHttpError, safe_http_request
from ..web_research import canonicalize_web_url_input, normalize_web_url
from .http_timeout import build_http_timeout_budget, format_http_timeout_error


class WebFetchError(RuntimeError):
    """Raised for any web_fetch failure.

    ``recoverable=True`` marks failures the calling model can fix by adjusting its
    own tool arguments (input validation). The agent loop returns those to the
    model as plain errors instead of disabling web tools for the rest of the turn.
    """

    def __init__(self, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.recoverable = recoverable


_DEFAULT_MAX_CHARS = 20_000
_MAX_MAX_CHARS = 50_000
_MAX_REDIRECTS = 5
_DEFAULT_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 1_000_000
_REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
_HTML_CONTENT_TYPES = {"text/html", "application/xhtml+xml"}
_JSON_CONTENT_TYPES = {"application/json", "text/json"}
_XML_CONTENT_TYPES = {"application/xml", "text/xml"}
_EXTRA_TEXT_CONTENT_TYPES = {
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/x-www-form-urlencoded",
    "application/x-yaml",
    "application/yaml",
    "application/toml",
}
_BLOCKED_IPV4_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("169.254.169.254/32"),
    ipaddress.ip_network("100.100.100.200/32"),
)
_TEXT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "application/json;q=0.8,text/plain;q=0.8,*/*;q=0.5"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

_TAG_BLOCK_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?>.*?</\1>")
_HEAD_BLOCK_RE = re.compile(r"(?is)<head\b.*?</head>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
_TAG_RE = re.compile(r"(?is)<[^>]+>")
_BREAK_TAG_RE = re.compile(r"(?is)<br\s*/?>")
_BLOCK_CLOSE_TAG_RE = re.compile(
    r"(?is)</(p|div|section|article|header|footer|aside|nav|h[1-6]|li|tr|td|th|"
    r"table|blockquote|ul|ol)>"
)
_HTML_HINT_RE = re.compile(r"(?is)<!doctype\s+html|<html\b|<body\b|<head\b|<title\b")
_CHARSET_RE = re.compile(r"charset=([^\s;]+)", re.IGNORECASE)
_REMOTE_CHALLENGE_BODY_RE = re.compile(
    r"(?is)\b(cloudflare|checking your browser|just a moment|enable javascript and cookies)\b"
)


ResolverFn = Callable[[str, int], list[str]]


def _normalize_whitespace(text: str) -> str:
    return " ".join(str(text).split())


def _normalize_content_type(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    return text.split(";", 1)[0].strip().lower()


def _extract_charset(raw_content_type: str) -> str | None:
    match = _CHARSET_RE.search(str(raw_content_type or ""))
    if match is None:
        return None
    value = str(match.group(1) or "").strip().strip('"').strip("'")
    return value or None


def _decode_bytes(data: bytes, *, raw_content_type: str, fallback_encoding: str | None) -> str:
    encoding = _extract_charset(raw_content_type) or (fallback_encoding or "").strip() or "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _looks_binary(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:2048]
    text_like = set(range(32, 127)) | {9, 10, 13}
    non_text = sum(1 for byte in sample if byte not in text_like)
    return (non_text / max(1, len(sample))) > 0.30


def _looks_like_html(text: str) -> bool:
    return _HTML_HINT_RE.search(text) is not None


def _extract_html_title_and_text(html_text: str) -> tuple[str, str]:
    title = ""
    title_match = _TITLE_RE.search(html_text)
    if title_match is not None:
        raw_title = _TAG_RE.sub(" ", str(title_match.group(1) or ""))
        title = _normalize_whitespace(html.unescape(raw_title))

    body = _TAG_BLOCK_RE.sub(" ", html_text)
    body = _HEAD_BLOCK_RE.sub(" ", body)
    body = _BREAK_TAG_RE.sub("\n", body)
    body = _BLOCK_CLOSE_TAG_RE.sub("\n", body)
    body = _TAG_RE.sub(" ", body)
    body = html.unescape(body)

    lines = [_normalize_whitespace(line) for line in body.splitlines()]
    text = "\n".join(line for line in lines if line)
    if not text:
        text = _normalize_whitespace(body)
    return title, text


def _is_json_content_type(content_type: str) -> bool:
    return content_type in _JSON_CONTENT_TYPES or content_type.endswith("+json")


def _is_xml_content_type(content_type: str) -> bool:
    return content_type in _XML_CONTENT_TYPES or content_type.endswith("+xml")


def _is_text_like_content_type(content_type: str) -> bool:
    if not content_type:
        return True
    if content_type.startswith("text/"):
        return True
    if _is_json_content_type(content_type):
        return True
    if _is_xml_content_type(content_type):
        return True
    return content_type in _EXTRA_TEXT_CONTENT_TYPES


def _clip_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _coerce_max_chars(raw: Any) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as e:
        raise WebFetchError(
            f"max_chars must be between 1 and {_MAX_MAX_CHARS}.",
            recoverable=True,
        ) from e
    if value <= 0 or value > _MAX_MAX_CHARS:
        raise WebFetchError(
            f"max_chars must be between 1 and {_MAX_MAX_CHARS}.",
            recoverable=True,
        )
    return value


def _split_url(raw_url: str) -> tuple[str, Any]:
    url = str(raw_url or "").strip()
    if not url:
        raise WebFetchError("url must be a non-empty string.", recoverable=True)
    split = urlsplit(url)
    scheme = split.scheme.lower()
    if scheme not in {"http", "https"}:
        raise WebFetchError(
            f"Unsupported URL scheme: {split.scheme or '(missing)'}",
            recoverable=True,
        )
    if split.hostname is None:
        raise WebFetchError("URL must include a valid host.", recoverable=True)
    if split.username is not None or split.password is not None:
        raise WebFetchError("Embedded URL credentials are not allowed.", recoverable=True)
    try:
        _ = split.port
    except ValueError as e:
        raise WebFetchError("URL has an invalid port value.", recoverable=True) from e
    return url, split


def _blocked_ip_reason(ip: ipaddress._BaseAddress) -> str | None:
    if ip.is_loopback:
        return "loopback"
    if ip.is_private:
        return "private"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_reserved:
        return "reserved"
    if isinstance(ip, ipaddress.IPv4Address):
        if any(ip in network for network in _BLOCKED_IPV4_NETWORKS):
            return "cloud-metadata-or-special-range"
    return None


def _resolve_host_addresses(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise WebFetchError(f"Failed to resolve host '{host}': {e}") from e

    addresses: list[str] = []
    seen: set[str] = set()
    for _family, _socktype, _proto, _canonname, sockaddr in infos:
        if not sockaddr:
            continue
        ip_text = str(sockaddr[0] or "").strip()
        if not ip_text:
            continue
        if ip_text not in seen:
            seen.add(ip_text)
            addresses.append(ip_text)
    if not addresses:
        raise WebFetchError(f"Host '{host}' did not resolve to any IP address.")
    return addresses


def _validate_fetch_target(url: str, *, resolver: ResolverFn) -> None:
    _url, split = _split_url(url)
    host = str(split.hostname or "")
    host_normalized = host.rstrip(".").casefold()
    if host_normalized == "localhost" or host_normalized.endswith(".localhost"):
        raise WebFetchError(f"Blocked URL host '{host}': localhost is not allowed.")

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        reason = _blocked_ip_reason(literal_ip)
        if reason is not None:
            raise WebFetchError(f"Blocked URL host '{host}': {reason} address is not allowed.")
        return

    port = split.port
    if port is None:
        port = 443 if split.scheme.lower() == "https" else 80

    resolved_ips = resolver(host, port)
    blocked_entries: list[str] = []
    for ip_text in resolved_ips:
        try:
            parsed = ipaddress.ip_address(ip_text)
        except ValueError as e:
            raise WebFetchError(
                f"Resolver returned a non-IP address for host '{host}': {ip_text!r}"
            ) from e
        reason = _blocked_ip_reason(parsed)
        if reason is not None:
            blocked_entries.append(f"{parsed.compressed} ({reason})")

    if blocked_entries:
        details = ", ".join(blocked_entries)
        raise WebFetchError(
            f"Blocked URL host '{host}': resolved to blocked/local address(es): {details}."
        )


def _read_limited_response_bytes(response: httpx.Response, *, limit: int) -> tuple[bytes, bool]:
    buf = bytearray()
    truncated = False
    for chunk in response.iter_bytes():
        if not chunk:
            continue
        remaining = limit - len(buf)
        if remaining <= 0:
            truncated = True
            break
        if len(chunk) > remaining:
            buf.extend(chunk[:remaining])
            truncated = True
            break
        buf.extend(chunk)
    return bytes(buf), truncated


def _http_status_error_message(
    *,
    status_code: int,
    final_url: str,
    response_headers: dict[str, str],
    decoded_body: str,
) -> str:
    if status_code in {403, 429, 503}:
        cf_mitigated = str(response_headers.get("cf-mitigated") or "").strip().casefold()
        server = str(response_headers.get("server") or "").strip().casefold()
        body_sample = decoded_body[:4096]
        if cf_mitigated == "challenge" or (
            "cloudflare" in server and _REMOTE_CHALLENGE_BODY_RE.search(body_sample)
        ):
            return (
                f"HTTP error {status_code} while fetching '{final_url}': remote site "
                "blocked automated retrieval with anti-bot/challenge protection."
            )
    return f"HTTP error {status_code} while fetching '{final_url}'."


def web_fetch(
    *,
    url: str,
    max_chars: int = _DEFAULT_MAX_CHARS,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    # Defense-in-depth runtime guard: even if a web_fetch call reaches this layer
    # through a path that bypassed tool registration (resume/replay, patched tool
    # tables), the master web-tools switch still hard-blocks execution.
    # Lazy import to avoid a module-level import cycle with config.
    from ..config import resolve_web_tools_enabled

    if not resolve_web_tools_enabled(None):
        raise WebFetchError(
            "web_fetch is disabled by the web-tools policy "
            "(ALYSIS_WEB_TOOLS=off / web_tools_enabled=false). "
            "No network fetch was performed.",
            recoverable=False,
        )
    raw_input_url = str(url or "").strip()
    requested_url, _split = _split_url(
        normalize_web_url(raw_input_url)
        or canonicalize_web_url_input(raw_input_url)
        or raw_input_url
    )
    clipped_max_chars = _coerce_max_chars(max_chars)

    resolver: ResolverFn = _resolve_host_addresses
    timeout_budget = build_http_timeout_budget(_DEFAULT_TIMEOUT_SECONDS, profile="fetch")

    current_url = requested_url
    final_url = requested_url
    status_code = 0
    raw_content_type = ""
    body = b""
    body_truncated = False
    response_encoding: str | None = None
    response_headers: dict[str, str] = {}

    _validate_fetch_target(current_url, resolver=resolver)
    try:
        response = asyncio.run(
            safe_http_request(
                "GET",
                current_url,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
                max_bytes=_MAX_RESPONSE_BYTES,
                allow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                headers=_TEXT_HEADERS,
                _transport=transport,  # type: ignore[arg-type]
                _resolver=resolver,
            )
        )
    except SafeHttpError as e:
        # safe_http classifies URL-/host-specific failures (oversized body,
        # redirect problems, blocked or invalid hosts) as recoverable; propagate
        # that so one bad target does not disable web tools for the whole turn.
        raise WebFetchError(str(e), recoverable=getattr(e, "recoverable", False)) from e
    except httpx.TimeoutException as e:
        raise WebFetchError(
            format_http_timeout_error(
                operation=f"web_fetch request to '{current_url}'",
                budget=timeout_budget,
                error=e,
            )
        ) from e
    except httpx.HTTPError as e:
        raise WebFetchError(f"HTTP request failed for '{current_url}': {e}") from e

    status_code = int(response.status_code)
    final_url = str(response.url)
    raw_content_type = str(response.headers.get("content-type") or "")
    response_encoding = response.encoding
    response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    body = response.content
    body_truncated = False

    content_type = _normalize_content_type(raw_content_type)
    decoded = _decode_bytes(
        body, raw_content_type=raw_content_type, fallback_encoding=response_encoding
    )
    # The site responded, so the web itself is reachable: an HTTP error status or
    # unusable payload is specific to this URL and the model can recover by
    # choosing a different source. Only genuine connectivity signals (DNS
    # resolution outage, connect failures, timeouts) remain unrecoverable.
    if status_code >= 400:
        raise WebFetchError(
            _http_status_error_message(
                status_code=status_code,
                final_url=final_url,
                response_headers=response_headers,
                decoded_body=decoded,
            ),
            recoverable=True,
        )

    if content_type and not _is_text_like_content_type(content_type):
        raise WebFetchError(
            f"Unsupported content type for web_fetch: '{content_type}'. Only text-like responses "
            "are supported.",
            recoverable=True,
        )
    if not content_type and _looks_binary(body):
        raise WebFetchError(
            "Unsupported binary response without a text-like content type.",
            recoverable=True,
        )

    title = ""
    if content_type in _HTML_CONTENT_TYPES or (not content_type and _looks_like_html(decoded)):
        if not content_type:
            content_type = "text/html"
        title, extracted = _extract_html_title_and_text(decoded)
        readable_content = extracted
    elif _is_json_content_type(content_type):
        try:
            parsed_json = json.loads(decoded)
        except json.JSONDecodeError:
            readable_content = decoded
        else:
            readable_content = json.dumps(parsed_json, ensure_ascii=False, indent=2)
    else:
        if not content_type:
            content_type = "text/plain"
        readable_content = decoded

    readable_content = readable_content.replace("\r\n", "\n").replace("\r", "\n")
    clipped_content, clipped = _clip_text(readable_content, max_chars=clipped_max_chars)
    truncated = bool(body_truncated or clipped)

    return {
        "url": requested_url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "title": title,
        "content": clipped_content,
        "truncated": truncated,
        "backend": "httpx",
        **(
            {"raw_input_url": raw_input_url}
            if raw_input_url and raw_input_url != requested_url
            else {}
        ),
    }
