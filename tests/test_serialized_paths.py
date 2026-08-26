from __future__ import annotations

from pathlib import Path

from alysis_code.serialized_paths import (
    redacted_host_path_label,
    safe_serialized_path,
    safe_serialized_path_field,
    sanitize_paths_in_text,
)


def test_safe_serialized_path_preserves_workspace_relative_posix_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "src" / "app.py"
    nested.parent.mkdir(parents=True)
    nested.write_text("print('ok')\n", encoding="utf-8")

    assert safe_serialized_path(nested, workspace_root=workspace) == "src/app.py"


def test_safe_serialized_path_sanitizes_windows_drive_letter_paths() -> None:
    assert (
        safe_serialized_path(r"C:\Users\alice\secret\file.txt") == "[redacted host path: file.txt]"
    )


def test_safe_serialized_path_sanitizes_windows_unc_paths() -> None:
    assert (
        safe_serialized_path(r"\\server\share\folder\artifact.log")
        == "[redacted host path: artifact.log]"
    )


def test_safe_serialized_path_field_sanitizes_windows_absolute_original_path() -> None:
    assert (
        safe_serialized_path_field(
            "original_path",
            r"C:\Users\alice\secret\report.txt",
        )
        == "[redacted host path: report.txt]"
    )


def test_redacted_host_path_label_uses_cross_platform_windows_basename() -> None:
    assert (
        redacted_host_path_label(r"C:\Users\alice\secret\file.txt")
        == "[redacted host path: file.txt]"
    )
    assert (
        redacted_host_path_label(r"\\server\share\folder\artifact.log")
        == "[redacted host path: artifact.log]"
    )


def test_sanitize_paths_in_text_sanitizes_windows_absolute_paths_and_preserves_api_routes() -> None:
    text = (
        r"check C:\Users\alice\secret\file.txt, "
        r"C:\tmp, "
        r"C:/foo.txt, "
        r"\\server\share\folder\artifact.log, "
        r"\\server/share/mixed-unc.log, "
        r"\\server\share/mixed-separators.log, "
        "keep /v1/chat/completions, "
        "and leave not-a-path alone"
    )

    sanitized = sanitize_paths_in_text(text)

    assert "[redacted host path: file.txt]" in sanitized
    assert "[redacted host path: tmp]" in sanitized
    assert "[redacted host path: foo.txt]" in sanitized
    assert "[redacted host path: artifact.log]" in sanitized
    assert "[redacted host path: mixed-unc.log]" in sanitized
    assert "[redacted host path: mixed-separators.log]" in sanitized
    assert r"C:\Users\alice\secret\file.txt" not in sanitized
    assert r"C:\tmp" not in sanitized
    assert r"C:/foo.txt" not in sanitized
    assert r"\\server\share\folder\artifact.log" not in sanitized
    assert r"\\server/share/mixed-unc.log" not in sanitized
    assert r"\\server\share/mixed-separators.log" not in sanitized
    assert "/v1/chat/completions" in sanitized
    assert "[redacted host path: completions]" not in sanitized
    assert "not-a-path" in sanitized


def test_sanitize_paths_in_text_ignores_generic_non_path_strings() -> None:
    text = "message C:Users alice and endpoint /v1/chat/completions"
    assert sanitize_paths_in_text(text) == text
