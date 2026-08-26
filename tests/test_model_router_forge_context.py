from __future__ import annotations

from typing import Any

from alysis_code.config import AppConfig
from alysis_code.model_router import ROLE_PLANNER, resolve_model_for_role


def _cfg(*, extra_fields: dict[str, Any] | None = None) -> AppConfig:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = extra_fields or {}
    return cfg


def test_resolve_model_forge_context_prefers_forge_role_models() -> None:
    cfg = _cfg(
        extra_fields={
            "role_models": {"planner": "subagent-planner"},
            "forge_role_models": {"planner": "forge-planner"},
        }
    )

    assert (
        resolve_model_for_role(cfg=cfg, role=ROLE_PLANNER, prefer_context="forge")
        == "forge-planner"
    )
    assert resolve_model_for_role(cfg=cfg, role=ROLE_PLANNER) == "subagent-planner"


def test_resolve_model_forge_context_falls_back_to_subagent_role() -> None:
    cfg = _cfg(
        extra_fields={
            "role_models": {"planner": "subagent-planner"},
            "forge_role_models": {"coding": "forge-coding"},
        }
    )

    assert (
        resolve_model_for_role(cfg=cfg, role=ROLE_PLANNER, prefer_context="forge")
        == "subagent-planner"
    )


def test_resolve_model_forge_context_falls_back_to_default_model() -> None:
    cfg = _cfg(extra_fields={"forge_role_models": {"coding": "forge-coding"}})

    assert (
        resolve_model_for_role(cfg=cfg, role=ROLE_PLANNER, prefer_context="forge")
        == "default-model"
    )


def test_resolve_model_env_var_still_wins_over_forge(monkeypatch: Any) -> None:
    monkeypatch.setenv("ALYSIS_MODEL_PLANNER", "env-planner")
    cfg = _cfg(extra_fields={"forge_role_models": {"planner": "forge-planner"}})

    assert (
        resolve_model_for_role(cfg=cfg, role=ROLE_PLANNER, prefer_context="forge") == "env-planner"
    )


def test_resolve_model_unknown_prefer_context_behaves_like_default() -> None:
    cfg = _cfg(
        extra_fields={
            "role_models": {"planner": "subagent-planner"},
            "forge_role_models": {"planner": "forge-planner"},
        }
    )

    assert (
        resolve_model_for_role(cfg=cfg, role=ROLE_PLANNER, prefer_context="bogus")
        == "subagent-planner"
    )
