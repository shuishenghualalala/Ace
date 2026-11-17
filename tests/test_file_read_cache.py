"""Regression tests for verified file reads, sparse windows and cancellation."""

import asyncio
import hashlib
import json
import os
import threading
from contextlib import contextmanager

import pytest

from crew.core.errors import ToolError
from crew.core.runctx import current_owner_account_id
from crew.tools import builtin, file_utils


def test_indexed_reads_verify_all_bytes_and_decode_only_the_window(tmp_path, monkeypatch):
    path = tmp_path / "paged.txt"
    data = b"\xef\xbb\xbf" + "".join(f"第{n}行\r\n" for n in range(1, 301)).encode("utf-8") + b"last"
    path.write_bytes(data)
    monkeypatch.setattr(file_utils, "_READ_INDEX_MIN_BYTES", 64)
    monkeypatch.setattr(file_utils, "_READ_CHUNK_BYTES", 67)
    read = file_utils.read_file_window
    first = read(path, offset=1, limit=8)
    assert first.content.startswith("1: 第1行\n2: 第2行")
    assert first.total_lines == 301

    raw_sizes = []
    decoded_sizes = []
    original_read = file_utils._read_blocks
    original_decode = file_utils._decode_read_blocks

    def count_raw(*args):
        for block in original_read(*args):
            raw_sizes.append(len(block))
            yield block

    def count_decoded(*args):
        for chunk in original_decode(*args):
            decoded_sizes.append(len(chunk))
            yield chunk

    monkeypatch.setattr(file_utils, "_read_blocks", count_raw)
    monkeypatch.setattr(file_utils, "_decode_read_blocks", count_decoded)
    assert read(path, offset=1, limit=8) == first
    assert sum(raw_sizes) == len(data)
    assert not decoded_sizes

    for offset, limit in [(156, 8), (297, 8), (-5, 3), (-301, 1)]:
        raw_sizes.clear()
        decoded_sizes.clear()
        cached = read(path, offset=offset, limit=limit)
        assert sum(raw_sizes) == len(data)
        if offset != -301:
            assert 0 < sum(decoded_sizes) < len(data.decode()) // 3
        assert cached.total_lines == 301
        assert cached == read(path, offset=offset, limit=limit, force_refresh=True)

    raw_sizes.clear()
    decoded_sizes.clear()
    owner = current_owner_account_id.set("different-reader")
    try:
        assert read(path, offset=1, limit=8) == first
    finally:
        current_owner_account_id.reset(owner)
    assert sum(raw_sizes) == len(data)
    assert sum(decoded_sizes) == len(data.decode("utf-8-sig"))


@pytest.mark.parametrize("offset,seed_offset", [(1, 1), (10, 1)])
def test_cached_window_has_no_stat_only_freshness_shortcut(tmp_path, monkeypatch, offset, seed_offset):
    path = tmp_path / "externally-edited.txt"
    original = b"old\n" * 24
    edited = b"new\n" * 24
    path.write_bytes(original)
    monkeypatch.setattr(file_utils, "_READ_CHUNK_BYTES", 16)
    monkeypatch.setattr(file_utils, "_READ_INDEX_MIN_BYTES", 16)
    read = file_utils.read_file_window
    # Model Windows' creation-time ctime semantics even on POSIX.
    stamp = file_utils._read_stamp
    monkeypatch.setattr(file_utils, "_read_stamp", lambda info: (*stamp(info)[:4], 0, info.st_mode))
    stat_before = path.stat()
    cached = read(path, offset=seed_offset, limit=1)
    path.write_bytes(edited)
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    assert file_utils._read_stamp(path.stat()) == file_utils._read_stamp(stat_before)

    fresh = read(path, offset=offset, limit=1)
    assert fresh.content.startswith(f"{offset}: new\n")
    assert fresh.version.digest == hashlib.sha256(edited).hexdigest()
    assert fresh.version.digest != cached.version.digest


def test_indexed_read_does_not_skip_invalid_utf8_outside_window(tmp_path, monkeypatch):
    path = tmp_path / "corrupt-later.txt"
    data = b"a\n" * 8192
    path.write_bytes(data)
    monkeypatch.setattr(file_utils, "_READ_INDEX_MIN_BYTES", 64)
    monkeypatch.setattr(file_utils, "_READ_CHUNK_BYTES", 8192)
    orig_stat = file_utils._read_stamp
    monkeypatch.setattr(file_utils, "_read_stamp", lambda info: (*orig_stat(info)[:4], 0, info.st_mode))
    file_utils.read_file_window(path, offset=1, limit=2)
    stat_before = path.stat()
    path.write_bytes(data[:-1] + b"\xff")
    os.utime(path, ns=(stat_before.st_atime_ns, stat_before.st_mtime_ns))
    with pytest.raises(ToolError, match="不是有效的 UTF-8 文本"):
        file_utils.read_file_window(path, offset=1, limit=2)


async def test_cancellation_stops_read_worker_and_preserves_authorization(tmp_path, monkeypatch):
    path = tmp_path / "large.txt"
    path.write_bytes(b"row\n" * 250)
    monkeypatch.setattr(file_utils, "_READ_CHUNK_BYTES", 64)
    opened = threading.Event()
    closed = threading.Event()
    observed = []
    cycles = []
    checkpoints = []
    auth_calls = []
    original_open = file_utils.open_verified_read
    original_checkpoint = file_utils.ReadControl.checkpoint

    async def authorize(*_args, **_kwargs):
        auth_calls.append(path)
        if len(auth_calls) == 3:
            raise ToolError("denied after warm read")
        return path

    @contextmanager
    def track_open(*args):
        position = None
        try:
            with original_open(*args) as (info, handle):
                cycles.append(handle)
                try:
                    yield info, handle
                finally:
                    position = handle.tell()
        finally:
            if position == 64:
                closed.set()

    def checkpoint(control):
        checkpoints.append(control)
        if len(checkpoints) == 3:
            opened.set()
            assert control.cancelled.wait(timeout=5)
        original_checkpoint(control)

    monkeypatch.setattr(builtin, "authorize_file_tool", authorize)
    monkeypatch.setattr(builtin, "record_file_observation", lambda *_args, **_kwargs: observed.append(path) or "version")
    monkeypatch.setattr(file_utils, "open_verified_read", track_open)
    monkeypatch.setattr(file_utils.ReadControl, "checkpoint", checkpoint)

    task = asyncio.create_task(builtin.handle_file_read({"path": str(path)}))
    assert await asyncio.to_thread(opened.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(closed.wait, 5)
    assert len(cycles) == 1 and cycles[0].closed
    assert not observed
    assert not any(cache_path == path for _, cache_path in file_utils._READ_REVISIONS)

    payload = json.loads(await builtin.handle_file_read({"path": str(path), "limit": 2, "force_refresh": True}))
    assert payload["path"] == str(path)
    assert payload["content"].startswith("1: row\n2: row")
    assert payload["force_refresh"] is True
    assert payload["next_offset"] == 3 and payload["file_version"] == "version"
    assert observed == [path]
    with pytest.raises(ToolError, match="denied after warm read"):
        await builtin.handle_file_read({"path": str(path), "limit": 2})
    assert len(cycles) == 2
