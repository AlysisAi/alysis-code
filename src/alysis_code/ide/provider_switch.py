"""Apply a provider route without replacing the conversation or its workspace."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from ..config import AppConfig, resolve_api_key
from ..llm.factory import make_llm_client


def switch_session_provider(
    session: Any,
    cfg: AppConfig,
    *,
    refresh: Callable[[Any, AppConfig], None],
    persist: Callable[[], None],
) -> None:
    """Build protocol-specific clients first and roll back a rejected configuration.

    The caller holds the bridge's idle-session lock. Old clients remain untouched,
    including their authentication and route-local caches, until the switch commits.
    """
    key = resolve_api_key(cfg).key or ""

    def replacement(client: Any) -> Any:
        if client is None:
            return None
        # The config refresh below assigns role-specific models and cache policies.
        # The factory selects the actual transport class.
        return make_llm_client(
            cfg=cfg,
            api_key=key,
            model=cfg.model,
            session_id=getattr(getattr(session, "store", None), "session_id", None),
        )

    client = replacement(getattr(session, "client", None))
    old_router = getattr(session, "router_client", None)
    old_provisioned = getattr(session, "_provisioned_router_client", None)
    router = replacement(old_router)
    provisioned = router if old_provisioned is old_router else replacement(old_provisioned)
    compactor = copy.copy(getattr(session, "conversation_compactor", None))
    if compactor is not None:
        compactor.compactor_client = replacement(getattr(compactor, "compactor_client", None))

    snapshot = vars(session).copy()
    try:
        session.client = client
        # Refresh the provisioned selector too, even when temporarily disabled.
        session.router_client = router or provisioned
        session.conversation_compactor = compactor
        session.persona_client_cache = {}
        session.persona_client_key = None
        if hasattr(session, "messages"):
            session.messages = copy.deepcopy(session.messages)
        refresh(session, cfg)
        session._provisioned_router_client = provisioned
        session.router_client = router
        persist()
    except Exception:
        vars(session).clear()
        vars(session).update(snapshot)
        raise
