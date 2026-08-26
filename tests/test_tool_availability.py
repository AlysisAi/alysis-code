from __future__ import annotations

import pytest

from alysis_code.tools.availability import (
    WEB_UNAVAILABLE_OBSERVATION,
    is_tool_unavailable_result,
    web_unavailable_result,
)


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


def test_web_unavailable_result_rejects_non_web_tools() -> None:
    with pytest.raises(ValueError, match="not a web tool"):
        web_unavailable_result("fs_read", detail="whatever")
