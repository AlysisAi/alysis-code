"""Guarantees for installs that predate the Sylliptor -> Alysis Code rename.

Each test here stands in for a way a user could silently lose data or access.
They are deliberately blunt: if one fails, someone upgrading loses their
credentials, their stored tokens, or their working configuration.
"""

from __future__ import annotations

import base64
import json
import os
import runpy
import sys
import warnings
from pathlib import Path

import pytest

from alysis_code import branding
from alysis_code.branding import (
    ENV_PREFIX,
    LEGACY_ENV_PREFIX,
    env_get,
    env_get_from,
    legacy_env_name,
    plugin_manifest_candidates,
    resolve_project_dir,
    with_legacy_env_aliases,
)


def test_child_repetition_replay_uses_canonical_package_and_brand() -> None:
    root = Path(__file__).resolve().parents[1]
    namespace = runpy.run_path(os.fspath(root / "scripts" / "qa" / "replay_child_repetition.py"))

    fingerprint = namespace["_child_tool_outcome_fingerprint"]
    parser = namespace["_parser"]()
    assert fingerprint.__module__ == "alysis_code.agent.turn.core"
    assert "Alysis Code's real repetition" in parser.description


@pytest.fixture(autouse=True)
def _clear_notices():
    branding.consume_legacy_notices()
    yield
    branding.consume_legacy_notices()


# --- environment variables --------------------------------------------------


