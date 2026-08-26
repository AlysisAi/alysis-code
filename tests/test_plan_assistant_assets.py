from __future__ import annotations

import json
from pathlib import Path

import httpx
from _assets_test_helpers import FakeAssetComprehender, write_text_asset_source

from alysis_code.assets import AssetSurface
from alysis_code.config import AppConfig
from alysis_code.forge import create_plan_run, render_plan_markdown
from alysis_code.plan_assistant import apply_plan_update, run_planner_turn


def _transport_for_payloads(*payloads: dict[str, object]) -> tuple[httpx.MockTransport, list[dict]]:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        requests.append(body)
        payload = payloads[len(requests) - 1]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(payload)}}]},
        )

    return httpx.MockTransport(handler), requests


def _plan() -> dict:
    return {
        "schema_version": 1,
        "run_id": "run_1",
        "created_at": "2026-05-03T00:00:00+00:00",
        "updated_at": "2026-05-03T00:00:00+00:00",
        "project_goal": "Initial",
        "summary": "Initial",
        "requirements": ["Keep login working"],
        "tasks": [],
        "assets": [],
    }


def _surface(tmp_path: Path) -> AssetSurface:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    return AssetSurface(
        cfg=AppConfig(model="planner-model"),
        run_paths=paths,
        comprehender=FakeAssetComprehender(paths),  # type: ignore[arg-type]
    )


def test_planner_receives_assets_and_preserves_asset_briefing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    surface = _surface(tmp_path)
    asset = surface.add_asset(
        write_text_asset_source(tmp_path, "spec.txt", "Auth API spec\n"),
        title="Auth API spec",
        comprehend="sync",
    ).record
    planner_payload = {
        "assistant_message": "Plan updated.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Fix login",
                    "description": f"Implement endpoint behavior per {asset.id}.",
                    "acceptance_criteria": ["Login test passes."],
                    "estimated_files": ["src/auth.py"],
                    "write_scope": ["src/auth.py"],
                    "asset_briefing": {
                        "primary": [
                            {
                                "asset_id": asset.id,
                                "rationale": "Spec defines endpoint behavior.",
                                "expected_use": "Reference endpoint contract.",
                            }
                        ],
                        "may_need": [],
                    },
                }
            ]
        },
    }
    transport, requests = _transport_for_payloads(planner_payload)
    plan = _plan()

    result = run_planner_turn(
        cfg=surface.cfg,
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Fix the login endpoint using the attached auth spec and add a test.",
        transport=transport,
        run_paths=surface.run_paths,
        asset_surface=surface,
    )
    apply_plan_update(plan, result.plan_update or {})
    markdown = render_plan_markdown(plan, asset_index=surface.index)

    assert result.error is None
    assert "## Available Assets" in requests[0]["messages"][1]["content"]
    assert asset.id in requests[0]["messages"][1]["content"]
    assert plan["tasks"][0]["asset_briefing"]["primary"][0]["asset_id"] == asset.id
    assert '"Auth API spec" (' + asset.id + ") - primary" in markdown


def test_planner_rejects_unknown_asset_reference(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    surface = _surface(tmp_path)
    surface.add_asset(write_text_asset_source(tmp_path), title="Known", comprehend="sync")
    planner_payload = {
        "assistant_message": "Plan updated.",
        "questions": [],
        "plan_update": {
            "tasks_add": [
                {
                    "title": "Fix login",
                    "description": "Use unknown asset.",
                    "acceptance_criteria": ["Done."],
                    "estimated_files": ["src/auth.py"],
                    "write_scope": ["src/auth.py"],
                    "asset_briefing": {
                        "primary": [
                            {
                                "asset_id": "ast_unknown",
                                "rationale": "Unknown",
                                "expected_use": "Use it.",
                            }
                        ],
                        "may_need": [],
                    },
                }
            ]
        },
    }
    transport, _requests = _transport_for_payloads(planner_payload)

    result = run_planner_turn(
        cfg=surface.cfg,
        api_key_override=None,
        plan=_plan(),
        transcript_tail=[],
        user_text="Fix the login endpoint using the attached auth spec and add a test.",
        transport=transport,
        run_paths=surface.run_paths,
        asset_surface=surface,
    )

    assert result.plan_update is None
    assert "unknown asset ids: ast_unknown" in (result.error or "")


def test_deleted_asset_references_surface_as_drift(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    surface = _surface(tmp_path)
    asset = surface.add_asset(
        write_text_asset_source(tmp_path), title="Known", comprehend="sync"
    ).record
    surface.delete_asset(asset.id)
    plan = _plan()
    plan["tasks"] = [
        {
            "id": "T01",
            "title": "Existing",
            "description": "Existing",
            "acceptance_criteria": ["Done."],
            "dependencies": [],
            "estimated_files": [],
            "write_scope": [],
            "asset_briefing": {
                "primary": [
                    {
                        "asset_id": asset.id,
                        "rationale": "Old",
                        "expected_use": "Use old asset.",
                    }
                ],
                "may_need": [],
            },
        }
    ]
    transport, requests = _transport_for_payloads(
        {"assistant_message": "No change.", "questions": [], "plan_update": None}
    )

    result = run_planner_turn(
        cfg=surface.cfg,
        api_key_override=None,
        plan=plan,
        transcript_tail=[],
        user_text="Review the plan and fix broken asset references if needed.",
        transport=transport,
        run_paths=surface.run_paths,
        asset_surface=surface,
    )

    assert result.error is None
    assert "## Plan-Asset Drift" in requests[0]["messages"][1]["content"]
    assert asset.id in requests[0]["messages"][1]["content"]


def test_vision_capable_planner_receives_inline_image(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_API_KEY", "k")
    monkeypatch.setenv("ALYSIS_SUPPORTS_VISION", "true")
    from PIL import Image

    image_path = tmp_path / "shot.png"
    Image.new("RGB", (4, 4), color=(255, 0, 0)).save(image_path)
    surface = _surface(tmp_path)
    surface.add_asset(image_path, title="Screenshot", comprehend="sync")
    transport, requests = _transport_for_payloads(
        {"assistant_message": "No change.", "questions": [], "plan_update": None}
    )

    result = run_planner_turn(
        cfg=surface.cfg,
        api_key_override=None,
        plan=_plan(),
        transcript_tail=[],
        user_text="Use the screenshot if relevant while planning login UI tests.",
        transport=transport,
        run_paths=surface.run_paths,
        asset_surface=surface,
    )

    assert result.error is None
    content = requests[0]["messages"][1]["content"]
    assert isinstance(content, list)
    assert any(part.get("type") == "image_url" for part in content)
