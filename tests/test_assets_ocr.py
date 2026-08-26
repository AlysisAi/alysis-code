from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from alysis_code.assets import OcrError, ScriptToLanguagesMapper, TesseractOcrProvider

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "tesseract_osd"


def test_tesseract_is_unavailable_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("alysis_code.assets.ocr.shutil.which", lambda _binary: None)

    assert TesseractOcrProvider().is_available() is False


def test_script_detection_parses_osd_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("alysis_code.assets.ocr.shutil.which", lambda _binary: "/bin/tesseract")

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(FIXTURE_DIR / "greek.txt").read_text(encoding="utf-8"),
            stderr="",
        )

    monkeypatch.setattr("alysis_code.assets.ocr.subprocess.run", fake_run)

    detection = TesseractOcrProvider().detect_script(tmp_path / "image.png")
    assert detection is not None
    assert detection.script == "Greek"
    assert detection.confidence == 15.5


def test_script_to_languages_mapping() -> None:
    mapper = ScriptToLanguagesMapper()

    assert mapper.candidates("Latin", ["eng", "ell", "deu"]) == ["eng", "deu"]
    assert mapper.candidates("Greek", ["eng", "ell"]) == ["ell"]
    assert mapper.candidates("Cyrillic", ["rus", "eng"]) == ["rus"]
    assert mapper.candidates("Han", ["chi_sim", "jpn"]) == ["chi_sim"]
    assert mapper.candidates("Hiragana", ["jpn", "eng"]) == ["jpn"]


def test_extract_text_builds_multilanguage_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("alysis_code.assets.ocr.shutil.which", lambda _binary: "/bin/tesseract")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["tesseract", "--list-langs"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="List\neng\nell\n", stderr=""
            )
        if "--psm" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=(FIXTURE_DIR / "greek.txt").read_text(encoding="utf-8"),
                stderr="",
            )
        if args[-1] == "tsv":
            stdout = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            stdout += "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t80\tγειά\n"
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        if args == ["tesseract", "--version"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="tesseract 5.3.0\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="γειά\n", stderr="")

    monkeypatch.setattr("alysis_code.assets.ocr.subprocess.run", fake_run)

    result = TesseractOcrProvider().extract_text(tmp_path / "image.png")

    assert result.text == "γειά\n"
    assert result.languages_used == ["ell"]
    assert result.confidence == 0.8
    assert ["tesseract", str(tmp_path / "image.png"), "-", "-l", "ell"] in calls


def test_extract_text_falls_back_to_installed_languages_when_osd_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("alysis_code.assets.ocr.shutil.which", lambda _binary: "/bin/tesseract")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["tesseract", "--list-langs"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="List of available languages\neng\nell\n",
                stderr="",
            )
        if "--psm" in args:
            return subprocess.CompletedProcess(
                args=args,
                returncode=1,
                stdout="",
                stderr="Too few characters. Skipping this page",
            )
        if args[-1] == "tsv":
            stdout = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            stdout += "5\t1\t1\t1\t1\t1\t0\t0\t1\t1\t70\thello\n"
            return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr="")
        if args == ["tesseract", "--version"]:
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="tesseract 5.3.0\n", stderr=""
            )
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="hello\n", stderr="")

    monkeypatch.setattr("alysis_code.assets.ocr.subprocess.run", fake_run)

    result = TesseractOcrProvider().extract_text(tmp_path / "image.png")

    assert result.text == "hello\n"
    assert result.languages_used == ["eng", "ell"]
    assert result.confidence == 0.7
    assert ["tesseract", str(tmp_path / "image.png"), "-", "-l", "eng+ell"] in calls


def test_tesseract_timeout_and_nonzero_raise(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("alysis_code.assets.ocr.shutil.which", lambda _binary: "/bin/tesseract")

    def timeout_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(args, timeout=1)

    monkeypatch.setattr("alysis_code.assets.ocr.subprocess.run", timeout_run)
    provider = TesseractOcrProvider(timeout_seconds=1)
    with pytest.raises(OcrError, match="timed out"):
        provider.installed_languages()

    def nonzero_run(args: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=args, returncode=2, stdout="", stderr="bad image")

    monkeypatch.setattr("alysis_code.assets.ocr.subprocess.run", nonzero_run)
    with pytest.raises(OcrError, match="bad image"):
        provider.installed_languages()
