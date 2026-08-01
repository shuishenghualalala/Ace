"""后台进程注册表测试：输出捕获 / poll / wait / kill / watch_patterns / notify_on_complete。"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time

import pytest

from crew.core.runctx import current_owner_account_id
from crew.tools.process_registry import (
    ProcessRegistry,
    _handle_process,
    format_process_notification,
    process_registry,
)


@pytest.fixture(autouse=True)
def _isolate_checkpoint(tmp_path_factory, monkeypatch):
    """把 checkpoint 指向临时目录，避免测试写脏真实 .crew/processes.json。

    单独 monkeypatch 自己 tmp 的用例会再覆盖一次，互不影响。
    """
    import crew.tools.process_registry as pr

    d = tmp_path_factory.mktemp("ckpt")
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: d / "processes.json")


def _wait_exit(reg: ProcessRegistry, sid: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = reg.poll(sid)
        if r.get("status") == "exited":
            return r
        time.sleep(0.05)
    raise AssertionError(f"进程 {sid} 未在 {timeout}s 内退出: {reg.poll(sid)}")


def _py_cmd(code: str) -> str:
    """Build a shell command that runs Python consistently on Windows and POSIX."""
    if os.name == "nt":
        exe = sys.executable.replace("'", "''")
        ps_code = code.replace("'", "''")
        return f"& '{exe}' -c '{ps_code}'"
    parts = [sys.executable, "-c", code]
    return " ".join(shlex.quote(part) for part in parts)


def test_spawn_local_python_prints_unicode_emoji():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("print(chr(0x1f4cc))"), session_key="unicode-emoji")
    r = _wait_exit(reg, s.id)
    assert r["exit_code"] == 0
    assert "\U0001f4cc" in r.get("output_preview", "")


def test_capture_output_and_poll():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("print('hello-crew')"), session_key="sess1")
    r = _wait_exit(reg, s.id)
    assert r["exit_code"] == 0
    assert "hello-crew" in r["output_preview"]


def test_read_log():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("print('l1'); print('l2'); print('l3')"), session_key="s")
    _wait_exit(reg, s.id)
    log = reg.read_log(s.id, offset=0, limit=200)
    assert log["total_lines"] == 3
    assert "l2" in log["output"]


def test_wait_returns_exit():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("import sys; sys.exit(0)"), session_key="s")
    r = reg.wait(s.id, timeout=10)
    assert r["status"] == "exited"
    assert r["exit_code"] == 0


def test_wait_timeout():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("import time; time.sleep(5)"), session_key="s")
    r = reg.wait(s.id, timeout=1)
    assert r["status"] == "timeout"
    reg.kill_process(s.id)


def test_kill_running_process():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("import time; time.sleep(30)"), session_key="s")
    time.sleep(0.2)
    assert reg.poll(s.id)["status"] == "running"
    r = reg.kill_process(s.id)
    assert r["status"] == "killed"
    _wait_exit(reg, s.id)
    assert reg.poll(s.id)["status"] == "exited"


def test_notify_on_complete_enqueues():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("print('done')"), session_key="sessX", owner_account_id="A:uid-a", notify_on_complete=True)
    _wait_exit(reg, s.id)
    # reader 线程的 finally 里 enqueue，给一点时间
    time.sleep(0.1)
    events = reg.drain_for_session("sessX", owner_account_id="A:uid-a")
    assert len(events) == 1
    assert events[0]["type"] == "completion"
    assert events[0]["exit_code"] == 0
    assert "done" in events[0]["output"]
    # drain 后应清空
    assert reg.drain_for_session("sessX", owner_account_id="A:uid-a") == []


def test_process_notifications_are_owner_scoped():
    reg = ProcessRegistry()
    a = reg.spawn_local(_py_cmd("print('owner-a')"), session_key="same", owner_account_id="A:uid-a", notify_on_complete=True)
    b = reg.spawn_local(_py_cmd("print('owner-b')"), session_key="same", owner_account_id="B:uid-b", notify_on_complete=True)
    _wait_exit(reg, a.id)
    _wait_exit(reg, b.id)
    time.sleep(0.1)

    events_a = reg.drain_for_session("same", owner_account_id="A:uid-a")
    events_b = reg.drain_for_session("same", owner_account_id="B:uid-b")

    assert len(events_a) == 1 and "owner-a" in events_a[0]["output"]
    assert len(events_b) == 1 and "owner-b" in events_b[0]["output"]


def test_watch_patterns_match():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("print('READY-NOW')"), session_key="sessW", watch_patterns=["READY"])
    _wait_exit(reg, s.id)
    time.sleep(0.1)
    events = reg.drain_for_session("sessW")
    matches = [e for e in events if e["type"] == "watch_match"]
    assert matches, f"期望 watch_match, got {events}"
    assert matches[0]["pattern"] == "READY"
    assert "READY-NOW" in matches[0]["output"]


def test_watch_no_match_no_event():
    reg = ProcessRegistry()
    s = reg.spawn_local(_py_cmd("print('nothing-here')"), session_key="sessN", watch_patterns=["ZZZ"])
    _wait_exit(reg, s.id)
    time.sleep(0.1)
    assert reg.drain_for_session("sessN") == []


def test_format_notification():
    text = format_process_notification({
        "type": "completion", "session_id": "proc_x", "command": "echo hi",
        "exit_code": 0, "output": "hi",
    })
    assert "proc_x" in text and "退出码 0" in text

    text2 = format_process_notification({
        "type": "watch_match", "session_id": "proc_y", "command": "tail -f log",
        "pattern": "ERROR", "output": "ERROR boom", "suppressed": 2,
    })
    assert "watch 模式" in text2 and "ERROR" in text2 and "2 条" in text2


def test_recover_from_checkpoint(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    # 把 checkpoint 指向临时目录
    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")

    reg = pr.ProcessRegistry()
    # 起一个真实存活的长进程，让它写进 checkpoint
    s = reg.spawn_local(_py_cmd("import time; time.sleep(30)"), session_key="recov", notify_on_complete=True)
    assert (tmp_path / "processes.json").exists()
    real_pid = s.pid

    # 模拟主程序重启：新 registry 从 checkpoint 认领
    reg2 = pr.ProcessRegistry()
    n = reg2.recover_from_checkpoint()
    assert n == 1
    r = reg2.poll(s.id)
    assert r["status"] == "running"
    assert r.get("detached") is True
    assert "无输出历史" in r.get("note", "")

    # detached 进程可被 kill（走 host PID）
    kr = reg2.kill_process(s.id)
    assert kr["status"] == "killed"
    # 真实进程应已被杀
    time.sleep(0.3)
    assert not pr._pid_alive(real_pid)
    # 原 registry 也清理掉
    reg.kill_process(s.id)


def test_recover_skips_dead_pid(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")
    # 手写一个指向已死 PID 的 checkpoint（99999 基本不存在）
    (tmp_path / "processes.json").write_text(
        json.dumps([{"session_id": "proc_dead", "command": "x", "pid": 999999, "session_key": "k"}]),
        encoding="utf-8",
    )
    reg = pr.ProcessRegistry()
    assert reg.recover_from_checkpoint() == 0
    assert reg.poll("proc_dead")["status"] == "not_found"


def test_detached_dead_pid_transitions_to_exited(tmp_path, monkeypatch):
    import crew.tools.process_registry as pr

    monkeypatch.setattr(pr, "_checkpoint_path", lambda: tmp_path / "processes.json")
    reg = pr.ProcessRegistry()
    # 起一个短进程并认领为 detached，等它自己退出后 poll 应转 exited
    s = reg.spawn_local(_py_cmd("import time; time.sleep(0.2)"), session_key="d")
    pid = s.pid
    reg2 = pr.ProcessRegistry()
    reg2.recover_from_checkpoint()
    # 等真实进程退出
    deadline = time.monotonic() + 5
    while pr._pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.1)
    r = reg2.poll(s.id)
    assert r["status"] == "exited"
    reg.kill_process(s.id)


def test_process_tool_handler():
    # 走全局单例（工具 handler 用的是它）
    s = process_registry.spawn_local(_py_cmd("print('tool-test')"), session_key="th")
    _wait_exit(process_registry, s.id)
    out = json.loads(_handle_process({"action": "poll", "session_id": s.id}))
    assert out["status"] == "exited"
    listed = json.loads(_handle_process({"action": "list"}))
    assert any(p["session_id"] == s.id for p in listed["processes"])
    err = json.loads(_handle_process({"action": "poll"}))  # 缺 session_id
    assert "error" in err


def test_process_tool_handler_is_owner_scoped():
    s = process_registry.spawn_local(_py_cmd("print('tool-owner')"), session_key="own", owner_account_id="A:uid-a")
    _wait_exit(process_registry, s.id)

    token = current_owner_account_id.set("B:uid-b")
    try:
        listed = json.loads(_handle_process({"action": "list"}))
        assert all(p["session_id"] != s.id for p in listed["processes"])
        polled = json.loads(_handle_process({"action": "poll", "session_id": s.id}))
        assert polled["status"] == "not_found"
    finally:
        current_owner_account_id.reset(token)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
