from __future__ import annotations

import copy
import json

import httpx

from alysis_code.config import AppConfig
from alysis_code.failure_category import FailureCategory
from alysis_code.llm.metadata import PROVIDER_METADATA_KEY
from alysis_code.llm.types import LLMError, LLMResponse, ToolCall
from alysis_code.plan_assistant import (
    PLANNER_SYSTEM_PROMPT,
    _assistant_tool_call_message,
    _extract_balanced_json_object,
    _plan_is_thin,
    _planner_retry_user_prompt,
    _planner_user_prompt,
    apply_guarded_planner_plan_update,
    apply_plan_update,
    compact_plan_for_planner,
    compact_workspace_context_for_planner,
    run_planner_turn,
    sanitize_guarded_planner_plan_update,
    summarize_plan_update,
)


def _base_plan() -> dict:
    return {
        "schema_version": 1,
        "run_id": "run_1",
        "created_at": "2026-02-20T00:00:00+00:00",
        "updated_at": "2026-02-20T00:00:00+00:00",
        "project_goal": "Initial goal",
        "summary": "Initial summary",
        "requirements": [],
        "tasks": [
            {
                "id": "T01",
                "title": "Existing",
                "description": "existing desc",
                "acceptance_criteria": [],
                "dependencies": [],
                "estimated_files": [],
                "branch": "",
                "status": "planned",
            }
        ],
        "assets": [],
    }


def _question_repair_payload(
    *,
    should_replace: bool = False,
    assistant_message: str = "",
    questions: list[str] | None = None,
) -> dict:
    return {
        "should_replace": should_replace,
        "assistant_message": assistant_message,
        "questions": list(questions or []),
    }


def _planner_no_update_payload(message: str = "Planner ran.") -> dict:
    return {
        "assistant_message": message,
        "questions": ["Any constraints?"],
        "plan_update": None,
    }


def test_planner_usage_payload_backfills_cache_creation_from_api_usage() -> None:
    from types import SimpleNamespace

    from alysis_code.llm.types import BillingMode, LLMUsage, UsageContract
    from alysis_code.model_registry import ModelMeta
    from alysis_code.plan_assistant import _planner_usage_event_payload

    class _Reg:
        def get(self, model_name: str) -> ModelMeta:
            return ModelMeta(
                model_name=model_name,
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.000003,
                output_cost_per_token=0.000015,
                cache_read_input_cost_per_token=0.0000003,
                cache_creation_input_cost_per_token=0.00000375,
                source="test",
            )

    response = SimpleNamespace(
        content="plan text",
        response_model="claude-sonnet-4-5",
        tool_calls=[],
        usage=LLMUsage(
            prompt_tokens=26000,
            completion_tokens=500,
            total_tokens=26500,
            cache_read_input_tokens=20000,
            cache_creation_input_tokens=5000,
            input_tokens_uncached=1000,
        ),
    )

    payload = _planner_usage_event_payload(
        role="forge_planner",
        requested_model="claude-sonnet-4-5",
        request_messages=[{"role": "user", "content": "plan this"}],
        response=response,
        registry=_Reg(),  # type: ignore[arg-type]
        client=SimpleNamespace(
            provider_key="anthropic",
            base_url="https://api.anthropic.com",
            usage_contract=UsageContract(billing_mode=BillingMode.SUBSCRIPTION),
        ),  # type: ignore[arg-type]
    )

    assert payload is not None
    # api_usage back-fill preserves cache accounting (dropped before the fix).
    assert payload["cache_creation_input_tokens"] == 5000
    assert payload["cache_read_input_tokens"] == 20000
    # Uncached input must not swallow the cache-creation tokens.
    assert payload["input_tokens_uncached"] == 1000
    assert payload["billing_mode"] == BillingMode.SUBSCRIPTION.value
    assert payload["cost_source"] == "subscription"


def test_assistant_tool_call_message_preserves_provider_metadata() -> None:
    response = LLMResponse(
        content="",
        tool_calls=[
            ToolCall(id="call_1", name="planner_asset_search", arguments={"query": "docs"})
        ],
        raw={},
        provider_metadata={"deepseek": {"reasoning_content": "Need asset context."}},
    )

    message = _assistant_tool_call_message(response)

    assert message["role"] == "assistant"
    assert message["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "planner_asset_search",
                "arguments": '{"query": "docs"}',
            },
        }
    ]
    assert message[PROVIDER_METADATA_KEY] == {
        "deepseek": {"reasoning_content": "Need asset context."}
    }


def _mock_transport_for_payloads(*payloads: dict | str | tuple[int, dict | str]):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        index = len(requests) - 1
        assert index < len(payloads), f"unexpected extra LLM request: {body}"
        payload = payloads[index]
        if isinstance(payload, tuple):
            status_code, response_payload = payload
            if isinstance(response_payload, str):
                response_json = {"error": {"message": response_payload}}
            else:
                response_json = response_payload
            return httpx.Response(status_code, json=response_json)
        content = payload if isinstance(payload, str) else json.dumps(payload)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return httpx.MockTransport(handler), requests


def test_run_planner_turn_valid_json_parses_and_applies(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    monkeypatch.delenv("ALYSIS_MODEL_PLANNER", raising=False)

    reply_obj = {
        "assistant_message": "Captured updates.",
        "questions": ["Any latency constraints?"],
        "plan_update": {
            "project_goal": "Build planning assistant",
            "summary": "Plan with assisted updates",
            "requirements_append": ["Must keep manual mode unchanged"],
            "tasks_add": [
                {
                    "title": "Add planner loop",
                    "description": "Wire planner into plan command",
                    "acceptance_criteria": ["Assistant can be toggled"],
                    "dependencies": ["T01", "T99"],
                    "estimated_files": ["src/alysis_code/cli.py"],
                    "write_scope": [".alysis/runs"],
                    "parallel_group": "planning",
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assert body["model"] == "plan-planner-model"
        assert body["temperature"] == 0.2
        prompt = str(body["messages"][1]["content"])
        assert '"requirements_total"' in prompt
        assert "role_models" not in prompt
        data = {"choices": [{"message": {"content": json.dumps(reply_obj)}}]}
        return httpx.Response(200, json=data)

    cfg = AppConfig(
        base_url="https://example.com/v1",
        model="base-model",
        planner_temperature=0.33,
    )
    cfg.extra_fields = {"role_models": {"planner": "cfg-planner-model"}}
    plan_with_role_models = _base_plan()
    plan_with_role_models["role_models"] = {"planner": "plan-planner-model"}
    result = run_planner_turn(
        cfg=cfg,
        api_key_override=None,
        plan=plan_with_role_models,
        transcript_tail=[{"role": "user", "content": "Need safer planning updates"}],
        user_text="Please update the plan",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.assistant_message == "Captured updates."
    assert result.questions == ["Any latency constraints?"]
    assert result.plan_update is not None

    plan = _base_plan()
    apply_result = apply_plan_update(plan, result.plan_update)
    assert apply_result.changed is True
    assert apply_result.added_task_ids == ["T02"]
    assert apply_result.warnings
    assert "T99" in " ".join(apply_result.warnings)
    assert plan["project_goal"] == "Build planning assistant"
    assert plan["summary"] == "Plan with assisted updates"
    assert plan["requirements"] == ["Must keep manual mode unchanged"]
    assert plan["tasks"][1]["id"] == "T02"
    assert plan["tasks"][1]["dependencies"] == ["T01"]
    assert int(plan["tasks"][1]["attempts"]) == 0


def test_run_planner_turn_uses_resolved_llm_timeout(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    captured: dict[str, float] = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            captured["timeout_s"] = float(kwargs["timeout_s"])

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            payload = {
                "assistant_message": "ok",
                "questions": [],
                "plan_update": None,
            }
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model", llm_timeout_s=17.5),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.error is None
    assert captured["timeout_s"] == 17.5


def test_run_planner_turn_retries_transient_request_failure_once(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            payload = {
                "assistant_message": "Recovered planner response.",
                "questions": [],
                "plan_update": None,
            }
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.error is None
    assert result.assistant_message == "Recovered planner response."
    assert result.request_retry_count == 1
    assert calls["count"] == 2


def test_run_planner_turn_preserves_retry_count_on_empty_response_after_retry(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            return type("Resp", (), {"content": ""})()

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.plan_update is None
    assert result.error == "empty_response"
    assert result.request_retry_count == 1
    assert calls["count"] == 2


def test_run_planner_turn_retry_exhaustion_returns_safe_error(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise LLMError("LLM request failed: ReadTimeout")

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.plan_update is None
    assert result.request_retry_count == 1
    assert result.error is not None
    assert "retry exhausted after 2 attempts" in result.error
    assert "ReadTimeout" in result.error
    assert result.failure_category == FailureCategory.PROVIDER_UNAVAILABLE.value
    assert calls["count"] == 2


def test_run_planner_turn_provider_429_exhaustion_is_provider_throttled(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise LLMError("LLM error 429: rate limit quota exceeded")

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.plan_update is None
    assert result.failure_category == FailureCategory.PROVIDER_THROTTLED.value
    assert result.request_retry_count == 0
    assert calls["count"] == 1


def test_run_planner_turn_does_not_retry_nontransient_request_error(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            raise LLMError("LLM error 401: invalid_api_key")

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.plan_update is None
    assert result.request_retry_count == 0
    assert result.error == "LLM error 401: invalid_api_key"
    assert calls["count"] == 1


def test_run_planner_turn_warns_for_fallback_model_metadata(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    warnings_seen: list[str] = []

    def _warn(message: str, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        _ = args, kwargs
        warnings_seen.append(str(message))

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **_kwargs):  # type: ignore[no-untyped-def]
            payload = {
                "assistant_message": "ok",
                "questions": [],
                "plan_update": None,
            }
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("warnings.warn", _warn)
    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="unknown-model-xyz"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.error is None
    assert warnings_seen
    assert "unknown-model-xyz" in warnings_seen[0]


def test_run_planner_turn_strict_model_metadata_policy_fails_before_client(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            raise AssertionError("OpenAICompatClient should not be constructed in strict mode")

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(
            base_url="https://example.com/v1",
            model="unknown-model-xyz",
            model_metadata_policy="strict",
        ),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
    )

    assert result.error
    assert "model_metadata_policy=strict" in result.error


def test_run_planner_turn_invalid_json_does_not_modify_plan(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    def handler(_request: httpx.Request) -> httpx.Response:
        data = {"choices": [{"message": {"content": "not-json"}}]}
        return httpx.Response(200, json=data)

    plan = _base_plan()
    before = copy.deepcopy(plan)
    cfg = AppConfig(base_url="https://example.com/v1", model="planner-model")
    result = run_planner_turn(
        cfg=cfg,
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Please add tasks",
        transport=httpx.MockTransport(handler),
    )

    assert result.plan_update is None
    assert result.error
    if result.plan_update:
        apply_plan_update(plan, result.plan_update)
    assert plan == before


def test_run_planner_turn_normalizes_content_array_response(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Captured planner response.",
        "questions": [],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": json.dumps(planner_payload)},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": "https://example.com/ignored.png"},
                                },
                            ]
                        }
                    }
                ]
            },
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.assistant_message == "Captured planner response."


def test_run_planner_turn_retries_invalid_json_once_with_higher_temperature(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    temperatures: list[float] = []
    role_sequences: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        temperatures.append(float(body.get("temperature")))
        role_sequences.append([str(item.get("role") or "") for item in body.get("messages", [])])
        if len(temperatures) == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]})
        payload = {
            "assistant_message": "Retry fixed JSON.",
            "questions": [],
            "plan_update": None,
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please add tasks",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.assistant_message == "Retry fixed JSON."
    assert result.request_retry_count == 0
    assert result.schema_failures[0]["attempt"] == 1
    assert result.schema_failures[0]["reason_code"] == "planner_schema_validation_failed"
    assert result.schema_failures[0]["error"]
    assert temperatures == [0.2, 0.5]
    assert role_sequences == [["system", "user"], ["system", "user"]]


def test_run_planner_turn_retry_for_nonrepairable_schema_mismatch_keeps_two_message_shape(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    role_sequences: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        role_sequences.append([str(item.get("role") or "") for item in body.get("messages", [])])
        return httpx.Response(200, json={"choices": [{"message": {"content": "[]"}}]})

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please add tasks",
        transport=httpx.MockTransport(handler),
    )

    assert result.plan_update is None
    assert result.error
    # Every attempt in the configured budget is spent, then the turn ends.
    assert [item["attempt"] for item in result.schema_failures] == [1, 2, 3]
    assert result.repair.terminal_state == "failed"
    assert role_sequences == [["system", "user"]] * 3


def test_planner_retry_user_prompt_includes_base_context_and_schema_repair_instructions() -> None:
    base_prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[{"role": "user", "content": "Please keep tests reviewable."}],
        user_text="Please update the plan",
    )

    retry_prompt = _planner_retry_user_prompt(
        base_prompt=base_prompt,
        validation_error="assistant_message must be non-empty",
        previous_response='{"assistant_message": ""}',
    )

    assert "Current plan JSON:" in retry_prompt
    assert "Please update the plan" in retry_prompt
    assert "Please keep tests reviewable." in retry_prompt
    assert "Validation error: assistant_message must be non-empty" in retry_prompt
    assert 'Previous response:\n{"assistant_message": ""}' in retry_prompt
    assert "Return ONLY one JSON object that matches the schema." in retry_prompt
    assert "Preserve the latest user intent, target roots" in retry_prompt


def test_planner_user_prompt_includes_monorepo_scope_constraints() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Only fix API. Worker is a decoy and should remain unchanged.",
        workspace_context={
            "workspace_kind": "git_repo",
            "manifests": [
                {"path": "services/api/package.json", "kind": "node"},
                {"path": "services/worker/package.json", "kind": "node"},
            ],
            "observed_paths": [
                "services/api/src/config.ts",
                "services/worker/src/config.ts",
            ],
        },
    )

    assert "Planning scope constraints JSON:" in prompt
    assert "services/api" in prompt
    assert "services/worker" in prompt
    assert "Decoy, unrelated, ignored, or forbidden paths" in prompt


def test_planner_user_prompt_includes_relevant_knowledge_section() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please refine the parser retry plan",
        workspace_context=None,
        relevant_knowledge_section=(
            "## Relevant Knowledge\n\n"
            "- Manifest: `.alysis/runs/x/plan/selected_knowledge/planner/manifest.json`\n"
            "- `decision` `K01`: Keep bounded parser retry backoff"
        ),
    )

    assert "## Relevant Knowledge" in prompt
    assert "selected_knowledge/planner/manifest.json" in prompt
    assert (
        "Treat active decisions and open issues as stronger guidance than historical facts."
        in prompt
    )


def test_planner_system_prompt_describes_parallel_group_as_conservative_scheduling_signal() -> None:
    assert "parallel_group" in PLANNER_SYSTEM_PROMPT
    assert "can be done concurrently" not in PLANNER_SYSTEM_PROMPT
    assert (
        "Leave parallel_group empty unless there is a concrete conservative scheduling reason to set it."
        in PLANNER_SYSTEM_PROMPT
    )
    assert "tasks with the same non-empty value are not batched together" in PLANNER_SYSTEM_PROMPT


def test_planner_system_prompt_describes_mcp_scope_as_remote_execution_policy() -> None:
    assert "mcp_scope" in PLANNER_SYSTEM_PROMPT
    assert "write_scope" in PLANNER_SYSTEM_PROMPT
    assert "remote MCP access" in PLANNER_SYSTEM_PROMPT
    assert "allow_resources" in PLANNER_SYSTEM_PROMPT
    assert "allowed_tools" in PLANNER_SYSTEM_PROMPT


def test_apply_plan_update_normalizes_task_mcp_scope() -> None:
    plan = _base_plan()
    update = {
        "tasks_add": [
            {
                "title": "Use MCP narrowly",
                "description": "Use a single remote MCP action.",
                "acceptance_criteria": ["Call the expected MCP tool."],
                "dependencies": [],
                "estimated_files": ["docs/notes.md"],
                "write_scope": ["docs/notes.md"],
                "mcp_scope": {
                    "allow_resources": True,
                    "allowed_tools": [
                        {"server_id": "github", "tool_name": "create_issue"},
                        {"server_id": "github", "tool_name": "create_issue"},
                        {"server_id": "", "tool_name": "comment_pull_request"},
                    ],
                },
            }
        ]
    }

    result = apply_plan_update(plan, update)

    assert result.changed is True
    task = plan["tasks"][-1]
    assert task["mcp_scope"] == {
        "allow_resources": True,
        "allowed_tools": [
            {"server_id": "github", "tool_name": "create_issue"},
        ],
    }
    joined_warnings = " ".join(result.warnings)
    assert "server_id cannot be empty" in joined_warnings


def test_apply_plan_update_invalid_task_mcp_scope_patch_preserves_existing_scope() -> None:
    plan = _base_plan()
    plan["tasks"][0]["mcp_scope"] = {
        "allowed_tools": [
            {"server_id": "github", "tool_name": "create_issue"},
        ]
    }

    result = apply_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "mcp_scope": {
                        "allowed_tools": [
                            {"server_id": "", "tool_name": "comment_pull_request"},
                        ]
                    },
                }
            ]
        },
    )

    assert result.changed is False
    assert plan["tasks"][0]["mcp_scope"] == {
        "allowed_tools": [
            {"server_id": "github", "tool_name": "create_issue"},
        ]
    }
    joined_warnings = " ".join(result.warnings)
    assert "server_id cannot be empty" in joined_warnings
    assert "Preserved existing mcp_scope" in joined_warnings


