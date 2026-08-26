from __future__ import annotations

import json
from pathlib import Path

import pytest

import alysis_code.tools.search as search_mod


class _FakeStream:
    def __init__(self, lines: list[str], *, on_exhaust=None) -> None:  # type: ignore[no-untyped-def]
        self._lines = list(lines)
        self._on_exhaust = on_exhaust
        self._closed = False

    def readline(self) -> str:
        if self._closed:
            return ""
        if self._lines:
            return self._lines.pop(0)
        if self._on_exhaust is not None:
            self._on_exhaust()
            self._on_exhaust = None
        return ""

    def close(self) -> None:
        self._closed = True


class _FakePopen:
    def __init__(
        self,
        *,
        argv: list[str],
        stdout_lines: list[str] | None = None,
        stderr_lines: list[str] | None = None,
        returncode: int = 1,
    ) -> None:
        self.args = list(argv)
        self._returncode = returncode
        self._stdout_complete = False
        self.terminated = False
        self.killed = False
        self.stdout = _FakeStream(stdout_lines or [], on_exhaust=self._mark_stdout_complete)
        self.stderr = _FakeStream(stderr_lines or [])

    def _mark_stdout_complete(self) -> None:
        self._stdout_complete = True

    def poll(self) -> int | None:
        if self.terminated:
            return -15
        if self._stdout_complete:
            return self._returncode
        return None

    def wait(self, timeout=None) -> int:  # type: ignore[no-untyped-def]
        _ = timeout
        if self.terminated:
            return -15
        self._stdout_complete = True
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self._stdout_complete = True

    def kill(self) -> None:
        self.killed = True
        self.terminated = True
        self._stdout_complete = True


def test_search_rg_passes_dash_prefixed_pattern_via_e_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: list[_FakePopen] = []

    def fake_popen(argv, **_kwargs):  # type: ignore[no-untyped-def]
        proc = _FakePopen(argv=list(argv), returncode=1)
        captured.append(proc)
        return proc

    monkeypatch.setattr(search_mod, "_rg_available", lambda: True)
    monkeypatch.setattr(search_mod, "_rg_supports_no_config", lambda: False)
    monkeypatch.setattr(search_mod.subprocess, "Popen", fake_popen)

    out = search_mod.search_rg(
        root=tmp_path,
        pattern="--pre=cat",
        root_path=".",
    )
    assert out["backend"] == "rg"
    assert captured, "expected rg subprocess invocation"
    cmd = captured[0].args
    assert cmd[0] == "rg"
    assert "-e" in cmd
    idx = cmd.index("-e")
    assert cmd[idx + 1] == "--pre=cat"
    assert "--no-config" not in cmd
    assert cmd.count("--pre=cat") == 1
    assert "--max-count" not in cmd


def test_search_rg_includes_no_config_when_supported(tmp_path: Path, monkeypatch) -> None:
    captured: list[_FakePopen] = []

    def fake_popen(argv, **_kwargs):  # type: ignore[no-untyped-def]
        proc = _FakePopen(argv=list(argv), returncode=1)
        captured.append(proc)
        return proc

    monkeypatch.setattr(search_mod, "_rg_available", lambda: True)
    monkeypatch.setattr(search_mod, "_rg_supports_no_config", lambda: True)
    monkeypatch.setattr(search_mod.subprocess, "Popen", fake_popen)

    out = search_mod.search_rg(
        root=tmp_path,
        pattern="needle",
        root_path=".",
    )
    assert out["backend"] == "rg"
    assert captured, "expected rg subprocess invocation"
    cmd = captured[0].args
    assert cmd[0] == "rg"
    assert "--no-config" in cmd
    assert "-e" in cmd


