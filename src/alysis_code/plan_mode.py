from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .llm.types import LLMError
from .plan_assistant import compact_workspace_context_for_planner
from .usage_tracker import build_usage_record, usage_context_from_client_response

PLAN_CONTEXT_MAX_MESSAGES = 20
TOOL_RESULT_SUMMARY_CHARS = 200
_APPROVED_PLAN_MARKER = "Approved plan:\n"
_APPROVED_PLAN_EXECUTE_MARKER = "Now execute this task"

PLAN_MODE_SYSTEM_PROMPT = """You are an execution planner for a local coding agent.

Produce a concise, actionable draft implementation plan only.

Rules:
- Output 3-7 numbered steps.
- Mention likely files/modules to change when possible.
- Include verification/tests to run.
- If user-facing behavior changes, include docs/help/README updates.
- Keep scope reviewable and practical.
- Treat host-provided workspace context as the source of truth for repo structure and likely test commands.
- Mention repo-relative files, modules, frameworks, and markup only when they are supported by host-provided workspace context or prior conversation evidence.
- If the available context does not support an exact path/framework/file, describe the area generically instead of inventing details.
- Do not claim that commands were run or files were inspected.
- Do not execute tools or propose tool calls; planning text only.
- Choose the natural response language from the user's request and recent visible conversation.
- Follow explicit language/script requests when present.
- For empty, malformed, ambiguous, gibberish, or code-only input, use English with Latin script.
- Never translate code identifiers, file paths, CLI commands, config keys, or code blocks; keep them exactly as written.
"""


def _to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=True)
    except Exception:
        return str(value)


def _truncate(value: str, max_len: int) -> str:
    text = value.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "..."


def _workspace_context_block(workspace_context: dict[str, Any] | None) -> str:
    if workspace_context is None:
        return ""
    digest = compact_workspace_context_for_planner(workspace_context)
    if not digest:
        return ""
    return (
        "Workspace context JSON:\n"
        f"{json.dumps(digest, ensure_ascii=False, sort_keys=True)}\n\n"
        "Grounding rules:\n"
        "- Only mention repo-relative files, modules, frameworks, or markup that this workspace context supports.\n"
        "- Prefer the provided likely_test_commands when suggesting verification.\n"
        "- If exact details are not supported here or in prior conversation context, describe the area generically."
    )


def _normalize_plan_history_entries(
    session_messages: list[dict[str, Any]],
    *,
    max_history_messages: int,
    tool_result_chars: int,
) -> list[dict[str, str]]:
    filtered: list[dict[str, str]] = []
    tool_call_names: dict[str, str] = {}

    for entry in session_messages:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip().lower()

        if role == "user":
            text = _to_text(entry.get("content"))
            if text.strip():
                filtered.append({"role": "user", "content": text})
            continue

        if role == "assistant":
            tool_calls_raw = entry.get("tool_calls")
            tool_calls = tool_calls_raw if isinstance(tool_calls_raw, list) else []
            if tool_calls:
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "").strip()
                    if not call_id:
                        continue
                    tool_call_names[call_id] = _extract_tool_call_name(call)
                filtered.append(
                    {
                        "role": "assistant",
                        "content": _summarize_assistant_tool_calls(
                            entry,
                            tool_result_chars=tool_result_chars,
                        ),
                    }
                )
                continue

            text = _to_text(entry.get("content"))
            if text.strip():
                filtered.append({"role": "assistant", "content": text})
            continue

        if role == "tool":
            filtered.append(
                {
                    "role": "tool",
                    "content": _summarize_tool_result(
                        entry,
                        tool_call_names=tool_call_names,
                        tool_result_chars=tool_result_chars,
                    ),
                }
            )

    if max_history_messages > 0 and len(filtered) > max_history_messages:
        return filtered[-max_history_messages:]
    return filtered