def test_apply_plan_update_auto_increments_and_validates_dependencies() -> None:
    plan = _base_plan()
    plan["tasks"].append(
        {
            "id": "T03",
            "title": "Second existing",
            "description": "existing second",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "",
            "status": "planned",
        }
    )

    update = {
        "tasks_add": [
            {
                "title": "First new",
                "description": "first",
                "acceptance_criteria": [],
                "dependencies": ["T01", "T88"],
                "estimated_files": ["src/first.py"],
                "write_scope": ["src/first.py"],
            },
            {
                "title": "Second new",
                "description": "second",
                "acceptance_criteria": [],
                "dependencies": ["T04", "T03"],
                "estimated_files": ["src/second.py"],
                "write_scope": ["src/second.py"],
            },
        ],
        "tasks_update": [
            {"id": "T03", "dependencies": ["T01", "T99"]},
            {"id": "T77", "title": "Unknown"},
        ],
    }

    result = apply_plan_update(plan, update)
    assert result.changed is True
    assert result.added_task_ids == ["T04", "T05"]
    assert result.updated_task_ids == ["T03"]
    assert len(result.warnings) == 3
    assert plan["tasks"][2]["dependencies"] == ["T01"]
    assert plan["tasks"][3]["dependencies"] == ["T04", "T03"]
    assert plan["tasks"][1]["dependencies"] == ["T01"]
    assert all(task["status"] == "planned" for task in plan["tasks"])
    assert int(plan["tasks"][2]["attempts"]) == 0
    assert int(plan["tasks"][3]["attempts"]) == 0


def test_apply_plan_update_skips_duplicate_task_add_by_title() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Create canonical requirements document",
            "description": "Capture requirements in one place",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "",
            "status": "planned",
        }
    ]

    update = {
        "tasks_add": [
            {
                "title": "Create canonical requirements document",
                "description": "Capture requirements in one place",
                "acceptance_criteria": [],
                "dependencies": [],
                "estimated_files": [],
                "write_scope": [],
            }
        ]
    }

    result = apply_plan_update(plan, update)
    assert result.changed is False
    assert result.added_task_ids == []
    assert len(plan["tasks"]) == 1
    assert any("Skipped duplicate task_add" in warning for warning in result.warnings)


def test_apply_plan_update_skips_duplicate_requirement_append() -> None:
    plan = _base_plan()
    plan["requirements"] = ["foo"]

    update = {"requirements_append": [" Foo "]}
    result = apply_plan_update(plan, update)

    assert result.changed is False
    assert result.requirements_added == 0
    assert plan["requirements"] == ["foo"]
    assert any("Skipped duplicate requirement_append" in warning for warning in result.warnings)


def test_apply_plan_update_normalizes_task_file_scopes_and_infers_estimated_files() -> None:
    plan = _base_plan()
    update = {
        "tasks_add": [
            {
                "title": "Add styles.css and wire it to index.html",
                "description": "Create styles.css and update index.html to load it.",
                "acceptance_criteria": [
                    "styles.css exists",
                    "index.html references styles.css",
                ],
                "dependencies": [],
                "estimated_files": ["styles.css"],
                "write_scope": [
                    "Add CSS in styles.css",
                    "Update index.html to load the stylesheet",
                ],
            }
        ]
    }

    result = apply_plan_update(plan, update)

    assert result.changed is True
    task = plan["tasks"][-1]
    assert task["estimated_files"] == ["styles.css", "index.html"]
    assert task["write_scope"] == ["styles.css", "index.html"]
    joined_warnings = " | ".join(result.warnings)
    assert "dropped invalid write_scope entries" in joined_warnings
    assert "inferred estimated_files from task text: index.html" in joined_warnings
    assert "expanded write_scope to include estimated_files" in joined_warnings


def test_apply_plan_update_rejects_mutating_task_add_without_file_scope() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Fix login bug",
                    "description": "Update auth flow.",
                    "acceptance_criteria": ["Login works"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.added_task_ids == []
    assert len(plan["tasks"]) == 1
    joined_warnings = " | ".join(result.warnings)
    assert (
        "runnable or ambiguous task requires runnable estimated_files/write_scope"
        in joined_warnings
    )
    assert "Skipped task_add 'Fix login bug'" in joined_warnings


def test_apply_plan_update_rejects_inflected_mutating_task_add_without_file_scope() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "OAuth cleanup",
                    "description": "Handle refresh token edge case.",
                    "acceptance_criteria": ["Tests updated", "Docs updated"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.added_task_ids == []
    assert len(plan["tasks"]) == 1
    joined_warnings = " | ".join(result.warnings)
    assert (
        "runnable or ambiguous task requires runnable estimated_files/write_scope"
        in joined_warnings
    )
    assert "Skipped task_add 'OAuth cleanup'" in joined_warnings


def test_apply_plan_update_allows_non_mutating_analysis_task_without_file_scope() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Investigate login failures",
                    "description": "Review the current auth flow and report findings only.",
                    "acceptance_criteria": ["Summarize root cause for the team"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is True
    assert result.added_task_ids == ["T02"]
    assert plan["tasks"][-1]["estimated_files"] == []
    assert plan["tasks"][-1]["write_scope"] == []
    assert plan["tasks"][-1]["analysis_only"] is True


def test_apply_plan_update_clears_scope_for_non_mutating_analysis_task_with_paths() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Explore contacts/client.py import surface",
                    "description": "Inspect contacts/client.py and report findings only.",
                    "acceptance_criteria": ["Summarize findings"],
                    "dependencies": [],
                    "estimated_files": ["contacts/client.py"],
                    "write_scope": ["contacts/client.py"],
                }
            ]
        },
    )

    assert result.changed is True
    task = plan["tasks"][-1]
    assert task["estimated_files"] == []
    assert task["write_scope"] == []
    assert task["analysis_only"] is True
    assert "cleared file mutation scope" in " | ".join(result.warnings)


