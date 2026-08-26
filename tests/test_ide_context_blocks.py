from __future__ import annotations

import json
from pathlib import Path

import pytest

from alysis_code.ide.context_blocks import (
    ContextLimits,
    ContextValidationError,
    context_capabilities,
    sanitize_context_blocks,
)


def _range() -> dict[str, object]:
    return {
        "start": {"line": 2, "character": 3},
        "end": {"line": 4, "character": 5},
    }


def test_context_capabilities_are_machine_readable_and_match_defaults() -> None:
    capabilities = context_capabilities()

    assert capabilities["schema_version"] == 1
    assert capabilities["limits"]["max_blocks"] == ContextLimits().max_blocks
    assert capabilities["types"] == sorted(capabilities["fields_by_type"])
    assert "range" in capabilities["fields_by_type"]["selection"]
    assert capabilities["deterministic_truncation"] is True


def test_selection_context_is_canonical_bounded_and_prompt_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "src" / "main.py"
    source.parent.mkdir()
    source.write_text("print('hello')\n", encoding="utf-8")
    monkeypatch.setenv("ALYSIS_API_KEY", "context-secret-value")

    bundle = sanitize_context_blocks(
        [
            {
                "type": "selection",
                "path": str(source),
                "uri": source.as_uri(),
                "document_version": 7,
                "content_hash": "a" * 64,
                "language": "python",
                "range": _range(),
                "content": "print('context-secret-value')\n",
                "provenance": {
                    "source": "vscode",
                    "captured_at": "2026-07-29T10:00:00Z",
                    "trust": True,
                    "version": 1,
                },
            }
        ],
        workspace_roots=[tmp_path],
    )

    assert bundle.total_bytes <= ContextLimits().max_total_bytes
    assert bundle.truncated is False
    block = bundle.blocks[0]
    assert block.kind == "selection"
    assert block.data["path"] == "src/main.py"
    assert block.data["uri"] == source.resolve().as_uri()
    assert block.data["workspace_root"] == 0
    assert block.data["content"] == "print('<redacted>')\n"
    assert block.data["range"] == _range()
    rendered = json.dumps(bundle.to_dict()) + bundle.to_prompt()
    assert "context-secret-value" not in rendered
    assert "untrusted data" in bundle.to_prompt()


@pytest.mark.parametrize(
    "block",
    [
        {"type": "file", "path": "a.py", "content": "x"},
        {"type": "file_range", "path": "a.py", "range": _range(), "content": "x"},
        {"type": "selection", "path": "a.py", "range": _range(), "content": "x"},
        {
            "type": "open_editors",
            "items": [{"path": "a.py", "active": True, "dirty": False}],
        },
        {
            "type": "diagnostics",
            "items": [
                {
                    "path": "a.py",
                    "severity": "warning",
                    "message": "unused name",
                    "range": _range(),
                }
            ],
        },
        {"type": "terminal", "content": "pytest passed", "cwd": ".", "exit_code": 0},
        {"type": "git_diff", "content": "+new", "repository": ".", "staged": False},
        {"type": "past_session", "session_id": "session-1", "content": "Earlier result"},
        {"type": "image", "path": "screen.png", "media_type": "image/png"},
    ],
)
def test_all_context_kinds_have_machine_readable_forms(tmp_path: Path, block: dict) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "screen.png").write_bytes(b"png")

    bundle = sanitize_context_blocks([block], workspace_roots=[tmp_path])

    assert bundle.blocks[0].data["type"] == block["type"]
    assert json.loads(json.dumps(bundle.to_dict()))["schema_version"] == 1


def test_context_rejects_unknown_fields_and_invalid_ranges(tmp_path: Path) -> None:
    with pytest.raises(ContextValidationError) as unknown:
        sanitize_context_blocks(
            [{"type": "file", "path": "a.py", "content": "x", "instructions": "do it"}],
            workspace_roots=[tmp_path],
        )
    assert unknown.value.code == "unexpected_field"
    assert unknown.value.field == "instructions"

    with pytest.raises(ContextValidationError) as invalid_range:
        sanitize_context_blocks(
            [
                {
                    "type": "selection",
                    "path": "a.py",
                    "content": "x",
                    "range": {
                        "start": {"line": 3, "character": 0},
                        "end": {"line": 2, "character": 0},
                    },
                }
            ],
            workspace_roots=[tmp_path],
        )
    assert invalid_range.value.code == "invalid_range"


