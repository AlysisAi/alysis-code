from __future__ import annotations

import base64
import subprocess
from pathlib import Path

import pytest

from alysis_code.clipboard import (
    ClipboardError,
    copy_text_to_clipboard,
    paste_clipboard_image,
)


@pytest.mark.parametrize(
    ("available", "expected_command"),
    [
        ("pbcopy", ["pbcopy"]),
        ("clip.exe", ["clip.exe"]),
        ("wl-copy", ["wl-copy", "--type", "text/plain;charset=utf-8"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-in"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ],
)
def test_copy_text_to_clipboard_uses_available_platform_writer(
    available: str,
    expected_command: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], bytes]] = []

    monkeypatch.setattr(
        "alysis_code.clipboard.shutil.which",
        lambda command: command if command == available else None,
    )

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    copy_text_to_clipboard("hello κόσμε")

    assert calls == [(expected_command, "hello κόσμε".encode())]


def test_copy_text_to_clipboard_uses_unicode_safe_powershell(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "alysis_code.clipboard.shutil.which",
        lambda command: command if command == "powershell.exe" else None,
    )

    def fake_run(command: list[str], **kwargs) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured["input"] = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    copy_text_to_clipboard("κόσμε")

    assert captured["command"][0] == "powershell.exe"  # type: ignore[index]
    assert base64.b64decode(captured["input"]) == "κόσμε".encode()  # type: ignore[arg-type]


def test_copy_text_to_clipboard_errors_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.shutil.which", lambda _command: None)

    with pytest.raises(ClipboardError, match="no supported clipboard command found"):
        copy_text_to_clipboard("hello")


def test_paste_clipboard_image_via_powershell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = b"\x89PNG\r\n\x1a\nfake"

    def fake_which(cmd: str) -> str | None:
        if cmd == "powershell.exe":
            return "powershell.exe"
        return None

    def fake_run(args: list[str], **_kwargs) -> subprocess.CompletedProcess[bytes]:
        assert args[0] == "powershell.exe"
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=base64.b64encode(png),
            stderr=b"",
        )

    monkeypatch.setattr("alysis_code.clipboard.shutil.which", fake_which)
    monkeypatch.setattr("alysis_code.clipboard.subprocess.run", fake_run)

    path = paste_clipboard_image(root=tmp_path)
    assert path.exists()
    assert path.read_bytes() == png


def test_paste_clipboard_image_errors_when_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("alysis_code.clipboard.shutil.which", lambda _cmd: None)

    with pytest.raises(ClipboardError, match="Could not read an image from clipboard"):
        paste_clipboard_image(root=tmp_path)
