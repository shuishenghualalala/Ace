"""process_lifecycle 唯一 ProcessOwner 的单元测试：平台收容选择、PID 复用围栏、整树终止。"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

from crew.security import process_lifecycle as pl


@pytest.fixture(autouse=True)
def _reset_containment(monkeypatch):
    monkeypatch.setattr(pl.sys, "platform", sys.platform)
    yield
    pl.reset_containment_mode_for_tests()


def _posix_group_kwargs() -> dict:
    return {"start_new_session": True} if os.name != "nt" else {}


def _spawn_sleeper_tree() -> subprocess.Popen:
    """leader + 孙进程的树：子进程再 fork 一个 sleeper，验证整树终止。"""
    code = (
        "import subprocess, sys, time;"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']);"
        "time.sleep(60)"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_posix_group_kwargs(),
    )


# ---- 平台收容策略集中选择 ----

def test_containment_mode_windows_group(monkeypatch):
    monkeypatch.setattr(pl.os, "name", "nt")
    monkeypatch.setattr(pl.sys, "platform", "win32")
    assert pl.select_containment_mode() == "windows-group"


def test_containment_mode_linux_scope_probe_pass(monkeypatch):
    monkeypatch.setattr(pl.os, "name", "posix")
    monkeypatch.setattr(pl.sys, "platform", "linux")
    monkeypatch.setattr(pl.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(
        pl.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0),
    )
    assert pl.select_containment_mode() == "linux-scope"


def test_containment_mode_linux_probe_fail_falls_back(monkeypatch, caplog):
    monkeypatch.setattr(pl.os, "name", "posix")
    monkeypatch.setattr(pl.sys, "platform", "linux")
    monkeypatch.setattr(pl.shutil, "which", lambda name: None)
    with caplog.at_level("WARNING", logger=pl.logger.name):
        assert pl.select_containment_mode() == "posix-group"
    assert any("孙进程" in rec.message for rec in caplog.records)


def test_containment_mode_darwin_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(pl.os, "name", "posix")
    monkeypatch.setattr(pl.sys, "platform", "darwin")
    with caplog.at_level("WARNING", logger=pl.logger.name):
        pl.select_containment_mode()
        pl.select_containment_mode()
    warnings = [r for r in caplog.records if "孙进程" in r.message]
    assert len(warnings) == 1


def test_wrap_argv_scope_mode_prefixes_unit(monkeypatch):
    monkeypatch.setattr(pl, "select_containment_mode", lambda: "linux-scope")
    argv, unit = pl.wrap_argv_for_containment(["/bin/sh", "-c", "echo hi"])
    assert argv[:5] == ["systemd-run", "--user", "--scope", "--collect", "--quiet"]
    assert argv[-3:] == ["/bin/sh", "-c", "echo hi"]
    assert unit is not None and unit.endswith(".scope")
    argv2, unit2 = pl.wrap_argv_for_containment(["true"])
    assert unit2 != unit  # 每次 spawn 独立单元，互不误杀


def test_wrap_argv_non_scope_passthrough(monkeypatch):
    monkeypatch.setattr(pl, "select_containment_mode", lambda: "posix-group")
    argv, unit = pl.wrap_argv_for_containment(["/bin/sh", "-c", "x"])
    assert argv == ["/bin/sh", "-c", "x"]
    assert unit is None


def test_isolated_process_kwargs_starts_new_session():
    kwargs = pl.isolated_process_kwargs()
    if os.name == "nt":
        assert "creationflags" in kwargs
    else:
        assert kwargs == {"start_new_session": True}


# ---- PID 复用围栏 ----

def test_read_process_start_time_matches_live_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        started = pl.read_process_start_time(proc.pid)
        assert started is not None
        assert pl.read_process_start_time(proc.pid) == started
    finally:
        proc.kill()
        proc.wait()


def test_signal_skipped_when_identity_reused(monkeypatch):
    """启动时刻与信号时不一致（PID 被复用）→ 拒绝发信号。"""
    kills: list = []

    class _FakeProc:
        pid = 424242
        returncode = None

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(pl.os, "killpg", lambda pgid, sig: kills.append((pgid, sig)))
    values = iter(["t-1", "t-2"])
    monkeypatch.setattr(pl, "read_process_start_time", lambda pid: next(values))
    owner = pl.ProcessOwner.capture(_FakeProc())
    assert owner.signal(signal.SIGTERM) is False
    assert kills == []


def test_signal_delivered_when_identity_matches(monkeypatch):
    kills: list = []
    monkeypatch.setattr(pl.os, "killpg", lambda pgid, sig: kills.append((pgid, sig)))
    monkeypatch.setattr(pl, "read_process_start_time", lambda pid: "same")
    monkeypatch.setattr(pl, "_pid_alive", lambda pid: True)
    owner = pl.ProcessOwner(1234, identity=pl.ProcessIdentity(1234, "same"))
    assert owner.signal(signal.SIGTERM) is True
    assert kills == [(1234, signal.SIGTERM)]


# ---- 整树终止 ----

@pytest.mark.asyncio
async def test_terminate_process_tree_kills_whole_group():
    proc = _spawn_sleeper_tree()
    child_pid = None
    try:
        # 等孙进程起来
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            out = subprocess.run(
                ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True
            )
            if out.stdout.strip():
                child_pid = int(out.stdout.split()[0])
                break
            time.sleep(0.05)
        assert child_pid is not None, "孙进程未启动"
        await pl.terminate_process_tree(proc, timeout=5.0)
        assert proc.returncode is not None
        # 整树：孙进程也必须在宽限内被回收
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not pl._pid_alive(child_pid):
                break
            time.sleep(0.05)
        assert not pl._pid_alive(child_pid), "孙进程逃逸未被整树终止"
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait()


def test_terminate_process_tree_sync_sends_term():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **_posix_group_kwargs(),
    )
    try:
        pl.terminate_process_tree_sync(proc.pid)
        proc.wait(timeout=5)
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


@pytest.mark.asyncio
async def test_owner_wait_for_exit_event_driven():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "print('done')",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    owner = pl.ProcessOwner.capture(proc)
    assert await owner.wait_for_exit(timeout=5) is True
    assert await owner.wait_for_exit(timeout=0.1) is True


def test_owner_wait_for_exit_sync_on_popen():
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('done')"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owner = pl.ProcessOwner.capture(proc)
    assert owner.wait_for_exit_sync(timeout=5) is True


def test_owner_for_pid_exited_detection():
    proc = subprocess.Popen(
        [sys.executable, "-c", "pass"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=5)
    owner = pl.ProcessOwner.for_pid(proc.pid)
    assert owner.exited() is True