def test_search_rg_rg_backend_passes_new_search_options(tmp_path: Path, monkeypatch) -> None:
    captured: list[_FakePopen] = []

    def fake_popen(argv, **_kwargs):  # type: ignore[no-untyped-def]
        proc = _FakePopen(argv=list(argv), returncode=1)
        captured.append(proc)
        return proc

    monkeypatch.setattr(search_mod, "_rg_available", lambda: True)
    monkeypatch.setattr(search_mod, "_rg_supports_no_config", lambda: False)
    monkeypatch.setattr(search_mod.subprocess, "Popen", fake_popen)

    out = search_mod.search_rg(
        root=tmp_path,
        pattern="Needle.*",
        literal=True,
        case_sensitive=False,
        include_hidden=True,
    )

    assert out["backend"] == "rg"
    assert out["literal"] is True
    assert out["case_sensitive"] is False
    assert out["include_hidden"] is True
    cmd = captured[0].args
    assert "--fixed-strings" in cmd
    assert "--ignore-case" in cmd
    assert "--hidden" in cmd
    assert "!.git/**" in cmd


def test_search_rg_timeout_returns_tool_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(search_mod, "_rg_available", lambda: True)
    monkeypatch.setattr(search_mod, "_rg_supports_no_config", lambda: False)
    monkeypatch.setattr(search_mod, "_SEARCH_RG_TIMEOUT_S", 0.0)
    monkeypatch.setattr(
        search_mod.subprocess,
        "Popen",
        lambda argv, **_kwargs: _FakePopen(argv=list(argv), returncode=0),  # type: ignore[no-untyped-def]
    )

    with pytest.raises(search_mod.SearchError, match="ripgrep search timed out after 0s"):
        search_mod.search_rg(root=tmp_path, pattern="TODO")


def test_search_rg_python_fallback_timeout_returns_tool_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "demo.txt").write_text("TODO\n", encoding="utf-8")
    monkeypatch.setattr(search_mod, "_rg_available", lambda: False)
    monkeypatch.setattr(search_mod, "_SEARCH_RG_TIMEOUT_S", 0.0)

    with pytest.raises(search_mod.SearchError, match="python fallback search timed out after"):
        search_mod.search_rg(root=tmp_path, pattern="TODO")


def test_search_rg_rg_backend_truncates_match_text_and_caps_matches_per_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lines: list[str] = []
    for idx in range(12):
        lines.append(
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": str(tmp_path / "src" / "a.py")},
                        "line_number": idx + 1,
                        "lines": {"text": ("TODO " + ("x" * 400) + "\n")},
                    },
                }
            )
        )
    for idx in range(2):
        lines.append(
            json.dumps(
                {
                    "type": "match",
                    "data": {
                        "path": {"text": str(tmp_path / "src" / "b.py")},
                        "line_number": idx + 1,
                        "lines": {"text": "TODO keep this useful\n"},
                    },
                }
            )
        )

    monkeypatch.setattr(search_mod, "_rg_available", lambda: True)
    monkeypatch.setattr(search_mod, "_rg_supports_no_config", lambda: False)
    monkeypatch.setattr(
        search_mod.subprocess,
        "Popen",
        lambda argv, **_kwargs: _FakePopen(  # type: ignore[no-untyped-def]
            argv=list(argv),
            stdout_lines=[line + "\n" for line in lines],
            returncode=0,
        ),
    )

    out = search_mod.search_rg(root=tmp_path, pattern="TODO")

    assert out["backend"] == "rg"
    assert out["truncated"] is True
    assert out["per_file_truncated"] is True
    assert out["match_text_truncated"] is True
    assert out["returned_matches"] == len(out["matches"])
    assert len([m for m in out["matches"] if m["path"].endswith("a.py")]) == 8
    assert len([m for m in out["matches"] if m["path"].endswith("b.py")]) == 2
    assert out["matches"][0]["text_truncated"] is True
    assert out["matches"][0]["text"].endswith("...(truncated)")
    assert len(out["matches"][0]["text"]) <= 240 + len("...(truncated)")