def test_apply_plan_update_allows_analysis_task_with_documented_findings_without_file_scope() -> (
    None
):
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Analyze login bug",
                    "description": "Review auth flow and report findings.",
                    "acceptance_criteria": ["Findings documented"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is True
    assert result.added_task_ids == ["T02"]
    assert plan["tasks"][-1]["estimated_files"] == []
    assert plan["tasks"][-1]["write_scope"] == []
    assert plan["tasks"][-1]["analysis_only"] is True


def test_apply_plan_update_rejects_analysis_task_with_documented_findings_and_mutating_work() -> (
    None
):
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Analyze login bug",
                    "description": "Implement token refresh fix.",
                    "acceptance_criteria": ["Findings documented"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.added_task_ids == []
    assert len(plan["tasks"]) == 1
    joined_warnings = " | ".join(result.warnings)
    assert (
        "runnable or ambiguous task requires runnable estimated_files/write_scope"
        in joined_warnings
    )
    assert "Skipped task_add 'Analyze login bug'" in joined_warnings


def test_apply_plan_update_clears_analysis_only_when_task_becomes_mutating() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Investigate login failures",
            "description": "Report findings only.",
            "acceptance_criteria": ["Findings documented."],
            "estimated_files": [],
            "write_scope": [],
            "analysis_only": True,
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Fix login failures",
                    "description": "Update auth.py implementation.",
                    "acceptance_criteria": ["Login failures are fixed."],
                    "estimated_files": ["auth.py"],
                    "write_scope": ["auth.py"],
                }
            ]
        },
    )

    assert result.changed is True
    assert "analysis_only" not in plan["tasks"][0]
    assert plan["tasks"][0]["estimated_files"] == ["auth.py"]
    assert plan["tasks"][0]["write_scope"] == ["auth.py"]


def test_apply_plan_update_allows_report_only_task_without_file_scope_when_text_mentions_changes() -> (
    None
):
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Review login changes",
                    "description": "Report findings only.",
                    "acceptance_criteria": ["Summarize changes for the team"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is True
    assert result.added_task_ids == ["T02"]
    assert plan["tasks"][-1]["estimated_files"] == []
    assert plan["tasks"][-1]["write_scope"] == []


def test_normalize_task_file_fields_recovers_explicit_path_from_task_text() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Fix login bug",
                    "description": "Update src/auth.py so login refresh tokens work correctly.",
                    "acceptance_criteria": ["Login works"],
                    "dependencies": [],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
    )

    assert result.changed is True
    assert result.added_task_ids == ["T02"]
    assert plan["tasks"][-1]["estimated_files"] == ["src/auth.py"]
    assert plan["tasks"][-1]["write_scope"] == ["src/auth.py"]
    assert "inferred estimated_files from task text: src/auth.py" in " | ".join(result.warnings)


def test_apply_plan_update_drops_forbidden_scope_paths() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Fix todo export to exclude USER_NOTES.md",
                    "description": "Update todo_export.py so private notes are ignored.",
                    "acceptance_criteria": ["todo_export.py excludes private notes"],
                    "dependencies": [],
                    "estimated_files": ["todo_export.py", "USER_NOTES.md"],
                    "write_scope": ["todo_export.py", "USER_NOTES.md"],
                }
            ]
        },
        latest_user_text="Preserve the untracked USER_NOTES.md file.",
    )

    assert result.changed is True
    task = plan["tasks"][-1]
    assert task["estimated_files"] == ["todo_export.py"]
    assert task["write_scope"] == ["todo_export.py"]
    joined_warnings = " | ".join(result.warnings)
    assert "dropped forbidden estimated_files entries: USER_NOTES.md" in joined_warnings
    assert "dropped forbidden write_scope entries: USER_NOTES.md" in joined_warnings


def test_apply_plan_update_keeps_conditional_bug_scope_path() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Add focused currency regression test",
                    "description": (
                        "Locate tests that cover formatting.py. Do not change formatting.py "
                        "itself unless a genuine bug is discovered."
                    ),
                    "acceptance_criteria": ["pytest -q passes."],
                    "dependencies": [],
                    "estimated_files": ["tests/**"],
                    "write_scope": ["tests/**"],
                }
            ]
        },
    )

    assert result.changed is True
    task = plan["tasks"][-1]
    assert task["estimated_files"] == ["tests/**", "formatting.py"]
    assert task["write_scope"] == ["tests/**", "formatting.py"]


def test_apply_plan_update_sequences_tests_and_docs_after_implementation() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Implement env layering in config_loader.py",
                    "description": "Update config_loader.py to apply env overrides after files.",
                    "acceptance_criteria": ["Runtime behavior is updated"],
                    "dependencies": [],
                    "estimated_files": ["config_loader.py"],
                    "write_scope": ["config_loader.py"],
                },
                {
                    "title": "Add regression tests for env layering",
                    "description": "Add pytest coverage for the new precedence behavior.",
                    "acceptance_criteria": ["pytest tests/test_config_loader.py passes"],
                    "dependencies": [],
                    "estimated_files": ["tests/test_config_loader.py"],
                    "write_scope": ["tests/test_config_loader.py"],
                },
                {
                    "title": "Update README docs for env layering",
                    "description": "Document the final env precedence.",
                    "acceptance_criteria": ["README.md describes env override order"],
                    "dependencies": [],
                    "estimated_files": ["README.md"],
                    "write_scope": ["README.md"],
                },
            ]
        },
    )

    assert result.changed is True
    impl_task = plan["tasks"][-3]
    test_task = plan["tasks"][-2]
    docs_task = plan["tasks"][-1]
    assert impl_task["id"] == "T02"
    assert test_task["dependencies"] == ["T02"]
    assert docs_task["dependencies"] == ["T02"]
    assert "Added dependency T03 -> T02" in " | ".join(result.warnings)
    assert "Added dependency T04 -> T02" in " | ".join(result.warnings)


def test_apply_plan_update_sequences_test_file_task_after_implementation() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Create habit_tracker.py with CLI persistence",
                    "description": "Implement add, list, and complete commands.",
                    "acceptance_criteria": ["CLI persists habits"],
                    "dependencies": [],
                    "estimated_files": ["habit_tracker.py"],
                    "write_scope": ["habit_tracker.py"],
                },
                {
                    "title": "Create test_habit_tracker.py with pytest tests",
                    "description": "Write tests for add, list, complete, and error cases.",
                    "acceptance_criteria": ["pytest passes"],
                    "dependencies": [],
                    "estimated_files": ["test_habit_tracker.py"],
                    "write_scope": ["test_habit_tracker.py"],
                },
            ]
        },
    )

    assert result.changed is True
    assert plan["tasks"][-1]["dependencies"] == ["T02"]
    assert "Added dependency T03 -> T02" in " | ".join(result.warnings)


def test_apply_plan_update_sequences_ordered_follow_up_after_predecessor() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Update src/calc.py helper behavior",
                    "description": "Adjust the calculator helper behavior.",
                    "acceptance_criteria": ["Helper behavior is updated."],
                    "dependencies": [],
                    "estimated_files": ["src/calc.py"],
                    "write_scope": ["src/calc.py"],
                },
                {
                    "title": "Update src/app.py caller after helper behavior",
                    "description": "Update the app caller after the helper behavior changes.",
                    "acceptance_criteria": ["Caller uses the updated helper behavior."],
                    "dependencies": [],
                    "estimated_files": ["src/app.py"],
                    "write_scope": ["src/app.py"],
                },
            ]
        },
    )

    assert result.changed is True
    assert plan["tasks"][-1]["dependencies"] == ["T02"]
    assert "Added dependency T03 -> T02" in " | ".join(result.warnings)


def test_apply_plan_update_treats_implementation_with_tests_as_implementation() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Add ms and min duration parsing and unit tests",
                    "description": "Extend parse_duration_ms and add Rust unit tests.",
                    "acceptance_criteria": ["cargo test passes"],
                    "dependencies": [],
                    "estimated_files": ["src/lib.rs"],
                    "write_scope": ["src/lib.rs"],
                },
                {
                    "title": "Update README duration units documentation",
                    "description": "Document ms and min suffixes.",
                    "acceptance_criteria": ["README.md lists supported units"],
                    "dependencies": [],
                    "estimated_files": ["README.md"],
                    "write_scope": ["README.md"],
                },
            ]
        },
    )

    assert result.changed is True
    assert plan["tasks"][-1]["dependencies"] == ["T02"]
    assert "Added dependency T03 -> T02" in " | ".join(result.warnings)


def test_apply_plan_update_keeps_standalone_docs_task_dependency_free() -> None:
    plan = _base_plan()

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Update README overview",
                    "description": "Refresh README.md wording.",
                    "acceptance_criteria": ["README.md is updated"],
                    "dependencies": [],
                    "estimated_files": ["README.md"],
                    "write_scope": ["README.md"],
                }
            ]
        },
    )

    assert result.changed is True
    assert plan["tasks"][-1]["dependencies"] == []


def test_apply_plan_update_rejects_mutating_task_update_without_runnable_scope() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Investigate login failures",
            "description": "Review the current auth flow and report findings only.",
            "estimated_files": [],
            "write_scope": [],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Fix login bug",
                    "description": "Update auth flow.",
                    "acceptance_criteria": ["Login works"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.updated_task_ids == []
    assert plan["tasks"][0]["title"] == "Investigate login failures"
    assert (
        plan["tasks"][0]["description"] == "Review the current auth flow and report findings only."
    )
    joined_warnings = " | ".join(result.warnings)
    assert (
        "runnable or ambiguous task requires runnable estimated_files/write_scope"
        in joined_warnings
    )
    assert "Ignored update for task 'T01'" in joined_warnings


def test_apply_plan_update_preserves_existing_scope_on_partial_mutating_update() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Fix login bug",
            "description": "Update src/auth.py.",
            "estimated_files": ["src/auth.py"],
            "write_scope": ["src/auth.py"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Update auth flow to handle refresh tokens safely.",
                    "acceptance_criteria": ["Login works"],
                }
            ]
        },
    )

    assert result.changed is True
    assert result.updated_task_ids == ["T01"]
    assert plan["tasks"][0]["estimated_files"] == ["src/auth.py"]
    assert plan["tasks"][0]["write_scope"] == ["src/auth.py"]


def test_apply_plan_update_drops_protected_file_scopes() -> None:
    plan = _base_plan()
    update = {
        "tasks_add": [
            {
                "title": "Touch internal files",
                "description": "Do not actually do this.",
                "acceptance_criteria": [],
                "dependencies": [],
                "estimated_files": [".alysis/runs/run_1/plan/plan.json", "src/app.py"],
                "write_scope": [".git/index", "src/app.py"],
            }
        ]
    }

    result = apply_plan_update(plan, update)

    task = plan["tasks"][-1]
    assert task["estimated_files"] == ["src/app.py"]
    assert task["write_scope"] == ["src/app.py"]
    joined_warnings = " | ".join(result.warnings)
    assert "dropped protected estimated_files entries" in joined_warnings
    assert "dropped protected write_scope entries" in joined_warnings


def test_apply_plan_update_removes_assigned_task_and_cleans_dependencies() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Assigned task",
            "description": "active work",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "feature/t01-assigned",
            "status": "in_progress",
            "attempts": 2,
        },
        {
            "id": "T02",
            "title": "Dependent task",
            "description": "depends on assigned task",
            "acceptance_criteria": [],
            "dependencies": ["T01"],
            "estimated_files": [],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        },
    ]

    result = apply_plan_update(plan, {"tasks_remove": ["T01"]})

    assert result.changed is True
    assert result.removed_task_ids == ["T01"]
    assert [task["id"] for task in plan["tasks"]] == ["T02"]
    assert plan["tasks"][0]["dependencies"] == []
    assert any("referencing deleted tasks: T01" in warning for warning in result.warnings)


