from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path

import pytest

from alysis_code.server.app import _create_run_from_upload
from alysis_code.server.settings import ServerSettings
from alysis_code.server.store import ServerStore, ServerStoreError


class _BytesUpload:
    def __init__(self, payload: bytes, *, fail_on_unbounded_read: bool = False) -> None:
        self._payload = payload
        self._offset = 0
        self.fail_on_unbounded_read = fail_on_unbounded_read
        self.read_sizes: list[int] = []
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.fail_on_unbounded_read and size < 0:
            raise AssertionError("expected bounded chunk reads")
        if self._offset >= len(self._payload):
            return b""
        if size < 0:
            end = len(self._payload)
        else:
            end = min(len(self._payload), self._offset + size)
        chunk = self._payload[self._offset : end]
        self._offset = end
        return chunk

    async def close(self) -> None:
        self.closed = True


def _settings(tmp_path: Path) -> ServerSettings:
    return ServerSettings(
        host="127.0.0.1",
        port=7070,
        data_dir=tmp_path / "server-data",
        token=None,
        max_upload_bytes=1024,
        max_concurrent_jobs=1,
        worker_backend="bwrap",
        worker_sandbox_mode="strict",
        worker_network="on",
        default_model="gpt-test",
        default_base_url=None,
        allow_client_model=True,
        allow_client_base_url=False,
    )


def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_create_run_from_upload_with_valid_zip_succeeds(tmp_path: Path) -> None:
    upload = _BytesUpload(
        _zip_bytes({"README.md": "ok\n", "src/app.py": "print('hi')\n"}),
        fail_on_unbounded_read=True,
    )
    store = ServerStore(_settings(tmp_path))

    run_id = asyncio.run(
        _create_run_from_upload(
            store=store,
            upload=upload,
            max_upload_bytes=1024,
            chunk_size=32,
        )
    )

    run_paths = store.get_run_paths(run_id)
    assert (run_paths.workspace_dir / "README.md").read_text(encoding="utf-8") == "ok\n"
    assert (run_paths.workspace_dir / "src" / "app.py").read_text(
        encoding="utf-8"
    ) == "print('hi')\n"
    assert upload.closed is True
    assert all(size == 32 for size in upload.read_sizes)


def test_create_run_from_upload_with_malformed_zip_preserves_error(tmp_path: Path) -> None:
    upload = _BytesUpload(b"not a zip archive", fail_on_unbounded_read=True)
    store = ServerStore(_settings(tmp_path))

    with pytest.raises(ServerStoreError, match="Invalid ZIP file."):
        asyncio.run(
            _create_run_from_upload(
                store=store,
                upload=upload,
                max_upload_bytes=1024,
                chunk_size=8,
            )
        )

    assert upload.closed is True
    assert all(size == 8 for size in upload.read_sizes)
