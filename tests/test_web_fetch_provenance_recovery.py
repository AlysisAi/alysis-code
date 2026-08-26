from __future__ import annotations

from pathlib import Path
from typing import Any

import alysis_code.agent.tools_assembly as tools_assembly
import alysis_code.agent_loop as agent_loop
from alysis_code.agent_loop import ToolDef, build_tools
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.session_store import SessionStore
from alysis_code.tools.web_search import WebSearchRuntimeStatus


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="web-provenance",
        cwd=str(tmp_path),
        repo_root=str(tmp_path),
    )


def _build_tools(
    tmp_path: Path,
    store: SessionStore,
    *,
    cfg: AppConfig | None = None,
    execution_deadline: ExecutionDeadline | None = None,
) -> dict[str, ToolDef]:
    return build_tools(
        root=tmp_path,
        console=None,
        surface=None,
        store=store,
        mode="auto",
        yes=True,
        cfg=cfg or AppConfig(model="test-model", web_search_mode="off"),
        api_key="test-key",
        max_steps=3,
        execution_deadline=execution_deadline,
    )


def _ready_web_search_status() -> WebSearchRuntimeStatus:
    return WebSearchRuntimeStatus(
        mode="auto",
        provider="fake",
        base_url=None,
        model=None,
        api_key_available=True,
        registration_ready=True,
        notes=(),
    )


