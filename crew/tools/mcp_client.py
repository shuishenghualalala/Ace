"""MCP Client：连接外部 MCP server，把其 tools 注册进 Crew 的 Registry。

agent 即可像调内置工具一样调用它们（工具名 ``{server}__{tool}``）。

连接模型：每个 server 一个常驻 worker task，task 内打开传输 + MCP 2 Client 并保持，
然后从 asyncio.Queue 取 (tool_name, args, future)，在**本 task** 内 call_tool 后回填 future。
工具 handler 只负责入队 + await future——所有 session I/O 都在持有它的 task 里，
单事件循环下正确，且避免 Hermes 那套专用事件循环/跨 loop 编组（其 mcp_tool.py 达 3915 行）。

仅复用 MCP SDK 的调用范式（stdio_client / streamable_http_client / sse_client +
Client.list_tools/call_tool）。Client 使用 ``mode="auto"``，优先协商 MCP 2 的现代协议，
并自动回退旧服务端。OAuth / 自动重试一律不做；生命周期采用有界预算。
未安装 mcp 包或连接失败 → 跳过该 server，不影响主流程。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crew.core.errors import ToolError
from crew.state.logging import get_logger
from crew.tools.registry import Registry, tool_error

log = get_logger("tools.mcp")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

MCP_QUEUE_CAPACITY = 32
MCP_CALL_TIMEOUT_SECONDS = 60.0
MCP_STARTUP_TIMEOUT_SECONDS = 30.0
MCP_SHUTDOWN_TIMEOUT_SECONDS = 10.0


@dataclass(slots=True)
class _CallRequest:
    """一次 MCP 调用及其从入队时开始计算的绝对截止时间。"""

    tool_name: str
    args: dict[str, Any]
    future: asyncio.Future[str]
    deadline: float
    started: bool = False


def _interpolate(value: Any) -> Any:
    """递归解析 ${VAR}（from os.environ）。找不到保留字面。对齐 hermes _interpolate_env_vars。"""
    if isinstance(value, str):
        return _ENV_VAR_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(0)), value
        )
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _resolve_junction(path: str) -> str:
    """穿透路径中任意层级的 NTFS junction，返回真实路径。

    cua-driver 的 bin 是 junction（bin → current → releases/<version>）。文件路径
    `...\\bin\\cua-driver.exe` 的父目录含 junction，直接对文件 readlink 读不到。
    这里逐级检查路径的每一层父目录，遇到 junction 就用其 target 替换并继续，
    最终拼出不含任何 junction 的真实路径。

    用 os.readlink（读 reparse point 元数据，不遍历 target，不抛 448），而非
    Path.resolve()/os.path.realpath（3.13+ 可能因打开 mountpoint 抛 448）。
    """
    if sys.platform != "win32":
        return path
    current = os.path.normpath(path)
    seen: set[str] = set()
    for _ in range(32):  # 防循环链接 + 多层 junction
        if current in seen:
            break
        seen.add(current)
        try:
            target = os.readlink(current)
        except (OSError, ValueError):
            # current 本身不是 junction；检查它的父目录是否是 junction
            parent = os.path.dirname(current)
            if not parent or parent == current:
                break
            try:
                parent_target = os.readlink(parent)
            except (OSError, ValueError):
                break  # 父目录也不是 junction，current 即真实路径
            # 父目录是 junction：用 target 替换父目录，重新拼 current 继续穿透
            parent_target = parent_target.removeprefix("\\\\?\\")
            if not os.path.isabs(parent_target):
                parent_target = os.path.join(os.path.dirname(parent), parent_target)
            current = os.path.normpath(os.path.join(parent_target, os.path.basename(current)))
            continue
        # current 本身是 junction
        target = target.removeprefix("\\\\?\\")
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(current), target)
        current = os.path.normpath(target)
    return current


def _exists_resolved(path: str) -> str | None:
    """安全检测路径是否存在，返回穿透 junction 后的真实路径或 None。

    Python 3.13+ 的 Path.exists()/is_file()/stat() 在含不受信任挂载点（NTFS junction）
    的路径上抛 WinError 448。cua-driver 的 bin 是 junction，直接 exists() 会抛错。
    先 _resolve_junction（readlink 穿透，不抛 448）拿到真实路径，再对真实路径（无 junction）
    做 exists()。
    """
    try:
        real = _resolve_junction(path)
        if Path(real).exists():
            return real
    except OSError:
        return None
    return None


def _resolve_command(command: str) -> str:
    """解析 stdio MCP server 的 command 为可执行路径。

    shutil.which 依赖当前进程 PATH；但本地工具（如 cua-driver）的安装脚本把 bin 目录
    加到的是「用户 PATH」环境变量，已运行的 gateway 进程不会刷新，导致 which 找不到、
    子进程启动报 WinError 2。这里对裸命令名做 fallback：在常见本地安装目录里查找。

    解析结果经 _resolve_junction 穿透 junction，避免下游（Python 3.13+ / mcp SDK）
    遍历含挂载点的路径时抛 WinError 448。
    """
    if not command:
        return command
    if os.path.isabs(command):
        return _resolve_junction(command)
    try:
        found = shutil.which(command)
    except OSError:
        # Python 3.13+ which 遍历 PATH 时若命中含挂载点目录可能抛 448
        found = None
    if found:
        return _resolve_junction(found)
    # 裸命令名 fallback：常见本地工具安装目录
    candidates: list[Path] = []
    home = Path.home()
    candidates += [home / ".local" / "bin", home / ".cargo" / "bin"]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        # cua-driver Rust 安装脚本默认位置（v0.2.14 前 trycua/cua-driver-rs）
        candidates += [
            Path(localappdata) / "Programs" / "Cua" / "cua-driver" / "bin",
            Path(localappdata) / "Programs" / "trycua" / "cua-driver-rs",
        ]
    exe_suffixes = [".exe", ""] if sys.platform == "win32" else [""]
    name = command
    for d in candidates:
        for suf in exe_suffixes:
            p = d / f"{name}{suf}"
            # 用 _exists_resolved 而非 p.is_file()：后者在 junction 路径上 3.13+ 抛 448
            real = _exists_resolved(str(p))
            if real:
                return real
    return command


def _resolve_paths(cfg: dict[str, Any]) -> dict[str, Any]:
    """插值 command/env；args 里的相对路径若命中 REPO_ROOT 下真实文件则转绝对。
    flag 类参数（-y / --foo / 裸单词）不会被误伤（candidate.exists() 守门）。"""
    cfg = dict(cfg)
    cfg["command"] = _resolve_command(_interpolate(cfg.get("command", "")))
    cfg["env"] = _interpolate(cfg.get("env") or {}) or None
    resolved_args: list[Any] = []
    for a in cfg.get("args", []):
        if isinstance(a, str) and not os.path.isabs(a):
            candidate = _REPO_ROOT / a
            resolved_args.append(str(candidate) if candidate.exists() else a)
        else:
            resolved_args.append(a)
    cfg["args"] = resolved_args
    return cfg


def _stdio_env(cfg_env: dict[str, Any] | None) -> dict[str, str] | None:
    """Build stdio child env by inheriting parent env and applying config overrides."""
    if not cfg_env:
        return None
    env = dict(os.environ)
    for key, value in cfg_env.items():
        env[str(key)] = str(value)
    return env


def _extract_text(result: Any) -> str:
    """从 MCP call_tool 返回里抽取文本与图片。

    兼容纯文本结果与混合内容（TextContent + ImageContent）。
    无图片时保持原有行为返回拼接文本；有图片时返回 JSON：
    {"text": "<文本>", "images": [{"mime_type": "image/png", "data": "base64...", "url": "data:image/png;base64,..."}]}
    """
    text_parts: list[str] = []
    image_parts: list[dict[str, str]] = []

    def _block_value(block: Any, *keys: str) -> Any:
        """兼容 dataclass 对象与 dict。"""
        if isinstance(block, dict):
            for k in keys:
                if k in block:
                    return block[k]
            return None
        for k in keys:
            value = getattr(block, k, None)
            if value is not None:
                return value
        return None

    for block in getattr(result, "content", None) or []:
        if block is None:
            continue
        block_type = str(_block_value(block, "type") or "")
        if block_type == "text":
            text = _block_value(block, "text")
            if text:
                text_parts.append(str(text))
        elif block_type in {"image", "image_url"}:
            mime = str(_block_value(block, "mimeType", "mime_type") or "image/png")
            data = str(_block_value(block, "data") or "")
            if data:
                url = f"data:{mime};base64,{data}"
                image_parts.append({"mime_type": mime, "data": data, "url": url})
        else:
            # 兜底：仍尝试读取 text 字段
            text = _block_value(block, "text")
            if text:
                text_parts.append(str(text))

    joined = "\n".join(text_parts)
    if getattr(result, "is_error", False):
        return tool_error(joined or "MCP 工具返回错误")

    if not image_parts:
        return joined or "(空结果)"

    return json.dumps(
        {"text": joined, "images": image_parts},
        ensure_ascii=False,
    )


class _ServerWorker:
    """单个 MCP server 的常驻连接 + 调用编组。"""

    def __init__(
        self,
        name: str,
        cfg: dict[str, Any],
        registry: Registry,
        *,
        queue_capacity: int = MCP_QUEUE_CAPACITY,
        call_timeout: float = MCP_CALL_TIMEOUT_SECONDS,
        startup_timeout: float = MCP_STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.name = name
        self.cfg = cfg
        self.registry = registry
        self._queue: asyncio.Queue[_CallRequest] = asyncio.Queue(maxsize=queue_capacity)
        self._call_timeout = call_timeout
        self._startup_timeout = startup_timeout
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._tools: list[Any] = []
        self._task: asyncio.Task[None] | None = None
        self._current: _CallRequest | None = None
        self._closing = False
        # True when this worker was started as a host stdio subprocess (only possible
        # under ACE_ALLOW_HOST_MCP_STDIO=1 in a non-managed context). A worker
        # started that way must not be re-used by a later managed conversation: the
        # tool call would route host side-effects past the managed boundary (H-21).
        self._host_stdio_spawn = False

    async def _open(self, stack: AsyncExitStack):
        from mcp import Client, StdioServerParameters
        from mcp.client.stdio import stdio_client

        cfg = _resolve_paths(self.cfg)
        if cfg.get("command"):
            # managed（包括宽权限 full_access）模式下不得经 stdio spawn 宿主子进程——
            # 那是绕过 broker 的执行面（决策 #13、spec §7.2）。只有真正 disabled
            #（或无 launch 上下文的 dev/装机场景）在操作者显式
            # ACE_ALLOW_HOST_MCP_STDIO=1 时放行。
            from crew.security.launch import current_process_launch

            launch = current_process_launch.get()
            managed = launch is not None and getattr(launch, "managed", False)
            host_stdio_allowed = (
                os.environ.get("ACE_ALLOW_HOST_MCP_STDIO") == "1"
                and not managed
            )
            if not host_stdio_allowed:
                reason = (
                    "managed profile disables MCP stdio host spawn"
                    if managed
                    else "MCP stdio host process is disabled; use a managed transport "
                    "or explicitly approved host configuration"
                )
                raise ValueError(reason)
            # Mark the worker so per-call handlers can refuse reuse by a later managed
            # conversation (H-21): the spawn context is non-managed, but a subsequent
            # managed dialog must not route tool calls through this host stdio worker.
            self._host_stdio_spawn = True
            child_env = _stdio_env(cfg.get("env"))
            params = StdioServerParameters(
                command=cfg["command"], args=cfg.get("args", []), env=child_env
            )
            transport = stdio_client(params)
        elif cfg.get("url"):
            transport_name = str(cfg.get("transport") or "http").lower()
            if transport_name == "sse":
                from mcp.client.sse import sse_client

                transport = sse_client(cfg["url"], headers=cfg.get("headers"))
            else:
                import httpx2
                from mcp.client.streamable_http import streamable_http_client

                http_client = await stack.enter_async_context(
                    httpx2.AsyncClient(
                        headers=cfg.get("headers"),
                        timeout=httpx2.Timeout(30.0, read=300.0),
                        follow_redirects=True,
                    )
                )
                transport = streamable_http_client(
                    cfg["url"],
                    http_client=http_client,
                )
        else:
            raise ValueError("MCP server 配置需要 'command'（stdio）或 'url'（http/sse）")

        return await stack.enter_async_context(Client(transport, mode="auto"))

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                session = await self._open(stack)
                resp = await session.list_tools()
                self._tools = list(resp.tools)
                self._ready.set()
                # 服务循环：在本 task 内执行所有 call_tool
                while True:
                    request = await self._queue.get()
                    if request.future.done():
                        continue
                    if request.deadline <= asyncio.get_running_loop().time():
                        self._complete(
                            request,
                            tool_error(
                                f"MCP server {self.name} 调用在执行前已超过截止时间；"
                                "远端未被调用"
                            ),
                        )
                        continue
                    self._current = request
                    request.started = True
                    try:
                        async with asyncio.timeout_at(request.deadline):
                            result = await session.call_tool(request.tool_name, request.args or {})
                        extracted = _extract_text(result)
                        if getattr(result, "is_error", False):
                            try:
                                message = str(json.loads(extracted).get("error") or extracted)
                            except (AttributeError, json.JSONDecodeError):
                                message = extracted
                            self._fail(request, message)
                        else:
                            self._complete(request, extracted)
                    except TimeoutError:
                        self._complete(
                            request,
                            tool_error(
                                f"MCP server {self.name} 调用超过绝对截止时间；"
                                "远端最终副作用状态可能未知，系统不会自动重试"
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001 - 回填给调用方而非崩溃
                        self._complete(request, tool_error(f"MCP 调用失败: {exc}"))
                    finally:
                        # CancelledError 会直接离开本层；保留 current 给外层 finally 完成 Future。
                        if request.future.done():
                            self._current = None
        except Exception as exc:
            self._error = exc
            # 打完整 traceback 到日志，便于定位连接失败根因（如 WinError 448
            # 不受信任挂载点——需看调用栈确认是 which/resolve/open_process 哪步抛的）。
            log.exception("MCP server %s 连接异常", self.name)
        finally:
            self._ready.set()  # 失败也要解除 start() 的等待

            # worker 退出后不得遗留永久 pending 的调用；Future 只由 _complete 写一次。
            reason = (
                f"MCP server {self.name} 正在关闭"
                if self._closing
                else f"MCP server {self.name} 连接已断开"
            )
            if self._current is not None:
                self._complete(
                    self._current,
                    tool_error(f"{reason}；远端最终副作用状态可能未知，系统不会自动重试"),
                )
                self._current = None
            self._drain_queue(reason)

    @staticmethod
    def _complete(request: _CallRequest, result: str) -> None:
        """恰好一次完成调用 Future；调用方取消时保持取消状态。"""
        if not request.future.done():
            request.future.set_result(result)

    @staticmethod
    def _fail(request: _CallRequest, message: str) -> None:
        """把远端 MCP 错误保留为 Crew ToolResult.is_error，而非成功文本。"""
        if not request.future.done():
            request.future.set_exception(ToolError(message))

    def _drain_queue(self, reason: str) -> None:
        """完成所有尚未执行的排队请求，明确保证远端未被调用。"""
        while True:
            try:
                request = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._complete(request, tool_error(f"{reason}；排队请求未调用远端"))

    def _register_tools(self) -> int:
        count = 0
        for tool in self._tools:
            qualified = f"{self.name}__{tool.name}"
            schema = {
                "name": qualified,
                "description": getattr(tool, "description", "") or "",
                "parameters": getattr(tool, "input_schema", None) or {"type": "object", "properties": {}},
            }
            self.registry.register(
                name=qualified,
                toolset=f"mcp:{self.name}",
                schema=schema,
                handler=self._make_handler(tool.name),
                is_async=True,
                override=True,
                display_name=f"MCP {tool.name}",
                ui_label_template=f"MCP {tool.name}",
                should_defer=True,
                is_mcp=True,
                search_hint=f"mcp {self.name} {tool.name} {getattr(tool, 'description', '') or ''}",
            )
            count += 1
        return count

    def _unregister_tools(self) -> int:
        """从 Registry 注销本 worker 注册过的工具（用于删除/重连单 server）。"""
        count = 0
        for tool in self._tools:
            qualified = f"{self.name}__{tool.name}"
            if self.registry.unregister(qualified):
                count += 1
        return count

    # ---- 只读状态（供管理面板查询）----
    # 单事件循环下读取这些字段无需加锁：worker 的可变状态（_task/_error/_tools）只在
    # _run() task 内或 start()/stop()（由 manager 在同一 loop 串行调度）中改写。
    @property
    def is_connected(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and self._ready.is_set()
            and self._error is None
        )

    @property
    def error(self) -> str:
        return str(self._error) if self._error is not None else ""

    @property
    def tool_names(self) -> list[str]:
        return [getattr(t, "name", "") for t in self._tools]

    def _make_handler(self, tool_name: str):
        async def handler(args: dict[str, Any]) -> str:
            # Per-call launch re-check: a host stdio worker is spawned under a
            # non-managed context, but tool calls arrive from whatever conversation
            # invokes them. A later managed dialog must not route side-effects through
            # this host worker (H-21).
            if self._host_stdio_spawn:
                from crew.security.launch import current_process_launch

                launch = current_process_launch.get()
                if launch is None or getattr(launch, "managed", False):
                    return tool_error(
                        f"MCP server {self.name} 为宿主 stdio 进程，缺少 disabled 安全上下文"
                    )
            if (
                self._closing
                or self._error is not None
                or self._task is None
                or self._task.done()
            ):
                return tool_error(f"MCP server {self.name} 连接已断开")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[str] = loop.create_future()
            request = _CallRequest(
                tool_name=tool_name,
                args=args,
                future=future,
                deadline=loop.time() + self._call_timeout,
            )
            try:
                self._queue.put_nowait(request)
            except asyncio.QueueFull:
                return tool_error(
                    f"MCP server {self.name} 调用队列已满（上限 {self._queue.maxsize}）"
                )

            try:
                async with asyncio.timeout_at(request.deadline):
                    return await asyncio.shield(future)
            except TimeoutError:
                if request.started:
                    result = tool_error(
                        f"MCP server {self.name} 调用超过绝对截止时间；"
                        "远端最终副作用状态可能未知，系统不会自动重试"
                    )
                else:
                    result = tool_error(
                        f"MCP server {self.name} 调用在执行前已超过截止时间；远端未被调用"
                    )
                self._complete(request, result)
                return future.result()
            except asyncio.CancelledError:
                # 调用方已离开时取消 Future；worker 取到后会跳过，不触达远端。
                future.cancel()
                raise

        return handler

    async def start(self) -> bool:
        """打开连接并注册工具。成功返回 True。"""
        if self._closing:
            return False
        self._task = asyncio.create_task(self._run())
        try:
            async with asyncio.timeout(self._startup_timeout):
                await self._ready.wait()
        except TimeoutError:
            self._error = TimeoutError(
                f"MCP server {self.name} 启动超过 {self._startup_timeout:g} 秒总预算"
            )
            self.force_abort("MCP server 启动超时")
            log.warning("%s", self._error)
            return False
        except asyncio.CancelledError:
            self.force_abort("MCP server 启动被取消")
            raise
        if self._error is not None:
            log.warning("MCP server %s 连接失败：%s", self.name, self._error)
            return False
        n = self._register_tools()
        log.info("MCP server %s 已连接，注册 %d 个工具", self.name, n)
        return True

    async def stop(self) -> None:
        """取消当前远端调用并关闭连接；总时间预算由 manager 统一约束。"""
        self._closing = True
        task = self._task
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("关闭 MCP server %s 异常", self.name)
        self._task = None
        self._drain_queue(f"MCP server {self.name} 正在关闭")

    def force_abort(self, reason: str) -> None:
        """在外层总预算耗尽时立即取消 worker 并完成所有可见 Future。"""
        self._closing = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
        if self._current is not None:
            self._complete(
                self._current,
                tool_error(f"{reason}；远端最终副作用状态可能未知，系统不会自动重试"),
            )
        self._drain_queue(reason)


class MCPClientManager:
    """管理所有配置的 MCP server 连接。"""

    def __init__(
        self,
        servers_config: dict[str, Any] | None,
        *,
        queue_capacity: int = MCP_QUEUE_CAPACITY,
        call_timeout: float = MCP_CALL_TIMEOUT_SECONDS,
        startup_timeout: float = MCP_STARTUP_TIMEOUT_SECONDS,
        shutdown_timeout: float = MCP_SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self._config = servers_config or {}
        self._workers: list[_ServerWorker] = []
        self._queue_capacity = queue_capacity
        self._call_timeout = call_timeout
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._closing = False
        # 后台启动不阻塞 gateway lifespan；aclose() 会在同一关闭预算内取消并等待它。
        self._start_task: asyncio.Task | None = None
        # registry 引用：增量管理（add/remove/reload_one）需用它注册/注销工具。
        # start() 时赋值；管理 API 在 start 之前调用会拒绝。
        self._registry: Registry | None = None

    async def start(self, registry: Registry) -> None:
        """后台连接所有 MCP server 并注册工具，立即返回不阻塞。

        连接/工具注册在后台 task 内完成；单个 server 连接失败只 warning（见 _ServerWorker.start）
        不影响主流程。连接完成前调用对应工具会返回“连接已断开”错误（_make_handler 守门），
        不会崩溃。工具用 should_defer=True，未就绪前不暴露给 LLM。
        """
        self._registry = registry
        # 配置为空时无 server 可连，但仍保留 registry 引用，
        # 供后续 add_server 增量启动（管理面板首次新增 server 的场景）。
        if self._closing or self._start_task is not None or not self._config:
            return
        self._start_task = asyncio.create_task(self._start_blocking(registry))

    async def await_started(self, timeout: float | None = 30.0) -> None:
        """等待后台 start 完成。生产启动路径不需要调用（fire-and-forget 即可），
        仅供需要确认 MCP 就绪的测试/诊断场景同步。

        Args:
            timeout: 等待超时秒数；None 表示无限等待。防止某个 server 连接 hang 住
                导致关闭流程被阻塞。
        """
        if self._start_task is None:
            return
        try:
            await asyncio.wait_for(self._start_task, timeout=timeout)
        except TimeoutError:
            log.warning("等待 MCP 后台启动超时，取消剩余连接任务")
            self._start_task.cancel()
            try:
                await self._start_task
            except asyncio.CancelledError:
                pass
        except Exception:
            log.exception("等待 MCP 后台启动结束异常")
        self._start_task = None

    async def _start_blocking(self, registry: Registry) -> None:
        try:
            import mcp  # noqa: F401
        except ImportError:
            log.warning("未安装 mcp 包，跳过 MCP Client（pip install mcp）")
            return
        pending: list[tuple[_ServerWorker, asyncio.Task[bool]]] = []
        for name, cfg in self._config.items():
            if not isinstance(cfg, dict):
                log.warning("MCP server %s 配置非法，跳过", name)
                continue
            if cfg.get("auto_connect") is False:
                log.info("MCP server %s 配置为手动连接（auto_connect: false），跳过自动启动", name)
                continue
            worker = _ServerWorker(
                name,
                cfg,
                registry,
                queue_capacity=self._queue_capacity,
                call_timeout=self._call_timeout,
                startup_timeout=self._startup_timeout,
            )
            self._workers.append(worker)
            pending.append((worker, asyncio.create_task(worker.start())))

        # 各 server 并行启动并独立失败，避免一个慢 server 阻塞其他 server 注册。
        for worker, result in zip(
            (item[0] for item in pending),
            await asyncio.gather(*(item[1] for item in pending), return_exceptions=True),
            strict=True,
        ):
            if isinstance(result, BaseException) and not isinstance(
                result, asyncio.CancelledError
            ):
                log.error("MCP server %s 启动异常：%s", worker.name, result)

    async def aclose(self) -> None:
        """在单一 10 秒总预算内取消启动并并行关闭所有 server。"""
        if self._closing:
            return
        self._closing = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._shutdown_timeout

        if self._start_task is not None and not self._start_task.done():
            self._start_task.cancel()
        if self._start_task is not None:
            try:
                async with asyncio.timeout_at(deadline):
                    await self._start_task
            except (TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                log.exception("取消 MCP 后台启动异常")
            self._start_task = None

        stop_tasks = [asyncio.create_task(worker.stop()) for worker in self._workers]
        if stop_tasks:
            try:
                async with asyncio.timeout_at(deadline):
                    await asyncio.gather(*stop_tasks, return_exceptions=True)
            except TimeoutError:
                log.warning("MCP server 关闭超过 %.1f 秒总预算，强制取消", self._shutdown_timeout)
                for worker in self._workers:
                    worker.force_abort("MCP server 关闭总预算已耗尽")
                for task in stop_tasks:
                    task.cancel()
        self._workers.clear()

    # ------------------------------------------------------------------ #
    # 运行时管理：供桌面端 MCP 管理面板调用（增量增删改查 + 单 server 重连）
    # ------------------------------------------------------------------ #
    def _worker_for(self, name: str) -> _ServerWorker | None:
        for w in self._workers:
            if w.name == name:
                return w
        return None

    @staticmethod
    def _transport_of(cfg: dict[str, Any]) -> str:
        """探测配置的传输类型：stdio / sse / http。"""
        if cfg.get("command"):
            return "stdio"
        if cfg.get("url"):
            t = str(cfg.get("transport") or "http").lower()
            return "sse" if t == "sse" else "http"
        return "unknown"

    def status(self) -> list[dict[str, Any]]:
        """返回所有已配置 server 的连接状态 + 工具名（不脱敏，由 router 层脱敏配置）。

        覆盖三类：已连接成功的 worker、配置中但启动失败的（_workers 不含，但 _config 含）、
        启动失败被记录的会在 worker.is_connected=False 体现。配置里有但 _workers 没有的
        视为「未连接」。
        """
        seen = set()
        rows: list[dict[str, Any]] = []
        for w in self._workers:
            seen.add(w.name)
            rows.append({
                "name": w.name,
                "transport": self._transport_of(w.cfg),
                "connected": w.is_connected,
                "error": w.error,
                "tools": list(w.tool_names),
                "config": dict(w.cfg),
            })
        # 配置里有、但没成功进 _workers 的（启动失败 / 尚未启动）
        for name, cfg in self._config.items():
            if name in seen:
                continue
            rows.append({
                "name": name,
                "transport": self._transport_of(cfg),
                "connected": False,
                "error": "",
                "tools": [],
                "config": dict(cfg),
            })
        return rows

    async def add_server(self, name: str, cfg: dict[str, Any]) -> bool:
        """新增一个 server：更新配置 + 启动单 worker。不打断其他 server。

        若已存在同名 worker，返回 False（调用方应先 remove 或 reload）。
        start() 未就绪（registry 未注入或后台 start 未完成）时拒绝并返回 False。
        """
        if self._registry is None or self._closing:
            return False
        # 等后台初始 start 跑完，避免与 _start_blocking 同时写 _workers / _config。
        await self.await_started()
        if self._closing:
            return False
        if self._worker_for(name) is not None:
            return False
        self._config[name] = dict(cfg)
        worker = _ServerWorker(
            name,
            cfg,
            self._registry,
            queue_capacity=self._queue_capacity,
            call_timeout=self._call_timeout,
            startup_timeout=self._startup_timeout,
        )
        # 启动前登记，确保并发 aclose() 能发现并关闭尚在握手的 worker。
        self._workers.append(worker)
        try:
            return bool(await worker.start())
        except Exception:
            log.exception("新增 MCP server %s 启动异常", name)
            return False

    def register_pending(self, name: str, cfg: dict[str, Any]) -> None:
        """同步登记一个待连接的 server 配置（不启动 worker）。

        供管理 API 在 fire-and-forget 启动 worker 前，先让 status() 能看到该 server
        （connected=False），使前端列表立即刷新出新增项。add_server 后台执行时会
        覆盖该配置并真正连接。
        """
        if self._worker_for(name) is None:
            self._config[name] = dict(cfg)

    async def remove_server(self, name: str) -> bool:
        """删除一个 server：停止 worker + 注销工具 + 从配置移除。"""
        await self.await_started()
        worker = self._worker_for(name)
        self._config.pop(name, None)
        if worker is None:
            return False
        worker._unregister_tools()
        await worker.stop()
        self._workers = [w for w in self._workers if w.name != name]
        return True

    async def reload_one(self, name: str, cfg: dict[str, Any] | None = None) -> bool:
        """重连单个 server。cfg 非空时先更新配置再重连。

        停掉旧 worker（注销工具）→ 用新 cfg 起新 worker（注册工具）。
        其他 server 不受影响。配置里不存在该 name 返回 False。
        """
        if self._registry is None or self._closing:
            return False
        await self.await_started()
        if self._closing:
            return False
        if cfg is not None:
            self._config[name] = dict(cfg)
        if name not in self._config:
            return False
        old = self._worker_for(name)
        if old is not None:
            old._unregister_tools()
            await old.stop()
            self._workers = [w for w in self._workers if w.name != name]
        # aclose() 可能在等待旧 worker 关闭期间开始；此时不得再创建新连接。
        if self._closing:
            return False
        worker = _ServerWorker(
            name,
            self._config[name],
            self._registry,
            queue_capacity=self._queue_capacity,
            call_timeout=self._call_timeout,
            startup_timeout=self._startup_timeout,
        )
        # 与 add_server 一致，避免后台重连与 manager 关闭竞态泄漏 MCP 协程。
        self._workers.append(worker)
        try:
            return bool(await worker.start())
        except Exception:
            log.exception("重连 MCP server %s 异常", name)
            return False
