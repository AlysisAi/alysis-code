from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

AssetKind = Literal["text", "image"]
ComprehensionStatus = Literal["pending", "ready", "failed", "minimal"]
ComprehensionSource = Literal[
    "vision",
    "vision_with_ocr",
    "ocr_only",
    "user_description",
    "text_only",
    "minimal",
]


class AssetError(RuntimeError):
    pass


class AssetAlreadyExistsError(AssetError):
    def __init__(self, existing_id: str) -> None:
        self.existing_id = existing_id
        super().__init__(f"Asset content already exists as {existing_id}.")


class AssetNotFoundError(AssetError):
    pass


class OcrError(AssetError):
    pass


@dataclass(frozen=True)
class AssetRecord:
    id: str
    title: str
    description: str
    kind: AssetKind
    mime: str
    original_filename: str
    size_bytes: int
    sha256: str
    stored_path: str
    extracted_text_path: str | None
    thumbnail_path: str | None
    pinned: bool
    added_at: str
    added_by: dict[str, Any]
    deleted_at: str | None
    comprehension_status: ComprehensionStatus
    comprehension_current_version: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AssetRecord:
        return cls(
            id=str(payload.get("id") or "").strip(),
            title=str(payload.get("title") or "").strip(),
            description=str(payload.get("description") or ""),
            kind=_literal_value(payload.get("kind"), {"text", "image"}, field_name="kind"),  # type: ignore[arg-type]
            mime=str(payload.get("mime") or "").strip(),
            original_filename=str(payload.get("original_filename") or ""),
            size_bytes=int(payload.get("size_bytes") or 0),
            sha256=str(payload.get("sha256") or "").strip(),
            stored_path=str(payload.get("stored_path") or "").strip(),
            extracted_text_path=_optional_string(payload.get("extracted_text_path")),
            thumbnail_path=_optional_string(payload.get("thumbnail_path")),
            pinned=bool(payload.get("pinned", False)),
            added_at=str(payload.get("added_at") or "").strip(),
            added_by=dict(payload.get("added_by") or {}),
            deleted_at=_optional_string(payload.get("deleted_at")),
            comprehension_status=_literal_value(
                payload.get("comprehension_status"),
                {"pending", "ready", "failed", "minimal"},
                field_name="comprehension_status",
            ),  # type: ignore[arg-type]
            comprehension_current_version=_optional_int(
                payload.get("comprehension_current_version")
            ),
        )


@dataclass(frozen=True)
class ComprehensionData:
    semantic_summary: str = ""
    classification: dict[str, str] = field(default_factory=dict)
    key_entities: list[dict[str, str]] = field(default_factory=list)
    stated_facts: list[str] = field(default_factory=list)
    stated_decisions: list[str] = field(default_factory=list)
    stated_constraints: list[str] = field(default_factory=list)
    actionable_signals: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    relations_hint: str = ""
    confidence: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> ComprehensionData:
        raw = payload if isinstance(payload, dict) else {}
        return cls(
            semantic_summary=str(raw.get("semantic_summary") or ""),
            classification={
                str(key): str(value) for key, value in dict(raw.get("classification") or {}).items()
            },
            key_entities=_dict_string_list(raw.get("key_entities")),
            stated_facts=_string_list(raw.get("stated_facts")),
            stated_decisions=_string_list(raw.get("stated_decisions")),
            stated_constraints=_string_list(raw.get("stated_constraints")),
            actionable_signals=_string_list(raw.get("actionable_signals")),
            open_questions=_string_list(raw.get("open_questions")),
            relations_hint=str(raw.get("relations_hint") or ""),
            confidence=_float_dict(raw.get("confidence")),
        )


@dataclass(frozen=True)
class ComprehensionRecord:
    schema_version: int
    version: int
    asset_id: str
    status: Literal["ready", "failed", "minimal"]
    source: ComprehensionSource
    model: str | None
    role: str | None
    ocr_engine: str | None
    ocr_languages_used: list[str]
    detected_language: str | None
    language_confidence: float | None
    confidence_modifier: float
    tokens_used: dict[str, int]
    elapsed_ms: int
    generated_at: str
    error: str | None
    data: ComprehensionData

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["data"] = self.data.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ComprehensionRecord:
        return cls(
            schema_version=int(payload.get("schema_version") or 1),
            version=int(payload.get("version") or 0),
            asset_id=str(payload.get("asset_id") or "").strip(),
            status=_literal_value(
                payload.get("status"),
                {"ready", "failed", "minimal"},
                field_name="status",
            ),  # type: ignore[arg-type]
            source=_literal_value(
                payload.get("source"),
                {
                    "vision",
                    "vision_with_ocr",
                    "ocr_only",
                    "user_description",
                    "text_only",
                    "minimal",
                },
                field_name="source",
            ),  # type: ignore[arg-type]
            model=_optional_string(payload.get("model")),
            role=_optional_string(payload.get("role")),
            ocr_engine=_optional_string(payload.get("ocr_engine")),
            ocr_languages_used=_string_list(payload.get("ocr_languages_used")),
            detected_language=_optional_string(payload.get("detected_language")),
            language_confidence=_optional_float(payload.get("language_confidence")),
            confidence_modifier=float(payload.get("confidence_modifier") or 0.0),
            tokens_used=_int_dict(payload.get("tokens_used")),
            elapsed_ms=int(payload.get("elapsed_ms") or 0),
            generated_at=str(payload.get("generated_at") or "").strip(),
            error=_optional_string(payload.get("error")),
            data=ComprehensionData.from_dict(payload.get("data")),
        )


def _literal_value(value: Any, allowed: set[str], *, field_name: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        allowed_label = ", ".join(sorted(allowed))
        raise AssetError(f"{field_name} must be one of: {allowed_label}")
    return text


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as e:
        raise AssetError(f"Expected integer-compatible value: {value!r}") from e


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise AssetError(f"Expected float-compatible value: {value!r}") from e


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item).strip())]


def _dict_string_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        cleaned = {
            str(key).strip(): str(raw_value).strip()
            for key, raw_value in item.items()
            if str(key).strip() and str(raw_value).strip()
        }
        if cleaned:
            out.append(cleaned)
    return out


def _float_dict(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, raw_value in value.items():
        try:
            out[str(key)] = float(raw_value)
        except (TypeError, ValueError):
            continue
    return out


def _int_dict(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for key, raw_value in value.items():
        try:
            out[str(key)] = int(raw_value)
        except (TypeError, ValueError):
            continue
    return out
