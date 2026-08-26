from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alysis_code.tools import fs as fs_mod
from alysis_code.tools.fs import FsError, fs_list, fs_read, fs_read_lines


def _init_git_repo(path: Path) -> None:
    subprocess.run(
        ["git", "init", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_fs_read_large_file_respects_max_bytes(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_bytes(b"a" * (512 * 1024))

    result = fs_read(root=tmp_path, path="big.txt", max_bytes=4096)

    assert result["truncated"] is True
    assert len(result["content"].encode("utf-8")) <= 4096


def test_fs_read_truncation_reports_exact_line_continuation(tmp_path: Path) -> None:
    path = tmp_path / "continued.txt"
    path.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    result = fs_read(root=tmp_path, path="continued.txt", max_bytes=8)

    assert result["total_lines"] == 5
    assert result["returned_range"] == {"start_line": 1, "end_line": 2}
    assert result["next_range"] == {"start_line": 3, "end_line": 5}


def test_fs_read_default_is_bounded_and_reports_limit_metadata(tmp_path: Path) -> None:
    path = tmp_path / "big-default.txt"
    path.write_bytes(b"a" * 20_000)

    result = fs_read(root=tmp_path, path="big-default.txt")

    assert result["truncated"] is True
    assert result["max_bytes"] == 12_000
    assert result["bytes_read"] == 12_000
    assert len(result["content"].encode("utf-8")) <= 12_000


def test_fs_read_lines_returns_requested_range_with_line_numbers(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8", newline="\n")

    result = fs_read_lines(root=tmp_path, path="demo.txt", start_line=2, end_line=3)

    assert result == {
        "path": "demo.txt",
        "start_line": 2,
        "end_line": 3,
        "total_lines": None,
        "content": "2: beta\n3: gamma\n",
        "truncated": False,
    }


def test_fs_read_lines_handles_end_line_past_eof_and_reports_total_lines(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8", newline="\n")

    result = fs_read_lines(
        root=tmp_path,
        path="demo.txt",
        start_line=2,
        end_line=10,
        include_line_numbers=False,
    )

    assert result == {
        "path": "demo.txt",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 3,
        "content": "beta\ngamma\n",
        "truncated": False,
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"start_line": 0}, "Invalid start_line"),
        ({"start_line": 3, "end_line": 2}, "Invalid line range"),
        ({"start_line": 1, "max_lines": 0}, "Invalid max_lines"),
    ],
)
def test_fs_read_lines_rejects_invalid_ranges(
    tmp_path: Path,
    kwargs: dict[str, int],
    message: str,
) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")

    with pytest.raises(FsError, match=message):
        fs_read_lines(root=tmp_path, path="demo.txt", **kwargs)


def test_fs_read_lines_rejects_start_line_beyond_eof(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("alpha\nbeta\n", encoding="utf-8")

    with pytest.raises(FsError, match="beyond end of file"):
        fs_read_lines(root=tmp_path, path="demo.txt", start_line=5)


def test_fs_read_lines_marks_truncated_when_max_lines_caps_output(tmp_path: Path) -> None:
    path = tmp_path / "demo.txt"
    path.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8", newline="\n")

    result = fs_read_lines(root=tmp_path, path="demo.txt", start_line=2, max_lines=2)

    assert result == {
        "path": "demo.txt",
        "start_line": 2,
        "end_line": 3,
        "total_lines": 5,
        "content": "2: two\n3: three\n",
        "truncated": True,
        "returned_range": {"start_line": 2, "end_line": 3},
        "next_range": {"start_line": 4, "end_line": 5},
    }


def test_fs_read_lines_truncation_reports_exact_line_continuation(tmp_path: Path) -> None:
    path = tmp_path / "continued-lines.txt"
    path.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")

    result = fs_read_lines(
        root=tmp_path,
        path="continued-lines.txt",
        start_line=2,
        max_lines=2,
    )

    assert result["total_lines"] == 5
    assert result["returned_range"] == {"start_line": 2, "end_line": 3}
    assert result["next_range"] == {"start_line": 4, "end_line": 5}


def test_fs_read_lines_reports_missing_file_and_directory_errors(tmp_path: Path) -> None:
    (tmp_path / "subdir").mkdir()

    with pytest.raises(FsError, match="Not found: missing.txt"):
        fs_read_lines(root=tmp_path, path="missing.txt", start_line=1)

    with pytest.raises(FsError, match="Is a directory: subdir"):
        fs_read_lines(root=tmp_path, path="subdir", start_line=1)


def test_fs_read_lines_rejects_root_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")

    with pytest.raises(FsError, match="Path escapes root"):
        fs_read_lines(root=tmp_path, path="../outside.txt", start_line=1)


def test_fs_list_ignored_candidates_do_not_consume_visible_result_budget(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored-*.txt\n", encoding="utf-8")
    for idx in range(1, 4):
        (tmp_path / f"ignored-{idx}.txt").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("visible\n", encoding="utf-8")

    result = fs_list(
        root=tmp_path,
        globs=["ignored-1.txt", "ignored-2.txt", "ignored-3.txt", "visible.txt"],
        max_results=1,
    )

    assert [entry["path"] for entry in result["entries"]] == ["visible.txt"]
    assert result["truncated"] is False


def test_fs_list_keeps_tracked_file_that_matches_gitignore(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    (tmp_path / "keep.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", ".gitignore", "-f", "keep.txt"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = fs_list(root=tmp_path, globs=["*.txt"], max_results=10)

    assert [entry["path"] for entry in result["entries"]] == ["keep.txt"]
    assert result["truncated"] is False


def test_fs_list_plain_dir_does_not_probe_git(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "visible.txt").write_text("visible\n", encoding="utf-8")

    def fail_git_probe(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        raise KeyboardInterrupt("plain directory should not probe git")

    monkeypatch.setattr(fs_mod.subprocess, "run", fail_git_probe)

    result = fs_list(root=tmp_path, globs=["*"], max_results=10)

    assert [entry["path"] for entry in result["entries"]] == ["visible.txt"]
    assert result["truncated"] is False


def test_fs_list_truncated_is_false_when_all_visible_results_fit(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "visible.txt").write_text("visible\n", encoding="utf-8")

    result = fs_list(
        root=tmp_path,
        globs=["ignored.txt", "visible.txt"],
        max_results=1,
    )

    assert [entry["path"] for entry in result["entries"]] == ["visible.txt"]
    assert result["truncated"] is False


def test_fs_list_truncated_is_true_only_when_more_visible_results_remain(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored-*.txt\n", encoding="utf-8")
    for idx in range(1, 4):
        (tmp_path / f"ignored-{idx}.txt").write_text("ignored\n", encoding="utf-8")
    (tmp_path / "visible-a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "visible-b.txt").write_text("b\n", encoding="utf-8")

    result = fs_list(
        root=tmp_path,
        globs=[
            "ignored-1.txt",
            "ignored-2.txt",
            "ignored-3.txt",
            "visible-a.txt",
            "visible-b.txt",
        ],
        max_results=1,
    )

    assert [entry["path"] for entry in result["entries"]] == ["visible-a.txt"]
    assert result["truncated"] is True


def test_fs_list_default_is_bounded_and_reports_counts(tmp_path: Path) -> None:
    for idx in range(170):
        (tmp_path / f"file-{idx:03d}.txt").write_text("x\n", encoding="utf-8")

    result = fs_list(root=tmp_path)

    assert len(result["entries"]) == 150
    assert result["returned_count"] == 150
    assert result["max_results"] == 150
    assert result["truncated"] is True


def test_fs_read_withholds_large_derived_artifact_content(tmp_path: Path) -> None:
    path = tmp_path / "package-lock.json"
    path.write_text('{"lockfileVersion": 3}\n' + "x" * 30_000, encoding="utf-8")

    result = fs_read(root=tmp_path, path="package-lock.json")

    assert result["derived_artifact"] is True
    assert result["derived_artifact_reason"] == "dependency lockfile"
    assert result["truncated"] is True
    assert result["size_bytes"] == path.stat().st_size
    assert len(result["content"].encode("utf-8")) <= 1_000
    assert "allow_derived=true" in result["note"]


def test_fs_read_allow_derived_returns_full_content(tmp_path: Path) -> None:
    body = '{"lockfileVersion": 3}\n' + "x" * 5_000
    (tmp_path / "package-lock.json").write_bytes(body.encode("utf-8"))

    result = fs_read(root=tmp_path, path="package-lock.json", allow_derived=True)

    assert "derived_artifact" not in result
    assert result["truncated"] is False
    assert result["content"] == body


def test_fs_read_small_derived_artifact_is_returned_whole(tmp_path: Path) -> None:
    body = "tiny lock contents\n"
    (tmp_path / "yarn.lock").write_bytes(body.encode("utf-8"))

    result = fs_read(root=tmp_path, path="yarn.lock")

    assert "derived_artifact" not in result
    assert result["content"] == body
    assert result["truncated"] is False


def test_fs_read_withholds_generated_dir_content(tmp_path: Path) -> None:
    generated = tmp_path / "dist"
    generated.mkdir()
    (generated / "bundle.js").write_text("x" * 30_000, encoding="utf-8")

    result = fs_read(root=tmp_path, path="dist/bundle.js")

    assert result["derived_artifact"] is True
    assert result["derived_artifact_reason"] == "generated or vendored path"


def test_fs_read_lines_byte_ceiling_bounds_enormous_single_line(tmp_path: Path) -> None:
    (tmp_path / "generated.js").write_text("y" * 200_000 + "\n", encoding="utf-8")

    result = fs_read_lines(root=tmp_path, path="generated.js", start_line=1)

    assert result["byte_truncated"] is True
    assert result["line_clipped"] is True
    assert result["truncated"] is True
    assert result["end_line"] == 1
    assert len(result["content"].encode("utf-8")) <= 48_000
    assert "max_bytes" in result

    small = fs_read_lines(root=tmp_path, path="generated.js", start_line=1, max_bytes=500)
    assert len(small["content"].encode("utf-8")) <= 500
    assert small["byte_truncated"] is True


def test_fs_read_derived_stub_honors_explicit_max_bytes(tmp_path: Path) -> None:
    """The head sample is bounded by the caller's ceiling, not only the fixed
    1000-byte stub size, and the reported bytes never exceed max_bytes."""
    (tmp_path / "package-lock.json").write_text("{}" + "x" * 60_000, encoding="utf-8")

    for limit in (1, 10, 100):
        stub = fs_read(root=tmp_path, path="package-lock.json", max_bytes=limit)
        assert stub["derived_artifact"] is True
        assert stub["bytes_read"] <= limit
        assert len(stub["content"].encode("utf-8")) <= limit
        assert stub["max_bytes"] == limit

    default_stub = fs_read(root=tmp_path, path="package-lock.json")
    assert default_stub["bytes_read"] <= 1000


def test_fs_read_lines_clip_never_exceeds_ceiling_on_multibyte_text(tmp_path: Path) -> None:
    """Clipping at a UTF-8 boundary drops the partial character instead of
    substituting U+FFFD, which would re-encode to three bytes and overshoot
    a one-byte ceiling."""
    (tmp_path / "emoji.txt").write_text("你好世界" * 5000 + "\n", encoding="utf-8")

    for limit in (1, 2, 3, 4, 5, 500):
        clipped = fs_read_lines(
            root=tmp_path,
            path="emoji.txt",
            start_line=1,
            max_bytes=limit,
            include_line_numbers=False,
        )
        payload = clipped["content"].encode("utf-8")
        assert len(payload) <= limit, f"max_bytes={limit} produced {len(payload)} bytes"
        assert "�" not in clipped["content"]
        assert clipped["line_clipped"] is True


def test_fs_read_lines_byte_ceiling_stops_between_lines(tmp_path: Path) -> None:
    lines = [f"line-{index} " + "z" * 30_000 for index in range(1, 11)]
    (tmp_path / "wide.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    result = fs_read_lines(
        root=tmp_path,
        path="wide.txt",
        start_line=1,
        max_bytes=65_000,
    )

    assert result["byte_truncated"] is True
    assert "line_clipped" not in result
    assert result["truncated"] is True
    assert result["end_line"] == 2
    assert result["total_lines"] == 10
    assert result["returned_range"] == {"start_line": 1, "end_line": 2}
    assert result["next_range"] == {"start_line": 3, "end_line": 10}
    assert len(result["content"].encode("utf-8")) <= 65_000


def test_fs_read_lines_normal_reads_have_no_byte_truncation_keys(tmp_path: Path) -> None:
    (tmp_path / "normal.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")

    result = fs_read_lines(root=tmp_path, path="normal.txt", start_line=1)

    assert "byte_truncated" not in result
    assert "line_clipped" not in result
    assert "note" not in result
    assert result["total_lines"] == 3


def test_fs_read_lines_rejects_invalid_max_bytes(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")

    with pytest.raises(FsError, match="max_bytes"):
        fs_read_lines(root=tmp_path, path="a.txt", start_line=1, max_bytes=0)
