from __future__ import annotations

import os
from pathlib import Path

from alysis_code.execution_shared import (
    copy_workspace_snapshot,
    snapshot_runtime_tree,
    sync_snapshot_changed_files,
)


def test_snapshot_large_file_uses_metadata_fingerprint_without_reading_bytes(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    big = root / ".alysis" / "large.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    target = big.resolve()

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path) -> bytes:
        if self.resolve() == target:
            raise AssertionError("large file should not be read fully during snapshot")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    snapshot = snapshot_runtime_tree(root)
    rel = os.fspath(target.relative_to(root.resolve()))
    assert snapshot[rel].startswith("meta:")


def test_snapshot_large_file_fingerprint_changes_when_file_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    big = root / ".alysis" / "large.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"a" * (2 * 1024 * 1024))

    first = snapshot_runtime_tree(root)
    with big.open("ab") as fh:
        fh.write(b"b")
    second = snapshot_runtime_tree(root)

    rel = os.fspath(big.resolve().relative_to(root.resolve()))
    assert first[rel] != second[rel]


def test_snapshot_twelve_megabyte_binary_uses_bounded_metadata_fingerprint(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    big = root / ".alysis" / "twelve-megabyte.bin"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"\x00\xff" * (6 * 1024 * 1024))
    target = big.resolve()
    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path) -> bytes:
        if self.resolve() == target:
            raise AssertionError(
                "12 MiB binary must not be loaded into memory for snapshot hashing"
            )
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    snapshot = snapshot_runtime_tree(root)
    rel = os.fspath(target.relative_to(root.resolve()))
    assert big.stat().st_size == 12 * 1024 * 1024
    assert snapshot[rel].startswith("meta:")