def test_context_rejects_outside_ignored_and_sensitive_paths(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ContextValidationError) as outside_error:
        sanitize_context_blocks(
            [{"type": "file", "path": str(outside), "content": "x"}],
            workspace_roots=[tmp_path],
        )
    assert outside_error.value.code == "path_outside_workspace"

    with pytest.raises(ContextValidationError) as ignored_error:
        sanitize_context_blocks(
            [{"type": "file", "path": "ignored.txt", "content": "x"}],
            workspace_roots=[tmp_path],
            is_ignored=lambda path: path.name == "ignored.txt",
        )
    assert ignored_error.value.code == "ignored_path"

    for sensitive_path in (".env", ".kube/config", "keys/deploy.ppk"):
        with pytest.raises(ContextValidationError) as sensitive_error:
            sanitize_context_blocks(
                [{"type": "file", "path": sensitive_path, "content": "PASSWORD=hidden"}],
                workspace_roots=[tmp_path],
            )
        assert sensitive_error.value.code == "sensitive_path"

    allowed = sanitize_context_blocks(
        [{"type": "file", "path": ".env", "content": "PASSWORD=hidden"}],
        workspace_roots=[tmp_path],
        path_policy=lambda _path, _kind: "allow",
    )
    assert "hidden" not in allowed.to_prompt()
    assert allowed.blocks[0].data["path"] == ".env"

    redacted = sanitize_context_blocks(
        [{"type": "image", "path": "private.pem", "alt_text": "key screenshot"}],
        workspace_roots=[tmp_path],
        path_policy=lambda _path, _kind: "redact",
    )
    assert redacted.blocks[0].data["uri"] == "<redacted>"
    assert redacted.blocks[0].data["path"].startswith("<redacted-path:")


def test_context_caps_are_deterministic_for_content_items_count_and_total(tmp_path: Path) -> None:
    limits = ContextLimits(
        max_blocks=2,
        max_block_bytes=300,
        max_total_bytes=480,
        max_items_per_block=2,
    )
    raw = [
        {"type": "terminal", "content": "🙂" * 500, "cwd": "."},
        {
            "type": "diagnostics",
            "items": [
                {"path": f"{index}.py", "severity": "error", "message": "m" * 200}
                for index in range(3)
            ],
        },
        {"type": "past_session", "session_id": "later", "content": "omitted"},
    ]

    first = sanitize_context_blocks(raw, workspace_roots=[tmp_path], limits=limits)
    second = sanitize_context_blocks(raw, workspace_roots=[tmp_path], limits=limits)

    assert first.to_dict() == second.to_dict()
    assert first.total_bytes <= limits.max_total_bytes
    assert all(block.size_bytes <= limits.max_block_bytes for block in first.blocks)
    assert first.truncated is True
    assert first.dropped_block_count >= 1
    assert "\ufffd" not in first.to_prompt()


def test_diagnostics_are_strict_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DIAGNOSTIC_TOKEN", "diagnostic-secret-value")
    bundle = sanitize_context_blocks(
        [
            {
                "type": "diagnostics",
                "items": [
                    {
                        "path": "a.py",
                        "severity": "error",
                        "message": "token=diagnostic-secret-value",
                        "source": "pyright",
                        "code": 123,
                    }
                ],
            }
        ],
        workspace_roots=[tmp_path],
    )
    assert "diagnostic-secret-value" not in bundle.to_prompt()
    assert bundle.blocks[0].data["items"][0]["code"] == 123

    with pytest.raises(ContextValidationError) as severity_error:
        sanitize_context_blocks(
            [
                {
                    "type": "diagnostics",
                    "items": [{"path": "a.py", "severity": "critical", "message": "bad"}],
                }
            ],
            workspace_roots=[tmp_path],
        )
    assert severity_error.value.code == "invalid_severity"


def test_path_policy_failures_are_fail_closed(tmp_path: Path) -> None:
    def broken_policy(_path: Path, _kind: str) -> str:
        raise RuntimeError("policy unavailable")

    with pytest.raises(ContextValidationError) as excinfo:
        sanitize_context_blocks(
            [{"type": "file", "path": "a.py", "content": "x"}],
            workspace_roots=[tmp_path],
            path_policy=broken_policy,  # type: ignore[arg-type]
        )
    assert excinfo.value.code == "path_policy_failed"
