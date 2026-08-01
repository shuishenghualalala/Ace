"""CUA Driver 一键安装与 MCP 接入服务。

用户在桌面端点击「安装 CUA MCP」后，后端异步完成：
1. 平台检测；
2. 安装 CUA Driver 二进制；
3. 安装系统依赖（Linux）；
4. 按平台启动并保活 daemon；
5. 更新 config.yaml；
6. 热重载 MCPClientManager。
"""

from __future__ import annotations

import asyncio
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crew.state.logging import get_logger

log = get_logger("tools.cua_setup")


# 官方安装脚本（macOS/Linux curl | bash / Windows irm | iex）
_CUA_INSTALL_URL_POSIX = "https://cua.ai/driver/install.sh"
_CUA_INSTALL_URL_WINDOWS = "https://cua.ai/driver/install.ps1"
_CUA_MACOS_APP = Path("/Applications/CuaDriver.app")

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


def _clean_system_env() -> dict[str, str]:
    """返回剥离了动态链接器污染变量的环境副本，供调用系统二进制时使用。"""
    env = dict(os.environ)
    for var in _LD_TAINT_VARS:
        env.pop(var, None)
    return env


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
        self.log.append(line)

    def update_step(self, name: str, status: str, message: str = "") -> None:
        for step in self.steps:
            if step.name == name:
                step.status = status
                step.message = message
                step.ts = time.time()
                return
        self.steps.append(SetupStep(name=name, status=status, message=message))

    def finish(self, status: str, error: str | None = None) -> None:
        self.status = status
        self.error = error
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
        force_reinstall: bool = False,
        start_daemon: bool = True,
    ) -> SetupTask:
        """启动新的安装任务。"""
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        plat = _detect_platform()
        task = SetupTask(task_id=task_id, platform=plat)
        self._tasks[task_id] = task

        async def _run() -> None:
            try:
                await self._do_setup(task, crew, force_reinstall, start_daemon)
            except asyncio.CancelledError:
                task.finish("cancelled")
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("CUA Driver 安装任务异常")
                task.finish("failed", str(exc))

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
        if plat not in ("linux", "macos", "windows"):
            raise RuntimeError(f"不支持的操作系统: {plat}")
        task.update_step("detect_platform", "success", plat)

        # 2. 安装二进制
        task.update_step("install_binary", "running")
        binary_path = await self._ensure_binary(task, plat, force_reinstall)
        version = await _run_command(
            [binary_path, "--version"], timeout=10, env=_clean_system_env()
        )
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
        cfg.set_mcp_server(
            "cua-driver",
            {
                # 安装脚本可能把程序写入当前 Gateway 尚未包含在 PATH 的目录。
                # 使用本次安装解析到的绝对路径，确保热重载后即可启动 MCP。
                "command": binary_path,
                "args": ["mcp"],
                "env": {},
            },
        )
        cfg.persist_mcp_servers()
        task.update_step("update_config", "success", "mcp_servers.cua-driver enabled")

        # 6. 热重载 MCP
        task.update_step("reload_mcp", "running")
        await crew.reload_mcp_manager()
        tool_names = [name for name in crew.registry.names() if name.startswith("cua-driver__")]
        task.update_step("reload_mcp", "success", f"{len(tool_names)} tools registered")

        task.finish("success")

    # ------------------------------------------------------------------ #
    # 平台相关
    # ------------------------------------------------------------------ #
    async def _ensure_binary(self, task: SetupTask, plat: str, force_reinstall: bool) -> str:
        binary = _find_cua_binary(plat)
        macos_app = _find_cua_app() if plat == "macos" else None
        if binary and not force_reinstall and (plat != "macos" or macos_app):
            task.add_log(f"已找到 cua-driver: {binary}")
            return binary

        if plat == "macos" and binary and not macos_app:
            task.add_log("已找到 cua-driver 命令，但缺少 CuaDriver.app；将运行官方安装程序补齐应用")

        # Linux 冻结态优先用预制二进制：信创系统 glibc 太旧(buster 2.28)，
        # 联网 curl|bash 装的官方 gnu 版需 glibc≥2.30 跑不起来(GLIBC_2.29 not found)。
        # 若下游 .deb 选择内嵌预制版，可兼容 glibc≤2.28，且不依赖联网下载。
        # force_reinstall 时预制版也无法重装(它是打包产物)，直接复用即可。
        if plat == "linux" and getattr(sys, "frozen", False):
            prebaked = _find_cua_binary(plat)
            if prebaked:
                task.add_log(f"使用预制 cua-driver（信创 glibc 兼容版）: {prebaked}")
                return prebaked
            task.add_log(
                "未找到预制 cua-driver，回退联网下载（注意：信创系统可能因 glibc 过旧失败）"
            )

        if force_reinstall and binary:
            task.add_log("force_reinstall=True，重新安装")

        if plat in ("linux", "macos"):
            script_url = _CUA_INSTALL_URL_POSIX
            cmd = [
                "/bin/bash",
                "-c",
                f"curl -fsSL {shlex.quote(script_url)} | /bin/bash",
            ]
        else:
            script_url = _CUA_INSTALL_URL_WINDOWS
            cmd = [
                "powershell",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                f"irm {script_url} | iex",
            ]

        task.add_log(f"下载安装脚本: {script_url}")
        await _run_command_streaming(
            cmd,
            timeout=300,
            stdout_cb=task.add_log,
            stderr_cb=lambda line: task.add_log(f"[stderr] {line}"),
            # curl 是系统二进制：剥离 LD_LIBRARY_PATH，避免加载打包的未打国密补丁 libssl
            env=_clean_system_env(),
        )

        # 安装后重新定位（gateway 进程 PATH 不会动态刷新，shutil.which 可能仍查不到）
        binary = _find_cua_binary(plat)
        if not binary:
            raise RuntimeError("安装完成后仍找不到 cua-driver 命令，请检查 PATH")
        if plat == "macos" and not _find_cua_app():
            raise RuntimeError(
                "安装完成后仍找不到 /Applications/CuaDriver.app，请检查安装日志和目录权限"
            )
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
        if _is_at_spi_installed():
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
                    [
                        "gsettings",
                        "set",
                        "org.gnome.desktop.interface",
                        "toolkit-accessibility",
                        "true",
                    ],
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
        elif plat == "windows":
            await self._start_daemon_windows(task, binary)
        else:  # _do_setup 已做平台校验，此处仅作防御
            raise RuntimeError(f"不支持的操作系统: {plat}")

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
                await _run_command(
                    ["systemctl", "--user", "daemon-reload"], timeout=15, env=_clean_system_env()
                )
                await _run_command(
                    ["systemctl", "--user", "enable", "--now", service_name],
                    timeout=15,
                    env=_clean_system_env(),
                )
                task.add_log("已启用 systemd --user cua-driver 服务")
            except Exception as exc:  # noqa: BLE001
                task.add_log(f"systemd 方式启动失败，回退直接启动: {exc}")
                systemd_available = False

        if not systemd_available:
            # 用 nohup 后台启动
            proc = await asyncio.create_subprocess_exec(
                "nohup",
                binary,
                "serve",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                env=_clean_system_env(),
            )
            task.add_log(f"直接启动 daemon，pid={proc.pid}")

        # 等待 daemon 就绪
        await _wait_for_daemon(binary, timeout=30)

    async def _start_daemon_macos(self, task: SetupTask, binary: str) -> None:
        """通过 CuaDriver.app 启动 macOS daemon，确保权限归属到稳定应用身份。"""
        app = _find_cua_app()
        if not app:
            raise RuntimeError("未找到 /Applications/CuaDriver.app，无法安全启动 macOS CUA Driver")

        await _run_command(
            ["open", "-n", "-g", "-a", "CuaDriver", "--args", "serve"],
            timeout=15,
            env=_clean_system_env(),
        )
        task.add_log(f"已通过 {app} 启动 daemon")
        task.add_log(
            "首次使用请在“系统设置 → 隐私与安全性”中授予 CuaDriver 辅助功能权限；"
            "使用截图、SOM 或视觉模式时还需授予屏幕录制权限"
        )
        await _wait_for_daemon(binary, timeout=30)

    async def _start_daemon_windows(self, task: SetupTask, binary: str) -> None:
        """Windows 下启动 daemon：autostart + 当前会话启动。"""
        try:
            await _run_command([binary, "autostart", "enable"], timeout=15)
            task.add_log("已启用 cua-driver autostart")
        except Exception as exc:  # noqa: BLE001
            task.add_log(f"autostart enable 失败（可继续）: {exc}")

        # 当前会话启动
        proc = await asyncio.create_subprocess_exec(
            binary,
            "serve",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0,
        )
        task.add_log(f"Windows daemon 启动，pid={proc.pid}")

        await _wait_for_daemon(binary, timeout=30)

    # ------------------------------------------------------------------ #
    # 状态查询
    # ------------------------------------------------------------------ #
    async def status(self, registry: Any) -> dict[str, Any]:
        """返回当前 CUA Driver 安装与 MCP 注册状态。"""
        binary = _find_cua_binary(_detect_platform())
        installed = binary is not None
        version = ""
        daemon_running = False
        if installed:
            try:
                version = (
                    await _run_command([binary, "--version"], timeout=10, env=_clean_system_env())
                ).strip()
            except Exception:  # noqa: BLE001
                pass
            try:
                status_out = (
                    await _run_command([binary, "status"], timeout=10, env=_clean_system_env())
                ).strip()
                daemon_running = "running" in status_out.lower() or "ok" in status_out.lower()
            except Exception:  # noqa: BLE001
                pass

        tool_names = [name for name in registry.names() if name.startswith("cua-driver__")]
        return {
            "ok": True,
            "installed": installed,
            "binary": binary,
            "version": version,
            "daemon_running": daemon_running,
            "mcp_enabled": any(name.startswith("cua-driver__") for name in registry.names()),
            "tools_registered": tool_names,
        }


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #
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