def test_legacy_env_var_is_still_read(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_API_KEY", raising=False)
    monkeypatch.setenv("SYLLIPTOR_API_KEY", "from-legacy")

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert env_get("ALYSIS_API_KEY") == "from-legacy"


def test_current_env_var_wins_over_legacy(monkeypatch) -> None:
    """A user midway through migrating gets the value they most recently set."""
    monkeypatch.setenv("SYLLIPTOR_API_KEY", "old")
    monkeypatch.setenv("ALYSIS_API_KEY", "new")

    assert env_get("ALYSIS_API_KEY") == "new"


def test_legacy_env_fallback_works_with_an_injected_mapping() -> None:
    source = {
        "SYLLIPTOR_REMOTE_SYNC": "strict",
        "SYLLIPTOR_REMOTE_NAME": "legacy-origin",
    }

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert env_get_from(source, "ALYSIS_REMOTE_SYNC") == "strict"
        assert env_get_from(source, "ALYSIS_REMOTE_NAME") == "legacy-origin"


def test_current_env_wins_in_an_injected_mapping() -> None:
    source = {
        "ALYSIS_REMOTE_SYNC": "warn",
        "SYLLIPTOR_REMOTE_SYNC": "strict",
    }

    assert env_get_from(source, "ALYSIS_REMOTE_SYNC") == "warn"


def test_reading_a_legacy_var_warns_exactly_once(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_THEME", raising=False)
    monkeypatch.setenv("SYLLIPTOR_THEME", "dark")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        env_get("ALYSIS_THEME")
        env_get("ALYSIS_THEME")

    deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(deprecations) == 1
    assert "SYLLIPTOR_THEME" in str(deprecations[0].message)
    assert "ALYSIS_THEME" in str(deprecations[0].message)


def test_unknown_var_without_legacy_counterpart_returns_default(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_NOT_A_REAL_SETTING", raising=False)
    assert env_get("ALYSIS_NOT_A_REAL_SETTING", "fallback") == "fallback"


def test_non_prefixed_names_are_untouched(monkeypatch) -> None:
    """PATH must not acquire a phantom SYLLIPTOR_PATH lookup."""
    monkeypatch.setenv("PATH", "/usr/bin")
    assert legacy_env_name("PATH") is None
    assert env_get("PATH") == "/usr/bin"


def test_legacy_name_derivation_is_prefix_only() -> None:
    assert legacy_env_name(f"{ENV_PREFIX}TOOL_NAME") == f"{LEGACY_ENV_PREFIX}TOOL_NAME"


# --- user-authored subprocesses ---------------------------------------------


def test_hook_and_tool_env_carries_both_spellings() -> None:
    """User hooks and custom tools predate the rename and read the old names."""
    env = with_legacy_env_aliases({"ALYSIS_TOOL_NAME": "fs_write", "PATH": "/usr/bin"})

    assert env["ALYSIS_TOOL_NAME"] == "fs_write"
    assert env["SYLLIPTOR_TOOL_NAME"] == "fs_write"
    assert "SYLLIPTOR_PATH" not in env


def test_existing_legacy_value_is_not_overwritten() -> None:
    env = with_legacy_env_aliases(
        {"ALYSIS_TOOL_NAME": "new", "SYLLIPTOR_TOOL_NAME": "explicitly-set"}
    )
    assert env["SYLLIPTOR_TOOL_NAME"] == "explicitly-set"


# --- per-repo project directory ---------------------------------------------


def test_legacy_project_dir_is_used_in_place(tmp_path: Path) -> None:
    """Renaming a committed .sylliptor/ would show up as an unexplained diff."""
    (tmp_path / ".sylliptor").mkdir()
    assert resolve_project_dir(tmp_path).name == ".sylliptor"


def test_current_project_dir_wins_when_both_exist(tmp_path: Path) -> None:
    (tmp_path / ".sylliptor").mkdir()
    (tmp_path / ".alysis").mkdir()
    assert resolve_project_dir(tmp_path).name == ".alysis"


def test_fresh_repo_gets_the_new_project_dir(tmp_path: Path) -> None:
    assert resolve_project_dir(tmp_path).name == ".alysis"


# --- user config / data directories -----------------------------------------


def test_config_dir_is_seeded_from_a_legacy_install(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    (legacy / "credentials.json").write_text(json.dumps({"profile_keys": {}}), encoding="utf-8")

    monkeypatch.setattr(
        branding,
        "user_config_dir",
        lambda app, _author: os.fspath(legacy if app == branding.LEGACY_APP_NAME else current),
    )

    resolved = branding.canonical_user_config_dir()

    assert resolved == current
    assert (current / "credentials.json").is_file()
    # Copied, not moved: an older install on the same machine still works.
    assert (legacy / "credentials.json").is_file()


def test_legacy_dir_migration_runs_only_once(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    (legacy / "config.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        branding,
        "user_config_dir",
        lambda app, _author: os.fspath(legacy if app == branding.LEGACY_APP_NAME else current),
    )

    branding.canonical_user_config_dir()
    # Simulate the user deleting the migrated copy: the marker must stop us from
    # silently resurrecting stale credentials.
    import shutil

    shutil.rmtree(current)
    branding.canonical_user_config_dir()

    assert not current.exists()
    assert (legacy / branding.MIGRATION_MARKER_NAME).is_file()


def test_existing_current_dir_is_never_clobbered(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "legacy"
    current = tmp_path / "current"
    legacy.mkdir()
    current.mkdir()
    (legacy / "config.json").write_text('{"from": "legacy"}', encoding="utf-8")
    (current / "config.json").write_text('{"from": "current"}', encoding="utf-8")

    monkeypatch.setattr(
        branding,
        "user_config_dir",
        lambda app, _author: os.fspath(legacy if app == branding.LEGACY_APP_NAME else current),
    )

    branding.canonical_user_config_dir()

    assert json.loads((current / "config.json").read_text(encoding="utf-8")) == {"from": "current"}


# --- MCP OAuth token store --------------------------------------------------


def test_envelope_sealed_with_the_old_aad_still_decrypts(tmp_path: Path, monkeypatch) -> None:
    """AES-GCM authenticates the AAD, so the rename would hard-fail decryption."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from alysis_code.mcp import token_store

    key = bytes(range(32))
    nonce = bytes(range(12))
    payload = {"servers": {"example": {"access_token": "secret"}}}
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(payload).encode("utf-8"), token_store.LEGACY_TOKEN_STORE_AAD
    )

    envelope = {
        "version": token_store.CURRENT_ENVELOPE_VERSION,
        "key_source": token_store.KEY_SOURCE_KEYRING,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }

    monkeypatch.setattr(
        token_store,
        "_key_material_for_source",
        lambda _source, path: token_store._KeyMaterial(token_store.KEY_SOURCE_KEYRING, key),
    )
    monkeypatch.setattr(token_store, "_maybe_rewrite_envelope_after_read", lambda *a, **k: None)

    decrypted = token_store._decrypt_envelope(tmp_path / "tokens.json", envelope)
    assert decrypted == payload


def test_keyring_master_key_is_adopted_from_the_legacy_service(monkeypatch) -> None:
    """Losing this entry orphans every stored MCP OAuth token, silently."""
    from alysis_code.mcp import token_store

    stored: dict[tuple[str, str], str] = {
        (token_store._LEGACY_KEYRING_SERVICE, token_store._KEYRING_ACCOUNT): "legacy-master-key"
    }

    monkeypatch.setattr(
        token_store,
        "_get_keyring_password",
        lambda service, account: stored.get((service, account)),
    )
    monkeypatch.setattr(
        token_store,
        "_set_keyring_password",
        lambda service, account, value: stored.__setitem__((service, account), value),
    )

    assert token_store._read_keyring_master_key() == "legacy-master-key"
    # Adopted under the new service name, old entry left intact.
    assert (
        stored[(token_store._KEYRING_SERVICE, token_store._KEYRING_ACCOUNT)] == "legacy-master-key"
    )
    assert stored[(token_store._LEGACY_KEYRING_SERVICE, token_store._KEYRING_ACCOUNT)] == (
        "legacy-master-key"
    )


# --- plugins ----------------------------------------------------------------


def test_both_plugin_manifest_filenames_resolve(tmp_path: Path) -> None:
    from alysis_code.extensions.manifest import resolve_manifest_path

    legacy_plugin = tmp_path / "legacy"
    legacy_plugin.mkdir()
    (legacy_plugin / "sylliptor-plugin.toml").write_text("", encoding="utf-8")
    assert resolve_manifest_path(legacy_plugin).name == "sylliptor-plugin.toml"

    current_plugin = tmp_path / "current"
    current_plugin.mkdir()
    (current_plugin / "alysis-plugin.toml").write_text("", encoding="utf-8")
    assert resolve_manifest_path(current_plugin).name == "alysis-plugin.toml"


def test_manifest_candidates_prefer_the_current_name() -> None:
    assert plugin_manifest_candidates()[0] == "alysis-plugin.toml"


def test_manifest_compatibility_accepts_the_legacy_key() -> None:
    """extra='forbid' would otherwise reject every already-published plugin."""
    from alysis_code.extensions.manifest import Compatibility

    assert Compatibility.model_validate({"sylliptor": ">=0.1"}).alysis == ">=0.1"
    assert Compatibility.model_validate({"alysis": ">=0.1"}).alysis == ">=0.1"


# --- hosted Pro profile -----------------------------------------------------


def test_cloud_profile_is_renamed_on_config_load() -> None:
    from alysis_code.config import CLOUD_PROFILE_KEY, LEGACY_CLOUD_PROFILE_KEY
    from alysis_code.profiles import _rename_cloud_profile

    extra = {
        "profiles": {LEGACY_CLOUD_PROFILE_KEY: {"name": LEGACY_CLOUD_PROFILE_KEY, "protocol": "x"}},
        "active_profile": LEGACY_CLOUD_PROFILE_KEY,
    }

    assert _rename_cloud_profile(extra) is True
    assert LEGACY_CLOUD_PROFILE_KEY not in extra["profiles"]
    assert extra["profiles"][CLOUD_PROFILE_KEY]["name"] == CLOUD_PROFILE_KEY
    assert extra["active_profile"] == CLOUD_PROFILE_KEY


def test_cloud_profile_rename_prefers_an_existing_current_entry() -> None:
    from alysis_code.config import CLOUD_PROFILE_KEY, LEGACY_CLOUD_PROFILE_KEY
    from alysis_code.profiles import _rename_cloud_profile

    extra = {
        "profiles": {
            LEGACY_CLOUD_PROFILE_KEY: {"name": LEGACY_CLOUD_PROFILE_KEY, "protocol": "stale"},
            CLOUD_PROFILE_KEY: {"name": CLOUD_PROFILE_KEY, "protocol": "current"},
        }
    }

    _rename_cloud_profile(extra)

    assert LEGACY_CLOUD_PROFILE_KEY not in extra["profiles"]
    assert extra["profiles"][CLOUD_PROFILE_KEY]["protocol"] == "current"


# --- write protection -------------------------------------------------------


def test_legacy_runtime_dirs_stay_write_protected() -> None:
    """A repo still carrying .sylliptor/ must not become writable by the agent."""
    from alysis_code.agent.prompt_context import ALWAYS_PROTECTED_WRITE_PREFIXES

    for prefix in (".alysis", ".sylliptor", ".git"):
        assert prefix in ALWAYS_PROTECTED_WRITE_PREFIXES


# --- frozen wire constants --------------------------------------------------


def test_release_signature_domain_is_frozen_at_its_prerebrand_value() -> None:
    """This string is hashed into every signature already published.

    Renaming it with the rest of the rebrand would make every existing signed
    managed-CLI release fail verification. It is frozen for the life of schema
    v3. When the optional VS Code extension source is present, both verifiers
    must also match byte for byte.
    """
    import re

    root = Path(__file__).resolve().parents[1]
    python_domain = re.search(
        r'^DOMAIN = "([^"]+)"',
        (root / "scripts" / "release" / "build_managed_cli_manifest.py").read_text(
            encoding="utf-8"
        ),
        re.M,
    )
    assert python_domain is not None
    assert python_domain.group(1) == "sylliptor-managed-cli-release-v3"
    ts_path = (
        root
        / "extensions"
        / "vscode-alysis"
        / "src"
        / "runtime"
        / "ManagedCliReleaseSecurity.ts"
    )
    if ts_path.is_file():
        ts_domain = re.search(
            r'RELEASE_ATTESTATION_DOMAIN = "([^"]+)"',
            ts_path.read_text(encoding="utf-8"),
        )
        assert ts_domain is not None
        assert python_domain.group(1) == ts_domain.group(1)


def test_retiring_release_trust_anchor_id_is_frozen() -> None:
    from scripts.release.build_managed_cli_manifest import LEGACY_SIGNING_KEY_ID

    assert LEGACY_SIGNING_KEY_ID == "sylliptor-release-2026-01"


def test_token_store_aad_pair_is_intact() -> None:
    """Both tags must exist: the current one to seal, the legacy one to open."""
    from alysis_code.mcp import token_store

    assert token_store.TOKEN_STORE_AAD == b"alysis-mcp-oauth-store"
    assert token_store.LEGACY_TOKEN_STORE_AAD == b"sylliptor-mcp-oauth-store"


def test_keyring_service_pair_is_intact() -> None:
    from alysis_code.mcp import token_store

    assert token_store._KEYRING_SERVICE == "alysis-code"
    assert token_store._LEGACY_KEYRING_SERVICE == "sylliptor-agent-cli"


# --- product site move ------------------------------------------------------


def test_every_user_visible_site_link_follows_one_constant(monkeypatch) -> None:
    """Simulate the alysiscode.com switch and check nothing is left behind.

    ALYSIS_SITE_URL overrides the same constant the flip will change, so this
    exercises the real code path. If someone re-hardcodes the host in help
    text or an error message, this fails.
    """
    from alysis_code import alysis_cloud
    from alysis_code.llm.openai_compat import alysis_trial_error_message
    from alysis_code.llm.types import LLMError

    monkeypatch.setenv("ALYSIS_SITE_URL", alysis_cloud.PRODUCT_SITE_URL)

    assert alysis_cloud.site_url() == "https://alysiscode.com"
    assert alysis_cloud.site_host() == "alysiscode.com"
    assert alysis_cloud.account_url() == "https://alysiscode.com/account"
    assert alysis_cloud.activate_url() == "https://alysiscode.com/activate"

    err = LLMError(
        "LLM error 402: "
        + json.dumps({"error": {"message": "Trial window passed.", "code": "trial_expired"}})
    )
    message = alysis_trial_error_message(err)
    assert message is not None
    assert "https://alysiscode.com/account" in message
    assert "sylliptor.alysisai.com" not in message


def test_proxy_error_messages_have_no_stale_hardcoded_host() -> None:
    """No message may name a site host directly; they must use the placeholder."""
    from alysis_code.llm.openai_compat import _ALYSIS_PROXY_ERROR_MESSAGES

    for code, template in _ALYSIS_PROXY_ERROR_MESSAGES.items():
        assert "alysisai.com" not in template, f"{code} hardcodes a site host"
        assert "alysiscode.com" not in template, f"{code} hardcodes a site host"


def test_both_gateway_hosts_stay_classified(monkeypatch) -> None:
    """A config written against the pre-rebrand gateway host must not misclassify."""
    from alysis_code.provider_url import known_provider_key_from_base_url

    for host in ("api.sylliptor.alysisai.com", "api.alysiscode.com"):
        assert known_provider_key_from_base_url(f"https://{host}/v1") == "deepseek"


# --- module-loading invariants ----------------------------------------------


def test_custom_tool_runtime_still_loads_standalone() -> None:
    """``custom_tools/runtime.py`` must import cleanly with no parent package.

    The tool worker loads it via ``spec_from_file_location`` with a synthetic
    module name, so it has no ``__package__``. A package-relative import at
    module scope raises ImportError before any custom tool can run — and the
    failure surfaces only as ``success: False`` from every tool, not as an
    import error anyone would notice while editing.

    This is not hypothetical: adding ``from ..branding import ...`` here during
    the rename broke all 36 runtime tests exactly that way.
    """
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    runtime_path = root / "src" / "alysis_code" / "custom_tools" / "runtime.py"

    spec = importlib.util.spec_from_file_location(
        "_standalone_runtime_probe", os.fspath(runtime_path)
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)

    # Both spellings must be blocked from passthrough, since the runtime
    # injects each of them itself.
    injected = module._INJECTED_ENV_NAMES
    assert "ALYSIS_TOOL_NAME" in injected
    assert "SYLLIPTOR_TOOL_NAME" in injected


# --- persisted user config --------------------------------------------------


def test_stale_feedback_repo_in_config_resolves_to_the_renamed_one() -> None:
    """A config written before the rename pins the old repo name.

    GitHub redirects a rename, so feedback still lands today — but the stored
    value is stale and would break if the old name were ever reused. Found in
    a real config.json that had already been through the directory migration.
    """
    from alysis_code.config import (
        DEFAULT_FEEDBACK_GITHUB_REPO,
        AppConfig,
        resolve_feedback_github_repo,
    )

    for legacy in ("AlysisAi/Sylliptor", "apfivos/sylliptor"):
        cfg = AppConfig(feedback_github_repo=legacy)
        assert resolve_feedback_github_repo(cfg) == DEFAULT_FEEDBACK_GITHUB_REPO

    # A repo that merely contains the old name is somebody's own fork; leave it.
    custom = AppConfig(feedback_github_repo="someone/sylliptor-fork")
    assert resolve_feedback_github_repo(custom) == "someone/sylliptor-fork"


def test_config_show_reports_the_repo_feedback_actually_reaches(tmp_path, monkeypatch) -> None:
    """Displayed value and runtime behaviour must not disagree.

    Redirecting only inside resolve_feedback_github_repo() left `config show`
    printing the stale repo next to an already-migrated profile name, in the
    same output. A config that reports one destination and uses another is
    worse than one that is simply out of date.
    """
    from alysis_code.config import (
        DEFAULT_FEEDBACK_GITHUB_REPO,
        load_config,
        resolve_feedback_github_repo,
    )

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"feedback_github_repo": "AlysisAi/Sylliptor"}), encoding="utf-8"
    )
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))

    cfg = load_config()
    assert cfg.feedback_github_repo == DEFAULT_FEEDBACK_GITHUB_REPO
    assert resolve_feedback_github_repo(cfg) == cfg.feedback_github_repo

    # The user's file is never rewritten behind their back.
    on_disk = json.loads((cfg_dir / "config.json").read_text(encoding="utf-8"))
    assert on_disk["feedback_github_repo"] == "AlysisAi/Sylliptor"
