from __future__ import annotations

from pathlib import Path

from alysis_code.git_safe import (
    build_git_cmd,
    git_hooks_enabled,
    resolve_disabled_hooks_dir,
)


def test_git_hooks_enabled_truthy_values() -> None:
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "1"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "true"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "enable"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "enabled"}) is True
    assert git_hooks_enabled({"ALYSIS_GIT_HOOKS": "off"}) is False
    assert git_hooks_enabled({}) is False


def test_git_settings_honor_legacy_environment_names(tmp_path: Path) -> None:
    legacy_env = {
        "SYLLIPTOR_GIT_HOOKS": "enabled",
        "SYLLIPTOR_GIT_HOOKS_PATH": str(tmp_path / "legacy-hooks"),
    }

    assert git_hooks_enabled(legacy_env) is True
    assert resolve_disabled_hooks_dir(tmp_path, legacy_env) == tmp_path / "legacy-hooks"


def test_resolve_disabled_hooks_dir_honors_override(tmp_path: Path) -> None:
    override = tmp_path / "hooks-custom"
    target = resolve_disabled_hooks_dir(tmp_path, {"ALYSIS_GIT_HOOKS_PATH": str(override)})
    assert target == override
    assert target.exists()
    assert target.is_dir()


def test_build_git_cmd_adds_hooks_path_by_default(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    cmd = build_git_cmd(
        tmp_path,
        ["status"],
        env={"ALYSIS_GIT_HOOKS_PATH": str(hooks_dir)},
    )
    assert cmd[:3] == ["git", "-C", str(tmp_path)]
    assert "-c" in cmd
    assert f"core.hooksPath={hooks_dir}" in cmd


def test_build_git_cmd_omits_hooks_override_when_enabled(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    cmd = build_git_cmd(
        tmp_path,
        ["status"],
        env={
            "ALYSIS_GIT_HOOKS": "enable",
            "ALYSIS_GIT_HOOKS_PATH": str(hooks_dir),
        },
    )
    assert all(not str(part).startswith("core.hooksPath=") for part in cmd)


def test_build_git_cmd_respects_explicit_core_hooks_path(tmp_path: Path) -> None:
    cmd = build_git_cmd(
        tmp_path,
        ["status"],
        extra_config={"core.hooksPath": "manual", "user.name": "alysis"},
        env={"ALYSIS_GIT_HOOKS_PATH": str(tmp_path / "ignored")},
    )
    assert "core.hooksPath=manual" in cmd
    assert sum(1 for part in cmd if str(part).startswith("core.hooksPath=")) == 1