def test_canonically_equivalent_user_url_is_fetchable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append(
            "user_message",
            {"content": "Read HTTPS://Docs.Example.COM:443/spec#section"},
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/spec")

        assert classification == "user_provided"
        assert resolved == "https://docs.example.com/spec"
    finally:
        store.close()


def test_trailing_slash_equivalent_search_result_is_fetchable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append(
            "tool_call",
            {"name": "web_search", "arguments": {"query": "docs"}, "step": 1},
        )
        store.append(
            "tool_result",
            {
                "name": "web_search",
                "step": 1,
                "result": {
                    "query": "docs",
                    "sources": [{"title": "Docs", "url": "https://docs.example.com/spec"}],
                },
            },
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/spec/")

        assert classification == "returned_by_web_search"
        assert resolved == "https://docs.example.com/spec/"
    finally:
        store.close()


def test_observed_redirect_preserves_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append("user_message", {"content": "Read https://example.com/start"})
        store.append(
            "tool_call",
            {"name": "web_fetch", "arguments": {"url": "https://example.com/start"}, "step": 1},
        )
        store.append(
            "tool_result",
            {
                "name": "web_fetch",
                "step": 1,
                "result": {
                    "url": "https://example.com/start",
                    "final_url": "https://docs.example.com/final",
                    "status_code": 200,
                    "content_type": "text/plain",
                    "title": "",
                    "backend": "httpx",
                },
            },
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/final")

        assert classification == "canonical_redirect"
        assert resolved == "https://docs.example.com/final"
    finally:
        store.close()


def test_link_extracted_from_trusted_fetched_page_is_fetchable_with_parent_event(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.append("user_message", {"content": "Read https://docs.example.com/start"})
        store.append(
            "tool_call",
            {
                "name": "web_fetch",
                "arguments": {"url": "https://docs.example.com/start"},
                "step": 1,
            },
        )
        store.append(
            "tool_result",
            {
                "name": "web_fetch",
                "step": 1,
                "result": {
                    "url": "https://docs.example.com/start",
                    "final_url": "https://docs.example.com/start",
                    "status_code": 200,
                    "content_type": "text/html",
                    "title": "Docs",
                    "content": "Next page: https://docs.example.com/child",
                    "backend": "httpx",
                },
            },
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/child")
        artifact = store.web_research_artifact_payload()
        graph = artifact["url_provenance_graph"]
        child_node = next(
            node
            for node in graph["nodes"]
            if node["normalized_url"] == "https://docs.example.com/child"
        )

        assert classification == "fetched_page_link"
        assert resolved == "https://docs.example.com/child"
        assert child_node["parent_url"] == "https://docs.example.com/start"
        assert child_node["source_event_id"]
        assert child_node["discovery_mechanism"] == "fetched_page_content"
    finally:
        store.close()


def test_url_extracted_from_trusted_local_file_is_fetchable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append(
            "tool_result",
            {
                "name": "fs_read",
                "step": 1,
                "result": {
                    "path": "notes.md",
                    "content": "Official docs: https://docs.example.com/from-file",
                    "truncated": False,
                },
            },
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/from-file")

        assert classification == "trusted_local_file"
        assert resolved == "https://docs.example.com/from-file"
    finally:
        store.close()


def test_url_extracted_from_registered_tool_output_is_fetchable(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append(
            "tool_result",
            {
                "name": "shell_run",
                "step": 1,
                "result": {
                    "cmd": "print url",
                    "exit_code": 0,
                    "stdout": "Generated docs URL https://docs.example.com/from-tool\n",
                    "stderr": "",
                },
            },
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/from-tool")

        assert classification == "trusted_tool_output"
        assert resolved == "https://docs.example.com/from-tool"
    finally:
        store.close()


def test_same_domain_unrelated_url_remains_untrusted_without_provenance(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    try:
        store.append(
            "tool_result",
            {
                "name": "web_search",
                "step": 1,
                "result": {
                    "query": "docs",
                    "sources": [{"title": "Docs", "url": "https://docs.example.com/exact"}],
                },
            },
        )

        classification, resolved = store.resolve_web_fetch_url("https://docs.example.com/unrelated")

        assert classification is None
        assert resolved == "https://docs.example.com/unrelated"
    finally:
        store.close()


def test_provenance_graph_is_bounded_and_omits_source_content(tmp_path: Path) -> None:
    store = _store(tmp_path)
    source_content = "\n".join(f"https://docs.example.com/item-{idx}" for idx in range(80))
    try:
        store.append(
            "tool_result",
            {
                "name": "fs_read",
                "step": 1,
                "result": {"path": "notes.md", "content": source_content},
            },
        )

        artifact = store.web_research_artifact_payload()
        graph = artifact["url_provenance_graph"]
        rendered = str(graph)

        assert graph["node_count"] <= 24
        assert "item-79" not in rendered
        assert source_content not in rendered
    finally:
        store.close()


def test_promoted_private_url_still_blocked_by_fetch_security(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.append(
            "tool_result",
            {
                "name": "fs_read",
                "step": 1,
                "result": {"path": "notes.md", "content": "http://127.0.0.1/internal"},
            },
        )
        tools = _build_tools(tmp_path, store)

        try:
            tools["web_fetch"].run({"url": "http://127.0.0.1/internal"})
        except Exception as exc:  # noqa: BLE001
            assert "Blocked URL host" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("promoted loopback URL must still be blocked")
    finally:
        store.close()


def test_unproven_url_returns_structured_search_recovery_guidance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        tools = _build_tools(tmp_path, store)
        result = tools["web_fetch"].run({"url": "https://docs.example.com/spec"})

        assert result["error_code"] == "web_fetch_provenance_required"
        assert result["provenance_recovery"]["suggested_search_query"] == "docs.example.com spec"
        assert result["provenance_recovery"]["web_search_available"] is False
        assert "canonical_redirect" in result["allowed_provenance"]
    finally:
        store.close()


def test_credential_bearing_unproven_url_is_not_echoed_or_recovered(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        tools_assembly,
        "resolve_web_search_runtime_status",
        lambda **_kwargs: _ready_web_search_status(),
    )
    monkeypatch.setattr(
        tools_assembly,
        "web_search",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("credential-bearing URLs must not start recovery search")
        ),
    )
    try:
        tools = _build_tools(
            tmp_path,
            store,
            cfg=AppConfig(model="test-model", web_search_mode="auto"),
        )
        result = tools["web_fetch"].run({"url": "https://user:secret-token@docs.example.com/spec"})

        rendered = str(result)
        assert result["error_code"] == "web_fetch_provenance_required"
        assert result["url"] == "https://docs.example.com/spec"
        assert result["provenance_recovery"]["automatic_recovery_attempted"] is False
        assert "raw_input_url" not in result
        assert "secret-token" not in rendered
        assert "user:secret" not in rendered
    finally:
        store.close()


def test_search_mediated_recovery_fetches_after_provenance_and_security_checks(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        tools_assembly,
        "resolve_web_search_runtime_status",
        lambda **_kwargs: _ready_web_search_status(),
    )
    monkeypatch.setattr(
        tools_assembly,
        "web_search",
        lambda **_kwargs: {
            "query": "docs.example.com spec",
            "sources": [{"title": "Docs", "url": "https://docs.example.com/spec"}],
        },
    )

    def fake_web_fetch(*, url: str, max_chars: int = 20000) -> dict[str, Any]:
        assert url == "https://docs.example.com/spec"
        assert max_chars == 20000
        return {
            "url": url,
            "final_url": url,
            "status_code": 200,
            "content_type": "text/plain",
            "content": "ok",
            "title": "",
            "truncated": False,
            "backend": "fake",
        }

    monkeypatch.setattr(agent_loop, "web_fetch", fake_web_fetch)
    try:
        tools = _build_tools(
            tmp_path,
            store,
            cfg=AppConfig(model="test-model", web_search_mode="auto"),
        )
        result = tools["web_fetch"].run({"url": "https://docs.example.com/spec"})

        assert result["content"] == "ok"
        assert result["provenance_classification"] == "search_mediated_recovery"
        assert (
            store.resolve_web_fetch_url("https://docs.example.com/spec")[0]
            == "search_mediated_recovery"
        )
    finally:
        store.close()


def test_finalization_mode_suppresses_optional_web_recovery(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(
        tools_assembly,
        "resolve_web_search_runtime_status",
        lambda **_kwargs: _ready_web_search_status(),
    )
    monkeypatch.setattr(
        tools_assembly,
        "web_search",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("finalization must not start optional web research")
        ),
    )
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0.0,
        deadline_monotonic=0.5,
        configured_duration_seconds=10.0,
        clock=lambda: 0.0,
    )
    try:
        tools = _build_tools(
            tmp_path,
            store,
            cfg=AppConfig(model="test-model", web_search_mode="auto"),
            execution_deadline=deadline,
        )
        result = tools["web_fetch"].run({"url": "https://docs.example.com/spec"})

        assert result["error_code"] == "web_fetch_provenance_required"
        assert result["provenance_recovery"]["finalization_suppressed"] is True
        assert result["provenance_recovery"]["automatic_recovery_attempted"] is False
    finally:
        store.close()
