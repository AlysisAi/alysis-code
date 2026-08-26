from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image

from alysis_code.assets import AssetComprehender, AssetIndex, ingest_asset
from alysis_code.assets.ocr import OcrResult
from alysis_code.config import ApiKeyResolution, AppConfig
from alysis_code.forge import create_plan_run
from alysis_code.llm.openai_compat import LLMError, LLMResponse, LLMUsage
from alysis_code.model_registry import ModelRegistry
from alysis_code.profiles import ProfileSpec, add_profile


def _cfg(*, primary_vision: bool = False, fallback_vision: bool = False) -> AppConfig:
    cfg = AppConfig(model="primary-model")
    models: dict[str, dict[str, object]] = {
        "primary-model": {
            "context_window_tokens": 32000,
            "max_output_tokens": 4096,
            "supports_vision": primary_vision,
        },
        "fallback-vision-model": {
            "context_window_tokens": 32000,
            "max_output_tokens": 4096,
            "supports_vision": fallback_vision,
        },
    }
    cfg.extra_fields = {
        "role_models": {"comprehension": "primary-model"},
        "model_metadata_overrides": {"models": models},
    }
    return cfg


def _payload(summary: str = "Structured summary") -> dict[str, object]:
    return {
        "detected_language": "en",
        "language_confidence": 0.9,
        "data": {
            "semantic_summary": summary,
            "classification": {"kind": "text", "subkind": "brief", "domain": "software"},
            "key_entities": [{"type": "endpoint", "value": "/auth/login"}],
            "stated_facts": ["Fact"],
            "stated_decisions": ["Decision"],
            "stated_constraints": ["Constraint"],
            "actionable_signals": ["Signal"],
            "open_questions": ["Question"],
            "relations_hint": "Related to auth",
            "confidence": {"summary": 0.85},
        },
    }


class _FakeClient:
    def __init__(self, calls: list[dict[str, Any]], responses: list[str]) -> None:
        self.calls = calls
        self.responses = responses

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        content = self.responses.pop(0) if self.responses else json.dumps(_payload())
        return LLMResponse(
            content=content,
            tool_calls=[],
            raw={},
            response_model="response-model",
            usage=LLMUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


class _FakeOcrProvider:
    name = "fake-ocr"

    def __init__(self, *, available: bool = True, text: str = "OCR text") -> None:
        self.available = available
        self.text = text

    def is_available(self) -> bool:
        return self.available

    def installed_languages(self) -> list[str]:
        return ["eng"]

    def detect_script(self, image_path: Path):  # type: ignore[no-untyped-def]
        _ = image_path
        return None

    def extract_text(self, image_path: Path, *, languages: list[str] | None = None) -> OcrResult:
        _ = image_path
        _ = languages
        return OcrResult(
            text=self.text,
            languages_used=["eng"],
            confidence=0.8,
            engine_version="fake-ocr-1",
            elapsed_ms=1,
        )


class _ToolRejectingClient:
    def __init__(self, calls: list[dict[str, Any]]) -> None:
        self.calls = calls

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        if "tools" in kwargs:
            raise LLMError("tool_choice unsupported by provider")
        return LLMResponse(
            content=json.dumps(_payload()),
            tool_calls=[],
            raw={},
            response_model="response-model",
            usage=LLMUsage(prompt_tokens=11, completion_tokens=7, total_tokens=18),
        )


def _patch_llm(
    monkeypatch,
    *,
    responses: list[str] | None = None,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    queued = list(responses or [json.dumps(_payload())])
    monkeypatch.setattr("alysis_code.assets.comprehender.get_api_key", lambda: "k")

    def fake_make_llm_client(**kwargs: Any) -> _FakeClient:
        calls.append({"make_client": kwargs})
        return _FakeClient(calls, queued)

    monkeypatch.setattr("alysis_code.assets.comprehender.make_llm_client", fake_make_llm_client)
    return calls


def _text_asset(tmp_path: Path):
    paths = create_plan_run(tmp_path, create_if_missing=True)
    source = tmp_path / "brief.txt"
    source.write_text("Asset text\n", encoding="utf-8")
    return paths, ingest_asset(source, title="Brief", run_paths=paths)


def _image_asset(tmp_path: Path, *, description: str = ""):
    paths = create_plan_run(tmp_path, create_if_missing=True)
    source = tmp_path / "diagram.png"
    Image.new("RGB", (40, 40), color="white").save(source)
    return paths, ingest_asset(source, title="Diagram", description=description, run_paths=paths)


def test_text_asset_uses_text_comprehension_path(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _text_asset(tmp_path)
    calls = _patch_llm(monkeypatch)
    cfg = _cfg()

    record = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    ).comprehend(asset)

    assert record.source == "text_only"
    assert record.status == "ready"
    assert record.model == "primary-model"
    assert record.role == "comprehension"
    assert record.tokens_used == {"input": 11, "output": 7}
    assert calls[0]["make_client"]["model"] == "primary-model"
    chat_calls = [call for call in calls if "messages" in call]
    assert chat_calls[0]["tools"][0]["function"]["name"] == "record_asset_comprehension"
    assert chat_calls[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "record_asset_comprehension"},
    }


def test_image_with_vision_and_ocr_uses_combined_source(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _image_asset(tmp_path)
    calls = _patch_llm(monkeypatch)
    cfg = _cfg(primary_vision=True)

    record = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=_FakeOcrProvider(),
        run_paths=paths,
    ).comprehend(asset)

    assert record.source == "vision_with_ocr"
    assert record.ocr_engine == "fake-ocr-1"
    chat_calls = [call for call in calls if "messages" in call]
    assert chat_calls
    assert isinstance(chat_calls[0]["messages"][1]["content"], list)


def test_image_uses_configured_vision_fallback_profile(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _image_asset(tmp_path)
    calls = _patch_llm(monkeypatch)
    cfg = _cfg(primary_vision=False, fallback_vision=True)
    cfg.assets.comprehension.vision_fallback_profile = "vision"
    add_profile(
        cfg,
        ProfileSpec(
            name="vision",
            base_url="https://example.com/v1",
            default_model="fallback-vision-model",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.assets.comprehender.resolve_profile_api_key",
        lambda _cfg, _name: ApiKeyResolution(key="profile-key", source="test"),
    )

    record = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    ).comprehend(asset)

    assert record.source == "vision"
    assert record.model == "fallback-vision-model"
    assert calls[0]["make_client"]["profile"].name == "vision"


def test_image_without_vision_uses_ocr_only(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _image_asset(tmp_path)
    _patch_llm(monkeypatch)
    cfg = _cfg(primary_vision=False)

    record = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=_FakeOcrProvider(text="Recognized text"),
        run_paths=paths,
    ).comprehend(asset)

    assert record.source == "ocr_only"
    assert record.confidence_modifier == 0.7
    updated = AssetIndex(paths).get(asset.id)
    assert updated.extracted_text_path is not None
    assert (paths.root / updated.extracted_text_path).read_text(
        encoding="utf-8"
    ) == "Recognized text"


def test_image_without_vision_or_ocr_uses_description_or_minimal(tmp_path: Path) -> None:
    paths, asset = _image_asset(tmp_path, description="User supplied description")
    cfg = _cfg(primary_vision=False)

    described = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    ).comprehend(asset)

    assert described.source == "user_description"
    assert described.confidence_modifier == 0.5

    other_paths, other_asset = _image_asset(tmp_path / "other")
    minimal = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=other_paths,
    ).comprehend(other_asset)
    assert minimal.source == "minimal"
    assert minimal.confidence_modifier == 0.3


