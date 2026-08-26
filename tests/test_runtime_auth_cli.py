from __future__ import annotations

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.agent_runtimes.base import RuntimeAccountStatus
from alysis_code.cli import app
from alysis_code.cli_impl.commands import auth as auth_mod
from alysis_code.config import AppConfig
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    set_active_profile,
)
from alysis_code.provider_auth import (
    ProviderAccountStatus,
    ProviderModel,
    ProviderReasoningEffort,
)


def test_unified_login_rows_offer_alysis_and_chatgpt_codex() -> None:
    rows = auth_mod.login_connection_rows()

    assert [row[0] for row in rows] == ["alysis", "openai-codex"]
    assert [row[1] for row in rows] == [
        "Alysis Code account",
        "ChatGPT Codex subscription",
    ]


def test_unified_login_routes_subscription_choice_to_provider_flow(monkeypatch) -> None:
    connected: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        auth_mod,
        "login_provider_interactively",
        lambda *, runtime_id=None, device_code=False: connected.append((runtime_id, device_code)),
    )

    auth_mod.login_connection_interactively("openai-codex")

    assert connected == [("openai-codex", False)]


class _DirectAdapter:
    profile_name = "chatgpt-codex"
    protocol = "openai_responses"
    base_url = "https://chatgpt.com/backend-api/codex"
    display_name = "ChatGPT Codex subscription"

    def __init__(self) -> None:
        self.login_methods: list[str] = []

    def login(self, *, method: str, output_write):  # type: ignore[no-untyped-def]
        self.login_methods.append(method)
        return ProviderAccountStatus(
            connected=True,
            account_label="developer@example.test",
        )

    def logout(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(connected=False)

    def account_status(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(
            connected=True,
            account_label="developer@example.test",
            detail="Connected with ChatGPT.",
        )

    def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        assert refresh is True
        return (
            ProviderModel(
                id="gpt-codex-test",
                label="GPT Codex Test",
                is_default=True,
                reasoning_efforts=(ProviderReasoningEffort("medium", "Medium"),),
                default_reasoning_effort="medium",
            ),
        )


def test_auth_login_activates_native_subscription_profile(monkeypatch) -> None:
    cfg = AppConfig()
    saved: list[AppConfig] = []
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        cli_mod, "save_config", lambda value: saved.append(value.model_copy(deep=True))
    )
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert "Connected" in result.output
    assert adapter.login_methods == ["browser"]
    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.model == ""
    assert cfg.llm_reasoning_effort is None
    assert get_active_profile(cfg).auth_provider == "openai-codex"
    assert get_active_profile(cfg).reasoning_effort is None
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert "Choose the subscription model and reasoning effort" in result.output
    assert saved and saved[0].model_dump() == cfg.model_dump()
    profile_payload = cfg.extra_fields["profiles"]["chatgpt-codex"]
    assert not any("token" in key.casefold() for key in profile_payload)


def test_auth_login_preserves_existing_config_selection(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-codex-test", llm_reasoning_effort="medium")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-codex-test",
        reasoning_effort="medium",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert get_active_profile(cfg).default_model == "gpt-codex-test"
    assert get_active_profile(cfg).reasoning_effort == "medium"
    assert "subscription_model_selection_required" not in cfg.extra_fields


def test_auth_login_does_not_confirm_a_migrated_selection(monkeypatch) -> None:
    cfg = AppConfig(model="gpt-codex-test", llm_reasoning_effort="medium")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="gpt-codex-test",
        reasoning_effort="medium",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    cfg.extra_fields["subscription_model_selection_required"] = "openai-codex"
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert "Choose the subscription model and reasoning effort" in result.output


def test_auth_login_marks_unavailable_saved_selection_for_config_without_replacing_it(
    monkeypatch,
) -> None:
    cfg = AppConfig(model="old-account-model", llm_reasoning_effort="high")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="old-account-model",
        reasoning_effort="high",
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "login", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert get_active_profile(cfg).default_model == "old-account-model"
    assert get_active_profile(cfg).reasoning_effort == "high"
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"


def test_provider_auth_login_supports_device_code(monkeypatch) -> None:
    cfg = AppConfig()
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(cli_mod, "save_config", lambda _value: None)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(
        app,
        ["auth", "login", "openai-codex", "--device-code"],
    )

    assert result.exit_code == 0, result.output
    assert adapter.login_methods == ["device-code"]


def test_top_level_connect_command_is_removed() -> None:
    result = CliRunner().invoke(app, ["connect", "openai-codex"])

    assert result.exit_code == 2
    assert "No such command 'connect'" in result.output


def test_auth_status_reports_native_subscription_account(monkeypatch) -> None:
    cfg = AppConfig()
    adapter = _DirectAdapter()
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(auth_mod, "create_provider_auth", lambda _provider_id: adapter)

    result = CliRunner().invoke(app, ["auth", "status", "openai-codex"])

    assert result.exit_code == 0, result.output
    assert "ChatGPT Codex subscription" in result.output
    assert "native Alysis Code client (openai_responses)" in result.output
    assert "Authenticated: yes" in result.output
    assert "developer@example.test" in result.output


def test_auth_logout_leaves_selection_but_removes_provider_credentials(monkeypatch) -> None:
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "codex"},
        },
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        auth_mod,
        "logout_runtime",
        lambda *_args, **_kwargs: RuntimeAccountStatus(authenticated=False),
    )

    result = CliRunner().invoke(app, ["auth", "logout"])

    assert result.exit_code == 0, result.output
    assert "Disconnected" in result.output
    assert cfg.execution.backend == "delegated"
    assert cfg.execution.runtime == "openai-codex"


def test_auth_logout_does_not_claim_success_when_provider_could_not_run(monkeypatch) -> None:
    cfg = AppConfig(
        execution={"backend": "delegated", "runtime": "openai-codex"},
        agent_runtimes={
            "openai-codex": {"adapter": "codex-cli", "executable": "missing-codex"},
        },
    )
    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(
        auth_mod,
        "logout_runtime",
        lambda *_args, **_kwargs: RuntimeAccountStatus(
            authenticated=False,
            verified=False,
            detail="Codex CLI is missing.",
        ),
    )

    result = CliRunner().invoke(app, ["auth", "logout"])

    assert result.exit_code == 1
    assert "Logout failed" in result.output
    assert "Disconnected" not in result.output
