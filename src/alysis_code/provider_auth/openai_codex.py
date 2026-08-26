from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from ..branding import env_get
from ..chatgpt_codex_static_provider import resolve_chatgpt_codex_static_model
from ..host_browser import open_url
from .base import (
    ProviderAccountStatus,
    ProviderAuthError,
    ProviderLoginRequiredError,
    ProviderModel,
    ProviderReasoningEffort,
)
from .store import (
    ProviderTokenRecord,
    delete_provider_token,
    load_provider_token,
    save_provider_token,
)

SESSION_EXPIRED_DETAIL = "session expired"
"""Stable ``detail`` value meaning the stored session must be re-authorized."""

_ISSUER = "https://auth.openai.com"
_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_RESPONSES_URL = f"{_CODEX_BASE_URL}/responses"
_MODELS_URL = f"{_CODEX_BASE_URL}/models"
_DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_DEFAULT_CODEX_COMPAT_VERSION = "0.144.6"
_CALLBACK_PORTS = (1455, 1457)
_BROWSER_TIMEOUT_SECONDS = 300.0
_DEVICE_TIMEOUT_SECONDS = 15.0 * 60.0
_REFRESH_SKEW_SECONDS = 300.0
_DEFAULT_TOKEN_LIFETIME_SECONDS = 3600.0
_ALLOWED_REQUESTS = frozenset({_RESPONSES_URL, _MODELS_URL})
_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_REFRESH_LOCKS_GUARD = threading.Lock()
_LOGIN_LOCK = threading.Lock()


def _client_id() -> str:
    return str(env_get("ALYSIS_OPENAI_OAUTH_CLIENT_ID") or "").strip() or _DEFAULT_CLIENT_ID


def _codex_compat_version() -> str:
    """Codex catalog compatibility version, intentionally not Alysis Code's version."""

    return (
        str(env_get("ALYSIS_OPENAI_CODEX_COMPAT_VERSION") or "").strip()
        or _DEFAULT_CODEX_COMPAT_VERSION
    )