def _plan_context_user_prompt(
    *,
    history_entries: list[dict[str, str]],
    workspace_context: dict[str, Any] | None,
    user_message: str,
    previous_plan: str | None = None,
    feedback: str | None = None,
) -> str:
    user_text = user_message.strip()
    prompt_parts = [
        "Recent conversation transcript JSON:",
        json.dumps(history_entries, ensure_ascii=False, indent=2),
        "",
    ]
    workspace_block = _workspace_context_block(workspace_context)
    if workspace_block:
        prompt_parts.extend([workspace_block, ""])
    prompt_parts.extend(
        [
            "Draft an execution plan for this request:",
            user_text,
            "",
            "Return only the plan text in plain markdown.",
        ]
    )
    prev = (previous_plan or "").strip()
    notes = (feedback or "").strip()
    if prev and notes:
        prompt_parts.extend(
            [
                "",
                "Revise this previous draft plan based on user feedback.",
                "Previous draft plan:",
                prev,
                "",
                "User feedback:",
                notes,
            ]
        )
    prompt_parts.extend(
        [
            "",
            "Grounding rules:",
            "- Base exact path/module/framework references only on host-provided workspace context or prior conversation evidence.",
            "- If exact file/framework details are uncertain, keep the plan generic instead of guessing.",
        ]
    )
    return "\n".join(prompt_parts)


def extract_approved_plan_user_message(text: str) -> str | None:
    clean = str(text or "").strip()
    if not clean:
        return None
    plan_idx = clean.find(_APPROVED_PLAN_MARKER)
    execute_idx = clean.find(_APPROVED_PLAN_EXECUTE_MARKER)
    if plan_idx < 0 or execute_idx < 0 or plan_idx >= execute_idx:
        return None
    base = clean[:plan_idx].rstrip()
    if not base:
        return None
    return base


def _extract_tool_call_name(raw_call: Any) -> str:
    if not isinstance(raw_call, dict):
        return "unknown_tool"
    fn = raw_call.get("function")
    if isinstance(fn, dict):
        name = str(fn.get("name") or "").strip()
        if name:
            return name
    name = str(raw_call.get("name") or "").strip()
    return name or "unknown_tool"


def _summarize_assistant_tool_calls(
    message: dict[str, Any],
    *,
    tool_result_chars: int,
) -> str:
    tool_calls_raw = message.get("tool_calls")
    tool_calls = tool_calls_raw if isinstance(tool_calls_raw, list) else []
    names: list[str] = []
    for item in tool_calls[:8]:
        name = _extract_tool_call_name(item)
        if name and name not in names:
            names.append(name)
    names_text = ", ".join(names) if names else "unknown_tool"
    content = _to_text(message.get("content"))
    if content.strip():
        return (
            f"Assistant requested tool calls: {names_text}. "
            f"Assistant note: {_truncate(content, tool_result_chars)}"
        )
    return f"Assistant requested tool calls: {names_text}."


def _summarize_tool_result(
    message: dict[str, Any],
    *,
    tool_call_names: dict[str, str],
    tool_result_chars: int,
) -> str:
    call_id = str(message.get("tool_call_id") or "").strip()
    tool_name = tool_call_names.get(call_id) or "unknown_tool"
    content = _to_text(message.get("content"))
    if not content.strip():
        return f"Tool result ({tool_name}): (empty)"
    return f"Tool result ({tool_name}): {_truncate(content, tool_result_chars)}"


def build_plan_context_messages(
    session_messages: list[dict[str, Any]],
    user_message: str,
    previous_plan: str | None = None,
    feedback: str | None = None,
    workspace_context: dict[str, Any] | None = None,
    *,
    plan_system_prompt: str = PLAN_MODE_SYSTEM_PROMPT,
    max_history_messages: int = PLAN_CONTEXT_MAX_MESSAGES,
    tool_result_chars: int = TOOL_RESULT_SUMMARY_CHARS,
) -> list[dict[str, Any]]:
    # Keep planner transport invariant simple and provider-safe: exactly one system
    # message and one user message, with capped normalized history serialized into
    # the final user prompt rather than flattened as chat-message turns.
    history_entries = _normalize_plan_history_entries(
        session_messages,
        max_history_messages=max_history_messages,
        tool_result_chars=tool_result_chars,
    )
    return [
        {"role": "system", "content": plan_system_prompt},
        {
            "role": "user",
            "content": _plan_context_user_prompt(
                history_entries=history_entries,
                workspace_context=workspace_context,
                user_message=user_message,
                previous_plan=previous_plan,
                feedback=feedback,
            ),
        },
    ]


