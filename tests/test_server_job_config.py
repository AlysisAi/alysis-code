from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.server.job_config import resolve_effective_model_base_url
from alysis_code.server.settings import ServerSettings


def _settings(
    *,
    default_model: str | None = None,
    default_base_url: str | None = None,
    allow_client_model: bool = True,
    allow_client_base_url: bool = False,
) -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=Path("/tmp/alysis-server-test"),
        token=None,
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="bwrap",
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model=default_model,
        default_base_url=default_base_url,
        allow_client_model=allow_client_model,
        allow_client_base_url=allow_client_base_url,
    )


def test_base_url_is_ignored_by_default() -> None:
    model, base_url = resolve_effective_model_base_url(
        _settings(default_model="server-model"),
        requested_model=None,
        requested_base_url="https://attacker.example/v1",
    )
    assert model == "server-model"
    assert base_url is None


def test_model_required_unless_default_model_present() -> None:
    with pytest.raises(ValueError, match="Model is required"):
        resolve_effective_model_base_url(
            _settings(default_model=None, allow_client_model=False),
            requested_model=None,
            requested_base_url=None,
        )


def test_allow_client_model_when_enabled_and_no_default_model() -> None:
    model, _ = resolve_effective_model_base_url(
        _settings(default_model=None, allow_client_model=True),
        requested_model="client-model",
        requested_base_url=None,
    )
    assert model == "client-model"


def test_default_model_takes_precedence_over_client_model() -> None:
    model, _ = resolve_effective_model_base_url(
        _settings(default_model="server-model", allow_client_model=True),
        requested_model="client-model",
        requested_base_url=None,
    )
    assert model == "server-model"


def test_client_base_url_requires_http_or_https_when_override_enabled() -> None:
    with pytest.raises(ValueError, match="http:// or https://"):
        resolve_effective_model_base_url(
            _settings(default_model="server-model", allow_client_base_url=True),
            requested_model=None,
            requested_base_url="ftp://example.com",
        )
