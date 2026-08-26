from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..internal_artifacts import INTERNAL_ARTIFACT_MESSAGE_KEY

PROVIDER_METADATA_KEY = "_alysis_provider_metadata"
ROUTE_IDENTITY_PROVIDER_METADATA_KEY = "_route_identity"
OPENAI_RESPONSES_PROVIDER_METADATA_KEY = "openai_responses"
ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY = "anthropic_messages"
GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY = "gemini_generate_content"
GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY = "gemini_interactions"
MISTRAL_PROVIDER_METADATA_KEY = "mistral"
MOONSHOT_PROVIDER_METADATA_KEY = "moonshot"
QWEN_PROVIDER_METADATA_KEY = "qwen"
MISTRAL_CONTENT_CHUNKS_KEY = "content_chunks"
TOOL_CALL_PROVIDER_METADATA_KEY = "_tool_calls"
DEEPSEEK_REASONING_CONTENT_KEY = "reasoning_content"
DEEPSEEK_PROVIDER_METADATA_KEY = "deepseek"
OPENROUTER_REASONING_KEY = "reasoning"
OPENROUTER_REASONING_DETAILS_KEY = "reasoning_details"

STATEFUL_PROVIDER_METADATA_KEYS = frozenset(
    {
        OPENAI_RESPONSES_PROVIDER_METADATA_KEY,
        ANTHROPIC_MESSAGES_PROVIDER_METADATA_KEY,
        GEMINI_GENERATE_CONTENT_PROVIDER_METADATA_KEY,
        GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY,
        # DeepSeek requires opaque reasoning_content to survive every
        # tools-enabled assistant turn, even when that turn called no tool.
        DEEPSEEK_PROVIDER_METADATA_KEY,
        # Mistral requires the full assistant content, including opaque
        # ThinkChunk/signature state, to be replayed on later same-route turns.
        MISTRAL_PROVIDER_METADATA_KEY,
        # Kimi requires reasoning_content to be replayed for same-route
        # multi-turn and tool continuations, including ordinary K3 replies.
        MOONSHOT_PROVIDER_METADATA_KEY,
        # Qwen reasoning is retained privately so model contracts that require
        # preserved thinking can replay it on later same-route assistant turns.
        QWEN_PROVIDER_METADATA_KEY,
    }
)
_HIERARCHICAL_URL_RE = re.compile(
    r"\b[A-Za-z][A-Za-z0-9+.-]*://\S+",
)


@dataclass(frozen=True)
class ProviderRouteIdentity:
    """Immutable identity for provider-owned continuation state.

    Provider reasoning payloads, signatures, and server-side continuation IDs
    are only valid on the exact route that produced them.  Factory-created
    clients include the configured profile and auth adapter; directly-created
    clients use deterministic empty values for those two fields.
    """

    protocol: str
    base_url: str
    provider_key: str
    model: str
    profile_name: str
    auth_provider: str
    credential_scope: str
    routing_headers: tuple[tuple[str, str], ...]
    routing_fields: tuple[tuple[str, str], ...]
    reasoning_state_adapter: str
    protocol_revision: str
    session_scope: str
    fingerprint: str

    def as_metadata(self) -> dict[str, Any]:
        # Persist only the versioned digest. Route inputs can contain API-key
        # query parameters or account-routing identifiers and must not enter
        # transcripts/support bundles even in normalized form.
        return {
            "version": 1,
            "fingerprint": self.fingerprint,
        }


def _normalize_route_base_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    path = parsed.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)))
    userinfo, separator, authority = parsed.netloc.rpartition("@")
    normalized_netloc = (
        f"{userinfo}@{authority.casefold()}" if separator else parsed.netloc.casefold()
    )
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            normalized_netloc,
            path,
            query,
            "",
        )
    )


def endpoint_descriptor(value: str) -> dict[str, Any]:
    """Return a persistence-safe identity for a configured provider endpoint.

    Provider URLs can contain credentials in userinfo, signed path segments, or
    API keys in query parameters.  Session logs need an exact-match signal for
    safe resume, but must not retain those route inputs.  The versioned digest
    covers the same normalized endpoint material used by route identities while
    the optional host is derived through ``urlsplit().hostname`` so userinfo,
    ports, paths, queries, and fragments cannot enter persisted metadata.
    """

    normalized = _normalize_route_base_url(value)
    fingerprint = hashlib.sha256(f"alysis-endpoint:v1:{normalized}".encode()).hexdigest()
    descriptor: dict[str, Any] = {
        "version": 1,
        "fingerprint": fingerprint,
    }
    try:
        hostname = str(urlsplit(str(value or "").strip()).hostname or "").rstrip(".")
        safe_host = hostname.encode("idna").decode("ascii").casefold()
    except (UnicodeError, ValueError):
        safe_host = ""
    if safe_host and not any(char.isspace() or char in "/@?#" for char in safe_host):
        descriptor["host"] = safe_host
    return descriptor


