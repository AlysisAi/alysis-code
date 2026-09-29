from __future__ import annotations

import pytest

from alysis_code.tools.availability import (
    WEB_UNAVAILABLE_OBSERVATION,
    _reset_tool_availability_for_tests,
    get_tool_availability,
    is_tool_unavailable_result,
    mark_available,
    mark_unavailable,
    register_tool_availability,
    tool_availability_snapshot,
    tool_unavailable_cause,
    unavailable_tool_result,
    web_unavailable_result,
)


@pytest.fixture(autouse=True)
def _reset_availability() -> None:
    _reset_tool_availability_for_tests()
    yield
    _reset_tool_availability_for_tests()


def test_private_availability_snapshot_overrides_global_without_mutating_it() -> None:
    register_tool_availability("shared_optional", optional=True)
    mark_unavailable("shared_optional", "global session is missing its provider")

    private_snapshot = tool_availability_snapshot({})
    register_tool_availability(
        "shared_optional",
        optional=True,
        availability=private_snapshot,
    )
    mark_available("shared_optional", availability=private_snapshot)

    assert unavailable_tool_result("shared_optional", availability=private_snapshot) is None
    assert unavailable_tool_result("shared_optional") == {
        "status": "tool_unavailable",
        "tool": "shared_optional",
        "reason": "global session is missing its provider",
    }

    mark_unavailable(
        "shared_optional",
        "private session lost its provider",
        availability=private_snapshot,
    )

    assert unavailable_tool_result("shared_optional", availability=private_snapshot) == {
        "status": "tool_unavailable",
        "tool": "shared_optional",
        "reason": "private session lost its provider",
    }
    global_state = get_tool_availability("shared_optional")
    assert global_state is not None
    assert global_state.unavailable_reason == "global session is missing its provider"


def test_web_unavailable_result_defaults_to_generic_observation() -> None:
    result = web_unavailable_result("web_search")

    assert result["status"] == "tool_unavailable"
    assert result["tool"] == "web_search"
    assert result["reason"] == WEB_UNAVAILABLE_OBSERVATION
    assert is_tool_unavailable_result(result)


def test_web_unavailable_result_detail_surfaces_the_cause() -> None:
    result = web_unavailable_result(
        "web_search",
        detail=(
            "native web search via openai_responses failed and web_search_mode="
            "native disables external fallback backends (set web_search_mode "
            "to 'auto' or 'external' to allow them): Responses error 400"
        ),
    )

    assert result["reason"].startswith(WEB_UNAVAILABLE_OBSERVATION)
    assert "Cause:" in result["reason"]
    assert "set web_search_mode to 'auto' or 'external'" in result["reason"]
    assert is_tool_unavailable_result(result)


def test_web_unavailable_result_ignores_blank_detail() -> None:
    result = web_unavailable_result("web_fetch", detail="   ")

    assert result["reason"] == WEB_UNAVAILABLE_OBSERVATION


def test_tool_unavailable_cause_keeps_only_what_went_wrong() -> None:
    web = web_unavailable_result("web_fetch", detail="[Errno 101] Network is unreachable")
    assert tool_unavailable_cause(web) == "[Errno 101] Network is unreachable"
    assert tool_unavailable_cause(web_unavailable_result("web_fetch")) == ""
    optional = {
        "status": "tool_unavailable",
        "tool": "image_generate",
        "reason": "image_generation.enabled is false",
    }
    assert tool_unavailable_cause(optional) == "image_generation.enabled is false"
    assert tool_unavailable_cause({"error": "boom"}) == ""


def test_web_unavailable_result_rejects_non_web_tools() -> None:
    with pytest.raises(ValueError, match="not a web tool"):
        web_unavailable_result("fs_read", detail="whatever")