def test_snapshot_preserves_unicode_emoji_and_rtl_filenames(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    runtime = root / ".alysis" / "unicode"
    runtime.mkdir(parents=True)
    names = ["café-🚀.txt", "مرحبا-שלום.txt", "資料-δοκιμή.txt"]
    for index, name in enumerate(names):
        (runtime / name).write_text(f"payload-{index}\n", encoding="utf-8")

    snapshot = snapshot_runtime_tree(root)

    for name in names:
        rel = os.fspath((runtime / name).resolve().relative_to(root.resolve()))
        assert rel in snapshot
        assert snapshot[rel].startswith("sha256:")


def test_snapshot_plan_assets_use_metadata_even_when_small(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    asset = root / ".alysis" / "runs" / "r1" / "plan" / "assets" / "small.bin"
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_bytes(b"tiny")

    plan_json = root / ".alysis" / "runs" / "r1" / "plan" / "plan.json"
    plan_json.write_text('{"ok": true}\n', encoding="utf-8")
    target = asset.resolve()

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(self: Path) -> bytes:
        if self.resolve() == target:
            raise AssertionError("plan asset should not be content-hashed")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)

    snapshot = snapshot_runtime_tree(root)
    asset_rel = os.fspath(asset.resolve().relative_to(root.resolve()))
    plan_rel = os.fspath(plan_json.resolve().relative_to(root.resolve()))

    assert snapshot[asset_rel].startswith("meta:")
    assert snapshot[plan_rel].startswith("sha256:")


def test_copy_workspace_snapshot_excludes_internal_dirs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "target" / "debug").mkdir(parents=True)
    (root / "target" / "debug" / "demo").write_bytes(b"bin")
    (root / "cli.pyc").write_bytes(b"pyc")
    (root / "legacy.pyo").write_bytes(b"pyo")
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "mod.cpython-310.pyc").write_bytes(b"cache")
    (root / ".pytest_cache").mkdir()
    (root / ".pytest_cache" / "state").write_text("x\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".alysis").mkdir()
    (root / ".alysis" / "tmp.txt").write_text("x\n", encoding="utf-8")
    (root / ".alysis_images").mkdir()
    (root / ".alysis_images" / "img.png").write_bytes(b"png")
    (root / "alysis-feedback").mkdir()
    (root / "alysis-feedback" / "bundle.zip").write_bytes(b"zip")

    snapshot_root = tmp_path / "snapshot"
    copy_workspace_snapshot(src_root=root, dest_root=snapshot_root)

    assert (snapshot_root / "src" / "app.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not (snapshot_root / ".git").exists()
    assert not (snapshot_root / ".alysis").exists()
    assert not (snapshot_root / ".alysis_images").exists()
    assert not (snapshot_root / "alysis-feedback").exists()
    assert not (snapshot_root / "target").exists()
    assert not (snapshot_root / "cli.pyc").exists()
    assert not (snapshot_root / "legacy.pyo").exists()
    assert not (snapshot_root / "pkg" / "__pycache__").exists()
    assert not (snapshot_root / ".pytest_cache").exists()


def test_copy_workspace_snapshot_keeps_non_rust_target_dirs(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "target").mkdir()
    (root / "target" / "generated.txt").write_text("keep me\n", encoding="utf-8")

    snapshot_root = tmp_path / "snapshot"
    copy_workspace_snapshot(src_root=root, dest_root=snapshot_root)

    assert (snapshot_root / "target" / "generated.txt").read_text(encoding="utf-8") == "keep me\n"


def test_sync_snapshot_changed_files_copies_and_deletes_back(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    snapshot = tmp_path / "snapshot"
    workspace.mkdir()
    snapshot.mkdir()

    (workspace / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (workspace / "src").mkdir()
    (workspace / "src" / "keep.py").write_text("old\n", encoding="utf-8")
    (workspace / "src" / "delete.py").write_text("gone soon\n", encoding="utf-8")
    (workspace / "pkg" / "__pycache__").mkdir(parents=True)
    (workspace / "pkg" / "__pycache__" / "mod.cpython-310.pyc").write_bytes(b"old-cache")
    (workspace / "cli.pyc").write_bytes(b"old-pyc")
    (workspace / "target" / "debug").mkdir(parents=True)
    (workspace / "target" / "debug" / "demo").write_bytes(b"old-bin")

    (snapshot / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (snapshot / "src").mkdir()
    (snapshot / "src" / "keep.py").write_text("new\n", encoding="utf-8")
    (snapshot / "pkg" / "__pycache__").mkdir(parents=True)
    (snapshot / "pkg" / "__pycache__" / "mod.cpython-310.pyc").write_bytes(b"new-cache")
    (snapshot / "cli.pyc").write_bytes(b"new-pyc")
    (snapshot / "target" / "debug").mkdir(parents=True)
    (snapshot / "target" / "debug" / "demo").write_bytes(b"new-bin")

    applied = sync_snapshot_changed_files(
        snapshot_root=snapshot,
        workspace_root=workspace,
        changed_files=[
            "src/keep.py",
            "src/delete.py",
            ".alysis/ignored.txt",
            "cli.pyc",
            "pkg/__pycache__/mod.cpython-310.pyc",
            "target/debug/demo",
        ],
    )

    assert applied == ["src/keep.py", "src/delete.py"]
    assert (workspace / "src" / "keep.py").read_text(encoding="utf-8") == "new\n"
    assert not (workspace / "src" / "delete.py").exists()
    assert (workspace / "cli.pyc").read_bytes() == b"old-pyc"
    assert (workspace / "pkg" / "__pycache__" / "mod.cpython-310.pyc").read_bytes() == b"old-cache"
    assert (workspace / "target" / "debug" / "demo").read_bytes() == b"old-bin"


def test_sync_snapshot_changed_files_keeps_non_rust_target_paths_material(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    snapshot = tmp_path / "snapshot"
    workspace.mkdir()
    snapshot.mkdir()

    (workspace / "target").mkdir()
    (workspace / "target" / "generated.txt").write_text("old\n", encoding="utf-8")
    (snapshot / "target").mkdir()
    (snapshot / "target" / "generated.txt").write_text("new\n", encoding="utf-8")

    applied = sync_snapshot_changed_files(
        snapshot_root=snapshot,
        workspace_root=workspace,
        changed_files=["target/generated.txt"],
    )

    assert applied == ["target/generated.txt"]
    assert (workspace / "target" / "generated.txt").read_text(encoding="utf-8") == "new\n"