def test_apply_plan_update_remove_unknown_task_warns() -> None:
    plan = _base_plan()

    result = apply_plan_update(plan, {"tasks_remove": ["T77"]})

    assert result.changed is False
    assert result.removed_task_ids == []
    assert any("Ignored remove for unknown task id: T77" in warning for warning in result.warnings)


def test_replan_drop_toml_invalidates_obsolete_planned_tasks() -> None:
    plan = _base_plan()
    plan["requirements"] = [
        "Support TOML config",
        "Load settings from settings.toml",
        "Expose timeout configuration",
    ]
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Implement TOML settings loader",
            "description": "Load timeout settings from settings.toml.",
            "acceptance_criteria": ["TOML settings load correctly."],
            "dependencies": [],
            "estimated_files": ["src/settings.py"],
            "write_scope": ["src/settings.py"],
            "status": "planned",
        }
    ]

    result = apply_plan_update(
        plan,
        {
            "requirements_append": ["Use APP_TIMEOUT_SECONDS environment variable"],
            "tasks_add": [
                {
                    "title": "Implement APP_TIMEOUT_SECONDS environment timeout",
                    "description": "Read APP_TIMEOUT_SECONDS in src/settings.py.",
                    "acceptance_criteria": ["APP_TIMEOUT_SECONDS controls timeout."],
                    "dependencies": [],
                    "estimated_files": ["src/settings.py"],
                    "write_scope": ["src/settings.py"],
                }
            ],
        },
        latest_user_text=(
            "drop TOML from the plan entirely; use env var APP_TIMEOUT_SECONDS instead"
        ),
    )

    assert result.changed is True
    assert result.superseded_task_ids == ["T01"]
    assert plan["tasks"][0]["status"] == "superseded"
    assert plan["tasks"][1]["status"] == "planned"
    assert "Support TOML config" not in plan["requirements"]
    assert "Load settings from settings.toml" not in plan["requirements"]
    assert "Use APP_TIMEOUT_SECONDS environment variable" in plan["requirements"]
    assert [item["text"] for item in plan["superseded_requirements"]] == [
        "Support TOML config",
        "Load settings from settings.toml",
    ]


def test_direction_change_preserves_completed_history_but_supersedes_planned_work() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Investigate TOML settings approach",
            "description": "Research settings.toml behavior.",
            "acceptance_criteria": ["Findings documented."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "status": "done",
        },
        {
            "id": "T02",
            "title": "Implement TOML settings loader",
            "description": "Load timeout settings from settings.toml.",
            "acceptance_criteria": ["TOML settings load correctly."],
            "dependencies": ["T01"],
            "estimated_files": ["src/settings.py"],
            "write_scope": ["src/settings.py"],
            "status": "planned",
        },
    ]

    result = apply_plan_update(
        plan,
        {},
        latest_user_text=(
            "drop TOML from the plan entirely; use env var APP_TIMEOUT_SECONDS instead"
        ),
    )

    assert result.superseded_task_ids == ["T02"]
    assert plan["tasks"][0]["status"] == "done"
    assert "superseded" not in plan["tasks"][0]
    assert plan["tasks"][1]["status"] == "superseded"
    assert plan["tasks"][1]["superseded"]["reason_code"] == "user_changed_goal"


def test_apply_plan_update_cannot_supersede_target_task_for_decoy_plan() -> None:
    plan = _base_plan()
    plan["planning_constraints"] = {
        "schema_version": 1,
        "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
        "forbidden_roots": [],
        "decoy_roots": [{"path": "services/worker", "reason_code": "decoy_path_constraint"}],
        "unrelated_roots": [],
    }
    plan["tasks"][0].update(
        {
            "title": "Fix API APP_REGION precedence",
            "description": "Update services/api config precedence.",
            "acceptance_criteria": ["API precedence is correct."],
            "estimated_files": ["services/api/src/config.ts"],
            "write_scope": ["services/api/src/config.ts"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_supersede": ["T01"],
            "tasks_add": [
                {
                    "title": "Fix worker APP_REGION precedence",
                    "description": "Update services/worker config.",
                    "acceptance_criteria": ["Worker precedence changes."],
                    "estimated_files": ["services/worker/src/config.ts"],
                    "write_scope": ["services/worker/src/config.ts"],
                }
            ],
        },
        latest_user_text="Worker is a decoy. services/api is the target.",
    )

    assert result.superseded_task_ids == []
    assert result.added_task_ids == []
    assert plan["tasks"][0]["status"] == "planned"
    joined = " | ".join(result.warnings)
    assert "Ignored supersede for target-root task 'T01'" in joined
    assert "violates planning scope constraints" in joined


def test_apply_plan_update_uses_workspace_context_for_leaf_constraints() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Fix API APP_REGION precedence",
            "description": "Update services/api config precedence.",
            "acceptance_criteria": ["API precedence is correct."],
            "estimated_files": ["services/api/src/config.ts"],
            "write_scope": ["services/api/src/config.ts"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_supersede": ["T01"],
            "tasks_add": [
                {
                    "title": "Fix worker APP_REGION precedence",
                    "description": "Update worker config.",
                    "acceptance_criteria": ["Worker precedence changes."],
                    "estimated_files": ["services/worker/src/config.ts"],
                    "write_scope": ["services/worker/src/config.ts"],
                }
            ],
        },
        latest_user_text="Only fix API. Worker is a decoy and should remain unchanged.",
        workspace_context={
            "manifests": [
                {"path": "services/api/package.json", "kind": "node"},
                {"path": "services/worker/package.json", "kind": "node"},
            ],
            "observed_paths": [
                "services/api/src/config.ts",
                "services/worker/src/config.ts",
            ],
        },
    )

    assert result.superseded_task_ids == []
    assert result.added_task_ids == []
    assert plan["tasks"][0]["status"] == "planned"
    constraints = plan["planning_constraints"]
    assert [item["path"] for item in constraints["target_roots"]] == ["services/api"]
    assert [item["path"] for item in constraints["decoy_roots"]] == ["services/worker"]


def test_apply_plan_update_allows_explicit_retarget_and_records_scope_reason() -> None:
    plan = _base_plan()
    plan["planning_constraints"] = {
        "schema_version": 1,
        "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
        "forbidden_roots": [],
        "decoy_roots": [{"path": "services/worker", "reason_code": "decoy_path_constraint"}],
        "unrelated_roots": [],
    }
    plan["tasks"][0].update(
        {
            "title": "Fix API APP_REGION precedence",
            "description": "Update services/api config precedence.",
            "acceptance_criteria": ["API precedence is correct."],
            "estimated_files": ["services/api/src/config.ts"],
            "write_scope": ["services/api/src/config.ts"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_supersede": ["T01"],
            "tasks_add": [
                {
                    "title": "Fix worker APP_REGION precedence",
                    "description": "Update services/worker config.",
                    "acceptance_criteria": ["Worker precedence changes."],
                    "estimated_files": ["services/worker/src/config.ts"],
                    "write_scope": ["services/worker/src/config.ts"],
                }
            ],
        },
        latest_user_text="Actually switch to fixing Worker now.",
        workspace_context={
            "manifests": [
                {"path": "services/api/package.json", "kind": "node"},
                {"path": "services/worker/package.json", "kind": "node"},
            ],
        },
    )

    assert result.superseded_task_ids == ["T01"]
    assert result.added_task_ids == ["T02"]
    assert plan["tasks"][0]["status"] == "superseded"
    assert plan["tasks"][0]["superseded"]["reason_code"] == "invalid_out_of_scope"
    assert plan["tasks"][1]["write_scope"] == ["services/worker/src/config.ts"]
    constraints = plan["planning_constraints"]
    assert [item["path"] for item in constraints["target_roots"]] == ["services/worker"]
    assert constraints["decoy_roots"] == []


def test_direction_change_replacement_task_must_have_runnable_scope() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Implement TOML settings loader",
            "description": "Load timeout settings from settings.toml.",
            "acceptance_criteria": ["TOML settings load correctly."],
            "estimated_files": ["src/settings.py"],
            "write_scope": ["src/settings.py"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Implement APP_TIMEOUT_SECONDS support",
                    "description": "Read timeout from the environment.",
                    "acceptance_criteria": ["Timeout env var works."],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
        latest_user_text=(
            "drop TOML from the plan entirely; use env var APP_TIMEOUT_SECONDS instead"
        ),
    )

    assert result.added_task_ids == []
    assert result.superseded_task_ids == ["T01"]
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["status"] == "superseded"
    assert any(
        "Skipped task_add 'Implement APP_TIMEOUT_SECONDS support'" in w for w in result.warnings
    )


def test_direction_change_does_not_infer_obsolete_path_from_instead_of_phrase() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Implement TOML settings loader",
            "description": "Load timeout settings from settings.toml.",
            "acceptance_criteria": ["TOML settings load correctly."],
            "estimated_files": ["src/settings.py"],
            "write_scope": ["src/settings.py"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Use APP_TIMEOUT_SECONDS env var",
                    "description": "Use APP_TIMEOUT_SECONDS instead of settings.toml.",
                    "acceptance_criteria": ["APP_TIMEOUT_SECONDS controls timeout."],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
        latest_user_text=(
            "drop TOML from the plan entirely; use APP_TIMEOUT_SECONDS env var instead"
        ),
    )

    assert result.added_task_ids == []
    assert result.superseded_task_ids == ["T01"]
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["status"] == "superseded"
    joined_warnings = " | ".join(result.warnings)
    assert "ignored obsolete inferred path hints from direction-change text: settings.toml" in (
        joined_warnings
    )
    assert "Skipped task_add 'Use APP_TIMEOUT_SECONDS env var'" in joined_warnings


def test_direction_change_keeps_positive_replacement_scope_while_dropping_obsolete_path() -> None:
    plan = _base_plan()
    plan["tasks"][0].update(
        {
            "title": "Implement TOML settings loader",
            "description": "Load timeout settings from settings.toml.",
            "acceptance_criteria": ["TOML settings load correctly."],
            "estimated_files": ["src/settings.py"],
            "write_scope": ["src/settings.py"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Use APP_TIMEOUT_SECONDS env var",
                    "description": (
                        "Use src/settings.py for APP_TIMEOUT_SECONDS instead of settings.toml."
                    ),
                    "acceptance_criteria": ["APP_TIMEOUT_SECONDS controls timeout."],
                    "estimated_files": [],
                    "write_scope": [],
                }
            ]
        },
        latest_user_text=(
            "drop TOML from the plan entirely; use APP_TIMEOUT_SECONDS env var instead"
        ),
    )

    assert result.added_task_ids == ["T02"]
    assert result.superseded_task_ids == ["T01"]
    assert plan["tasks"][1]["status"] == "planned"
    assert plan["tasks"][1]["estimated_files"] == ["src/settings.py"]
    assert plan["tasks"][1]["write_scope"] == ["src/settings.py"]
    assert "settings.toml" not in plan["tasks"][1]["estimated_files"]
    joined_warnings = " | ".join(result.warnings)
    assert "ignored obsolete inferred path hints from direction-change text: settings.toml" in (
        joined_warnings
    )


def test_direction_change_supersedes_obsolete_requirements() -> None:
    plan = _base_plan()
    plan["requirements"] = ["Support TOML config", "Keep CLI output stable"]

    result = apply_plan_update(
        plan,
        {"requirements_append": ["Use APP_TIMEOUT_SECONDS environment variable"]},
        latest_user_text="remove TOML from the plan entirely; use APP_TIMEOUT_SECONDS instead",
    )

    assert result.superseded_requirements == ["Support TOML config"]
    assert plan["requirements"] == [
        "Keep CLI output stable",
        "Use APP_TIMEOUT_SECONDS environment variable",
    ]
    assert plan["superseded_requirements"][0]["text"] == "Support TOML config"


