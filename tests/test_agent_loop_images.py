from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from alysis_code.agent_loop import _build_user_message, create_session
from alysis_code.config import AppConfig, ConfigError
from alysis_code.llm.openai_compat import LLMResponse


def test_build_user_message_with_image_hides_base64_in_logs(tmp_path: Path) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    message, log_payload = _build_user_message(
        root=tmp_path,
        instruction="describe this image",
        image_paths=["sample.png"],
    )

    assert message["role"] == "user"
    content = message["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[1]["type"] == "text"
    assert content[1]["text"].startswith("describe this image")
    assert "1 image attached" in content[1]["text"]
    assert "visual content" in content[1]["text"]

    assert log_payload["content"] == "describe this image"
    assert log_payload["images"][0]["path"] == os.fspath(image.resolve())
    assert "base64" not in json.dumps(log_payload)


def test_run_turn_with_image_adds_visual_input_hint(tmp_path: Path) -> None:
    image = tmp_path / "sample.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    captured: dict[str, list[dict[str, Any]]] = {}

    class _CaptureClient:
        model = "test-model"
        temperature = 0.2

        def chat(
            self,
            *,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None = None,
            stream: bool = False,
            on_text_delta: Any = None,
            temperature: float | None = None,
        ) -> LLMResponse:
            _ = tools, stream, on_text_delta, temperature
            captured["messages"] = messages
            return LLMResponse(content="I can inspect the attachment.", tool_calls=[], raw={})

    session = create_session(
        cfg=AppConfig(model="test-model", stream=False),
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        verification_enabled=False,
    )
    session.client = _CaptureClient()  # type: ignore[assignment]

    try:
        assert session.run_turn("can you see the image?", image_paths=["sample.png"]) == 0
    finally:
        session.close()

    request_messages = captured["messages"]
    system_text = "\n".join(
        str(msg.get("content") or "") for msg in request_messages if msg.get("role") == "system"
    )
    assert "includes image attachment" in system_text
    assert "visual input" in system_text

    user_messages = [msg for msg in request_messages if msg.get("role") == "user"]
    image_message = next(
        msg for msg in reversed(user_messages) if isinstance(msg.get("content"), list)
    )
    content = image_message["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[1]["type"] == "text"
    assert "can you see the image?" in content[1]["text"]


def test_build_user_message_records_display_content_for_approved_plan_instruction(
    tmp_path: Path,
) -> None:
    instruction = (
        "Build a project.\n\nApproved plan:\n1. Inspect the repo\n\n"
        "Now execute this task in the repository and follow the approved plan."
    )

    message, log_payload = _build_user_message(
        root=tmp_path,
        instruction=instruction,
        image_paths=None,
    )

    assert message == {"role": "user", "content": instruction}
    assert log_payload["content"] == instruction
    assert log_payload["display_content"] == "Build a project."


def test_build_user_message_errors_for_missing_image(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Image file not found"):
        _build_user_message(root=tmp_path, instruction="hi", image_paths=["missing.png"])


def test_build_user_message_errors_for_non_image_file(tmp_path: Path) -> None:
    text_file = tmp_path / "note.txt"
    text_file.write_text("not an image", encoding="utf-8")

    with pytest.raises(ConfigError, match="Unsupported image type"):
        _build_user_message(root=tmp_path, instruction="hi", image_paths=["note.txt"])