def _find_cua_app() -> str | None:
    """定位 macOS CuaDriver.app；daemon 必须由该稳定应用身份启动。"""
    try:
        if _CUA_MACOS_APP.is_dir():
            return str(_CUA_MACOS_APP)
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
    if plat == "macos":
        candidates.insert(0, _CUA_MACOS_APP / "Contents" / "MacOS" / "cua-driver")
    # 冻结态可选预置二进制：下游发行包可以把 cua-driver 放到
    # _internal/runtimes/cua-driver/bin/（见 _bundled_runtime_paths）。官方 Crew
    # 源码与自构建安装包不携带该第三方二进制，通常会继续走按需安装流程。
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        # PyInstaller --onedir: exe 在 crew-gateway/，runtimes 在 _internal/runtimes/
        candidates.insert(
            0, exe_dir / "_internal" / "runtimes" / "cua-driver" / "bin" / "cua-driver"
        )
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


def _is_at_spi_installed() -> bool:
    """粗略检测 at-spi2-core 是否已安装。"""
    try:
        # 尝试通过 pkg-config 检测
        result = subprocess.run(
            ["pkg-config", "--exists", "atk"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


async def _run_command(cmd: list[str], timeout: float, env: dict[str, str] | None = None) -> str:
    """运行命令并返回 stdout。

    ``env`` 默认 None（继承 gateway 全环境，含打包内嵌库路径）。调用 *系统* 二进制
    （curl/apt/gsettings/systemctl 等）时必须传 ``_clean_system_env()``，否则
    PyInstaller bootloader 泄漏的 LD_LIBRARY_PATH 会让系统 libcurl 加载到打包的、
    未打国密补丁的 libssl → relocation error。
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"命令超时: {' '.join(cmd)}")

    text = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"命令失败 (rc={proc.returncode}): {err or text}")
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
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    async def _read_stream(
        stream: asyncio.StreamReader | None, cb: Callable[[str], None] | None
    ) -> None:
        if stream is None or cb is None:
            return
        while True:
            line = await stream.readline()
            if not line:
                break
            cb(line.decode("utf-8", errors="replace").rstrip("\n"))

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _read_stream(proc.stdout, stdout_cb),
                _read_stream(proc.stderr, stderr_cb),
                proc.wait(),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"命令超时: {' '.join(cmd)}")

    if proc.returncode != 0:
        raise RuntimeError(f"命令失败，rc={proc.returncode}")


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
            last_err = str(exc)
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