def test_direction_change_detector_does_not_invalidate_on_report_only_mentions() -> None:
    for latest_user_text in [
        "compare TOML and env var options, report only",
        "document why TOML was dropped earlier",
        "summarize the TOML issue, no code changes",
    ]:
        plan = _base_plan()
        plan["requirements"] = ["Support TOML config"]
        plan["tasks"][0].update(
            {
                "title": "Implement TOML settings loader",
                "description": "Load timeout settings from settings.toml.",
                "acceptance_criteria": ["TOML settings load correctly."],
                "estimated_files": ["src/settings.py"],
                "write_scope": ["src/settings.py"],
            }
        )

        result = apply_plan_update(
            plan,
            {},
            latest_user_text=latest_user_text,
        )

        assert result.changed is False
        assert plan["tasks"][0]["status"] == "planned"
        assert plan["requirements"] == ["Support TOML config"]


def test_direction_change_detector_ignores_negated_drop_instruction() -> None:
    plan = _base_plan()
    plan["requirements"] = ["Unknown config keys from the local file are preserved."]
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Fix config precedence",
            "description": "Preserve unknown config keys and validate timeout values.",
            "acceptance_criteria": ["Unknown local-file keys are preserved."],
            "dependencies": [],
            "estimated_files": ["config_stack.py"],
            "write_scope": ["config_stack.py"],
            "status": "planned",
        },
        {
            "id": "T02",
            "title": "Add config regression tests",
            "description": "Cover unknown-key preservation.",
            "acceptance_criteria": ["Regression tests cover env overrides."],
            "dependencies": ["T01"],
            "estimated_files": ["tests/test_config_stack.py"],
            "write_scope": ["tests/test_config_stack.py"],
            "status": "planned",
        },
    ]

    result = apply_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T02",
                    "acceptance_criteria": [
                        "Regression test covers local unknown keys when env overrides timeout."
                    ],
                }
            ]
        },
        latest_user_text=(
            "Add a regression test where the local file contains an unknown key and env "
            "overrides only timeout. Do not drop unrelated keys."
        ),
    )

    assert result.superseded_task_ids == []
    assert plan["tasks"][0]["status"] == "planned"
    assert plan["tasks"][1]["status"] == "planned"
    assert plan["requirements"] == ["Unknown config keys from the local file are preserved."]


def test_replan_switch_from_old_approach_to_new_approach_supersedes_old_tasks() -> None:
    plan = _base_plan()
    plan["requirements"] = ["Expose timeout through a CLI flag"]
    plan["tasks"][0].update(
        {
            "title": "Implement CLI flag timeout configuration",
            "description": "Add a --timeout flag.",
            "acceptance_criteria": ["CLI flag controls timeout."],
            "estimated_files": ["src/cli.py"],
            "write_scope": ["src/cli.py"],
        }
    )

    result = apply_plan_update(
        plan,
        {
            "tasks_add": [
                {
                    "title": "Implement APP_TIMEOUT_SECONDS env var timeout",
                    "description": "Read APP_TIMEOUT_SECONDS in src/config.py.",
                    "acceptance_criteria": ["APP_TIMEOUT_SECONDS controls timeout."],
                    "estimated_files": ["src/config.py"],
                    "write_scope": ["src/config.py"],
                }
            ]
        },
        latest_user_text="switch from CLI flag to APP_TIMEOUT_SECONDS env var",
    )

    assert result.superseded_task_ids == ["T01"]
    assert plan["tasks"][0]["status"] == "superseded"
    assert plan["tasks"][1]["status"] == "planned"
    assert plan["tasks"][1]["estimated_files"] == ["src/config.py"]


def test_sanitize_guarded_planner_plan_update_drops_protected_history_mutations() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Completed task",
            "description": "done",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
        {
            "id": "T02",
            "title": "Planned task",
            "description": "planned",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": ["src/planned.py"],
            "write_scope": ["src/planned.py"],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        },
    ]

    sanitization = sanitize_guarded_planner_plan_update(
        plan=plan,
        plan_update={
            "project_goal": "Refine the rollout",
            "summary": "Keep history immutable",
            "requirements_append": ["Need a follow-up task"],
            "tasks_update": [
                {"id": "T01", "title": "Rewrite completed task"},
                {"id": "T02", "title": "Refine planned task"},
            ],
            "tasks_remove": ["T01"],
            "tasks_add": [
                {
                    "title": "Add follow-up task",
                    "description": "Track the new follow-up separately",
                    "acceptance_criteria": ["Follow-up task exists"],
                    "dependencies": ["T01"],
                    "estimated_files": ["src/follow_up.py"],
                    "write_scope": ["src/follow_up.py"],
                }
            ],
        },
    )

    assert sanitization.plan_update["tasks_update"] == [
        {"id": "T02", "title": "Refine planned task"}
    ]
    assert sanitization.plan_update["tasks_remove"] == []
    assert len(sanitization.plan_update["tasks_add"]) == 1
    assert len(sanitization.warnings) == 2
    assert [item.task_id for item in sanitization.rejected_protected_updates] == ["T01"]
    assert "tasks_update" in sanitization.warnings[0]
    assert "tasks_remove" in sanitization.warnings[1]
    assert "immutable" in " ".join(sanitization.warnings)
    assert "follow-up work as new tasks instead" in " ".join(sanitization.warnings)


def test_apply_guarded_planner_plan_update_preserves_safe_changes_and_warns() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Completed task",
            "description": "done",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
        {
            "id": "T02",
            "title": "Planned task",
            "description": "planned",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": ["src/planned.py"],
            "write_scope": ["src/planned.py"],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        },
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "project_goal": "Refine the rollout",
            "summary": "Keep history immutable",
            "requirements_append": ["Need a follow-up task"],
            "tasks_update": [
                {"id": "T01", "title": "Rewrite completed task"},
                {"id": "T02", "title": "Refine planned task"},
            ],
            "tasks_remove": ["T01"],
            "tasks_add": [
                {
                    "title": "Add follow-up task",
                    "description": "Track the new follow-up separately",
                    "acceptance_criteria": ["Follow-up task exists"],
                    "dependencies": ["T01"],
                    "estimated_files": ["src/follow_up.py"],
                    "write_scope": ["src/follow_up.py"],
                }
            ],
        },
    )

    assert result.changed is True
    assert result.updated_task_ids == ["T02"]
    assert result.requirements_added == 1
    assert result.goal_updated is True
    assert result.summary_updated is True
    assert len(result.added_task_ids) == 1
    assert plan["project_goal"] == "Refine the rollout"
    assert plan["summary"] == "Keep history immutable"
    assert plan["requirements"] == ["Need a follow-up task"]
    assert [task["id"] for task in plan["tasks"]] == ["T01", "T02", "T03"]
    assert plan["tasks"][0]["title"] == "Completed task"
    assert plan["tasks"][0]["status"] == "done"
    assert plan["tasks"][1]["title"] == "Refine planned task"
    assert plan["tasks"][2]["title"] == "Add follow-up task"
    assert plan["tasks"][2]["dependencies"] == ["T01"]
    joined_warnings = " ".join(result.warnings)
    assert "tasks_update" in joined_warnings
    assert "tasks_remove" in joined_warnings
    assert "immutable" in joined_warnings
    assert "follow-up work as new tasks instead" in joined_warnings


def test_apply_guarded_planner_plan_update_preserves_protected_dependency_history() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Completed task",
            "description": "done",
            "acceptance_criteria": [],
            "dependencies": ["T02"],
            "estimated_files": [],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
        {
            "id": "T02",
            "title": "Planned task to remove",
            "description": "planned",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        },
    ]

    result = apply_guarded_planner_plan_update(plan, {"tasks_remove": ["T02"]})

    assert result.changed is True
    assert result.removed_task_ids == ["T02"]
    assert [task["id"] for task in plan["tasks"]] == ["T01"]
    assert plan["tasks"][0]["dependencies"] == ["T02"]
    joined_warnings = " ".join(result.warnings)
    assert "Preserved protected non-planned task dependency history" in joined_warnings
    assert "Removed dependencies from task 'T01'" not in joined_warnings


def test_apply_guarded_planner_plan_update_synthesizes_follow_up_task_from_protected_update() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add slugify follow-up tests",
                    "description": "Track fresh follow-up test work separately.",
                    "acceptance_criteria": ["Add node tests for slugify"],
                    "dependencies": ["T01"],
                    "estimated_files": ["test/slugify.test.js"],
                    "write_scope": ["test/slugify.test.js"],
                }
            ]
        },
    )

    assert result.changed is True
    assert result.updated_task_ids == []
    assert result.synthesized_task_ids == ["T02"]
    assert result.added_task_ids == ["T02"]
    assert plan["tasks"][0]["title"] == "Ship slugify"
    assert plan["tasks"][0]["status"] == "done"
    assert plan["tasks"][1]["id"] == "T02"
    assert plan["tasks"][1]["status"] == "planned"
    assert int(plan["tasks"][1]["attempts"]) == 0
    assert plan["tasks"][1]["branch"] == ""
    assert plan["tasks"][1]["title"] == "Add slugify follow-up tests"
    assert plan["tasks"][1]["dependencies"] == ["T01"]
    joined_warnings = " ".join(result.warnings)
    assert "protected non-planned task history" in joined_warnings
    assert "Synthesized new planned follow-up tasks" in joined_warnings


def test_apply_guarded_planner_plan_update_remaps_dependencies_between_synthesized_tasks() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship API",
            "description": "done",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": ["src/api.py"],
            "write_scope": ["src/api.py"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
        {
            "id": "T02",
            "title": "Ship docs",
            "description": "done",
            "acceptance_criteria": [],
            "dependencies": ["T01"],
            "estimated_files": ["README.md"],
            "write_scope": ["README.md"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add API follow-up task",
                    "description": "follow-up",
                    "acceptance_criteria": ["API follow-up exists"],
                    "estimated_files": ["src/api_follow_up.py"],
                    "write_scope": ["src/api_follow_up.py"],
                },
                {
                    "id": "T02",
                    "title": "Add docs follow-up task",
                    "description": "follow-up",
                    "acceptance_criteria": ["Docs follow-up exists"],
                    "dependencies": ["T01"],
                    "estimated_files": ["docs/api.md"],
                    "write_scope": ["docs/api.md"],
                },
            ]
        },
    )

    assert result.synthesized_task_ids == ["T03", "T04"]
    assert plan["tasks"][2]["id"] == "T03"
    assert plan["tasks"][3]["id"] == "T04"
    assert plan["tasks"][3]["dependencies"] == ["T03"]