class OpenAICodexSubscriptionAuth:
    """ChatGPT-backed Codex Responses auth for Alysis Code's native agent loop.

    The model transport intentionally lives behind this adapter because the
    ChatGPT Codex endpoint is a compatibility surface, not the ordinary OpenAI
    API. Credentials are attached only to two exact, HTTPS allowlisted URLs.
    """

    provider_id = "openai-codex"
    display_name = "ChatGPT Codex subscription"
    description = "Use Codex models from ChatGPT while keeping Alysis Code's native agent."
    profile_name = "chatgpt-codex"
    auth_hint = "Browser or device-code sign-in; credentials use Alysis Code's encrypted vault."
    base_url = _CODEX_BASE_URL
    protocol = "openai_responses"
    supports_previous_response_id = False
    supports_temperature = False
    requires_streaming = True

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._models_cache: tuple[ProviderModel, ...] | None = None

    def account_status(self) -> ProviderAccountStatus:
        """Report account state without ever raising.

        A supervising app polls this to decide what to show the user, so every
        failure has to come back as data. Reading the credential store can fail
        on its own — a spawned process may not reach the OS keyring holding the
        store's master key — so the load is guarded too, not just the refresh.
        """

        try:
            record = load_provider_token(self.provider_id)
        except ProviderAuthError as exc:
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a status probe must not crash its caller
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                detail=f"Could not read stored ChatGPT credentials ({exc.__class__.__name__}).",
            )
        if record is None:
            return ProviderAccountStatus(
                connected=False,
                detail="No ChatGPT subscription account is connected.",
            )
        try:
            record = self._valid_record(record)
        except ProviderLoginRequiredError:
            # A stable marker, not the thrown message: this is the one state a
            # supervising app must be able to branch on to prompt a re-login.
            return ProviderAccountStatus(
                connected=False,
                verified=True,
                account_label=record.account_label,
                detail=SESSION_EXPIRED_DETAIL,
            )
        except ProviderAuthError as exc:
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                account_label=record.account_label,
                detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - a status probe must not crash its caller
            return ProviderAccountStatus(
                connected=False,
                verified=False,
                account_label=record.account_label,
                detail=f"Could not verify ChatGPT credentials ({exc.__class__.__name__}).",
            )
        return ProviderAccountStatus(
            connected=True,
            account_label=record.account_label,
            detail="Connected with ChatGPT.",
        )

    def route_credential_scope(self) -> str:
        """Return a non-secret stable account scope for continuation isolation."""

        record = load_provider_token(self.provider_id)
        if record is None:
            return ""
        material = record.account_id or record.account_label or record.access_token
        if not material:
            return ""
        return hashlib.sha256(f"alysis-route-scope:v1:{material}".encode()).hexdigest()

    def login(
        self,
        method: str = "browser",
        *,
        output_write: Callable[[str], None] | None = None,
    ) -> ProviderAccountStatus:
        normalized = str(method or "browser").strip().lower()
        write = output_write or print
        if normalized not in {"browser", "device-code"}:
            raise ProviderAuthError(f"Unsupported ChatGPT sign-in method: {method!r}.")
        if not _LOGIN_LOCK.acquire(blocking=False):
            raise ProviderAuthError("Another ChatGPT sign-in is already in progress.")
        try:
            record = (
                self._browser_login(write) if normalized == "browser" else self._device_login(write)
            )
            save_provider_token(self.provider_id, record)
            self._models_cache = None
        finally:
            _LOGIN_LOCK.release()
        return ProviderAccountStatus(
            connected=True,
            account_label=record.account_label,
            detail="Connected with ChatGPT.",
        )

    def logout(self) -> ProviderAccountStatus:
        record = load_provider_token(self.provider_id)
        revoke_error: str | None = None
        if record is not None:
            try:
                with self._client(timeout=10.0) as client:
                    response = client.post(
                        f"{_ISSUER}/oauth/revoke",
                        json={
                            "token": record.refresh_token,
                            "token_type_hint": "refresh_token",
                            "client_id": _client_id(),
                        },
                    )
                if response.status_code >= 400:
                    revoke_error = f"remote revocation returned status {response.status_code}"
            except Exception as exc:  # noqa: BLE001
                # Local credential removal is still deterministic. Remote
                # revocation is best effort because auth services can be offline.
                revoke_error = f"remote revocation failed: {type(exc).__name__}"
        delete_provider_token(self.provider_id)
        self._models_cache = None
        return ProviderAccountStatus(
            connected=False,
            verified=revoke_error is None,
            detail=(
                "Disconnected locally."
                if revoke_error is None
                else f"Disconnected locally; {revoke_error}."
            ),
        )

    def authorization_headers(
        self,
        url: str,
        *,
        force_refresh: bool = False,
        session_id: str | None = None,
    ) -> Mapping[str, str]:
        normalized_url = _canonical_request_url(url)
        if normalized_url not in _ALLOWED_REQUESTS:
            raise ProviderAuthError(
                "Refusing to attach ChatGPT credentials to a non-Codex destination."
            )
        record = load_provider_token(self.provider_id)
        if record is None:
            raise ProviderLoginRequiredError(
                "ChatGPT is not connected. Run `alysis auth login openai-codex`."
            )
        record = self._valid_record(record, force_refresh=force_refresh)
        headers: dict[str, str] = {
            "Authorization": f"Bearer {record.access_token}",
            "ChatGPT-Account-Id": str(record.account_id or ""),
            # The ChatGPT Codex backend gates model availability on the native
            # Codex client identity as well as the account entitlement.  Keep
            # these headers paired with the compatibility version used for model
            # discovery; an Alysis Code-specific identity can make catalog-listed
            # models fail at inference time with a misleading 404.
            "originator": "codex_cli_rs",
            "User-Agent": f"codex_cli_rs/{_codex_compat_version()}",
        }
        if not record.account_id:
            headers.pop("ChatGPT-Account-Id", None)
        normalized_session = str(session_id or "").strip()
        if normalized_session:
            headers.update(
                {
                    "session-id": normalized_session,
                    "x-session-affinity": normalized_session,
                    "X-Session-Id": normalized_session,
                }
            )
        return headers

    def adapt_responses_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        adapted = copy.deepcopy(dict(payload))
        input_items = adapted.get("input")
        if isinstance(input_items, list):
            instructions: list[str] = []
            retained: list[Any] = []
            for item in input_items:
                if isinstance(item, dict) and str(item.get("role") or "") in {
                    "system",
                    "developer",
                }:
                    text = _message_text(item.get("content"))
                    if text:
                        instructions.append(text)
                    continue
                if isinstance(item, dict):
                    item = copy.deepcopy(item)
                    item.pop("id", None)
                    item.pop("status", None)
                retained.append(item)
            adapted["input"] = retained
            if instructions:
                existing = str(adapted.get("instructions") or "").strip()
                combined = "\n\n".join(instructions)
                adapted["instructions"] = f"{existing}\n\n{combined}" if existing else combined

        adapted["store"] = False
        adapted.pop("temperature", None)
        adapted.pop("max_output_tokens", None)
        adapted.pop("prompt_cache_retention", None)
        adapted.pop("previous_response_id", None)

        include = adapted.get("include")
        include_values = list(include) if isinstance(include, list) else []
        if "reasoning.encrypted_content" not in include_values:
            include_values.append("reasoning.encrypted_content")
        adapted["include"] = include_values

        text_config = adapted.get("text")
        if isinstance(text_config, dict):
            text_config.setdefault("verbosity", "low")
        else:
            adapted["text"] = {"verbosity": "low"}

        tools = adapted.get("tools")
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict) and str(tool.get("type") or "") == "function":
                    tool["strict"] = False
                    tool["parameters"] = _sanitize_openai_tool_schema(
                        tool.get("parameters", {"type": "object"})
                    )
        return adapted

    def list_models(self, *, refresh: bool = False) -> tuple[ProviderModel, ...]:
        if self._models_cache is not None and not refresh:
            return self._models_cache
        url = f"{_MODELS_URL}?{urlencode({'client_version': _codex_compat_version()})}"
        headers = dict(self.authorization_headers(_MODELS_URL))
        try:
            with self._client(timeout=20.0) as client:
                response = client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderAuthError("Could not load models for this ChatGPT account.") from exc
        if response.status_code == 401:
            headers = dict(self.authorization_headers(_MODELS_URL, force_refresh=True))
            try:
                with self._client(timeout=20.0) as client:
                    response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise ProviderAuthError(
                    "Could not load models for this ChatGPT account after refreshing credentials."
                ) from exc
        if response.status_code >= 400:
            raise ProviderAuthError(
                f"ChatGPT model discovery failed with status {response.status_code}."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderAuthError("ChatGPT returned an invalid model catalog.") from exc
        raw_models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(raw_models, list):
            raise ProviderAuthError("ChatGPT returned an invalid model catalog.")

        parsed: list[tuple[int, ProviderModel]] = []
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            model_id = str(raw.get("slug") or raw.get("id") or "").strip()
            visibility = str(raw.get("visibility") or "list").strip().lower()
            if not model_id or visibility in {"hide", "hidden"}:
                continue
            efforts: list[ProviderReasoningEffort] = []
            raw_efforts = raw.get("supported_reasoning_levels")
            if isinstance(raw_efforts, list):
                for entry in raw_efforts:
                    if not isinstance(entry, dict):
                        continue
                    effort_id = str(entry.get("effort") or entry.get("id") or "").strip()
                    if not effort_id:
                        continue
                    efforts.append(
                        ProviderReasoningEffort(
                            id=effort_id,
                            label=effort_id.replace("_", " ").title(),
                            description=str(entry.get("description") or "").strip(),
                        )
                    )
            static_model = resolve_chatgpt_codex_static_model(model_id)
            if not efforts and static_model is not None:
                efforts = [
                    ProviderReasoningEffort(
                        id=effort_id,
                        label=effort_id.replace("_", " ").title(),
                        description=description,
                    )
                    for effort_id, description in static_model.reasoning_efforts
                ]
            try:
                priority = int(raw.get("priority") or 9999)
            except (TypeError, ValueError):
                priority = static_model.priority if static_model is not None else 9999
            input_modalities = _input_modalities(raw)
            if (
                static_model is not None
                and "input_modalities" not in raw
                and "supports_image_input" not in raw
            ):
                input_modalities = static_model.input_modalities
            parsed.append(
                (
                    priority,
                    ProviderModel(
                        id=model_id,
                        label=str(raw.get("display_name") or model_id).strip(),
                        description=str(raw.get("description") or "").strip(),
                        reasoning_efforts=tuple(efforts),
                        default_reasoning_effort=(
                            str(raw.get("default_reasoning_level") or "").strip()
                            or (
                                static_model.default_reasoning_effort
                                if static_model is not None
                                else None
                            )
                        ),
                        input_modalities=input_modalities,
                        context_window_tokens=(
                            _positive_int(raw.get("context_window"))
                            or (
                                static_model.context_window_tokens
                                if static_model is not None
                                else None
                            )
                        ),
                        max_output_tokens=(
                            _positive_int(raw.get("max_output_tokens"))
                            or (
                                static_model.max_output_tokens if static_model is not None else None
                            )
                        ),
                    ),
                )
            )
        parsed.sort(key=lambda pair: (pair[0], pair[1].label.casefold(), pair[1].id))
        models = [model for _priority, model in parsed]
        if models:
            first = models[0]
            models[0] = ProviderModel(
                id=first.id,
                label=first.label,
                description=first.description,
                is_default=True,
                reasoning_efforts=first.reasoning_efforts,
                default_reasoning_effort=first.default_reasoning_effort,
                input_modalities=first.input_modalities,
                context_window_tokens=first.context_window_tokens,
                max_output_tokens=first.max_output_tokens,
            )
        self._models_cache = tuple(models)
        return self._models_cache

    def _valid_record(
        self,
        record: ProviderTokenRecord,
        *,
        force_refresh: bool = False,
    ) -> ProviderTokenRecord:
        if not force_refresh and record.expires_at > time.time() + _REFRESH_SKEW_SECONDS:
            return record
        lock = _refresh_lock(self.provider_id)
        with lock:
            current = load_provider_token(self.provider_id)
            if current is None:
                raise ProviderLoginRequiredError("ChatGPT credentials were removed; connect again.")
            if force_refresh and current.access_token != record.access_token:
                return current
            if not force_refresh and current.expires_at > time.time() + _REFRESH_SKEW_SECONDS:
                return current
            return self._refresh(
                current,
                allow_unexpired_fallback=not force_refresh,
            )

    def _refresh(
        self,
        record: ProviderTokenRecord,
        *,
        allow_unexpired_fallback: bool,
    ) -> ProviderTokenRecord:
        try:
            with self._client(timeout=30.0) as client:
                response = client.post(
                    f"{_ISSUER}/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": record.refresh_token,
                        "client_id": _client_id(),
                    },
                )
        except httpx.HTTPError as exc:
            if allow_unexpired_fallback and record.expires_at > time.time():
                return record
            raise ProviderAuthError(
                "ChatGPT credential refresh is temporarily unavailable; try again shortly."
            ) from exc
        if response.status_code >= 400:
            error_code = _oauth_error_code(response)
            if response.status_code in {400, 401} or error_code in {
                "invalid_grant",
                "invalid_token",
            }:
                raise ProviderLoginRequiredError(
                    "ChatGPT session expired; connect your account again."
                )
            if allow_unexpired_fallback and record.expires_at > time.time():
                return record
            raise ProviderAuthError(
                "ChatGPT credential refresh is temporarily unavailable; try again shortly."
            )
        data = _json_object(response, "ChatGPT returned an invalid refresh response.")
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or record.refresh_token).strip()
        if not access_token or not refresh_token:
            raise ProviderLoginRequiredError("ChatGPT session expired; connect your account again.")
        id_token = str(data.get("id_token") or "").strip()
        claims = _jwt_claims(id_token) if id_token else _jwt_claims(access_token)
        refreshed = ProviderTokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_token_expiry(data, access_token),
            account_id=_account_id(claims) or record.account_id,
            account_label=_account_label(claims) or record.account_label,
        )
        save_provider_token(self.provider_id, refreshed)
        return refreshed

    def _browser_login(self, write: Callable[[str], None]) -> ProviderTokenRecord:
        verifier = _pkce_verifier()
        challenge = _pkce_challenge(verifier)
        expected_state = secrets.token_urlsafe(32)
        server = _bind_callback_server()
        actual_port = int(server.server_address[1])
        redirect_uri = f"http://localhost:{actual_port}/auth/callback"
        params = {
            "response_type": "code",
            "client_id": _client_id(),
            "redirect_uri": redirect_uri,
            "scope": "openid profile email offline_access",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "state": expected_state,
            "originator": "alysis",
        }
        auth_url = f"{_ISSUER}/oauth/authorize?{urlencode(params)}"
        callback: dict[str, str] = {}
        server.RequestHandlerClass = _callback_handler(callback, expected_state)
        server.timeout = 0.5
        write("Opening ChatGPT sign-in in your browser…")
        if not open_url(auth_url):
            write(f"Open this URL to continue: {auth_url}")
        deadline = time.monotonic() + _BROWSER_TIMEOUT_SECONDS
        try:
            while time.monotonic() < deadline and "done" not in callback:
                server.handle_request()
        finally:
            server.server_close()
        if "done" not in callback:
            raise ProviderAuthError("ChatGPT browser sign-in timed out after five minutes.")
        if callback.get("error"):
            raise ProviderAuthError(f"ChatGPT sign-in failed: {callback['error']}")
        code = callback.get("code") or ""
        if not code:
            raise ProviderAuthError("ChatGPT sign-in did not return an authorization code.")
        return self._exchange_code(code=code, verifier=verifier, redirect_uri=redirect_uri)

    def _device_login(self, write: Callable[[str], None]) -> ProviderTokenRecord:
        try:
            with self._client(timeout=30.0) as client:
                response = client.post(
                    f"{_ISSUER}/api/accounts/deviceauth/usercode",
                    json={"client_id": _client_id()},
                )
        except httpx.HTTPError as exc:
            raise ProviderAuthError("Could not start ChatGPT device-code sign-in.") from exc
        if response.status_code >= 400:
            raise ProviderAuthError(
                f"ChatGPT device-code sign-in is unavailable (status {response.status_code})."
            )
        data = _json_object(response, "ChatGPT returned an invalid device-code response.")
        device_auth_id = str(data.get("device_auth_id") or "").strip()
        user_code = str(data.get("user_code") or data.get("usercode") or "").strip()
        try:
            interval = max(1.0, float(data.get("interval") or 5.0))
        except (TypeError, ValueError):
            interval = 5.0
        if not device_auth_id or not user_code:
            raise ProviderAuthError("ChatGPT returned an invalid device-code response.")
        verification_url = f"{_ISSUER}/codex/device"
        write(f"Open {verification_url}")
        write(f"Enter this one-time code: {user_code}")
        deadline = time.monotonic() + _DEVICE_TIMEOUT_SECONDS
        code_data: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            try:
                with self._client(timeout=30.0) as client:
                    poll = client.post(
                        f"{_ISSUER}/api/accounts/deviceauth/token",
                        json={"device_auth_id": device_auth_id, "user_code": user_code},
                    )
            except httpx.HTTPError as exc:
                raise ProviderAuthError("ChatGPT device-code sign-in was interrupted.") from exc
            if poll.status_code < 400:
                code_data = _json_object(poll, "ChatGPT returned an invalid device token.")
                break
            if poll.status_code not in {403, 404}:
                raise ProviderAuthError(
                    f"ChatGPT device-code sign-in failed with status {poll.status_code}."
                )
            time.sleep(min(interval, max(0.1, deadline - time.monotonic())))
        if code_data is None:
            raise ProviderAuthError("ChatGPT device-code sign-in timed out after 15 minutes.")
        code = str(code_data.get("authorization_code") or "").strip()
        verifier = str(code_data.get("code_verifier") or "").strip()
        if not code or not verifier:
            raise ProviderAuthError("ChatGPT returned an invalid device authorization.")
        return self._exchange_code(
            code=code,
            verifier=verifier,
            redirect_uri=f"{_ISSUER}/deviceauth/callback",
        )

    def _exchange_code(
        self,
        *,
        code: str,
        verifier: str,
        redirect_uri: str,
    ) -> ProviderTokenRecord:
        try:
            with self._client(timeout=30.0) as client:
                response = client.post(
                    f"{_ISSUER}/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": _client_id(),
                        "code_verifier": verifier,
                    },
                )
        except httpx.HTTPError as exc:
            raise ProviderAuthError("Could not complete ChatGPT token exchange.") from exc
        if response.status_code >= 400:
            raise ProviderAuthError(
                f"ChatGPT token exchange failed with status {response.status_code}."
            )
        data = _json_object(response, "ChatGPT returned an invalid token response.")
        access_token = str(data.get("access_token") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        id_token = str(data.get("id_token") or "").strip()
        if not access_token or not refresh_token:
            raise ProviderAuthError("ChatGPT token response was incomplete.")
        claims = _jwt_claims(id_token) if id_token else _jwt_claims(access_token)
        return ProviderTokenRecord(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=_token_expiry(data, access_token),
            account_id=_account_id(claims),
            account_label=_account_label(claims),
        )

    def _client(self, *, timeout: float) -> httpx.Client:
        return httpx.Client(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
        )


def _refresh_lock(provider_id: str) -> threading.Lock:
    with _REFRESH_LOCKS_GUARD:
        return _REFRESH_LOCKS.setdefault(provider_id, threading.Lock())


def _canonical_request_url(url: str) -> str:
    split = urlsplit(str(url or ""))
    if split.scheme != "https" or split.hostname != "chatgpt.com" or split.query or split.fragment:
        return ""
    port = split.port
    if port not in {None, 443}:
        return ""
    return f"https://chatgpt.com{split.path.rstrip('/')}"


def _pkce_verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(64)).decode("ascii").rstrip("=")


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _bind_callback_server() -> HTTPServer:
    last_error: OSError | None = None
    for port in _CALLBACK_PORTS:
        try:
            return HTTPServer(("127.0.0.1", port), BaseHTTPRequestHandler)
        except OSError as exc:
            last_error = exc
    raise ProviderAuthError("Could not open the local ChatGPT login callback port.") from last_error


