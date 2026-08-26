from __future__ import annotations

import re
from pathlib import Path

import pytest

from alysis_code.forge import (
    RUN_NAME_WORDS,
    create_plan_run,
    format_run_id,
    make_run_id,
)
from alysis_code.swarm_orchestrator import _candidate_branch_name

_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_LEGACY_RUN_ID_RE = re.compile(r"^\d{8}T\d{6}Z_[0-9a-f]{8}$")


def _runs_dir(root: Path) -> Path:
    return root / ".alysis" / "runs"


def test_run_name_words_are_clean() -> None:
    assert len(RUN_NAME_WORDS) == len(set(RUN_NAME_WORDS))
    for word in RUN_NAME_WORDS:
        assert re.fullmatch(r"[a-z]{3,9}", word), word
        assert word not in _WINDOWS_RESERVED


def test_format_run_id_pads_and_cycles_words() -> None:
    assert format_run_id(1) == f"001-{RUN_NAME_WORDS[0]}"
    assert format_run_id(17) == f"017-{RUN_NAME_WORDS[16]}"
    wrapped = len(RUN_NAME_WORDS) + 1
    assert format_run_id(wrapped).endswith(f"-{RUN_NAME_WORDS[0]}")
    assert format_run_id(1000).startswith("1000-")


def test_make_run_id_first_run_claims_directory(tmp_path: Path) -> None:
    run_id = make_run_id(tmp_path)
    assert run_id == format_run_id(1)
    assert (_runs_dir(tmp_path) / run_id).is_dir()


def test_make_run_id_increments_and_sorts_chronologically(tmp_path: Path) -> None:
    ids = [make_run_id(tmp_path) for _ in range(3)]
    assert [i.split("-", 1)[0] for i in ids] == ["001", "002", "003"]
    assert sorted(ids) == ids


def test_make_run_id_retries_on_claim_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = format_run_id(1)
    (_runs_dir(tmp_path) / first).mkdir(parents=True)
    monkeypatch.setattr("alysis_code.forge._next_run_counter", lambda runs_dir: 1)
    run_id = make_run_id(tmp_path)
    assert run_id == format_run_id(2)
    assert (_runs_dir(tmp_path) / run_id).is_dir()


def test_make_run_id_ignores_legacy_run_directories(tmp_path: Path) -> None:
    legacy = _runs_dir(tmp_path) / "20260716T142530Z_a1b2c3d4"
    legacy.mkdir(parents=True)
    assert make_run_id(tmp_path) == format_run_id(1)


def test_make_run_id_without_root_falls_back_to_legacy_format() -> None:
    assert _LEGACY_RUN_ID_RE.fullmatch(make_run_id())


def test_make_run_id_missing_root_does_not_create_directories(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    run_id = make_run_id(missing)
    assert run_id == format_run_id(1)
    assert not missing.exists()


def test_create_plan_run_uses_counter_run_id(tmp_path: Path) -> None:
    paths = create_plan_run(tmp_path, create_if_missing=True)
    assert paths.run_id == format_run_id(1)
    second = create_plan_run(tmp_path, create_if_missing=True)
    assert second.run_id == format_run_id(2)


def test_candidate_branch_name_uses_full_counter_run_id() -> None:
    branch = _candidate_branch_name(run_id="017-wayland", batch_label="batch_1")
    assert branch == "syc-017-wayland-b1"


def test_candidate_branch_name_keeps_legacy_tail_rule() -> None:
    branch = _candidate_branch_name(run_id="20260716T142530Z_a1b2c3d4", batch_label="batch_2")
    assert branch == "syc-a1b2c3d4-b2"


def test_make_run_id_stray_runs_file_falls_back_to_legacy_id(tmp_path: Path) -> None:
    (tmp_path / ".alysis").mkdir()
    (tmp_path / ".alysis" / "runs").write_text("stray", encoding="utf-8")
    run_id = make_run_id(tmp_path)
    assert _LEGACY_RUN_ID_RE.fullmatch(run_id)


def test_make_run_id_does_not_recycle_deleted_run_ids(tmp_path: Path) -> None:
    first = make_run_id(tmp_path)
    (_runs_dir(tmp_path) / first).rmdir()
    second = make_run_id(tmp_path)
    assert second == format_run_id(2)