def test_apply_guarded_planner_plan_update_drops_unsynthesized_protected_non_done_dependency() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Investigate rollout",
            "description": "active",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": ["src/rollout.py"],
            "write_scope": ["src/rollout.py"],
            "branch": "feat/t01",
            "status": "in_progress",
            "attempts": 1,
        },
        {
            "id": "T02",
            "title": "Ship slugify",
            "description": "done",
            "acceptance_criteria": [],
            "dependencies": ["T01"],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T02",
                    "title": "Add slugify follow-up",
                    "description": "follow-up",
                    "acceptance_criteria": ["Follow-up exists"],
                    "dependencies": ["T01"],
                    "estimated_files": ["test/slugify.test.js"],
                    "write_scope": ["test/slugify.test.js"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T03"]
    assert plan["tasks"][2]["dependencies"] == []
    joined_warnings = " ".join(result.warnings)
    assert (
        "Dropped protected non-done dependencies for synthesized follow-up task 'T03': T01"
        in joined_warnings
    )


def test_guarded_planner_follow_up_does_not_synthesize_mutating_task_without_runnable_scope() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship login",
            "description": "Released login flow.",
            "acceptance_criteria": ["Login shipped"],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Fix login bug",
                    "description": "Update auth flow.",
                    "acceptance_criteria": ["Login works"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    joined_warnings = " | ".join(result.warnings)
    assert (
        "runnable or ambiguous task requires runnable estimated_files/write_scope"
        in joined_warnings
    )
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in joined_warnings
    )


def test_apply_guarded_planner_plan_update_synthesizes_when_safe_update_still_leaves_no_runnable_work() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "done",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        },
        {
            "id": "T02",
            "title": "Blocked docs follow-up",
            "description": "planned but blocked",
            "acceptance_criteria": ["Docs follow-up exists"],
            "dependencies": ["T99"],
            "estimated_files": ["docs/slugify.md"],
            "write_scope": ["docs/slugify.md"],
            "branch": "",
            "status": "planned",
            "attempts": 0,
        },
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add slugify follow-up tests",
                    "description": "follow-up",
                    "acceptance_criteria": ["Tests exist"],
                    "dependencies": ["T01"],
                    "estimated_files": ["test/slugify.test.js"],
                    "write_scope": ["test/slugify.test.js"],
                },
                {
                    "id": "T02",
                    "description": "still blocked but clarified",
                },
            ]
        },
    )

    assert result.updated_task_ids == ["T02"]
    assert result.synthesized_task_ids == ["T03"]
    assert plan["tasks"][1]["description"] == "still blocked but clarified"
    assert plan["tasks"][2]["title"] == "Add slugify follow-up tests"


def test_apply_guarded_planner_plan_update_synthesizes_same_title_follow_up_with_suffix() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "done",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Ship slugify",
                    "description": "Track post-release follow-up separately.",
                    "acceptance_criteria": ["Follow-up exists"],
                    "dependencies": ["T01"],
                    "estimated_files": ["test/slugify.test.js"],
                    "write_scope": ["test/slugify.test.js"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][1]["title"] == "Ship slugify follow-up"
    assert "could not be synthesized" not in " ".join(result.warnings)


def test_apply_guarded_planner_plan_update_refuses_trivial_protected_update_without_new_path_signal() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship src/slugify.js",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Ship src/slugify.js",
                    "description": "Minor wording tweak only.",
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["description"] == "Released in src/slugify.js."
    joined_warnings = " ".join(result.warnings)
    assert "protected non-planned task history" in joined_warnings
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in joined_warnings
    )
    summary = summarize_plan_update(result)
    assert "protected history preserved" in summary
    assert "synthesis refused: T01 missing new runnable delta beyond protected history" in summary


def test_apply_guarded_planner_plan_update_refuses_repeated_old_estimated_files_without_new_delta() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship src/slugify.js",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Minor wording tweak only.",
                    "estimated_files": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_repeated_old_write_scope_without_new_delta() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship src/slugify.js",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Minor wording tweak only.",
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_punctuation_only_description_delta() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Released in src/slugify.js!",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_punctuation_only_acceptance_delta() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["src/slugify.js supports lowercase option"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "acceptance_criteria": ["src/slugify.js supports lowercase option."],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_case_only_description_delta() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "released in   SRC/slugify.js",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_formatting_only_description_with_path() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Formatting only in src/slugify.js.",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_comment_only_acceptance_with_path() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "acceptance_criteria": ["Comment-only update in src/slugify.js"],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_scope_repeated_under_protected_glob() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify sources",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/**/*.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Minor wording tweak only.",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_synthesizes_title_only_same_file_follow_up() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add lowercase option follow-up",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][0]["title"] == "Ship slugify"
    assert plan["tasks"][1]["title"] == "Add lowercase option follow-up"
    assert plan["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert plan["tasks"][1]["write_scope"] == ["src/slugify.js"]
    assert "missing new runnable delta beyond protected history" not in " ".join(result.warnings)


def test_apply_guarded_planner_plan_update_synthesizes_title_only_new_path_follow_up() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add docs follow-up",
                    "estimated_files": ["docs/slugify.md"],
                    "write_scope": ["docs/slugify.md"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][1]["title"] == "Add docs follow-up"
    assert plan["tasks"][1]["estimated_files"] == ["docs/slugify.md"]
    assert plan["tasks"][1]["write_scope"] == ["docs/slugify.md"]
    assert "missing new runnable delta beyond protected history" not in " ".join(result.warnings)


def test_apply_guarded_planner_plan_update_refuses_path_only_title_with_scope() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "src/slugify.js",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_follow_up_boilerplate_plus_path_title() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Follow-up for src/slugify.js",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_generic_action_plus_path_title() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Update src/slugify.js",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_refuses_weak_title_only_variants() -> None:
    titles = (
        "Follow-ups for src/slugify.js",
        "Changes for src/slugify.js",
        "Update file src/slugify.js",
        "Fix src/slugify.js",
        "Implement src/slugify.js",
        "Improve src/slugify.js",
        "Refactor src/slugify.js",
        "Review src/slugify.js",
        "Finalize src/slugify.js",
        "meta for src/slugify.js",
        "admin for src/slugify.js",
        "Rewrite done task",
        "Task A bad replan",
    )
    for title in titles:
        plan = _base_plan()
        plan["tasks"] = [
            {
                "id": "T01",
                "title": "Ship slugify",
                "description": "Released in src/slugify.js.",
                "acceptance_criteria": ["Slugify shipped"],
                "dependencies": [],
                "estimated_files": ["src/slugify.js"],
                "write_scope": ["src/slugify.js"],
                "branch": "",
                "status": "done",
                "attempts": 1,
            }
        ]

        result = apply_guarded_planner_plan_update(
            plan,
            {
                "tasks_update": [
                    {
                        "id": "T01",
                        "title": title,
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

        assert result.changed is False, title
        assert result.synthesized_task_ids == [], title
        assert (
            "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
            in " ".join(result.warnings)
        ), title


def test_apply_guarded_planner_plan_update_synthesizes_same_file_follow_up_with_new_semantics() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add lowercase option follow-up",
                    "description": "Update src/slugify.js to add lowercase: bool = True behavior.",
                    "acceptance_criteria": ["src/slugify.js supports lowercase option"],
                    "dependencies": ["T01"],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][0]["title"] == "Ship slugify"
    assert plan["tasks"][0]["status"] == "done"
    assert plan["tasks"][1]["title"] == "Add lowercase option follow-up"
    assert plan["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert plan["tasks"][1]["write_scope"] == ["src/slugify.js"]
    assert "missing new runnable delta beyond protected history" not in " ".join(result.warnings)


def test_apply_guarded_planner_plan_update_synthesizes_same_file_follow_up_from_description_delta() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "description": "Add lowercase option behavior with default True.",
                    "dependencies": ["T01"],
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][1]["title"] == "Ship slugify follow-up"
    assert plan["tasks"][1]["description"] == "Add lowercase option behavior with default True."
    assert plan["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert plan["tasks"][1]["write_scope"] == ["src/slugify.js"]


def test_apply_guarded_planner_plan_update_refuses_title_only_retitle_with_repeated_old_scope() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Ship slugify cleanup",
                    "estimated_files": ["src/slugify.js"],
                    "write_scope": ["src/slugify.js"],
                }
            ]
        },
    )

    assert result.changed is False
    assert result.synthesized_task_ids == []
    assert len(plan["tasks"]) == 1
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in " ".join(result.warnings)
    )


def test_apply_guarded_planner_plan_update_synthesizes_from_patch_text_path_hints() -> None:
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship slugify",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Add slugify follow-up tests",
                    "description": "Create test/slugify.test.js and docs/slugify.md.",
                    "acceptance_criteria": ["test/slugify.test.js exists"],
                    "dependencies": ["T01"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][1]["estimated_files"] == ["test/slugify.test.js", "docs/slugify.md"]
    assert plan["tasks"][1]["write_scope"] == ["test/slugify.test.js", "docs/slugify.md"]


def test_apply_guarded_planner_plan_update_keeps_same_file_scope_when_follow_up_intent_is_new() -> (
    None
):
    plan = _base_plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Ship src/slugify.js",
            "description": "Released in src/slugify.js.",
            "acceptance_criteria": ["Slugify shipped"],
            "dependencies": [],
            "estimated_files": ["src/slugify.js"],
            "write_scope": ["src/slugify.js"],
            "branch": "",
            "status": "done",
            "attempts": 1,
        }
    ]

    result = apply_guarded_planner_plan_update(
        plan,
        {
            "tasks_update": [
                {
                    "id": "T01",
                    "title": "Ship src/slugify.js",
                    "description": "Add test/slugify.test.js coverage for regressions.",
                    "estimated_files": ["src/slugify.js", "test/slugify.test.js"],
                    "write_scope": ["src/slugify.js", "test/slugify.test.js"],
                }
            ]
        },
    )

    assert result.synthesized_task_ids == ["T02"]
    assert plan["tasks"][1]["title"] == "Ship src/slugify.js follow-up"
    assert plan["tasks"][1]["estimated_files"] == ["src/slugify.js", "test/slugify.test.js"]
    assert plan["tasks"][1]["write_scope"] == ["src/slugify.js", "test/slugify.test.js"]


def test_compact_plan_for_planner_applies_truncation_rules() -> None:
    plan = _base_plan()
    plan["requirements"] = [f"req-{i:02d}" for i in range(40)]
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "A" * 10,
            "description": "D" * 240,
            "acceptance_criteria": [f"ac-{i}" for i in range(8)],
            "dependencies": ["T00", "T02"],
            "estimated_files": ["src/a.py"],
            "branch": "",
            "status": "planned",
        },
        {
            "id": "T02",
            "title": "Second",
            "description": "short",
            "acceptance_criteria": [],
            "dependencies": [],
            "estimated_files": [],
            "branch": "",
            "status": "planned",
        },
    ]
    plan["assets"] = [
        {"stored_path": f"plan/assets/a{i}.txt", "text_copy_path": "", "size_bytes": i}
        for i in range(80)
    ]

    compact = compact_plan_for_planner(plan)
    assert compact["project_goal"] == plan["project_goal"]
    assert compact["summary"] == plan["summary"]
    assert compact["requirements_total"] == 40
    assert compact["requirements_tail"][0] == "req-10"
    assert compact["requirements_tail"][-1] == "req-39"
    assert len(compact["requirements_tail"]) == 30
    assert len(compact["tasks"]) == 2
    assert compact["tasks"][0]["id"] == "T01"
    assert len(compact["tasks"][0]["description_trunc"]) <= 200
    assert compact["tasks"][0]["description_trunc"].endswith("...")
    assert len(compact["tasks"][0]["acceptance_criteria_trunc"]) == 5
    assert compact["assets_total"] == 80
    assert len(compact["assets"]) == 50


def test_compact_plan_for_planner_keeps_fallback_requirement_text() -> None:
    plan = _base_plan()
    plan["requirements"] = [
        {
            "text": "Implement robust planner parsing",
            "execution_ready": False,
            "source": "planner_error_fallback",
        }
    ]

    compact = compact_plan_for_planner(plan)

    assert compact["requirements_total"] == 1
    assert compact["requirements_tail"] == ["Implement robust planner parsing"]
    assert compact["requirements_not_execution_ready_total"] == 1
    assert compact["requirements_not_execution_ready_tail"] == ["Implement robust planner parsing"]


def test_planner_user_prompt_preserves_greek_text_without_ascii_escaping() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[{"role": "user", "content": "Πώς σε λένε;"}],
        user_text="Θέλω να υλοποιήσουμε login ροή με tests.",
    )
    assert "Πώς σε λένε;" in prompt
    assert "Θέλω να υλοποιήσουμε login ροή με tests." in prompt
    assert "\\u03" not in prompt