def endpoint_descriptor_matches(value: str, descriptor: Any) -> bool:
    """Return whether ``value`` exactly matches a persisted endpoint digest."""

    if not isinstance(descriptor, dict) or descriptor.get("version") != 1:
        return False
    fingerprint = descriptor.get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        return False
    try:
        int(fingerprint, 16)
    except ValueError:
        return False
    expected = endpoint_descriptor(value)["fingerprint"]
    return hmac.compare_digest(fingerprint.casefold(), expected)


def endpoint_label(value: str) -> str:
    """Return a human-readable endpoint label that is safe for logs/errors."""

    descriptor = endpoint_descriptor(value)
    fingerprint = str(descriptor["fingerprint"])
    host = str(descriptor.get("host") or "").strip()
    if host:
        authority = f"[{host}]" if ":" in host else host
        try:
            port = urlsplit(str(value or "").strip()).port
        except ValueError:
            port = None
        if port is not None:
            authority = f"{authority}:{port}"
        return f"{authority} (endpoint {fingerprint[:12]})"
    return f"endpoint {fingerprint[:12]}"


def sanitize_urls_for_output(value: Any) -> str:
    """Remove URL credentials and route material from persisted/displayed text.

    Provider/network exceptions can embed the full request URL.  Replace every
    hierarchical URL with the same safe host+digest label used by transport
    errors, retaining enough correlation for diagnostics without userinfo,
    path, query, or fragment data.
    """

    text = str(value or "")

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        trailing = ""
        while raw and raw[-1] in ".,;:!?)]":
            trailing = raw[-1] + trailing
            raw = raw[:-1]
        return endpoint_label(raw) + trailing

    return _HIERARCHICAL_URL_RE.sub(replace_url, text)