def test_cache_hit_reuses_ready_comprehension_without_llm(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _text_asset(tmp_path)
    calls = _patch_llm(monkeypatch)
    cfg = _cfg()
    comprehender = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    )

    first = comprehender.comprehend(asset)
    assert first.version == 1
    calls.clear()
    second = comprehender.comprehend(AssetIndex(paths).get(asset.id))

    assert second.version == 2
    assert calls == []
    assert (paths.assets_comprehensions_dir / asset.id / "v1.json").exists()
    assert (paths.assets_comprehensions_dir / asset.id / "v2.json").exists()
    pointer = json.loads((paths.assets_comprehensions_dir / asset.id / "current.json").read_text())
    assert pointer["version"] == 2


def test_inspection_angle_bypasses_identity_cache(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _text_asset(tmp_path)
    calls = _patch_llm(monkeypatch)
    cfg = _cfg()
    comprehender = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    )

    comprehender.comprehend(asset)
    calls.clear()
    record = comprehender.comprehend(AssetIndex(paths).get(asset.id), angle="security")

    chat_calls = [call for call in calls if "messages" in call]
    assert record.version == 2
    assert chat_calls
    assert (
        "Additional inspection angle requested by the planner"
        in chat_calls[0]["messages"][1]["content"]
    )
    assert "security" in chat_calls[0]["messages"][1]["content"]


def test_malformed_llm_response_retries_then_fails(tmp_path: Path, monkeypatch) -> None:
    paths, asset = _text_asset(tmp_path)
    calls = _patch_llm(monkeypatch, responses=["not json", '{"bad": true}'])
    cfg = _cfg()

    record = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    ).comprehend(asset)

    chat_calls = [call for call in calls if "messages" in call]
    assert len(chat_calls) == 2
    assert record.status == "failed"
    assert record.error is not None


def test_structured_tool_rejection_falls_back_to_json_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths, asset = _text_asset(tmp_path)
    calls: list[dict[str, Any]] = []
    cfg = _cfg()
    monkeypatch.setattr("alysis_code.assets.comprehender.get_api_key", lambda: "k")

    def fake_make_llm_client(**kwargs: Any) -> _ToolRejectingClient:
        calls.append({"make_client": kwargs})
        return _ToolRejectingClient(calls)

    monkeypatch.setattr(
        "alysis_code.assets.comprehender.make_llm_client",
        fake_make_llm_client,
    )

    record = AssetComprehender(
        cfg=cfg,
        model_registry=ModelRegistry(cfg=cfg),
        ocr_provider=None,
        run_paths=paths,
    ).comprehend(asset)

    chat_calls = [call for call in calls if "messages" in call]
    assert record.status == "ready"
    assert "tools" in chat_calls[0]
    assert chat_calls[1]["response_format"] == {"type": "json_object"}
