"""Shared pytest fixtures for gateway tests."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import pytest


@pytest.fixture
def external_write_dir(tmp_path: Path):
    """Allocate a real test directory outside the sandbox's default writable roots."""
    from crew.security.models import ConversationPermissionMode
    from crew.security.policy import settings_for_mode

    profile = settings_for_mode(ConversationPermissionMode.REQUEST_APPROVAL, None).profile
    writable_roots = [entry.root.resolve() for entry in profile.filesystem]
    for parent in (tmp_path.parent, Path("/var/tmp"), Path.cwd(), Path.home()):
        parent = parent.resolve()
        if any(parent.is_relative_to(root) for root in writable_roots):
            continue
        if parent.is_dir() and os.access(parent, os.W_OK):
            with tempfile.TemporaryDirectory(prefix="ace-approval-", dir=parent) as directory:
                yield Path(directory)
            return
    pytest.fail("The test needs a writable directory outside the managed sandbox's temporary roots")


@pytest.fixture
def directory_link():
    """Create a live directory alias in a test-owned root without elevation."""
    def create(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as exc:
            if os.name != "nt" or exc.winerror != 1314:
                raise
            from _winapi import CreateJunction

            CreateJunction(str(target), str(link))

    return create

# Windows 的 PATH 里常同时存在 WSL 转发桩（%SystemRoot%\System32\bash.exe）和
# Git for Windows 的 bash；转发桩对 `bash -c` 直接失败，且输出是 UTF-16，
# 混进 PATH 顺序就会让 shell 契约用例红。这里按固定优先级定位真实 POSIX shell。
_WINDOWS_BASH_HINTS = (
    (r"%ProgramFiles%\Git\bin\bash.exe",),
    (r"%ProgramFiles(x86)%\Git\bin\bash.exe",),
    (r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe",),
)


def _find_posix_bash() -> str | None:
    """返回可执行 POSIX shell 的绝对路径；Windows 上跳过 WSL 转发桩。"""

    override = os.environ.get("ACE_POSIX_SHELL", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return str(candidate) if candidate.is_file() else None

    if os.name != "nt":
        return shutil.which("bash")

    for (hint,) in _WINDOWS_BASH_HINTS:
        expanded = os.path.expandvars(hint)
        if "%" not in expanded and Path(expanded).is_file():
            return expanded

    for root in os.environ.get("PATH", "").split(os.pathsep):
        if not root or Path(root).resolve() == Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32":
            continue
        candidate = Path(root) / "bash.exe"
        if candidate.is_file():
            return str(candidate)
    return None


@pytest.fixture(scope="session")
def posix_bash() -> str:
    """跑 shell 脚本契约用例的 bash；Windows 没装 Git Bash 时明确跳过。"""

    shell = _find_posix_bash()
    if shell is None:
        pytest.skip("需要 POSIX bash 才能验证 shell 脚本契约（Windows 请安装 Git for Windows）")
    return shell

@pytest.fixture
def auth_headers(monkeypatch) -> dict[str, str]:
    """Use the historical test owner without sending identity headers."""

    monkeypatch.setattr("crew.gateway.auth.LOCAL_OWNER_ACCOUNT_ID", "A:uid-a")
    return {}


@pytest.fixture(autouse=True)
def _restore_process_env():
    """每个用例后还原进程环境变量。

    load_config() 会把 config.yaml 的 runtime.crew_home 写回 os.environ
    ["CREW_HOME"]（生产桥接行为，见 crew/state/config.py）。未隔离的用例一旦
    触发 load_config，本机真实 home/模型配置会泄漏给后续所有用例——后续
    build_app 会读到真实模型并发起真实 HTTP 调用（慢、烧钱、顺序依赖失败）。
    """
    before = dict(os.environ)
    # 隔离运行器 shell 的 CREW_*/GATEWAY_* 变量：否则 legacy CREW_MODEL/CREW_BASE_URL
    # 覆盖路径会把本会话的模型配置泄进测试（每个用例需显式 setenv 自己需要的变量）。
    leaked = [
        name
        for name in (*os.environ, "GATEWAY_PORT")
        if name.startswith("CREW_") or name == "GATEWAY_PORT"
    ]
    saved = {name: os.environ.pop(name) for name in leaked if name in os.environ}
    yield
    os.environ.clear()
    os.environ.update(before)
    for name, value in saved.items():
        os.environ[name] = value


@pytest.fixture(autouse=True)
def _isolate_machine_config(tmp_path, monkeypatch, _restore_process_env):
    """每个用例与开发机真实配置文件隔离：config.ROOT 与 CREW_HOME 指向用例私有目录。

    开发态 load_config() 读 ROOT/config/config.yaml（被 gitignore 的本机文件，
    首跑自动从 config.yaml.example 复制），其中 auth.mode、security.enabled 等
    键随机器/版本漂移——例如 example 默认 auth.mode=email，所有依赖本机免登录
    的 gateway 用例会稳定吃到 401。CREW_HOME 未隔离的用例还会读写真实 ~/.crew
    （含 owner overlay config.yaml），是另一处非确定性来源。重定向后
    load_config() 只剩代码默认值；需要特定配置的用例自行 monkeypatch ROOT 或
    setenv CREW_HOME 覆盖（monkeypatch 后写生效，互不冲突）。
    """
    monkeypatch.setattr("crew.state.config.ROOT", tmp_path)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))


@pytest.fixture(autouse=True)
async def _close_live_crew_apps():
    """Close the scopes and stores of hosts that ran without ASGI lifespan."""
    yield

    from crew.app import _LIVE_APPS
    from crew.features.hooks import hook_registry
    from crew.tools.process_registry import process_registry

    async def shutdown_host(app):
        try:
            await app.shutdown()
        finally:
            # Propagate shutdown errors to this test, after releasing its stores
            # and removing the failed instance from subsequent tests' ownership.
            if app in _LIVE_APPS:
                _LIVE_APPS.remove(app)
                app._close_persistent_stores()

    try:
        async with AsyncExitStack() as hosts:
            for app in tuple(_LIVE_APPS):
                hosts.push_async_callback(shutdown_host, app)
    finally:
        process_registry.configure_task_runtime(None)
        hook_registry.clear()
