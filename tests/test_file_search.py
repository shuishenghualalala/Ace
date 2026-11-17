"""Regression coverage for file search completion, pagination, and cancellation."""

import asyncio
import json
import os
import shutil
import sys
import threading

import pytest

from crew.agent.compact.microcompact import _summarize_tool_result
from crew.agent.loop.tool_result_display import tool_result_detail_for_ui
from crew.tools import file_tools


@pytest.fixture(params=("python", "ripgrep"))
def search_engine(request, monkeypatch):
    rg = None if request.param == "python" else shutil.which("rg")
    if request.param == "ripgrep" and rg is None:
        pytest.skip("ripgrep is unavailable")

    async def resolve():
        return rg

    monkeypatch.setattr(file_tools, "_resolve_rg", resolve)
    return request.param


@pytest.mark.parametrize("name", ["glob", "grep"])
async def test_complete_file_search_paginates_stably(tmp_path, search_engine, name):
    for index, filename in enumerate(("c.txt", "a.txt", "é.txt", "b.txt")):
        file = tmp_path / filename
        file.write_text("known\n", encoding="utf-8")
        modified = 1_700_000_000 + (index // 2) * 100
        os.utime(file, (modified, modified))

    if name == "glob":
        search = file_tools.handle_glob
        arguments = {"pattern": "*.txt", "limit": 2}
        expected = ["a.txt", "c.txt", "b.txt", "é.txt"]
    else:
        search = file_tools.handle_grep
        arguments = {"pattern": "known", "head_limit": 2}
        expected = ["b.txt", "é.txt", "a.txt", "c.txt"]

    payload = json.loads(await search({"path": str(tmp_path), **arguments}))
    assert payload["scan_complete"] and payload["truncated"]
    assert payload["partial_reasons"] == [] and payload["next_offset"] == 2
    second = json.loads(await search({"path": str(tmp_path), **arguments, "offset": payload["next_offset"]}))
    assert second["scan_complete"] and not second["truncated"]
    assert payload["files"] + second["files"] == expected
    assert "next_offset" not in second

    end = json.loads(await search({"path": str(tmp_path), **arguments, "offset": len(expected)}))
    assert end["scan_complete"] and end["files"] == []
    assert "偏移量" in end["search_notice"]
    missing = json.loads(await search({"path": str(tmp_path), **arguments, "pattern": "missing"}))
    assert missing["scan_complete"] and not missing["truncated"] and missing["files"] == []


@pytest.mark.parametrize("tool", ["glob", "grep"])
@pytest.mark.parametrize("after", [0, 1])
async def test_managed_search_timeout_is_distinct_from_a_complete_empty_result(tmp_path, monkeypatch, tool, after):
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("known", encoding="utf-8")

    async def no_rg():
        return None

    monkeypatch.setattr(file_tools, "_resolve_rg", no_rg)
    original = file_tools._SearchScan.checkpoint

    def exhaust(scan):
        if scan.scanned_files >= after:
            scan.started -= 100
        return original(scan)

    monkeypatch.setattr(file_tools._SearchScan, "checkpoint", exhaust)
    if tool == "glob":
        result = await file_tools.handle_glob({"path": str(tmp_path), "pattern": "*.txt"})
    else:
        result = await file_tools.handle_grep({"path": str(tmp_path), "pattern": "known"})
    payload = json.loads(result)
    assert payload["scan_complete"] is False and payload["partial_reasons"] == ["timeout"]
    assert payload["scanned_files"] == after
    assert payload["files"] == (["a.txt"] if after else [])
    assert "next_offset" not in payload
    assert "No files found" not in result
    assert tool_result_detail_for_ui(tool, result).startswith("搜索未完成（扫描超时）")
    assert "扫描超时" in _summarize_tool_result(tool, {"pattern": "fixture"}, result)


def test_managed_grep_reports_files_skipped_by_io_size_and_encoding(tmp_path, monkeypatch):
    for name in ("readable.txt", "unreadable.txt"):
        (tmp_path / name).write_text("known", encoding="utf-8")
    (tmp_path / "encoding.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "large.txt").write_bytes(b"a" * 100)
    monkeypatch.setattr(file_tools, "_DEFAULT_MAX_FILE_BYTES", 32)
    read = file_tools.read_verified_bytes

    def read_file(path, **kwargs):
        if path.name == "unreadable.txt":
            raise PermissionError(path.name)
        return read(path, **kwargs)

    monkeypatch.setattr(file_tools, "read_verified_bytes", read_file)
    data = json.loads(file_tools._grep_via_python({"pattern": "known"}, tmp_path, "files_with_matches"))
    assert not data["scan_complete"] and data["scanned_files"] == 1 and data["skipped_files"] == 3
    assert data["partial_reasons"] == ["file_size_limit", "unreadable_files", "unsupported_encoding"]
    assert data["files"] == ["readable.txt"]
    assert "next_offset" not in data


async def test_grep_context_lines_are_unique_and_offsets_count_visible_rows(tmp_path, search_engine, monkeypatch):
    (tmp_path / "lines.txt").write_text("one\nknown first\nshared\nknown second\nfinal\n", encoding="utf-8")
    args = {"path": str(tmp_path), "pattern": "known", "output_mode": "content", "context": 1, "head_limit": 0}
    full = json.loads(await file_tools.handle_grep(args))
    assert full["scan_complete"] and not full["truncated"] and full["num_lines"] == 5
    assert full["content"].count("shared") == 1
    monkeypatch.setattr(file_tools, "_SEARCH_TEXT_PAGE_CHARS", 48)
    seen = []
    while True:
        page = json.loads(await file_tools.handle_grep(args))
        seen.extend(page["content"].splitlines())
        if not page["truncated"]:
            break
        assert page["next_offset"] == len(seen) <= full["num_lines"]
        args["offset"] = page["next_offset"]
    assert seen == full["content"].splitlines()


async def test_path_modes_keep_a_unicode_line_separator_as_part_of_the_path(tmp_path, search_engine):
    name = "part\u2028name.txt"
    (tmp_path / name).write_text("known", encoding="utf-8")
    for search, pattern in ((file_tools.handle_glob, "*.txt"), (file_tools.handle_grep, "known")):
        result = json.loads(await search({"path": str(tmp_path), "pattern": pattern}))
        assert result["scan_complete"] and result["files"] == [name]


async def test_grep_reports_clipped_line_without_losing_the_match(tmp_path, search_engine):
    (tmp_path / "long.txt").write_text("known " + "x" * 15_000 + "\n", encoding="utf-8")
    data = json.loads(await file_tools.handle_grep({"path": str(tmp_path), "pattern": "known", "output_mode": "content"}))
    assert data["scan_complete"] and data["truncated"] and data["num_lines"] == 1
    assert data["content"].startswith("long.txt:1:known")
    assert "file_read" in data["search_notice"] and "next_offset" not in data


async def test_grep_head_limit_zero_does_not_apply_the_default_row_limit(tmp_path, search_engine):
    (tmp_path / "rows.txt").write_text("known\n" * 260, encoding="utf-8")
    args = {"path": str(tmp_path), "pattern": "known", "output_mode": "content"}
    default = json.loads(await file_tools.handle_grep(args))
    assert default["num_lines"] == 250 and default["next_offset"] == 250
    all_rows = json.loads(await file_tools.handle_grep({**args, "head_limit": 0}))
    assert all_rows["num_lines"] == 260 and not all_rows["truncated"]


@pytest.mark.parametrize("name", ["glob", "grep"])
async def test_search_falls_back_when_windows_style_loop_cannot_spawn_processes(tmp_path, monkeypatch, name):
    (tmp_path / "known.txt").write_text("known", encoding="utf-8")

    async def resolve():
        return "rg"

    async def cannot_spawn(*_args, **_kwargs):
        raise NotImplementedError

    monkeypatch.setattr(file_tools, "_resolve_rg", resolve)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", cannot_spawn)
    if name == "glob":
        output = await file_tools.handle_glob({"path": str(tmp_path), "pattern": "*.txt"})
    else:
        output = await file_tools.handle_grep({"path": str(tmp_path), "pattern": "known"})
    result = json.loads(output)
    assert result["scan_complete"] and result["scanned_files"] == 1 and result["files"] == ["known.txt"]


async def test_managed_cancel_stops_after_current_io(tmp_path, monkeypatch):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text("known", encoding="utf-8")
    scan = file_tools._SearchScan(tmp_path, 10)
    entered, release, worker_finished = threading.Event(), threading.Event(), threading.Event()
    read = file_tools.read_verified_bytes
    visited = []

    def read_file(path, **kwargs):
        visited.append(path.name)
        entered.set()
        assert release.wait(4)
        return read(path, **kwargs)

    def grep(*args, **kwargs):
        try:
            return file_tools._grep_via_python(*args, **kwargs)
        finally:
            worker_finished.set()

    monkeypatch.setattr(file_tools, "read_verified_bytes", read_file)
    task = asyncio.create_task(file_tools._run_python_scan(scan, grep, {"pattern": "known"}, tmp_path, "content"))
    try:
        assert await asyncio.to_thread(entered.wait, 4)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scan.cancelled.is_set()
    finally:
        release.set()
    assert await asyncio.to_thread(worker_finished.wait, 4)
    assert visited == ["a.txt"]


@pytest.mark.parametrize("action", ["timeout", "cancel", "buffer_limit"])
async def test_native_search_reaps_real_child_and_retains_complete_records(tmp_path, monkeypatch, action):
    ready = tmp_path / "running"
    script = (
        "import pathlib, sys, time; "
        "sys.stdout.write('first.txt\\nunfinished'); sys.stdout.flush(); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(20)"
    )
    actual_spawn = asyncio.create_subprocess_exec
    children = []

    async def spawn(*args, **kwargs):
        proc = await actual_spawn(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    if action == "buffer_limit":
        monkeypatch.setattr(file_tools, "_SEARCH_BUFFER_BYTES", 12)
    scan = file_tools._SearchScan(tmp_path, 2 if action == "timeout" else 10, native=True)
    task = asyncio.create_task(file_tools._run_rg([sys.executable, "-u", "-c", script, str(ready)], tmp_path, scan))
    try:
        if action == "cancel":
            async with asyncio.timeout(5):
                while not ready.exists():
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            rc, output, stderr = await task
            assert file_tools._rg_lines(rc, output, stderr, scan) == ["first.txt"]
            reason = "timeout" if action == "timeout" else "result_buffer_limit"
            assert scan.partial_reasons == {reason}
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert len(children) == 1 and children[0].returncode is not None