def _callback_handler(
    result: dict[str, str],
    expected_state: str,
) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            split = urlsplit(self.path)
            if split.path != "/auth/callback":
                self._respond(404, "Not found")
                return
            query = parse_qs(split.query, keep_blank_values=True)
            state = str((query.get("state") or [""])[0])
            if not hmac.compare_digest(state, expected_state):
                self._respond(400, "State mismatch. Return to Alysis Code and try again.")
                return
            error = str((query.get("error_description") or query.get("error") or [""])[0])
            if error:
                result.update({"done": "1", "error": error})
                self._respond(400, error)
                return
            code = str((query.get("code") or [""])[0])
            if not code:
                result.update({"done": "1", "error": "Missing authorization code."})
                self._respond(400, "Missing authorization code.")
                return
            result.update({"done": "1", "code": code})
            self._respond(200, "Connected. You can close this tab and return to Alysis Code.")

        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _respond(self, status: int, message: str) -> None:
            safe = html.escape(message, quote=True)
            body = (
                "<!doctype html><meta charset='utf-8'><title>Alysis Code sign-in</title>"
                "<style>body{font-family:system-ui;max-width:42rem;margin:4rem auto;"
                "padding:0 1.5rem;line-height:1.5}</style>"
                f"<h1>Alysis Code</h1><p>{safe}</p>"
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def _json_object(response: httpx.Response, message: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderAuthError(message) from exc
    if not isinstance(data, dict):
        raise ProviderAuthError(message)
    return data


def _oauth_error_code(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return ""
    if not isinstance(data, dict):
        return ""
    error = data.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or error.get("type") or "").strip().lower()
    return str(error or "").strip().lower()


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        data = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _auth_claims(claims: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = claims.get("https://api.openai.com/auth")
    return nested if isinstance(nested, dict) else {}


def _account_id(claims: Mapping[str, Any]) -> str | None:
    for value in (
        claims.get("chatgpt_account_id"),
        _auth_claims(claims).get("chatgpt_account_id"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    organizations = claims.get("organizations")
    if isinstance(organizations, list) and organizations:
        first = organizations[0]
        if isinstance(first, dict):
            normalized = str(first.get("id") or "").strip()
            if normalized:
                return normalized
    return None


def _account_label(claims: Mapping[str, Any]) -> str | None:
    email = str(claims.get("email") or "").strip()
    profile = claims.get("https://api.openai.com/profile")
    if not email and isinstance(profile, dict):
        email = str(profile.get("email") or "").strip()
    plan = str(_auth_claims(claims).get("chatgpt_plan_type") or "").strip()
    if email and plan:
        return f"{email} ({plan})"
    return email or (f"ChatGPT {plan}" if plan else None)


def _token_expiry(data: Mapping[str, Any], access_token: str) -> float:
    try:
        expires_in = float(data.get("expires_in") or 0)
    except (TypeError, ValueError):
        expires_in = 0
    if expires_in > 0:
        return time.time() + expires_in
    claims = _jwt_claims(access_token)
    try:
        expiration = float(claims.get("exp") or 0)
    except (TypeError, ValueError):
        expiration = 0
    return expiration if expiration > time.time() else time.time() + _DEFAULT_TOKEN_LIFETIME_SECONDS


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return str(content or "").strip()
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            value = item.get("text") or item.get("content")
            if isinstance(value, str):
                parts.append(value)
    return "\n".join(part for part in parts if part).strip()


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _input_modalities(raw: Mapping[str, Any]) -> tuple[str, ...]:
    advertised = raw.get("input_modalities")
    if isinstance(advertised, list):
        values = tuple(
            dict.fromkeys(
                str(item or "").strip().lower() for item in advertised if str(item or "").strip()
            )
        )
        if values:
            return values
    if raw.get("supports_image_input") is True:
        return ("text", "image")
    return ("text",)


def _sanitize_openai_tool_schema(value: Any) -> Any:
    """Lower broad JSON Schema/MCP shapes to Codex-compatible tool schemas."""

    supported_types = {"string", "number", "boolean", "integer", "object", "array", "null"}
    composition_keys = ("anyOf", "oneOf", "allOf")
    if isinstance(value, bool):
        return {"type": "string"}
    if isinstance(value, list):
        return [_sanitize_openai_tool_schema(item) for item in value]
    if not isinstance(value, dict):
        return value

    result: dict[str, Any] = {}
    if isinstance(value.get("$ref"), str):
        result["$ref"] = value["$ref"]
    if isinstance(value.get("description"), str):
        result["description"] = value["description"]
    if "const" in value:
        result["enum"] = [value["const"]]
    elif isinstance(value.get("enum"), list):
        result["enum"] = copy.deepcopy(value["enum"])
    properties = value.get("properties")
    if isinstance(properties, dict):
        result["properties"] = {
            str(key): _sanitize_openai_tool_schema(item) for key, item in properties.items()
        }
    required = value.get("required")
    if isinstance(required, list):
        result["required"] = [item for item in required if isinstance(item, str)]
    if "items" in value:
        result["items"] = _sanitize_openai_tool_schema(value["items"])
    if "additionalProperties" in value:
        additional = value["additionalProperties"]
        result["additionalProperties"] = (
            additional if isinstance(additional, bool) else _sanitize_openai_tool_schema(additional)
        )
    for key in composition_keys:
        if isinstance(value.get(key), list):
            result[key] = [_sanitize_openai_tool_schema(item) for item in value[key]]
    for key in ("$defs", "definitions"):
        definitions = value.get(key)
        if isinstance(definitions, dict):
            result[key] = {
                str(name): _sanitize_openai_tool_schema(item) for name, item in definitions.items()
            }

    raw_type = value.get("type")
    if isinstance(raw_type, str):
        schema_types = [raw_type] if raw_type in supported_types else []
    elif isinstance(raw_type, list):
        schema_types = [
            item for item in raw_type if isinstance(item, str) and item in supported_types
        ]
    else:
        schema_types = []
    if not schema_types and ("$ref" in result or any(key in result for key in composition_keys)):
        return result
    if not schema_types:
        if any(key in value for key in ("properties", "required", "additionalProperties")):
            schema_types = ["object"]
        elif any(key in value for key in ("items", "prefixItems")):
            schema_types = ["array"]
        elif "enum" in result or "format" in value:
            schema_types = ["string"]
        elif any(
            key in value
            for key in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf")
        ):
            schema_types = ["number"]
    if not schema_types:
        return {}
    result["type"] = schema_types[0] if len(schema_types) == 1 else schema_types
    if "object" in schema_types and "properties" not in result:
        result["properties"] = {}
    if "array" in schema_types and "items" not in result:
        result["items"] = {"type": "string"}
    return result


__all__ = ["SESSION_EXPIRED_DETAIL", "OpenAICodexSubscriptionAuth"]
