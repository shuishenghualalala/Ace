"""The first managed command can wait for its own Desktop's in-flight Windows setup."""

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from crew.security import windows_setup
from crew.security.runtime_client import NativeRuntimeError, RuntimeErrorCode


@pytest.fixture
def desktop_setup(tmp_path, monkeypatch):
    helper = tmp_path / "ace-security-runtime.exe"
    helper.touch()
    state_dir = tmp_path / "security"
    state_dir.mkdir()
    marker = tmp_path / "desktop-launch"
    monkeypatch.setattr(windows_setup, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("ACE_SECURITY_RUNTIME", str(helper))
    monkeypatch.setenv("ACE_SECURITY_STATE_DIR", str(state_dir))
    monkeypatch.setenv("ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR", str(marker))
    return (str(helper.resolve()),), marker


@pytest_asyncio.fixture
async def start_pending(monkeypatch):
    # Publish completion or cancel only after the caller actually saw pending.
    loop = asyncio.get_running_loop()
    observed = asyncio.Event()
    read_state = windows_setup._state

    def read_and_notify(directory):
        state = read_state(directory)
        if state == "pending":
            loop.call_soon_threadsafe(observed.set)
        return state

    monkeypatch.setattr(windows_setup, "_state", read_and_notify)
    tasks = []

    async def start(coroutine):
        task = asyncio.create_task(coroutine)
        tasks.append(task)
        await asyncio.wait_for(observed.wait(), 5)
        assert not task.done()
        return task

    try:
        yield start
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["no_desktop", "not_started", "other_runtime"])
async def test_unrelated_or_idle_setup_keeps_full_budget(desktop_setup, monkeypatch, scenario):
    argv, marker = desktop_setup
    if scenario == "no_desktop":
        marker.mkdir()
        monkeypatch.delenv("ACE_DESKTOP_WINDOWS_SANDBOX_SETUP_DIR")
    elif scenario == "other_runtime":
        marker.mkdir()
        argv = (str(marker.parent / "another-helper.exe"),)

    remaining = await asyncio.wait_for(
        windows_setup.wait_for_windows_sandbox_setup(argv, timeout=30), 5,
    )
    assert remaining == 30


@pytest.mark.asyncio
async def test_ready_deducts_wait_from_command_budget(desktop_setup, start_pending):
    argv, marker = desktop_setup
    marker.mkdir()
    budget = 30
    wait = await start_pending(windows_setup.wait_for_windows_sandbox_setup(argv, timeout=budget))
    (marker / "ready").touch()
    remaining = await asyncio.wait_for(wait, 5)
    assert 0 < remaining < budget


@pytest.mark.asyncio
async def test_failed_setup_stops_waiting_command(desktop_setup, start_pending):
    argv, marker = desktop_setup
    marker.mkdir()
    wait = await start_pending(windows_setup.wait_for_windows_sandbox_setup(argv, timeout=None))
    (marker / "failed").touch()
    with pytest.raises(NativeRuntimeError, match="尚未启动") as caught:
        await asyncio.wait_for(wait, 5)
    assert caught.value.code == RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_stop_while_pending_prevents_late_launch(desktop_setup, start_pending):
    argv, marker = desktop_setup
    marker.mkdir()
    launches = []

    async def command():
        await windows_setup.wait_for_windows_sandbox_setup(argv, timeout=None)
        launches.append(argv)

    task = await start_pending(command())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    (marker / "ready").touch()
    assert not launches


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout,setup_limit", [(0.25, 30), (None, 0.25)])
async def test_setup_wait_obeys_command_and_setup_limits(desktop_setup, monkeypatch, timeout, setup_limit):
    argv, marker = desktop_setup
    marker.mkdir()
    monkeypatch.setattr(windows_setup, "_SETUP_MAX_WAIT_SECONDS", setup_limit)
    with pytest.raises(NativeRuntimeError, match="超时") as caught:
        await asyncio.wait_for(windows_setup.wait_for_windows_sandbox_setup(argv, timeout=timeout), 5)
    assert caught.value.code == RuntimeErrorCode.SANDBOX_UNAVAILABLE


@pytest.mark.asyncio
async def test_non_waiting_caller_fails_while_pending(desktop_setup):
    argv, marker = desktop_setup
    marker.mkdir()
    with pytest.raises(NativeRuntimeError, match="尚未启动") as caught:
        await asyncio.wait_for(
            windows_setup.wait_for_windows_sandbox_setup(argv, timeout=30, allow_wait=False), 5,
        )
    assert caught.value.code == RuntimeErrorCode.SANDBOX_UNAVAILABLE
