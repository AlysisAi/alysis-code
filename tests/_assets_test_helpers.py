from __future__ import annotations

import time
from pathlib import Path

from alysis_code.assets import ComprehensionData, ComprehensionRecord
from alysis_code.assets.index import AssetIndex
from alysis_code.assets.models import AssetRecord
from alysis_code.forge import RunPaths


class FakeAssetComprehender:
    def __init__(
        self,
        run_paths: RunPaths,
        *,
        delay_seconds: float = 0.0,
        summary_prefix: str = "Summary",
    ) -> None:
        self.run_paths = run_paths
        self.delay_seconds = delay_seconds
        self.summary_prefix = summary_prefix
        self.calls: list[str] = []

    def comprehend(self, asset: AssetRecord, *, angle: str | None = None) -> ComprehensionRecord:
        _ = angle
        self.calls.append(asset.id)
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        record = ComprehensionRecord(
            schema_version=1,
            version=0,
            asset_id=asset.id,
            status="ready",
            source="text_only",
            model="fake-model",
            role="comprehension",
            ocr_engine=None,
            ocr_languages_used=[],
            detected_language="en",
            language_confidence=0.9,
            confidence_modifier=1.0,
            tokens_used={},
            elapsed_ms=1,
            generated_at="2026-05-03T00:00:00+00:00",
            error=None,
            data=ComprehensionData(
                semantic_summary=f"{self.summary_prefix} {len(self.calls)}",
                classification={"kind": asset.kind, "subkind": "test", "domain": "tests"},
                stated_facts=[f"Fact {len(self.calls)}"],
            ),
        )
        return AssetIndex(self.run_paths).write_comprehension(record)


def write_text_asset_source(tmp_path: Path, name: str = "asset.txt", text: str = "asset\n") -> Path:
    source = tmp_path / name
    source.write_text(text, encoding="utf-8")
    return source
