from __future__ import annotations

from pathlib import Path

from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig


def _fake_git_repo(root: Path) -> None:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")


def _session(
    root: Path,
    *,
    session_id: str,
    parent_session_id: str | None = None,
    enabled: bool = True,
):
    return create_session(
        cfg=AppConfig(
            model="gpt-test",
            web_search_mode="off",
            cache={"prompt_cache_key_enabled": enabled},
        ),
        root=root,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        session_id_override=session_id,
        prompt_cache_parent_session_id=parent_session_id,
        subagents_enabled=parent_session_id is None,
    )


def test_session_cache_affinity_is_stable_and_shared_only_across_children(
    tmp_path: Path,
) -> None:
    _fake_git_repo(tmp_path)
    parent = _session(tmp_path, session_id="parent-session")
    same_parent = _session(tmp_path, session_id="parent-session")
    first_child = _session(
        tmp_path,
        session_id="child-a",
        parent_session_id="parent-session",
    )
    second_child = _session(
        tmp_path,
        session_id="child-b",
        parent_session_id="parent-session",
    )
    disabled = _session(tmp_path, session_id="disabled", enabled=False)
    try:
        assert parent.client.prompt_cache_key == "parent-session"
        assert parent.client.prompt_cache_key == same_parent.client.prompt_cache_key
        launcher = parent.tools["subagent_run"].run.__self__
        assert launcher.prompt_cache_parent_session_id == "parent-session"
        assert first_child.client.prompt_cache_key == second_child.client.prompt_cache_key
        assert first_child.client.prompt_cache_key != parent.client.prompt_cache_key
        assert disabled.client.prompt_cache_key is None
    finally:
        parent.close()
        same_parent.close()
        first_child.close()
        second_child.close()
        disabled.close()
