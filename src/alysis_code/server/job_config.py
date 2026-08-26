from __future__ import annotations

from .settings import ServerSettings


def resolve_effective_model_base_url(
    settings: ServerSettings,
    requested_model: str | None,
    requested_base_url: str | None,
) -> tuple[str, str | None]:
    req_model = (requested_model or "").strip() or None
    req_base_url = (requested_base_url or "").strip() or None

    if settings.default_model:
        model = settings.default_model
    elif settings.allow_client_model and req_model:
        model = req_model
    else:
        raise ValueError("Model is required (set ALYSIS_SERVER_MODEL or provide request.model).")

    if settings.default_base_url:
        base_url = settings.default_base_url
    elif settings.allow_client_base_url and req_base_url:
        if not req_base_url.startswith(("http://", "https://")):
            raise ValueError("Client base_url must use http:// or https://.")
        base_url = req_base_url
    else:
        base_url = None

    return model, base_url
