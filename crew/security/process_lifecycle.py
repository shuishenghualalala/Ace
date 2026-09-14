"""跨平台进程树生命周期属主 —— 安全边界的唯一 ProcessOwner。

全仓所有整树终止（后台进程注册表、宿主安全运行时、任务运行时）都消费这里的
ProcessOwner 三操作（signal / wait_for_exit / terminate_for_host_exit），
不再有第二份杀树实现。平台收容策略在此集中选择：

- Linux：探测 ``systemd-run --user --scope --collect``，可用则后台进程包进
  user scope（整组信号走 ``systemctl --user kill --kill-whom=all``，宿主被
  SIGKILL 也不留孤儿）；探测失败回退进程组（setsid + killpg）。
- macOS：进程组 + 启动时显式警告一次（无持久进程组属主，setsid 逃逸的孙进程
  不保证回收）。
- Windows：``CREATE_NEW_PROCESS_GROUP`` + ``taskkill /T /F``。

PID 复用防御：spawn 后现读进程启动时刻建立 ``ProcessIdentity`` 围栏，发信号
前重新读取比对，围栏失效即拒绝发信号。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from functools import partial
from typing import Any, Sequence

logger = logging.getLogger(__name__)

_WINDOWS_CREATE_FLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)

# 统一宽限（毫秒）：一个配置项两个语义——exit 后输出管道 drain 宽限 ==
# SIGTERM→SIGKILL 升级宽限，同时也是终末等待的上界。
DEFAULT_GRACE_MS = 3000


def resolve_grace_ms(value: int | float | None) -> int:
    """归一宽限配置：None 取默认；其余取非负整毫秒。"""
    if value is None:
        return DEFAULT_GRACE_MS
    return max(0, int(value))


class ProcessTerminationTimeout(TimeoutError):
    """进程树在升级宽限加终末等待内仍未退出（如不可杀进程）。"""


@dataclass(frozen=True)
class ProcessIdentity:
    """PID + 启动时刻：发信号前现读现比，防 PID 复用误杀。"""

    pid: int
    started: str | None  # None = 平台读取失败，围栏失效（维持尽力而为）


def read_process_start_time(pid: int) -> str | None:
    """读取进程启动时刻（平台原生来源），任何失败都返回 None。"""
    try:
        if os.name == "nt":
            import psutil

            return str(psutil.Process(pid).create_time())
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
                fields = handle.read().rpartition(")")[2].split()
            return fields[19]  # starttime，/proc/<pid>/stat 第 22 字段
        out = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
        value = out.stdout.strip()
        return value or None
    except Exception:  # noqa: BLE001 - 围栏是尽力而为
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# ---- 平台收容策略集中选择 ----

_CONTAINMENT_MODE: str | None = None
_CONTAINMENT_LOCK = threading.Lock()
_fallback_warning_issued = False


def _probe_systemd_scope() -> bool:
    if shutil.which("systemd-run") is None:
        return False
    try:
        result = subprocess.run(
            ["systemd-run", "--user", "--scope", "--collect", "--quiet", "/bin/true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _warn_weaker_containment(reason: str) -> None:
    global _fallback_warning_issued
    if _fallback_warning_issued:
        return
    _fallback_warning_issued = True
    logger.warning(
        "进程收容降级为进程组（%s）：setsid 逃逸的孙进程不保证终止或延迟退出",
        reason,
    )


def select_containment_mode() -> str:
    """集中选择收容策略：linux-scope / windows-group / posix-group（结果缓存）。"""
    global _CONTAINMENT_MODE
    with _CONTAINMENT_LOCK:
        if _CONTAINMENT_MODE is None:
            if os.name == "nt":
                _CONTAINMENT_MODE = "windows-group"
            elif sys.platform.startswith("linux") and _probe_systemd_scope():
                _CONTAINMENT_MODE = "linux-scope"
            else:
                _CONTAINMENT_MODE = "posix-group"
        mode = _CONTAINMENT_MODE
    if mode == "posix-group":
        if sys.platform == "darwin":
            _warn_weaker_containment("macOS 无持久进程组属主")
        elif sys.platform.startswith("linux"):
            _warn_weaker_containment("user systemd scope 不可用")
    return mode


def reset_containment_mode_for_tests() -> None:
    """测试边界：清掉收容模式缓存与一次性警告。"""
    global _CONTAINMENT_MODE, _fallback_warning_issued
    with _CONTAINMENT_LOCK:
        _CONTAINMENT_MODE = None
    _fallback_warning_issued = False


def isolated_process_kwargs() -> dict[str, Any]:
    """Start a host process in a group that can be terminated as one tree."""
    if os.name == "nt":
        return {"creationflags": _WINDOWS_CREATE_FLAGS}
    return {"start_new_session": True}


def wrap_argv_for_containment(argv: Sequence[str]) -> tuple[list[str], str | None]:
    """linux-scope 模式下把 argv 包进 user systemd scope。

    返回 (argv, scope_unit)：其余模式原样返回 argv，scope_unit 为 None
    （按进程组终止）。仅后台长驻进程需要 scope 收容；短句柄的安全运行时
    helper 走 isolated_process_kwargs 的进程组即可。
    """
    if select_containment_mode() != "linux-scope":
        return list(argv), None
    unit = f"ace-proc-{uuid.uuid4().hex[:12]}.scope"
    return [
        "systemd-run", "--user", "--scope", "--collect", "--quiet",
        "--unit", unit, "--", *argv,
    ], unit


# ---- 平台原语 ----

def _taskkill_tree(pid: int) -> bool:
    """Windows 整树强杀；taskkill 退出码不检查（等价 POSIX 对 ESRCH 的容忍）。"""
    if pid <= 0:
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _scope_signal(scope_unit: str, sig: int) -> bool:
    """systemd user scope 整组信号，杀死单元内全部进程（含逃逸后代）。"""
    try:
        subprocess.run(
            [
                "systemctl", "--user", "kill",
                "--kill-whom=all", f"--signal={signal.Signals(sig).name}",
                scope_unit,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def scope_alive(scope_unit: str) -> bool:
    """scope 单元是否仍有存活进程（崩溃恢复认领无句柄时的存活检测）。"""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ActiveState", "--value", scope_unit],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() in {"active", "activating", "reloading"}
    except Exception:  # noqa: BLE001
        return False


class ProcessOwner:
    """一个受管进程树的唯一属主。

    三操作对应宿主全生命周期的三个时机：signal（TERM/KILL 升级）、
    wait_for_exit（结果等待，事件驱动）、terminate_for_host_exit（宿主退出
    同步强杀，不启动计时器）。
    """

    def __init__(
        self,
        pid: int,
        *,
        pgid: int | None = None,
        process: Any = None,
        identity: ProcessIdentity | None = None,
        scope_unit: str | None = None,
    ) -> None:
        self.pid = pid
        self.pgid = pgid if pgid is not None else pid
        self.process = process
        self.identity = identity
        self.scope_unit = scope_unit

    @classmethod
    def capture(
        cls,
        process: Any,
        *,
        pgid: int | None = None,
        scope_unit: str | None = None,
    ) -> ProcessOwner:
        """spawn 后立即调用：现读启动时刻建立 PID 复用围栏。"""
        return cls(
            process.pid,
            pgid=pgid,
            process=process,
            identity=ProcessIdentity(process.pid, read_process_start_time(process.pid)),
            scope_unit=scope_unit,
        )

    @classmethod
    def for_pid(
        cls,
        pid: int,
        *,
        pgid: int | None = None,
        scope_unit: str | None = None,
    ) -> ProcessOwner:
        """无句柄场景（崩溃恢复认领 / 任务运行时只有 pid）的尽力而为属主。"""
        return cls(
            pid,
            pgid=pgid,
            identity=ProcessIdentity(pid, read_process_start_time(pid)),
            scope_unit=scope_unit,
        )

    def _fenced(self) -> bool:
        if self.identity is None or self.identity.started is None:
            return True
        return read_process_start_time(self.pid) == self.identity.started

    def exited(self) -> bool:
        if self.scope_unit:
            return not scope_alive(self.scope_unit)
        if self.process is not None:
            return self.process.returncode is not None
        return not _pid_alive(self.pid)

    def signal(self, sig: int) -> bool:
        """向整树发信号；PID 已被复用（围栏失效）或已退出时拒绝。"""
        if self.scope_unit:
            return _scope_signal(self.scope_unit, sig)
        if self.exited():
            return False
        if not self._fenced():
            return False
        if os.name == "nt":
            return _taskkill_tree(self.pid)
        try:
            os.killpg(self.pgid, sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    async def wait_for_exit(self, timeout: float | None = None) -> bool:
        """结果等待：事件驱动等直接句柄退出；无句柄退化为有界轮询（仅清理路径）。

        spawn_tracked 产出的句柄经协议 future 拿到真 exit 事件（不受管道 EOF
        耦合拖住）；其余句柄回退原生 wait()。
        """
        exited_future = getattr(getattr(self.process, "_protocol", None), "exited_future", None)
        if exited_future is not None:
            try:
                await asyncio.wait_for(exited_future, timeout=timeout)
                return True
            except asyncio.TimeoutError:
                return False
        if self.process is None:
            return self._poll_dead(timeout)
        try:
            if timeout is None:
                await self.process.wait()
                return True
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def wait_for_exit_sync(self, timeout: float | None = None) -> bool:
        """线程侧的结果等待（reader 线程等句柄结算）。"""
        if self.process is not None:
            try:
                self.process.wait(timeout=timeout)
                return True
            except Exception:  # noqa: BLE001 - 含超时
                return False
        return self._poll_dead(timeout)

    def _poll_dead(self, timeout: float | None) -> bool:
        deadline = time.monotonic() + timeout if timeout is not None else None
        while True:
            if not _pid_alive(self.pid):
                return True
            if deadline is None or time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def terminate_for_host_exit(self) -> None:
        """宿主可观察退出点的同步强杀：不启动计时器、不 await、逐个容错。"""
        if self.pid <= 0 and not self.scope_unit:
            return
        if self.scope_unit:
            _scope_signal(self.scope_unit, _SIGKILL)
            return
        if os.name == "nt":
            _taskkill_tree(self.pid)
            return
        if not self._fenced():
            return
        try:
            os.killpg(self.pgid, _SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(self.pid, _SIGKILL)
            except OSError:
                pass

    async def terminate(self, *, grace_ms: int | float | None = None) -> None:
        """TERM → 宽限 → KILL → 终末有界等待；终末超时返回错误而非永久挂起。

        不可杀进程（如 D-state）在升级宽限 + 终末等待耗尽后抛
        ProcessTerminationTimeout，调用方决定如何报告。
        """
        grace = resolve_grace_ms(grace_ms) / 1000
        if self.exited():
            return
        self.signal(signal.SIGTERM)
        if await self.wait_for_exit(timeout=grace):
            return
        self.signal(_SIGKILL)
        if await self.wait_for_exit(timeout=grace):
            return
        raise ProcessTerminationTimeout(
            f"进程树 {self.pid} 在 SIGKILL 后 {grace}s 内仍未退出（不可杀进程）"
        )


# ---- 事件驱动的结果等待 ----

class _ExitNotifiedProtocol(asyncio.subprocess.SubprocessStreamProtocol):
    """补 process_exited 即时通知：原生 Process.wait() 在 PIPE 模式下被管道
    EOF 耦合（继承 fd 的孙进程持管时永久挂起）；退出事件经 future 先行唤醒，
    管道排干走独立宽限。"""

    def __init__(self, limit: int, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(limit=limit, loop=loop)
        self.exited_future: asyncio.Future[None] = loop.create_future()

    def process_exited(self) -> None:
        super().process_exited()
        if not self.exited_future.done():
            self.exited_future.set_result(None)


async def spawn_tracked(
    *args: Any,
    limit: int = 2**16,
    **kwargs: Any,
) -> tuple[asyncio.subprocess.Process, asyncio.Future[None]]:
    """``create_subprocess_exec`` 等价物，额外返回 exit 事件 future。

    结果等待（exit 事件，事件驱动、必然结算）与管道排干（exit 后宽限收尾）
    由此分离：调用方 await future 拿退出，再按 grace 排干管道。
    """
    loop = asyncio.get_running_loop()
    protocol_factory = partial(_ExitNotifiedProtocol, limit=limit, loop=loop)
    transport, protocol = await loop.subprocess_exec(protocol_factory, *args, **kwargs)
    return asyncio.subprocess.Process(transport, protocol, loop), protocol.exited_future


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_ms: int | float | None = None,
) -> None:
    """异步整树终止：launch.py / runtime_client 的共享入口。

    清理路径语义：不可杀进程的终止超时只记 warning，不遮蔽调用方原始异常；
    需要错误报告的调用方直接用 ProcessOwner.terminate。
    """
    owner = ProcessOwner.capture(process)
    try:
        await owner.terminate(grace_ms=grace_ms)
    except ProcessTerminationTimeout:
        logger.warning("进程树 %s 终止超时（不可杀进程）", process.pid)
    except Exception:  # noqa: BLE001 - 清理失败不遮蔽调用方原始异常
        logger.warning("进程树 %s 终止失败", process.pid, exc_info=True)


def terminate_process_tree_sync(
    pid: int,
    *,
    grace_ms: int | float | None = None,
) -> None:
    """线程侧整树终止（任务运行时取消 / 注册表杀进程）。

    先发 TERM 立即返回（不阻塞调用线程）；宽限由属主升级路径负责。
    """
    owner = ProcessOwner.for_pid(pid)
    owner.signal(signal.SIGTERM)