def build_provider_route_identity(
    *,
    protocol: str,
    base_url: str,
    provider_key: str | None,
    model: str,
    profile_name: str | None = None,
    auth_provider: str | None = None,
    credential_scope: str | None = None,
    routing_headers: Mapping[str, Any] | None = None,
    routing_fields: dict[str, str] | None = None,
    reasoning_state_adapter: str | None = None,
    protocol_revision: str | None = None,
    session_scope: str | None = None,
) -> ProviderRouteIdentity:
    normalized_routing_headers = canonicalize_extra_headers(routing_headers)
    canonical_routing_headers = tuple(
        sorted(
            (
                str(name).strip().casefold(),
                credential_scope_fingerprint(value),
            )
            for name, value in normalized_routing_headers.items()
        )
    )
    canonical_routing_fields = tuple(
        sorted(
            (
                str(name).strip(),
                credential_scope_fingerprint(value),
            )
            for name, value in (routing_fields or {}).items()
            if str(name).strip() and str(value or "").strip()
        )
    )
    fields = {
        "protocol": str(protocol or "").strip().casefold(),
        "base_url": _normalize_route_base_url(base_url),
        "provider_key": str(provider_key or "").strip().casefold(),
        "model": str(model or "").strip(),
        "profile_name": str(profile_name or "").strip(),
        "auth_provider": str(auth_provider or "").strip().casefold(),
        "credential_scope": str(credential_scope or "").strip().casefold(),
        "routing_headers": canonical_routing_headers,
        "routing_fields": canonical_routing_fields,
        "reasoning_state_adapter": str(reasoning_state_adapter or "").strip().casefold(),
        "protocol_revision": str(protocol_revision or "").strip(),
        "session_scope": str(session_scope or "").strip().casefold(),
    }
    canonical = json.dumps(fields, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return ProviderRouteIdentity(**fields, fingerprint=fingerprint)


def credential_scope_fingerprint(value: Any) -> str:
    material = str(value or "").strip()
    if not material:
        return ""
    return hashlib.sha256(f"alysis-route-scope:v1:{material}".encode()).hexdigest()


def canonicalize_extra_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize configured headers once for both transport and route identity.

    HTTP header names are case-insensitive, so case-variant duplicates are
    ambiguous and rejected. Optional whitespace is removed from names and
    values before either sending or hashing, preventing wire/fingerprint drift.
    """

    canonical: dict[str, str] = {}
    for raw_name, raw_value in (headers or {}).items():
        name = str(raw_name or "").strip().casefold()
        value = str(raw_value or "").strip()
        if not name or not value:
            continue
        if name in canonical:
            raise ValueError(f"Duplicate extra header name (case-insensitive): {name}")
        canonical[name] = value
    return canonical


def merge_canonical_headers(*header_maps: Mapping[str, Any] | None) -> dict[str, str]:
    """Case-insensitively merge headers, with each later map overriding earlier maps."""

    merged: dict[str, str] = {}
    for header_map in header_maps:
        merged.update(canonicalize_extra_headers(header_map))
    return merged


def stamp_provider_metadata_for_route(
    provider_metadata: dict[str, Any] | None,
    route_identity: ProviderRouteIdentity,
) -> dict[str, Any]:
    stamped = copy.deepcopy(provider_metadata) if isinstance(provider_metadata, dict) else {}
    stamped[ROUTE_IDENTITY_PROVIDER_METADATA_KEY] = route_identity.as_metadata()
    return stamped


def stamp_response_for_route(response: Any, route_identity: ProviderRouteIdentity) -> Any:
    """Return a response whose provider metadata is bound to ``route_identity``."""

    provider_metadata = getattr(response, "provider_metadata", None)
    tool_calls = getattr(response, "tool_calls", None)
    has_tool_metadata = isinstance(tool_calls, list) and any(
        isinstance(getattr(tool_call, "provider_metadata", None), dict)
        and bool(getattr(tool_call, "provider_metadata", None))
        for tool_call in tool_calls
    )
    if not (isinstance(provider_metadata, dict) and provider_metadata) and not has_tool_metadata:
        return response
    return replace(
        response,
        provider_metadata=stamp_provider_metadata_for_route(
            provider_metadata,
            route_identity,
        ),
    )


def provider_metadata_matches_route(
    provider_metadata: Any,
    route_identity: ProviderRouteIdentity,
) -> bool:
    if not isinstance(provider_metadata, dict):
        return False
    stamped = provider_metadata.get(ROUTE_IDENTITY_PROVIDER_METADATA_KEY)
    return isinstance(stamped, dict) and stamped == route_identity.as_metadata()


def gate_messages_for_provider_route(
    messages: list[dict[str, Any]],
    route_identity: ProviderRouteIdentity,
) -> list[dict[str, Any]]:
    """Keep provider state only when its exact route stamp matches.

    Legacy unstamped state and mismatched state fail closed.  Public message
    content and tool calls remain available for ordinary full-history replay.
    """

    gated: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        metadata = message.get(PROVIDER_METADATA_KEY)
        copied = strip_provider_metadata_from_message(copy.deepcopy(message))
        if provider_metadata_matches_route(metadata, route_identity):
            copied[PROVIDER_METADATA_KEY] = copy.deepcopy(metadata)
        gated.append(copied)
    return gated


def strip_provider_metadata_from_messages(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        strip_provider_metadata_from_message(copy.deepcopy(message))
        for message in messages
        if isinstance(message, dict)
    ]


def merge_provider_metadata(*items: dict[str, Any] | None) -> dict[str, Any] | None:
    merged: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        for key, value in item.items():
            if isinstance(value, dict) and value:
                merged[str(key)] = copy.deepcopy(value)
    return merged or None


def tool_call_metadata_entries(response: Any) -> list[dict[str, Any]]:
    tool_calls = getattr(response, "tool_calls", None)
    if not isinstance(tool_calls, list):
        return []
    entries: list[dict[str, Any]] = []
    for index, tool_call in enumerate(tool_calls):
        metadata = getattr(tool_call, "provider_metadata", None)
        merged = merge_provider_metadata(metadata)
        if not merged:
            continue
        entry: dict[str, Any] = {
            "index": index,
            "metadata": merged,
        }
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if tool_call_id:
            entry["id"] = tool_call_id
        entries.append(entry)
    return entries


def strip_provider_metadata_from_message(message: dict[str, Any]) -> dict[str, Any]:
    copied = dict(message)
    copied.pop(PROVIDER_METADATA_KEY, None)
    copied.pop(INTERNAL_ARTIFACT_MESSAGE_KEY, None)
    copied.pop(DEEPSEEK_REASONING_CONTENT_KEY, None)
    copied.pop(OPENROUTER_REASONING_KEY, None)
    copied.pop(OPENROUTER_REASONING_DETAILS_KEY, None)
    return copied


def attach_provider_metadata_to_assistant_message(
    message: dict[str, Any],
    response: Any,
) -> dict[str, Any]:
    if str(message.get("role") or "") != "assistant":
        return message
    message_metadata = merge_provider_metadata(getattr(response, "provider_metadata", None))
    if not message.get("tool_calls") and not (
        isinstance(message_metadata, dict)
        and any(key in message_metadata for key in STATEFUL_PROVIDER_METADATA_KEYS)
    ):
        return message
    tool_call_metadata = tool_call_metadata_entries(response)
    if not message_metadata and not tool_call_metadata:
        return message
    copied = dict(message)
    merged = dict(message_metadata or {})
    if tool_call_metadata:
        merged[TOOL_CALL_PROVIDER_METADATA_KEY] = tool_call_metadata
    copied[PROVIDER_METADATA_KEY] = merged
    return copied


def assistant_message_from_response(
    response: Any,
    *,
    content: str | None = None,
) -> dict[str, Any]:
    message_content = getattr(response, "content", "") if content is None else content
    message: dict[str, Any] = {
        "role": "assistant",
        "content": str(message_content or ""),
    }
    tool_calls = getattr(response, "tool_calls", None)
    if isinstance(tool_calls, list) and tool_calls:
        message["tool_calls"] = [
            {
                "id": str(getattr(tool_call, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(getattr(tool_call, "name", "") or ""),
                    "arguments": json.dumps(getattr(tool_call, "arguments", {}) or {}),
                },
            }
            for tool_call in tool_calls
        ]
    return attach_provider_metadata_to_assistant_message(message, response)
