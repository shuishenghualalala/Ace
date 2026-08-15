"""CUA Driver 一键安装与 MCP 接入服务。

用户在桌面端点击「安装 CUA MCP」后，后端异步完成：
1. 平台检测；
2. 安装 CUA Driver 二进制；
3. 安装系统依赖（Linux）；
4. 启动并保活 daemon；
5. 更新 config.yaml；
6. 热重载 MCPClientManager。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import platform
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crew.security.launch import (
    ProcessLaunch,
    bind_process_launch_task,
    current_process_launch,
    minimal_inherited_environment,
    validate_process_launch,
)
from crew.security.outbound import OutboundDenied, OutboundHttpClient
from crew.state.logging import get_logger
from crew.tools.file_utils import read_verified_bytes
from crew.tools.redact import safe_public_error

log = get_logger("tools.cua_setup")
_CUA_HTTP = OutboundHttpClient()
_MAX_SETUP_TASKS = 32
_MAX_CUA_BINARY_BYTES = 512 * 1024 * 1024


# 安装脚本（Linux curl | bash / Windows irm | iex）
_CUA_INSTALL_URL_LINUX = "https://cua.ai/driver/install.sh"
_CUA_INSTALL_URL_WINDOWS = "https://cua.ai/driver/install.ps1"

# 会强制动态链接器加载「打包内嵌」库（libssl/libcrypto 等）的环境变量。
# gateway 是 PyInstaller --onedir 产物，其 bootloader 会把 _internal/ 写进
# LD_LIBRARY_PATH 以加载自带的 _ssl/_hashlib 扩展；Electron 启动 gateway 时也
# 透传了 process.env（desktop/src/main/index.ts 的 { ...process.env }）。
# 该变量被 curl / apt / gsettings 等系统二进制继承后，系统 libcurl.so.4 会去加载
# 打包的（未打国密补丁的）libssl.so.1.1，缺 GMTLSv1_1_client_method 等 GM 符号 →
# `curl: relocation error ... OPENSSL_1_1_0 not defined in file libssl.so.1.1`。
# 信创 UOS/Kylin 系统自带的 libcurl/libssl 均为国密改造版，必须用系统的，故调用
# 系统工具前要剥离这些动态链接器变量，让其按 /etc/ld.so.cache 解析到系统库。
# 注意：只剥离 *副本*，不动 gateway 自己的 os.environ（否则 gateway 自身 import
# _ssl/_hashlib 会断）。PATH 保留（含 _internal/runtimes/*/bin，技能脚本要用），
# PATH 只影响可执行文件查找，不会造成 .so 版本错配。
_LD_TAINT_VARS = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "LD_AUDIT",
    "DYLD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "LD_LIBRARY_PATH_64",
)
_CUA_RUNTIME_ENV_NAMES = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "DESKTOP_SESSION",
        "DISPLAY",
        "GSETTINGS_SCHEMA_DIR",
        "NUMBER_OF_PROCESSORS",
        "POWERSHELL_DISTRIBUTION_CHANNEL",
        "PROCESSOR_ARCHITECTURE",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "WAYLAND_DISPLAY",
        "XAUTHORITY",
        "XDG_CURRENT_DESKTOP",
        "XDG_DATA_DIRS",
        "XDG_RUNTIME_DIR",
    }
)


def _clean_system_env() -> dict[str, str]:
    """返回剥离了动态链接器污染变量的环境副本，供调用系统二进制时使用。"""
    env = minimal_inherited_environment()
    env.update(
        {
            name: os.environ[name]
            for name in _CUA_RUNTIME_ENV_NAMES
            if name in os.environ
        }
    )
    for var in _LD_TAINT_VARS:
        env.pop(var, None)
    return env


def _download_verified_installer(url: str, target: Path, expected_sha256: str) -> None:
    """Download a small HTTPS installer script and verify its pinned SHA-256."""
    if not url.lower().startswith("https://"):
        raise RuntimeError("严格安全约束要求 CUA 安装脚本使用 HTTPS")
    expected = expected_sha256.strip().lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise RuntimeError("CUA 安装脚本 SHA-256 配置格式无效")
    try:
        response = _CUA_HTTP.fetch(
            url,
            method="GET",
            headers={"User-Agent": "Crew"},
            timeout=30.0,
            max_bytes=4 * 1024 * 1024,
            max_redirects=0,
        )
    except OutboundDenied as exc:
        raise RuntimeError("CUA 安装脚本网络策略拒绝下载") from exc
    if response.status != 200:
        raise RuntimeError(f"CUA 安装脚本下载失败：HTTP {response.status}")
    data = response.body
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError("CUA 安装脚本 SHA-256 校验失败")
    target.write_bytes(data)


@dataclass
class SetupStep:
    name: str
    status: str = "pending"  # pending / running / success / failed / skipped
    message: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class SetupTask:
    task_id: str
    platform: str
    status: str = "pending"  # pending / running / success / failed / cancelled
    steps: list[SetupStep] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _cancelled: bool = field(default=False, repr=False)

    def add_log(self, line: str) -> None:
        self.log.append(safe_public_error(line, "CUA Driver 输出已隐藏", limit=2_000))

    def update_step(self, name: str, status: str, message: str = "") -> None:
        safe_message = safe_public_error(message, "CUA Driver 状态已隐藏", limit=500) if message else ""
        for step in self.steps:
            if step.name == name:
                step.status = status
                step.message = safe_message
                step.ts = time.time()
                return
        self.steps.append(SetupStep(name=name, status=status, message=safe_message))

    def finish(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = safe_public_error(error, "CUA Driver 安装失败", limit=500) if error else None
        self.finished_at = time.time()


class CuaDriverSetupService:
    """管理 CUA Driver 的安装任务。"""

    def __init__(self) -> None:
        self._tasks: dict[str, SetupTask] = {}
        self._running: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # 任务管理
    # ------------------------------------------------------------------ #
    def start_setup(
        self,
        *,
        crew: Any,
        process_launch: ProcessLaunch,
        force_reinstall: bool = False,
        start_daemon: bool = True,
    ) -> SetupTask:
        """启动新的安装任务。"""
        validate_process_launch(process_launch)
        if any(not running.done() for running in self._running.values()):
            raise RuntimeError("已有 CUA Driver 安装任务正在运行")
        if len(self._tasks) >= _MAX_SETUP_TASKS:
            finished = sorted(
                (
                    task
                    for task in self._tasks.values()
                    if task.status in {"success", "failed", "cancelled"}
                ),
                key=lambda task: task.finished_at or task.started_at,
            )
            while len(self._tasks) >= _MAX_SETUP_TASKS and finished:
                self._tasks.pop(finished.pop(0).task_id, None)
        if len(self._tasks) >= _MAX_SETUP_TASKS:
            raise RuntimeError("CUA Driver 安装任务容量已满")
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        plat = _detect_platform()
        task = SetupTask(task_id=task_id, platform=plat)
        self._tasks[task_id] = task
        bound_launch = bind_process_launch_task(process_launch, task_id)

        async def _run() -> None:
            token = current_process_launch.set(bound_launch)
            try:
                await self._do_setup(task, crew, force_reinstall, start_daemon)
            except asyncio.CancelledError:
                task.finish("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("CUA Driver 安装任务异常")
                task.finish("failed", str(exc))
            finally:
                current_process_launch.reset(token)
                self._running.pop(task_id, None)

        self._running[task_id] = asyncio.create_task(_run())
        return task

    def get_task(self, task_id: str) -> SetupTask | None:
        return self._tasks.get(task_id)

    async def cancel_task(self, task_id: str) -> bool:
        running = self._running.get(task_id)
        if running is None or running.done():
            return False
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        task = self._tasks.get(task_id)
        if task is not None:
            task.finish("cancelled")
        return True

    def list_tasks(self) -> list[SetupTask]:
        return list(self._tasks.values())

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    async def _do_setup(
        self,
        task: SetupTask,
        crew: Any,
        force_reinstall: bool,
        start_daemon: bool,
    ) -> None:
        task.status = "running"

        # 1. 检测平台
        task.update_step("detect_platform", "running")
        plat = task.platform
        if plat not in ("linux", "windows", "macos"):
            raise RuntimeError(f"不支持的操作系统: {plat}")
        task.update_step("detect_platform", "success", plat)

        # 2. 安装二进制
        task.update_step("install_binary", "running")
        binary_path = await self._ensure_binary(task, plat, force_reinstall)
        binary_digest = _required_cua_binary_digest(plat)
        _verify_cua_binary(binary_path, binary_digest)
        version = await _run_command([binary_path, "--version"], timeout=10, env=_clean_system_env())
        task.update_step("install_binary", "success", version.strip() or str(binary_path))

        # 3. 安装系统依赖（仅 Linux）
        if plat == "linux":
            task.update_step("install_deps", "running")
            await self._install_linux_deps(task)
            task.update_step("install_deps", "success")

        # 4. 启动 daemon
        if start_daemon:
            task.update_step("start_daemon", "running")
            await self._start_daemon(task, plat, binary_path)
            task.update_step("start_daemon", "success", "daemon ready")

        # 5. 更新 config.yaml
        task.update_step("update_config", "running")
        cfg = crew.config
        previous_servers = copy.deepcopy(cfg.mcp_servers)
        persisted = False
        try:
            cfg.set_mcp_server(
                "cua-driver",
                {
                    "command": binary_path,
                    "command_sha256": binary_digest,
                    "args": ["mcp"],
                    "env": {},
                },
            )
            cfg.persist_mcp_servers()
            persisted = True
            task.update_step(
                "update_config",
                "success",
                "mcp_servers.cua-driver enabled",
            )

            # 6. 热重载 MCP
            task.update_step("reload_mcp", "running")
            await crew.reload_mcp_manager()
        except BaseException as exc:
            cfg.mcp_servers.clear()
            cfg.mcp_servers.update(previous_servers)
            rollback_failures: list[str] = []
            if persisted:
                try:
                    cfg.persist_mcp_servers()
                except BaseException as rollback_exc:
                    rollback_failures.append(f"config: {rollback_exc}")
                try:
                    await crew.reload_mcp_manager()
                except BaseException as rollback_exc:
                    rollback_failures.append(f"runtime: {rollback_exc}")
            if rollback_failures:
                raise RuntimeError(
                    "CUA MCP 配置失败且回滚未完成: "
                    + "; ".join(rollback_failures)
                ) from exc
            raise
        tool_names = [
            name for name in crew.registry.names() if name.startswith("cua-driver__")
        ]
        task.update_step("reload_mcp", "success", f"{len(tool_names)} tools registered")

        task.finish("success")

    # ------------------------------------------------------------------ #
    # 平台相关
    # ------------------------------------------------------------------ #
    async def _ensure_binary(
        self, task: SetupTask, plat: str, force_reinstall: bool
    ) -> str:
        expected_binary_digest = _required_cua_binary_digest(plat)
        binary = _find_cua_binary(plat)
        if binary and not force_reinstall:
            _verify_cua_binary(binary, expected_binary_digest)
            task.add_log(f"已找到 cua-driver: {binary}")
            return binary

        # Linux 冻结态优先用预制二进制：信创系统 glibc 太旧(buster 2.28)，
        # 联网 curl|bash 装的官方 gnu 版需 glibc≥2.30 跑不起来(GLIBC_2.29 not found)。
        # 预制版随 .deb 内嵌、glibc≤2.28 兼容，既不联网也不受 glibc 限制。
        # force_reinstall 时预制版也无法重装(它是打包产物)，直接复用即可。
        if plat == "linux" and getattr(sys, "frozen", False):
            prebaked = _find_cua_binary(plat)
            if prebaked:
                _verify_cua_binary(prebaked, expected_binary_digest)
                task.add_log(f"使用预制 cua-driver（信创 glibc 兼容版）: {prebaked}")
                return prebaked
            task.add_log("未找到预制 cua-driver，回退联网下载（注意：信创系统可能因 glibc 过旧失败）")

        if force_reinstall and binary:
            task.add_log("force_reinstall=True，重新安装")

        if plat in ("linux", "macos"):
            # macOS 与 Linux 复用同一个 install.sh（curl|bash，bash 脚本，跨 *nix 通用）。
            # 见 SKILL.md：「/bin/bash -c "$(curl -fsSL https://cua.ai/driver/install.sh)"」。
            script_url = _CUA_INSTALL_URL_LINUX
            checksum_env = "ACE_CUA_INSTALL_SHA256_LINUX"
        else:
            script_url = _CUA_INSTALL_URL_WINDOWS
            checksum_env = "ACE_CUA_INSTALL_SHA256_WINDOWS"

        checksum = os.getenv(checksum_env, "").strip()
        if not checksum:
            raise RuntimeError(
                f"CUA 安装已阻止未验证脚本；请配置 {checksum_env} SHA-256"
            )
        with tempfile.TemporaryDirectory(prefix="crew-cua-") as temporary_dir:
            posix_installer = plat in {"linux", "macos"}
            suffix = ".sh" if posix_installer else ".ps1"
            script_path = Path(temporary_dir) / f"install{suffix}"
            await asyncio.to_thread(
                _download_verified_installer,
                script_url,
                script_path,
                checksum,
            )
            cmd = [
                "/bin/bash",
                str(script_path),
            ] if posix_installer else [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script_path),
            ]
            task.add_log(f"执行已校验安装脚本: {script_url}")
            await _run_command_streaming(
                cmd,
                timeout=300,
                stdout_cb=task.add_log,
                stderr_cb=lambda line: task.add_log(f"[stderr] {line}"),
                env=_clean_system_env(),
            )

        # 安装后重新定位（gateway 进程 PATH 不会动态刷新，shutil.which 可能仍查不到）
        binary = _find_cua_binary(plat)
        if not binary:
            raise RuntimeError("安装完成后仍找不到 cua-driver 命令，请检查 PATH")
        _verify_cua_binary(binary, expected_binary_digest)
        task.add_log(f"已定位 cua-driver: {binary}")
        return binary

    async def _install_linux_deps(self, task: SetupTask) -> None:
        """安装 Linux 系统依赖。"""
        # 检测包管理器
        pm = None
        if shutil.which("apt"):
            pm = "apt"
        elif shutil.which("dnf"):
            pm = "dnf"
        elif shutil.which("pacman"):
            pm = "pacman"

        if not pm:
            task.add_log("无法检测包管理器，跳过依赖安装；如 AT-SPI 未安装请手动处理")
            return

        task.add_log(f"检测到包管理器: {pm}")

        if pm == "apt":
            cmd = ["sudo", "apt", "install", "-y", "at-spi2-core"]
        elif pm == "dnf":
            cmd = ["sudo", "dnf", "install", "-y", "at-spi2-core"]
        else:
            cmd = ["sudo", "pacman", "-S", "--noconfirm", "at-spi2-core"]

        # 检测是否已安装
        if await _is_at_spi_installed():
            task.add_log("at-spi2-core 已安装")
        else:
            task.add_log(f"执行: {' '.join(cmd)}")
            try:
                await _run_command_streaming(
                    cmd,
                    timeout=120,
                    stdout_cb=task.add_log,
                    stderr_cb=lambda line: task.add_log(f"[stderr] {line}"),
                    # apt/dnf/pacman 是系统二进制，同样剥离 LD_LIBRARY_PATH
                    env=_clean_system_env(),
                )
            except Exception as exc:  # noqa: BLE001
                task.add_log(f"依赖安装失败（可忽略，后续可手动安装）: {exc}")

        # GNOME 桌面开启 toolkit accessibility
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        if "gnome" in desktop:
            try:
                await _run_command(
                    ["gsettings", "set", "org.gnome.desktop.interface", "toolkit-accessibility", "true"],
                    timeout=10,
                    env=_clean_system_env(),
                )
                task.add_log("已启用 GNOME toolkit-accessibility")
            except Exception as exc:  # noqa: BLE001
                task.add_log(f"GNOME 设置失败: {exc}")

    async def _start_daemon(self, task: SetupTask, plat: str, binary: str) -> None:
        """启动 daemon 并等待就绪。"""
        # 先检查是否已有 daemon 在运行
        try:
            status = await _run_command([binary, "status"], timeout=10, env=_clean_system_env())
            task.add_log(f"daemon 状态: {status.strip()}")
            if "running" in status.lower() or "ok" in status.lower():
                return
        except Exception as exc:  # noqa: BLE001
            task.add_log(f"daemon 未运行或状态异常: {exc}")

        if plat == "linux":
            await self._start_daemon_linux(task, binary)
        elif plat == "macos":
            await self._start_daemon_macos(task, binary)
        else:
            await self._start_daemon_windows(task, binary)

    async def _start_daemon_linux(self, task: SetupTask, binary: str) -> None:
        """Linux 下启动 daemon。

        优先尝试 systemd --user；失败则前台/后台启动。
        """
        service_name = "cua-driver"
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_file = service_dir / f"{service_name}.service"

        systemd_available = shutil.which("systemctl") is not None
        if systemd_available:
            try:
                service_dir.mkdir(parents=True, exist_ok=True)
                service_file.write_text(
                    "[Unit]\n"
                    "Description=CUA Driver daemon\n\n"
                    "[Service]\n"
                    f"ExecStart={binary} serve\n"
                    "Restart=on-failure\n\n"
                    "[Install]\n"
                    "WantedBy=default.target\n",
                    encoding="utf-8",
                )
                await _run_command(["systemctl", "--user", "daemon-reload"], timeout=15, env=_clean_system_env())
                await _run_command(["systemctl", "--user", "enable", "--now", service_name], timeout=15, env=_clean_system_env())
                task.add_log("已启用 systemd --user cua-driver 服务")
            except Exception as exc:  # noqa: BLE001
                task.add_log(f"systemd 方式启动失败，回退直接启动: {exc}")
                systemd_available = False

        if not systemd_available:
            pid = _spawn_daemon(
                [binary, "serve"],
                env=_clean_system_env(),
            )
            task.add_log(f"直接启动 daemon，pid={pid}")

        # 等待 daemon 就绪
        await _wait_for_daemon(binary, timeout=30)

    async def _start_daemon_macos(self, task: SetupTask, binary: str) -> None:
        """macOS 下启动 daemon。

        install.sh 在 macOS 上安装为 .app bundle（CuaDriver.app），故用 ``open -a``
        启动而非直接执行二进制（见 SKILL.md「open -n -g -a CuaDriver --args serve」）。
        macOS 没有 Linux 的 systemd --user，也没有 Windows 的 autostart 动词
        （PLATFORMS.md：「Linux 没有 macOS 上的 autostart 动词」），故直接后台起
        CuaDriver.app 并等待 status 就绪。首次启动需用户在「系统设置 → 隐私与安全性」
        授予辅助功能权限，否则 daemon 起来但 AX 调用不可用（不阻断安装流程）。
        """
        # 若已有 daemon 在跑则跳过（_start_daemon 入口已查过 status，这里兜底）
        try:
            status = await _run_command([binary, "status"], timeout=10, env=_clean_system_env())
            if "running" in status.lower() or "ok" in status.lower():
                task.add_log("macOS daemon 已在运行")
                return
        except Exception as exc:  # noqa: BLE001
            task.add_log(f"macOS daemon 未运行: {exc}")

        pid = _spawn_daemon(
            ["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve"],
            env=_clean_system_env(),
        )
        task.add_log(f"macOS daemon 启动（CuaDriver.app），pid={pid}")

        await _wait_for_daemon(binary, timeout=30)

    async def _start_daemon_windows(self, task: SetupTask, binary: str) -> None:
        """Windows 下启动 daemon：autostart + 当前会话启动。"""
        try:
            await _run_command([binary, "autostart", "enable"], timeout=15)
            task.add_log("已启用 cua-driver autostart")
        except Exception as exc:  # noqa: BLE001
            task.add_log(f"autostart enable 失败（可继续）: {exc}")

        # 当前会话启动
        pid = _spawn_daemon(
            [binary, "serve"],
            env=_clean_system_env(),
        )
        task.add_log(f"Windows daemon 启动，pid={pid}")

        await _wait_for_daemon(binary, timeout=30)

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    async def status(self, registry: Any) -> dict[str, Any]:
        """返回当前 CUA Driver 安装与 MCP 注册状态。"""
        binary = _find_cua_binary(_detect_platform())
        installed = binary is not None
        binary_verified = False
        verification_error = ""
        version = ""
        daemon_running = False
        if installed:
            try:
                _verify_cua_binary(binary, _required_cua_binary_digest(_detect_platform()))
                binary_verified = True
            except (OSError, RuntimeError, ValueError) as exc:
                verification_error = safe_public_error(exc, "驱动验证失败")
            if binary_verified:
                try:
                    version = (
                        await _run_command(
                            [binary, "--version"],
                            timeout=10,
                            env=_clean_system_env(),
                        )
                    ).strip()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    status_out = (
                        await _run_command(
                            [binary, "status"],
                            timeout=10,
                            env=_clean_system_env(),
                        )
                    ).strip()
                    daemon_running = (
                        "running" in status_out.lower()
                        or "ok" in status_out.lower()
                    )
                except Exception:  # noqa: BLE001
                    pass

        tool_names = [name for name in registry.names() if name.startswith("cua-driver__")]
        return {
            "ok": True,
            "installed": installed,
            "binary": binary,
            "binary_verified": binary_verified,
            "verification_error": verification_error,
            "version": version,
            "daemon_running": daemon_running,
            "mcp_enabled": any(
                name.startswith("cua-driver__") for name in registry.names()
            ),
            "tools_registered": tool_names,
        }


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #
def _required_cua_binary_digest(plat: str) -> str:
    env_name = f"ACE_CUA_BINARY_SHA256_{plat.upper()}"
    value = os.environ.get(env_name, "").strip().casefold()
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise RuntimeError(f"未配置有效的 {env_name}，拒绝执行 CUA Driver")
    return value


def _verify_cua_binary(binary: str, expected_digest: str) -> None:
    try:
        read_verified_bytes(
            Path(binary),
            max_bytes=_MAX_CUA_BINARY_BYTES,
            expected_digest=expected_digest,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("CUA Driver 二进制完整性验证失败") from exc


def _detect_platform() -> str:
    sysname = platform.system().lower()
    if sysname == "linux":
        return "linux"
    if sysname == "windows":
        return "windows"
    if sysname == "darwin":
        return "macos"
    return sysname


def _resolve_junction(path: str) -> str:
    """穿透路径中任意层级的 NTFS junction，返回真实路径。

    cua-driver 的 bin 是 junction（bin → current → releases/<version>）。文件路径
    `...\\bin\\cua-driver.exe` 的父目录含 junction，直接对文件 readlink 读不到。
    这里逐级检查路径每一层父目录，遇到 junction 就用 target 替换并继续，最终拼出
    不含任何 junction 的真实路径。

    用 os.readlink（读 reparse point 元数据，不遍历 target，不抛 448），而非
    Path.resolve()/os.path.realpath（3.13+ 可能因打开 mountpoint 抛 448）。
    """
    current = os.path.normpath(path)
    seen: set[str] = set()
    for _ in range(32):
        if current in seen:
            break
        seen.add(current)
        try:
            target = os.readlink(current)
        except (OSError, ValueError):
            parent = os.path.dirname(current)
            if not parent or parent == current:
                break
            try:
                parent_target = os.readlink(parent)
            except (OSError, ValueError):
                break
            if parent_target.startswith("\\\\?\\"):
                parent_target = parent_target[4:]
            if not os.path.isabs(parent_target):
                parent_target = os.path.join(os.path.dirname(parent), parent_target)
            current = os.path.normpath(os.path.join(parent_target, os.path.basename(current)))
            continue
        if target.startswith("\\\\?\\"):
            target = target[4:]
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        current = os.path.normpath(target)
    return current


def _exists_resolved(path: str) -> str | None:
    """安全检测路径是否存在，返回穿透 junction 后的真实路径或 None。

    Python 3.13+ 的 Path.exists()/stat() 在含不受信任挂载点（NTFS junction）的路径上
    抛 WinError 448。cua-driver 的 bin 是 junction，直接 exists() 会抛错。
    先 _resolve_junction（readlink 穿透，不抛 448）拿真实路径，再对真实路径 exists()。
    """
    try:
        real = _resolve_junction(path)
        if Path(real).exists():
            return real
    except OSError:
        return None
    return None


def _find_cua_binary(plat: str) -> str | None:
    """定位 cua-driver 可执行文件。

    优先 shutil.which（PATH 已含安装目录的场景，如 gateway 重启后）；
    否则按平台查常见安装路径。gateway 进程的 PATH 不会随安装脚本动态刷新，
    所以刚装完必须显式定位（Windows 装到 %LOCALAPPDATA%\\Programs\\Cua\\cua-driver\\bin）。

    返回前穿透 junction，避免 daemon/子进程启动时遍历含挂载点路径抛 WinError 448。
    """
    binary = None
    try:
        binary = shutil.which("cua-driver")
    except OSError:
        # Python 3.13+ shutil.which 遍历 PATH 时若命中含挂载点目录可能抛 448
        binary = None
    if binary:
        return _resolve_junction(binary)
    candidates = [
        Path.home() / ".local" / "bin" / "cua-driver",
        Path.home() / ".cargo" / "bin" / "cua-driver",
    ]
    # 冻结态预制二进制：信创 deb 把 glibc≤2.28 兼容版 cua-driver 预制到
    # _internal/runtimes/cua-driver/bin/（见 _bundled_runtime_paths）。联网装的
    # 官方 gnu 版需 glibc≥2.30，信创系统跑不起来，故优先定位预制版。
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        # PyInstaller --onedir: exe 在 crew-gateway/，runtimes 在 _internal/runtimes/
        candidates.insert(0, exe_dir / "_internal" / "runtimes" / "cua-driver" / "bin" / "cua-driver")
    if plat == "windows":
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            # cua-driver Rust 安装脚本（install.ps1）默认位置（v0.2.14 前为 trycua\\cua-driver-rs）
            candidates += [
                Path(localappdata) / "Programs" / "Cua" / "cua-driver" / "bin" / "cua-driver.exe",
                Path(localappdata) / "Programs" / "trycua" / "cua-driver-rs" / "cua-driver.exe",
            ]
        candidates += [
            Path.home() / ".local" / "bin" / "cua-driver.exe",
            Path.home() / ".cargo" / "bin" / "cua-driver.exe",
        ]
    for c in candidates:
        # 用 _exists_resolved 而非 c.exists()：后者在 junction 路径上 Python 3.13+ 抛 WinError 448
        real = _exists_resolved(str(c))
        if real:
            return real
    return None


async def _is_at_spi_installed() -> bool:
    """粗略检测 at-spi2-core 是否已安装。"""
    try:
        await _run_command(
            ["pkg-config", "--exists", "atk"],
            timeout=5,
            env=_clean_system_env(),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def _spawn_daemon(cmd: list[str], *, env: dict[str, str]) -> int:
    """Launch a long-lived CUA process through the crash-recoverable registry."""
    launch = current_process_launch.get()
    validate_process_launch(launch)
    if launch is None:
        raise RuntimeError("CUA daemon launch authority is unavailable")
    from crew.tools.process_registry import process_registry

    session = process_registry.spawn_security(
        "CUA daemon",
        launch=launch,
        launch_argv=tuple(cmd),
        cwd=str(Path.cwd().resolve()),
        session_key=launch.session_id,
        owner_account_id=launch.owner_account_id,
        task_id=launch.task_id,
        explicit_environment=env,
    )
    if not session.pid:
        raise RuntimeError("CUA daemon process identity is unavailable")
    return int(session.pid)


async def _run_command(
    cmd: list[str],
    timeout: float,
    env: dict[str, str] | None = None,
) -> str:
    """运行明确授权的安装期/检测命令并返回 stdout。"""
    from crew.security.launch import execute_captured

    try:
        result = await execute_captured(
            tuple(cmd),
            cwd=Path.cwd().resolve(),
            timeout=timeout,
            env=env,
        )
    except TimeoutError:
        raise RuntimeError(f"命令超时: {' '.join(cmd)}")

    text = result.stdout.strip()
    if result.returncode != 0:
        err = result.stderr.strip()
        raise RuntimeError(f"命令失败 (rc={result.returncode}): {err or text}")
    return text


async def _run_command_streaming(
    cmd: list[str],
    *,
    timeout: float,
    stdout_cb: Callable[[str], None] | None = None,
    stderr_cb: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """运行命令并流式回调 stdout/stderr。

    ``env`` 同 ``_run_command``：调用系统二进制时传 ``_clean_system_env()``。
    """
    from crew.security.launch import execute_captured

    result = await execute_captured(
        tuple(cmd),
        cwd=Path.cwd().resolve(),
        timeout=timeout,
        env=env,
    )
    if stdout_cb is not None:
        for line in result.stdout.splitlines():
            stdout_cb(line)
    if stderr_cb is not None:
        for line in result.stderr.splitlines():
            stderr_cb(line)
    if result.returncode != 0:
        raise RuntimeError(f"命令失败，rc={result.returncode}")


async def _wait_for_daemon(binary: str, timeout: float) -> None:
    """等待 daemon 就绪。"""
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            out = await _run_command([binary, "status"], timeout=5, env=_clean_system_env())
            if "running" in out.lower() or "ok" in out.lower():
                return
        except Exception as exc:  # noqa: BLE001
            last_err = safe_public_error(exc, "驱动状态查询失败")
        await asyncio.sleep(1.0)
    raise RuntimeError(f"daemon 未在 {timeout}s 内就绪: {last_err}")


def task_to_dict(task: SetupTask) -> dict[str, Any]:
    """把 SetupTask 序列化为可 JSON 响应的字典。"""
    return {
        "task_id": task.task_id,
        "platform": task.platform,
        "status": task.status,
        "started_at": task.started_at,
        "finished_at": task.finished_at,
        "steps": [
            {
                "name": s.name,
                "status": s.status,
                "message": s.message,
                "ts": s.ts,
            }
            for s in task.steps
        ],
        "log": task.log,
        "error": task.error,
    }