def test_planner_prompt_language_policy_uses_latest_user_message_not_transcript() -> None:
    assert "Choose the natural response language from the latest user message only." in (
        PLANNER_SYSTEM_PROMPT
    )
    assert "Do not infer reply language from earlier transcript messages" in PLANNER_SYSTEM_PROMPT


def test_planner_user_prompt_reinforces_latest_message_language_policy() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[{"role": "user", "content": "Μίλα μου στα ελληνικά."}],
        user_text="Can you help me build a new website?",
    )

    assert "Latest user message:\nCan you help me build a new website?" in prompt
    assert "Reply in the natural language/script of the latest user message" in prompt
    assert "Do not infer reply language from the recent transcript tail" in prompt


def test_planner_user_prompt_surfaces_explicit_grounding_anchors() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text=(
            "Keep T01 and T02 separate. Update src/planner/release_planner.py and "
            "tests/test_release_planner.py."
        ),
    )

    assert "Latest user grounding anchors:" in prompt
    assert (
        '"repo_relative_paths": ["src/planner/release_planner.py", "tests/test_release_planner.py"]'
        in prompt
    )
    assert '"task_ids": ["T01", "T02"]' in prompt
    assert "preserve that structure instead of inventing an analogous task graph" in prompt


def test_planner_user_prompt_reuses_prior_user_grounding_anchors_on_follow_up_turn() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[
            {
                "role": "user",
                "content": (
                    "T01 update src/planner/release_planner.py and tests/test_release_planner.py."
                ),
            },
            {"role": "assistant", "content": "Need one confirmation."},
            {"role": "user", "content": "yes"},
        ],
        user_text="yes",
    )

    assert "Latest user grounding anchors:" in prompt
    assert (
        '"repo_relative_paths": ["src/planner/release_planner.py", "tests/test_release_planner.py"]'
        in prompt
    )
    assert '"task_ids": ["T01"]' in prompt


def test_planner_user_prompt_requires_file_scope_for_mutating_tasks() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Fix the login flow.",
    )

    assert "required for mutating tasks; may be empty for clearly analysis-only tasks" in prompt
    assert "Runnable mutating tasks must include non-empty file scope" in prompt
    assert (
        "Clearly analysis-only or report-only tasks may leave estimated_files/write_scope empty."
        in prompt
    )


def test_planner_metadata_missing_stays_hard_fail(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    transport, requests = _mock_transport_for_payloads()

    result = run_planner_turn(
        cfg=AppConfig(
            base_url="https://example.com/v1",
            model="planner-missing",
            model_metadata_policy="strict",
        ),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Plan this.",
        transport=transport,
        prefer_context="forge",
    )

    assert result.error is not None
    assert "active model metadata is incomplete" in result.assistant_message
    assert requests == []


def test_balanced_json_extraction_handles_multiple_objects() -> None:
    assert (
        _extract_balanced_json_object('prefix {"route":"planning"} prose {"route":"off_topic"}')
        == '{"route":"planning"}'
    )


def test_plan_is_thin_is_false_when_meaningful_asset_is_attached() -> None:
    plan = _base_plan()
    plan["requirements"] = []
    plan["tasks"] = []
    plan["assets"] = [{"stored_path": ".alysis/assets/spec.pdf"}]

    assert _plan_is_thin(plan) is False


def test_plan_is_thin_stays_true_with_empty_assets_list() -> None:
    plan = _base_plan()
    plan["requirements"] = []
    plan["tasks"] = []
    plan["assets"] = []

    assert _plan_is_thin(plan) is True


def test_plan_is_thin_stays_true_with_empty_asset_dict() -> None:
    plan = _base_plan()
    plan["requirements"] = []
    plan["tasks"] = []
    plan["assets"] = [{}]

    assert _plan_is_thin(plan) is True


def test_planner_user_prompt_includes_workspace_digest_when_provided() -> None:
    workspace_context = {
        "workspace_kind": "git_repo",
        "focus_relpath": "pkg/api",
        "current_branch": "main",
        "top_level_entries": [
            {"path": "src", "kind": "dir"},
            {"path": "tests", "kind": "dir"},
            {"path": "README.md", "kind": "file"},
        ],
        "manifests": [{"path": "pyproject.toml", "kind": "python"}],
        "readme_paths": ["README.md"],
        "readme_excerpts": [{"path": "README.md", "excerpt": "Build and test instructions."}],
        "conventions_path": "pkg/api/CONVENTIONS.md",
        "conventions_excerpt": "Keep API responses stable and documented.",
        "likely_test_commands": ["pytest -q"],
    }

    digest = compact_workspace_context_for_planner(workspace_context)
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Plan a safe API refactor.",
        workspace_context=workspace_context,
    )

    assert digest["focus_relpath"] == "pkg/api"
    assert digest["manifests"] == ["pyproject.toml"]
    assert digest["likely_test_commands"] == ["pytest -q"]
    assert "Workspace context JSON:" in prompt
    assert '"focus_relpath": "pkg/api"' in prompt
    assert '"manifests": ["pyproject.toml"]' in prompt
    assert "pkg/api/CONVENTIONS.md" in prompt
    assert "pytest -q" in prompt


def test_planner_user_prompt_omits_workspace_digest_when_not_provided() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Plan a safe API refactor.",
    )

    assert "Workspace context JSON:" not in prompt


def test_planner_user_prompt_includes_execution_safe_task_guidance() -> None:
    prompt = _planner_user_prompt(
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Fix parser behavior and add tests.",
        workspace_context={
            "workspace_kind": "git_repo",
            "top_level_entries": [{"path": "src", "kind": "dir"}],
        },
    )

    assert "Do not create blocking test-first tasks" in prompt
    assert "make the intended behavior contract explicit" in prompt
    assert "Do not include read-only context files in write_scope" in prompt
    assert "can be discovered by local search/read tools" in prompt