def generate_plan_draft(
    client: Any,
    session_messages: list[dict[str, Any]],
    user_message: str,
    previous_plan: str | None = None,
    feedback: str | None = None,
    workspace_context: dict[str, Any] | None = None,
    stream: bool = False,
    on_text_delta: Callable[[str], None] | None = None,
    *,
    details: dict[str, Any] | None = None,
) -> str:
    if client is None:
        raise RuntimeError("Plan generation unavailable: missing session client.")

    request_messages = build_plan_context_messages(
        session_messages=session_messages,
        user_message=user_message,
        previous_plan=previous_plan,
        feedback=feedback,
        workspace_context=workspace_context,
    )
    try:
        response = client.chat(
            messages=request_messages,
            tools=None,
            stream=bool(stream),
            on_text_delta=on_text_delta if stream else None,
        )
    except LLMError as e:
        raise RuntimeError(f"Plan generation failed: {e}") from e

    plan_text = str(getattr(response, "content", "") or "").strip()
    if not plan_text:
        raise RuntimeError("Plan generation returned an empty response.")

    if details is not None:
        details["request_messages"] = request_messages
        details["response"] = response
    return plan_text


def instruction_with_approved_plan(*, user_message: str, approved_plan: str) -> str:
    base = user_message.strip()
    plan_text = approved_plan.strip()
    if not plan_text:
        return base
    return (
        f"{base}\n\n"
        f"{_APPROVED_PLAN_MARKER}{plan_text}\n\n"
        "Now execute this task in the repository and follow the approved plan."
    )


def record_plan_usage(
    *,
    session: Any,
    request_messages: list[dict[str, Any]],
    response: Any,
) -> None:
    try:
        requested_model = str(getattr(getattr(session, "client", None), "model", "") or "").strip()
        if not requested_model:
            return
        registry = getattr(session, "model_registry", None)
        usage_summary = getattr(session, "usage_summary", None)
        if registry is None or usage_summary is None:
            return
        response_tool_calls = []
        for tc in list(getattr(response, "tool_calls", []) or []):
            response_tool_calls.append(
                {
                    "id": str(getattr(tc, "id", "") or ""),
                    "name": str(getattr(tc, "name", "") or ""),
                    "arguments": getattr(tc, "arguments", {}) or {},
                }
            )
        usage = getattr(response, "usage", None)
        usage_record = build_usage_record(
            role="plan",
            requested_model=requested_model,
            response_model=getattr(response, "response_model", None),
            messages=request_messages,
            response_content=str(getattr(response, "content", "") or ""),
            response_tool_calls=response_tool_calls,
            api_prompt_tokens=(
                getattr(usage, "prompt_tokens", None) if usage is not None else None
            ),
            api_usage=usage,
            api_cached_prompt_tokens=(
                getattr(usage, "cached_prompt_tokens", None) if usage is not None else None
            ),
            api_completion_tokens=(
                getattr(usage, "completion_tokens", None) if usage is not None else None
            ),
            api_total_tokens=(getattr(usage, "total_tokens", None) if usage is not None else None),
            registry=registry,
            **usage_context_from_client_response(
                client=getattr(session, "client", None),
                response=response,
                operation="plan_llm",
            ),
        )
        add_record = getattr(usage_summary, "add_record", None)
        if callable(add_record):
            add_record(usage_record)
        store = getattr(session, "store", None)
        append = getattr(store, "append", None)
        if callable(append):
            append("llm_usage", usage_record.to_payload())
    except Exception:
        return