def test_search_rg_python_fallback_caps_total_output_but_stays_useful(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(search_mod, "_rg_available", lambda: False)
    root = tmp_path / "src"
    root.mkdir()
    for idx in range(80):
        (root / f"file_{idx:03d}.py").write_text(
            "prefix\n" + ("TODO " + ("x" * 220) + "\n") + "suffix\n",
            encoding="utf-8",
        )

    out = search_mod.search_rg(root=tmp_path, pattern="TODO", root_path="src")

    assert out["backend"] == "python"
    assert out["truncated"] is True
    assert out["returned_matches"] == len(out["matches"])
    assert len(out["matches"]) > 10
    payload = json.dumps(out, ensure_ascii=True, separators=(",", ":"))
    assert len(payload) < 14_000
    assert any(match["path"].startswith("src/") for match in out["matches"])
    assert all("TODO" in match["text"] for match in out["matches"])


def test_search_rg_rejects_empty_pattern(tmp_path: Path) -> None:
    with pytest.raises(search_mod.SearchError, match="pattern must be a non-empty string"):
        search_mod.search_rg(root=tmp_path, pattern="")


def test_search_rg_python_fallback_respects_globs(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(search_mod, "_rg_available", lambda: False)
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.py").write_text("TODO a\n", encoding="utf-8")
    (root / "b.txt").write_text("TODO b\n", encoding="utf-8")

    out = search_mod.search_rg(root=tmp_path, pattern="TODO", root_path="src", globs=["a.py"])

    assert out["backend"] == "python"
    assert out["matches"] == [{"path": "src/a.py", "line": 1, "text": "TODO a"}]


def test_search_rg_python_fallback_supports_literal_case_context_and_hidden(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(search_mod, "_rg_available", lambda: False)
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("before\nNeedle.*\nafter\n", encoding="utf-8")
    (root / ".hidden.txt").write_text("needle.* hidden\n", encoding="utf-8")

    out = search_mod.search_rg(
        root=tmp_path,
        pattern="needle.*",
        root_path="src",
        literal=True,
        case_sensitive=False,
        before_context=1,
        after_context=1,
    )

    assert out["backend"] == "python"
    assert out["matches"] == [
        {
            "path": "src/a.txt",
            "line": 2,
            "text": "Needle.*",
            "before_context": [{"line": 1, "text": "before"}],
            "after_context": [{"line": 3, "text": "after"}],
        }
    ]
    assert out["before_context"] == 1
    assert out["after_context"] == 1
    assert out["context_text_truncated"] is False

    hidden_out = search_mod.search_rg(
        root=tmp_path,
        pattern="needle.*",
        root_path="src",
        literal=True,
        case_sensitive=False,
        include_hidden=True,
    )

    assert any(match["path"] == "src/.hidden.txt" for match in hidden_out["matches"])


def test_search_rg_rg_backend_stops_early_after_reaching_max_results(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lines = [
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(tmp_path / "src" / "a.py")},
                    "line_number": 1,
                    "lines": {"text": "TODO one\n"},
                },
            }
        ),
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(tmp_path / "src" / "b.py")},
                    "line_number": 2,
                    "lines": {"text": "TODO two\n"},
                },
            }
        ),
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": str(tmp_path / "src" / "c.py")},
                    "line_number": 3,
                    "lines": {"text": "TODO three\n"},
                },
            }
        ),
    ]
    captured: list[_FakePopen] = []

    def fake_popen(argv, **_kwargs):  # type: ignore[no-untyped-def]
        proc = _FakePopen(
            argv=list(argv), stdout_lines=[line + "\n" for line in lines], returncode=0
        )
        captured.append(proc)
        return proc

    monkeypatch.setattr(search_mod, "_rg_available", lambda: True)
    monkeypatch.setattr(search_mod, "_rg_supports_no_config", lambda: False)
    monkeypatch.setattr(search_mod.subprocess, "Popen", fake_popen)

    out = search_mod.search_rg(root=tmp_path, pattern="TODO", max_results=1)

    assert out["backend"] == "rg"
    assert out["matches"] == [{"path": "src/a.py", "line": 1, "text": "TODO one"}]
    assert out["truncated"] is True
    assert captured[0].terminated is True
