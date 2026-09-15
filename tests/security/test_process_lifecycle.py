"""process_lifecycle 唯一 ProcessOwner 的单元测试：平台收容选择、PID 复用围栏、整树终止。"""

from __future__ import annotations

import asyncio
import os
import shlex
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
        await pl.terminate_process_tree(proc, grace_ms=5000)
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


# ---- F4：统一宽限 + 终末超时 ----

def test_resolve_grace_ms_defaults_and_clamps():
    assert pl.resolve_grace_ms(None) == pl.DEFAULT_GRACE_MS == 3000
    assert pl.resolve_grace_ms(1500) == 1500
    assert pl.resolve_grace_ms(-5) == 0


class _UnkillableProcess:
    """句柄永远不结算的假进程：模拟 D-state 等不可杀场景。"""

    pid = 987654
    returncode = None

    async def wait(self):
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_terminate_returns_error_within_bounds_for_unkillable(monkeypatch):
    monkeypatch.setattr(pl.os, "killpg", lambda pgid, sig: True)
    monkeypatch.setattr(pl, "_pid_alive", lambda pid: True)
    owner = pl.ProcessOwner(987654, process=_UnkillableProcess())
    started = time.monotonic()
    with pytest.raises(pl.ProcessTerminationTimeout):
        await owner.terminate(grace_ms=200)
    elapsed = time.monotonic() - started
    # 升级宽限 + 终末等待各一次：必须返回错误而非挂起
    assert elapsed < 1.5


@pytest.mark.asyncio
async def test_terminate_escalates_term_to_kill(tmp_path):
    """忽略 SIGTERM 的进程在宽限后收到 SIGKILL 并在终末等待内结算。"""
    import signal as _signal

    ready = tmp_path / "ready"
    code = (
        "import signal, sys, time, pathlib;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        f"pathlib.Path({str(ready)!r}).write_text('1');"
        "time.sleep(30)"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    owner = pl.ProcessOwner.capture(proc)
    try:
        await owner.terminate(grace_ms=300)
        assert proc.returncode is not None
        assert proc.returncode == -getattr(_signal, "SIGKILL", _signal.SIGTERM)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


@pytest.mark.asyncio
async def test_terminate_already_exited_is_noop():
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    owner = pl.ProcessOwner.capture(proc)
    await owner.terminate(grace_ms=50)  # 不抛错、不等待


# ---- F5：live set + 宿主退出三时机清杀 ----

@pytest.fixture(autouse=True)
def _neutralize_host_hooks(monkeypatch):
    """单测不真改进程级 signal/atexit：记录调用即可。"""
    calls: list = []
    monkeypatch.setattr(pl.atexit, "register", lambda fn: calls.append(("atexit", fn)))
    monkeypatch.setattr(pl.signal, "signal", lambda sig, h: calls.append(("signal", sig, h)))
    pl.reset_host_exit_hooks_for_tests()
    yield
    pl.reset_host_exit_hooks_for_tests()


def test_register_and_release_live_owner():
    before = pl.live_owner_count()
    owner = pl.ProcessOwner(1, identity=pl.ProcessIdentity(1, None))
    pl.register_live(owner)
    assert pl.live_owner_count() == before + 1
    pl.register_live(owner)  # 幂等
    assert pl.live_owner_count() == before + 1
    pl.release_live(owner)
    assert pl.live_owner_count() == before
    pl.release_live(None)  # 容忍空属主


def test_host_exit_hooks_installed_once():
    pl._install_host_exit_hooks()
    pl._install_host_exit_hooks()
    assert pl._host_exit_hooks_installed is True


def test_terminate_all_for_host_exit_kills_registered_tree():
    proc = _spawn_sleeper_tree()
    owner = pl.ProcessOwner.capture(proc)
    pl.register_live(owner)
    try:
        pl.terminate_all_for_host_exit()
        # SIGKILL 后句柄必然结算（wait 回收退出码）
        proc.wait(timeout=5)
        assert proc.returncode is not None, "宿主退出同步强杀未结算句柄"
    finally:
        if proc.returncode is None:
            proc.kill()
            proc.wait()
        pl.release_live(owner)


def test_chained_termination_handler_calls_previous():
    called: list = []
    monkey_prev = lambda signum, frame: called.append(signum)  # noqa: E731
    handler = pl._chained_termination_handler(monkey_prev)
    handler(signal.SIGTERM, None)
    assert called == [signal.SIGTERM]


def test_chained_termination_handler_ignores_ignored_signal():
    handler = pl._chained_termination_handler(pl.signal.SIG_IGN)
    handler(signal.SIGTERM, None)  # 不抛错、不递送


@pytest.mark.asyncio
async def test_drain_live_processes_settles_and_clears():
    proc, _ = await pl.spawn_tracked(
        sys.executable, "-c", "import time; time.sleep(60)",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    owner = pl.ProcessOwner.capture(proc)
    pl.register_live(owner)
    try:
        await pl.drain_live_processes(grace_ms=500)
        assert proc.returncode is not None
        assert pl.live_owner_count() == 0 or owner not in pl._live_owners
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        pl.release_live(owner)


def test_registry_session_settles_out_of_live_set():
    from crew.tools.process_registry import ProcessRegistry

    before = pl.live_owner_count()
    reg = ProcessRegistry()
    # 命令必须在宿主 shell 下合法：进程存活 ~1s，覆盖 注册→结算 全窗口
    reg.spawn_local(
        f"{shlex.quote(sys.executable)} -c {shlex.quote('import time; time.sleep(1)')}",
        session_key="live-set",
        owner_account_id="local",
    )
    deadline = time.monotonic() + 5
    while pl.live_owner_count() == before and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pl.live_owner_count() == before + 1, "spawn 后未登记进 live set"
    deadline = time.monotonic() + 10
    while pl.live_owner_count() > before and time.monotonic() < deadline:
        time.sleep(0.05)
    assert pl.live_owner_count() == before, "reader 完全结算后未移出 live set"