def test_run_planner_turn_falls_back_to_repo_grounded_task_for_locator_questions(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Which file contains the customer helper?",
        "questions": ["Which module contains the existing customer helper function?"],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            **_base_plan(),
            "requirements": ["Rename customer helper to client terminology."],
        },
        transcript_tail=[],
        user_text=(
            "Rename the customer helper to client terminology while preserving old imports."
        ),
        workspace_context={
            "workspace_kind": "git_repo",
            "top_level_entries": [
                {"path": "contacts", "kind": "dir"},
                {"path": "tests", "kind": "dir"},
                {"path": "README.md", "kind": "file"},
            ],
            "language_hints": ["python"],
            "likely_test_commands": ["pytest -q"],
            "readme_paths": ["README.md"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.questions == []
    assert result.plan_update is not None
    task = result.plan_update["tasks_add"][0]
    assert task["title"] == "Implement requested repository change"
    assert task["estimated_files"] == ["contacts/**/*.py", "tests/**/*.py", "README.md"]
    assert task["write_scope"] == ["contacts/**/*.py", "tests/**/*.py", "README.md"]
    assert "pytest -q" in " ".join(task["acceptance_criteria"])


def test_run_planner_turn_falls_back_for_thin_repo_locator_question(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Which file contains the parser?",
        "questions": ["Which file should I edit for the parser implementation?"],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Fix the Rust parser so it handles empty input correctly.",
        workspace_context={
            "workspace_kind": "git_repo_no_head",
            "top_level_entries": [
                {"path": "Cargo.toml", "kind": "file"},
                {"path": "src", "kind": "dir"},
            ],
            "manifests": [{"path": "Cargo.toml", "kind": "rust"}],
            "observed_paths": ["Cargo.toml", "src", "src/lib.rs"],
            "language_hints": ["rust"],
            "likely_test_commands": ["cargo test"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.questions == []
    assert result.plan_update is not None
    task = result.plan_update["tasks_add"][0]
    assert "src/**" in task["write_scope"]
    assert "cargo test" in " ".join(task["acceptance_criteria"])


def test_run_planner_turn_falls_back_to_read_only_locator_when_update_is_unspecified(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "What specific change should the calculator make?",
        "questions": ["What specific change should the calculator make?"],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text=(
            "Find where the calculator behavior lives, then plan the smallest safe update "
            "with exact local file scope."
        ),
        workspace_context={
            "workspace_kind": "git_repo",
            "top_level_entries": [
                {"path": "README.md", "kind": "file"},
                {"path": "src", "kind": "dir"},
            ],
            "observed_paths": ["README.md", "src/app.py", "src/calc.py"],
            "language_hints": ["python"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.questions == []
    assert result.plan_update is not None
    task = result.plan_update["tasks_add"][0]
    assert task["title"] == "Locate requested repository implementation"
    assert task["estimated_files"] == []
    assert task["write_scope"] == []
    assert "Do not change files" in " ".join(task["acceptance_criteria"])


def test_run_planner_turn_conversational_turn_does_not_synthesize_locator_task(
    monkeypatch,
) -> None:
    # A conversational message must not be turned into a repository task just
    # because the planner's clarifying question mentions repo/codebase terms.
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Sure - what would you like to change in your repository?",
        "questions": ["What files or part of the codebase should we work on?"],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="can you help me",
        workspace_context={
            "workspace_kind": "git_repo",
            "top_level_entries": [
                {"path": "README.md", "kind": "file"},
                {"path": "src", "kind": "dir"},
            ],
            "observed_paths": ["README.md", "src/app.py"],
            "language_hints": ["python"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.plan_update is None
    assert result.assistant_message == planner_payload["assistant_message"]
    assert result.questions == planner_payload["questions"]


def test_run_planner_turn_greenfield_locator_question_becomes_scaffold_task(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Which existing implementation file should I edit?",
        "questions": ["Which file contains the existing implementation?"],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={**_base_plan(), "requirements": [], "tasks": []},
        transcript_tail=[],
        user_text="Build a Next.js portfolio with CV and thoughts pages.",
        workspace_context={
            "workspace_kind": "plain_dir",
            "greenfield": True,
            "top_level_entries": [],
            "observed_paths": [],
            "language_hints": [],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.questions == []
    assert result.plan_update is not None
    task = result.plan_update["tasks_add"][0]
    assert task["title"] == "Build requested project scaffold"
    assert task["estimated_files"] == ["**"]
    assert task["write_scope"] == ["**"]
    assert "Locate requested repository implementation" not in task["title"]


def test_run_planner_turn_greenfield_no_update_proactively_scaffolds(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "I can plan that.",
        "questions": [],
        "plan_update": None,
    }
    transport, _requests = _mock_transport_for_payloads(
        planner_payload,
        _question_repair_payload(should_replace=False),
    )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={**_base_plan(), "requirements": [], "tasks": []},
        transcript_tail=[],
        user_text="Build a Next.js portfolio with CV and thoughts pages.",
        workspace_context={
            "workspace_kind": "plain_dir",
            "greenfield": True,
            "top_level_entries": [],
            "observed_paths": [],
            "language_hints": [],
        },
        transport=transport,
    )

    assert result.error is None
    assert result.questions == []
    assert result.plan_update is not None
    task = result.plan_update["tasks_add"][0]
    assert task["title"] == "Build requested project scaffold"
    assert task["write_scope"] == ["**"]


def test_run_planner_turn_keeps_mutating_locator_request_executable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "I need more detail.",
        "questions": ["What should happen after login?"],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Find where the login implementation lives and fix the redirect bug.",
        workspace_context={
            "workspace_kind": "git_repo",
            "top_level_entries": [
                {"path": "src", "kind": "dir"},
                {"path": "tests", "kind": "dir"},
                {"path": "README.md", "kind": "file"},
            ],
            "observed_paths": ["src/auth.py", "tests/test_auth.py", "README.md"],
            "language_hints": ["python"],
            "likely_test_commands": ["pytest -q"],
            "readme_paths": ["README.md"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.questions == []
    assert result.plan_update is not None
    task = result.plan_update["tasks_add"][0]
    assert task["title"] == "Implement requested repository change"
    assert task["estimated_files"] == ["src/**/*.py", "tests/**/*.py", "README.md"]
    assert task["write_scope"] == ["src/**/*.py", "tests/**/*.py", "README.md"]
    assert "Do not change files" not in " ".join(task["acceptance_criteria"])


def test_run_planner_turn_preserves_greenfield_clarifying_questions(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "I need a few details before planning the website.",
        "questions": [
            "What type of website is this, which sections should it include, and are there style or technical constraints?"
        ],
        "plan_update": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={**_base_plan(), "requirements": [], "tasks": []},
        transcript_tail=[],
        user_text="Can you help me build a new website?",
        workspace_context={
            "workspace_kind": "git_repo",
            "top_level_entries": [
                {"path": "src", "kind": "dir"},
                {"path": "tests", "kind": "dir"},
                {"path": "README.md", "kind": "file"},
            ],
            "language_hints": ["python"],
            "likely_test_commands": ["pytest -q"],
            "readme_paths": ["README.md"],
        },
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.plan_update is None
    assert result.questions == planner_payload["questions"]
    assert "repository-grounded execution task" not in result.assistant_message


def test_run_planner_turn_repairs_common_schema_mismatches(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    broken_but_repairable = """
    Here is the planner result:
    ```json
    {
      "assistant_message": "",
      "questions": "Do you need backward compatibility?",
      "extra_top_level": "drop-me",
      "plan_update": {
        "requirements_append": "Keep legacy flags",
        "tasks_remove": "T01",
        "tasks_add": {
          "id": "T77",
          "status": "planned",
          "title": "Harden planner flow",
          "description": "Improve parser and fallback behavior",
          "acceptance_criteria": "planner handles fenced JSON",
          "estimated_files": "src/alysis_code/plan_assistant.py",
          "dependencies": "T01"
        }
      }
    }
    ```
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": broken_but_repairable}}]},
        )

    cfg = AppConfig(base_url="https://example.com/v1", model="planner-model", temperature=0.9)
    result = run_planner_turn(
        cfg=cfg,
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please improve planning reliability.",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.assistant_message
    assert result.questions == ["Do you need backward compatibility?"]
    assert result.plan_update is not None
    requirements_append = result.plan_update.get("requirements_append") or []
    assert requirements_append == ["Keep legacy flags"]
    assert (result.plan_update.get("tasks_remove") or []) == ["T01"]
    tasks_add = result.plan_update.get("tasks_add") or []
    assert len(tasks_add) == 1
    task = tasks_add[0]
    assert "id" not in task
    assert "status" not in task
    assert task["dependencies"] == ["T01"]
    assert task["acceptance_criteria"] == ["planner handles fenced JSON"]
    assert task["estimated_files"] == ["src/alysis_code/plan_assistant.py"]


def test_run_planner_turn_vague_greenfield_request_uses_llm_question_repair(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan updated with initial website tasks.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Create landing page",
                    "description": "Add a first version of the website",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    transport, _requests = _mock_transport_for_payloads(
        planner_payload,
        _question_repair_payload(
            should_replace=True,
            assistant_message="I need a few details before I lock the first plan.",
            questions=[
                "What should the first version do?",
                "Who will use it and through which interface?",
                "Are there any stack, file, or runtime constraints?",
            ],
        ),
    )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            "schema_version": 1,
            "run_id": "run_1",
            "created_at": "2026-02-20T00:00:00+00:00",
            "updated_at": "2026-02-20T00:00:00+00:00",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        },
        transcript_tail=[],
        user_text="Build me a simple one-page website for my business.",
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is None
    assert result.assistant_message == "I need a few details before I lock the first plan."
    assert result.questions == [
        "What should the first version do?",
        "Who will use it and through which interface?",
        "Are there any stack, file, or runtime constraints?",
    ]


def test_run_planner_turn_mixed_script_question_repair_preserves_llm_reply(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan updated with initial website tasks.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Create landing page",
                    "description": "Add a first version of the website",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    transport, _requests = _mock_transport_for_payloads(
        planner_payload,
        _question_repair_payload(
            should_replace=True,
            assistant_message="Χρειάζομαι λίγες διευκρινίσεις.",
            questions=["Ποιο κοινό στοχεύει το website;"],
        ),
    )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            "schema_version": 1,
            "run_id": "run_1",
            "created_at": "2026-02-20T00:00:00+00:00",
            "updated_at": "2026-02-20T00:00:00+00:00",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        },
        transcript_tail=[],
        user_text="Θέλω build me a simple one-page website for my business.",
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is None
    assert result.assistant_message == "Χρειάζομαι λίγες διευκρινίσεις."
    assert result.questions == ["Ποιο κοινό στοχεύει το website;"]


def test_run_planner_turn_question_repair_preserves_non_latin_reply(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan updated with initial website tasks.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Create landing page",
                    "description": "Add a first version of the website",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    transport, _requests = _mock_transport_for_payloads(
        planner_payload,
        _question_repair_payload(
            should_replace=True,
            assistant_message="Χρειάζομαι λίγες διευκρινίσεις πριν το πλάνο.",
            questions=["Ποιο είναι το βασικό κοινό;"],
        ),
    )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            "schema_version": 1,
            "run_id": "run_1",
            "created_at": "2026-02-20T00:00:00+00:00",
            "updated_at": "2026-02-20T00:00:00+00:00",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        },
        transcript_tail=[],
        user_text="Απάντησε στα ελληνικά. Build me a simple one-page website for my business.",
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is None
    assert result.assistant_message == ("Χρειάζομαι λίγες διευκρινίσεις πριν το πλάνο.")
    assert result.questions == ["Ποιο είναι το βασικό κοινό;"]


def test_run_planner_turn_detailed_greenfield_request_keeps_plan_update(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan updated with a minimal static site.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Add index.html",
                    "description": "Create a text-only landing page with title, intro, and footer",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    transport, _requests = _mock_transport_for_payloads(
        planner_payload,
        _question_repair_payload(should_replace=False),
    )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            "schema_version": 1,
            "run_id": "run_1",
            "created_at": "2026-02-20T00:00:00+00:00",
            "updated_at": "2026-02-20T00:00:00+00:00",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        },
        transcript_tail=[],
        user_text=(
            "Build me a text-only one-page website with a title, short intro, three sections, "
            "and a footer. Keep it clean, readable, and without frameworks."
        ),
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is not None
    assert result.questions == []
    assert result.assistant_message == "Plan updated with a minimal static site."


def test_run_planner_turn_greenfield_request_with_attached_spec_keeps_plan_update(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan updated from the attached spec.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Implement the site from the attached spec",
                    "description": "Create the initial site structure from the provided spec",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    plan = _base_plan()
    plan["requirements"] = []
    plan["tasks"] = []
    plan["assets"] = [{"stored_path": ".alysis/assets/spec.pdf"}]

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Build me a website based on the attached spec.",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.plan_update is not None
    assert result.questions == []
    assert result.assistant_message == "Plan updated from the attached spec."


def test_run_planner_turn_plan_update_uses_stock_ready_message_only_for_low_info_reply(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan updated.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Add index.html",
                    "description": "Create the main page",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    transport, _requests = _mock_transport_for_payloads(
        planner_payload,
        _question_repair_payload(should_replace=False),
    )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            "schema_version": 1,
            "run_id": "run_1",
            "created_at": "2026-02-20T00:00:00+00:00",
            "updated_at": "2026-02-20T00:00:00+00:00",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        },
        transcript_tail=[],
        user_text=(
            "Build me a text-only one-page website with a title, short intro, three sections, "
            "and a footer. Keep it clean, readable, and without frameworks."
        ),
        transport=transport,
    )

    assert result.error is None
    assert result.plan_update is not None
    assert result.questions == []
    assert result.assistant_message == "Plan updated."


def test_run_planner_turn_keeps_custom_follow_up_questions_with_plan_update(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")

    planner_payload = {
        "assistant_message": "Plan drafted. Need one last detail.",
        "questions": ["Should the page use a light or dark theme?"],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Add index.html",
                    "description": "Create the main page",
                    "acceptance_criteria": ["index.html exists"],
                    "estimated_files": ["index.html"],
                    "write_scope": ["index.html"],
                }
            ]
        },
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(planner_payload)}}]},
        )

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan={
            "schema_version": 1,
            "run_id": "run_1",
            "created_at": "2026-02-20T00:00:00+00:00",
            "updated_at": "2026-02-20T00:00:00+00:00",
            "project_goal": "",
            "summary": "",
            "requirements": [],
            "tasks": [],
            "assets": [],
        },
        transcript_tail=[],
        user_text=(
            "Build me a text-only one-page website with a title, short intro, three sections, "
            "and a footer. Keep it clean, readable, and without frameworks."
        ),
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.plan_update is not None
    assert result.questions == ["Should the page use a light or dark theme?"]
    assert result.assistant_message == "Plan drafted. Need one last detail."


def test_run_planner_turn_retries_with_default_temperature_when_model_rejects_custom(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    temperatures: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        temperatures.append(float(body.get("temperature")))
        if len(temperatures) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            "Unsupported value: 'temperature' does not support 0.2 with this model. "
                            "Only the default (1) value is supported."
                        ),
                        "type": "invalid_request_error",
                        "param": "temperature",
                        "code": "unsupported_value",
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "assistant_message": "Captured planner response.",
                                    "questions": [],
                                    "plan_update": None,
                                }
                            )
                        }
                    }
                ]
            },
        )

    cfg = AppConfig(base_url="https://example.com/v1", model="planner-model", temperature=1.0)
    result = run_planner_turn(
        cfg=cfg,
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Plan this task.",
        transport=httpx.MockTransport(handler),
    )

    assert result.error is None
    assert result.assistant_message == "Captured planner response."
    assert result.request_retry_count == 0
    assert temperatures == [0.2, 1.0]


def test_run_planner_turn_streaming_retry_allowed_before_any_delta(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}
    seen_deltas: list[str] = []

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            assert kwargs.get("stream") is True
            if calls["count"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            on_text_delta = kwargs.get("on_text_delta")
            if callable(on_text_delta):
                on_text_delta("Recovered")
            payload = {
                "assistant_message": "Recovered planner response.",
                "questions": [],
                "plan_update": None,
            }
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
        stream=True,
        on_text_delta=seen_deltas.append,
    )

    assert result.error is None
    assert result.request_retry_count == 1
    assert seen_deltas == ["Recovered"]
    assert calls["count"] == 2


def test_run_planner_turn_streaming_does_not_retry_after_visible_delta(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    calls = {"count": 0}
    seen_deltas: list[str] = []

    class FakeClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            calls["count"] += 1
            on_text_delta = kwargs.get("on_text_delta")
            if callable(on_text_delta):
                on_text_delta("partial")
            raise LLMError("LLM request failed: ReadTimeout")

    monkeypatch.setattr("alysis_code.plan_assistant.OpenAICompatClient", FakeClient)

    result = run_planner_turn(
        cfg=AppConfig(base_url="https://example.com/v1", model="planner-model"),
        api_key_override=None,
        plan=_base_plan(),
        transcript_tail=[],
        user_text="Please update the plan",
        stream=True,
        on_text_delta=seen_deltas.append,
    )

    assert result.plan_update is None
    assert result.request_retry_count == 0
    assert result.error is not None
    assert "streamed output had already started" in result.error
    assert "ReadTimeout" in result.error
    assert seen_deltas == ["partial"]
    assert calls["count"] == 1
