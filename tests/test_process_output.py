"""Terminal output cursors over memory, a durable prefix, and archived tasks."""

import json

from crew.state.home import get_owner_runtime_home
from crew.tools import process_output
from crew.tools.process_output import OutputSnapshot, ProcessOutput
from crew.tools.process_registry import ProcessRegistry, ProcessSession


def test_byte_pages_reassemble_utf8_and_crlf_across_rolling_buffer(tmp_path):
    log = tmp_path / "terminal.log"
    capture = ProcessOutput()
    chunks = ["first\r\n", "中🙂\r\n", "stderr marker\r\n", "last🙂"]
    for chunk in chunks:
        capture.append(chunk, max_chars=8, output_ref=str(log))
    snapshot = capture.snapshot(str(log))
    assert snapshot.tail_offset > 0 and snapshot.metadata()["output_retention"] == "persisted"
    expected = "".join(chunks)
    assert log.read_bytes() == expected.encode("utf-8")
    reconstructed = []
    cursor = 0
    while cursor < snapshot.end:
        page = snapshot.page(cursor, max_bytes=5)
        assert page["offset"] == cursor and not page["gap"]
        assert page["next_offset"] > cursor
        assert page["next_offset"] - cursor == len(page["output"].encode("utf-8"))
        reconstructed.append(page["output"])
        cursor = page["next_offset"]
        assert page["has_more"] == (cursor < snapshot.end)
    assert "".join(reconstructed) == expected


def test_memory_eviction_and_disk_cap_return_a_gap_at_the_real_cursor(tmp_path, monkeypatch):
    monkeypatch.setattr(process_output, "MAX_PERSISTED_BYTES", 5)
    log = tmp_path / "terminal.log"
    capture = ProcessOutput()
    capture.append("abc🙂", max_chars=4, output_ref=str(log))
    capture.append("not retained\n", max_chars=4, output_ref=str(log))
    capture.append("end🙂", max_chars=4, output_ref=str(log))
    snapshot = capture.snapshot(str(log))
    first = snapshot.page(0, max_bytes=12)
    assert first["output"] == "abc" and first["next_offset"] == 3
    assert first["output_retention"] == "partial" and first["output_truncated"]
    final = snapshot.page(first["next_offset"], max_bytes=12)
    assert final["gap"] and final["offset"] == snapshot.tail_offset
    assert final["output"] == "end🙂" and final["next_offset"] == snapshot.end and not final["has_more"]
    assert log.read_bytes() == b"abc"
    archive = OutputSnapshot.archived(str(log), known_end=snapshot.end, confirmed_bytes=3)
    assert archive.page(3, max_bytes=12)["gap"]
    assert archive.lines(None, limit=2, running=False)["output"] == "abc"


def test_legacy_default_tail_and_explicit_zero_have_distinct_stable_line_offsets(tmp_path):
    log = tmp_path / "terminal.log"
    capture = ProcessOutput()
    capture.append("one\r\ntwo\r\nthree\r\n", max_chars=8, output_ref=str(log))
    capture.append("partial", max_chars=8, output_ref=str(log))
    snapshot = capture.snapshot(str(log))
    tail = snapshot.lines(None, limit=1, running=True)
    assert tail["output"] == "partial" and tail["offset"] == 3
    first = snapshot.lines(0, limit=1, running=True)
    assert first["output"] == "one" and first["next_offset"] == 1
    second = snapshot.lines(first["next_offset"], limit=5, running=True)
    assert second["output"] == "two\nthree" and second["next_offset"] == 3
    capture.append(" line\r\n", max_chars=8, output_ref=str(log))
    third = capture.snapshot(str(log)).lines(second["next_offset"], limit=2, running=True)
    assert third["output"] == "partial line" and third["next_offset"] == 4


def test_paged_log_escapes_many_control_characters_without_hiding_cursor():
    capture = ProcessOutput()
    capture.append("\x01" * 24576, max_chars=24576, output_ref="")
    snapshot = capture.snapshot("")
    first = snapshot.page(0, max_bytes=process_output.MAX_PAGE_BYTES, tool_budget=True)
    assert first["has_more"] and 1 < first["next_offset"] < snapshot.end
    assert len(json.dumps(first, ensure_ascii=False)) < 50_000
    second = snapshot.page(first["next_offset"], max_bytes=process_output.MAX_PAGE_BYTES, tool_budget=True)
    assert second["offset"] == first["next_offset"]


def test_durable_log_is_owner_and_shell_task_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path))
    owner = "A:owner-a"
    task_id = "task_archive"
    log = get_owner_runtime_home(owner) / "tasks" / f"{task_id}.log"
    log.parent.mkdir()
    log.write_bytes("durable🙂\r\n".encode("utf-8"))

    class Tasks:
        def get(self, requested_id, *, owner_account_id):
            if requested_id != task_id or owner_account_id != owner:
                raise KeyError(requested_id)
            return {
                "kind": "shell", "status": "failed", "output_ref": str(log),
                "progress": {"exit_code": 7, "process_session_id": "proc_fixture"},
            }

    restarted = ProcessRegistry()
    restarted.configure_task_runtime(Tasks())
    result = restarted.read_log(task_id=task_id, cursor=0, owner_account_id=owner)
    assert result["archived"] and result["output"] == "durable🙂\r\n"
    assert result["task_status"] == "failed" and result["exit_code"] == 7
    assert restarted.read_log(task_id=task_id, cursor=0, owner_account_id="A:owner-b")["status"] == "not_found"


def test_registry_live_progress_uses_increasing_cursor_when_tail_length_is_constant():
    registry = ProcessRegistry()
    session = ProcessSession(id="proc_fixture", command="writer", max_output_chars=4)
    previous = 0
    for marker in ["ab🙂\n", "cd🙂\n", "ef🙂\n"]:
        registry._publish_output(session, marker)
        assert len(session.output_buffer) == 4
        page = registry.read_output(session, previous)
        assert page["offset"] == previous and not page["gap"] and page["output"] == marker
        previous = page["next_offset"]
