"""Codex app-server RuntimeAdapter over stdio JSONL."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator

from crew.agent.external.process_lifecycle import (
    finish_process_after_terminal,
    isolated_process_kwargs,
    terminate_process_tree,
)
from crew.agent.external.runtime_adapter import (
    ExternalPermissionRequest,
    ExternalStreamEvent,
    ExternalToolEvent,
    RuntimeAdapterProbe,
    RuntimeExecutionRequest,
    RuntimeResumeRejected,
    build_external_runtime_env,
    register_runtime_adapter,
)
from crew.agent.external.runtime_profile import RuntimeCapabilities, RuntimeModelProfile


CODEX_STREAM_LIMIT_BYTES = 64 * 1024 * 1024


class CodexAdapterError(RuntimeError):
    pass


class CodexAppServerUnsupported(CodexAdapterError):
    pass


class _CodexRpcClient:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self.reader_task = asyncio.create_task(self._read())

    async def _read(self) -> None:
        if self.proc.stdout is None:
            return
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    payload = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                message_id = payload.get("id")
                if isinstance(message_id, int) and message_id in self.pending:
                    future = self.pending.pop(message_id)
                    if "error" in payload:
                        error = payload.get("error")
                        message = (
                            str(error.get("message") or error)
                            if isinstance(error, dict)
                            else str(error)
                        )
                        future.set_exception(CodexAdapterError(message))
                    else:
                        future.set_result(payload.get("result"))
                else:
                    await self.events.put(payload)
        finally:
            error = CodexAdapterError(
                f"Codex app-server 已退出（exit={self.proc.returncode}）"
            )
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    async def send(self, payload: dict[str, Any]) -> None:
        if self.proc.stdin is None:
            raise CodexAdapterError("Codex app-server stdin 不可用")
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float,
    ) -> Any:
        message_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self.send({"id": message_id, "method": method, "params": params or {}})
        try:
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise CodexAdapterError(f"Codex {method} 请求超时") from exc
        finally:
            self.pending.pop(message_id, None)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self.send({"method": method, "params": params or {}})

    async def respond(self, message_id: Any, result: Any) -> None:
        await self.send({"id": message_id, "result": result})

    async def respond_error(self, message_id: Any, code: int, message: str) -> None:
        await self.send({
            "id": message_id,
            "error": {"code": code, "message": message},
        })

    async def close(self) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
            await asyncio.gather(self.reader_task, return_exceptions=True)


def _result_id(value: Any, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    nested = value.get(key.removesuffix("Id"))
    if isinstance(nested, dict):
        return str(nested.get("id") or nested.get(key) or "").strip()
    return str(value.get(key) or value.get("id") or "").strip()


def _event_scope(params: dict[str, Any]) -> tuple[str, str]:
    thread = params.get("thread")
    turn = params.get("turn")
    return (
        str(
            params.get("threadId")
            or params.get("thread_id")
            or (thread.get("id") if isinstance(thread, dict) else "")
            or ""
        ).strip(),
        str(
            params.get("turnId")
            or params.get("turn_id")
            or (turn.get("id") if isinstance(turn, dict) else "")
            or ""
        ).strip(),
    )


def _usage(params: dict[str, Any]) -> dict[str, int]:
    raw = params.get("tokenUsage") or params.get("token_usage") or params.get("usage") or {}
    if not isinstance(raw, dict):
        return {}
    last = raw.get("last") if isinstance(raw.get("last"), dict) else raw
    aliases = {
        "input_tokens": ("inputTokens", "input_tokens"),
        "cached_input_tokens": ("cachedInputTokens", "cached_input_tokens"),
        "output_tokens": ("outputTokens", "output_tokens"),
        "reasoning_output_tokens": ("reasoningOutputTokens", "reasoning_output_tokens"),
        "total_tokens": ("totalTokens", "total_tokens"),
    }
    result: dict[str, int] = {}
    for target, keys in aliases.items():
        for key in keys:
            value = last.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[target] = int(value)
                break
    if result and "total_tokens" not in result:
        result["total_tokens"] = result.get("input_tokens", 0) + result.get("output_tokens", 0)
    return result


def _item_tool(item: Any, *, phase: str) -> ExternalToolEvent | None:
    if not isinstance(item, dict):
        return None
    item_type = str(item.get("type") or "").lower()
    names = {
        "commandexecution": "shell",
        "command_execution": "shell",
        "filechange": "file_write",
        "file_change": "file_write",
        "mcptoolcall": str(item.get("tool") or item.get("name") or "mcp_tool"),
        "mcp_tool_call": str(item.get("tool") or item.get("name") or "mcp_tool"),
    }
    if item_type not in names:
        return None
    item_id = str(item.get("id") or item.get("itemId") or "").strip()
    args_value = item.get("command") or item.get("changes") or item.get("arguments") or {}
    args = (
        args_value
        if isinstance(args_value, str)
        else json.dumps(args_value, ensure_ascii=False)
    )
    detail_value = item.get("aggregatedOutput") or item.get("output") or item.get("result") or ""
    detail = detail_value if isinstance(detail_value, str) else json.dumps(detail_value, ensure_ascii=False)
    failed = str(item.get("status") or "").lower() in {"failed", "error"}
    return ExternalToolEvent(
        name=names[item_type],
        phase="error" if phase == "result" and failed else phase,
        detail=detail,
        tool_call_id=item_id,
        args=args,
    )


async def _spawn_app_server(
    request: RuntimeExecutionRequest,
) -> tuple[asyncio.subprocess.Process, _CodexRpcClient, list[bytes], asyncio.Task[None]]:
    from crew.security.launch import host_stream_launch_block_reason

    blocked = host_stream_launch_block_reason()
    if blocked:
        raise CodexAdapterError(f"严格安全约束已拒绝 Codex 宿主流式启动：{blocked}")
    env = build_external_runtime_env(request.custom_env)
    cwd = str(Path(request.cwd or ".").expanduser().resolve())
    try:
        proc = await asyncio.create_subprocess_exec(
            request.executable_path,
            "app-server",
            "--listen",
            "stdio://",
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=CODEX_STREAM_LIMIT_BYTES,
            **isolated_process_kwargs(),
        )
    except FileNotFoundError as exc:
        raise CodexAdapterError(f"找不到可执行文件: {request.executable_path}") from exc
    stderr_parts: list[bytes] = []

    async def _drain() -> None:
        if proc.stderr is None:
            return
        while True:
            chunk = await proc.stderr.read(64 * 1024)
            if not chunk:
                return
            stderr_parts.append(chunk)
            if sum(map(len, stderr_parts)) > 64 * 1024:
                stderr_parts[:] = [b"".join(stderr_parts)[-64 * 1024:]]

    stderr_task = asyncio.create_task(_drain())
    client = _CodexRpcClient(proc)
    await client.start()
    return proc, client, stderr_parts, stderr_task


async def _initialize(client: _CodexRpcClient, timeout: float) -> None:
    await client.request(
        "initialize",
        {
            "clientInfo": {"name": "crew", "title": "Crew", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        },
        timeout=min(timeout, 10.0),
    )
    await client.notify("initialized")


def _codex_thread_config(request: RuntimeExecutionRequest) -> dict[str, Any]:
    if not request.mcp_servers:
        return {}
    return {
        "mcp_servers": {
            server.name: {
                key: value
                for key, value in server.stdio_config().items()
                if key != "name"
            }
            for server in request.mcp_servers
        }
    }


def _codex_thread_params(request: RuntimeExecutionRequest) -> dict[str, Any]:
    params: dict[str, Any] = {
        "cwd": str(Path(request.cwd or ".").expanduser().resolve()),
        "approvalPolicy": "on-request",
        "sandbox": "workspace-write",
    }
    if request.model and request.model != "default":
        params["model"] = request.model
    if request.system_prompt:
        params["developerInstructions"] = request.system_prompt
    if request.dynamic_tools:
        params["dynamicTools"] = list(request.dynamic_tools)
    config = _codex_thread_config(request)
    if config:
        params["config"] = config
    return params


async def _stream_codex_app_server(
    request: RuntimeExecutionRequest,
) -> AsyncIterator[ExternalStreamEvent]:
    proc, client, stderr_parts, stderr_task = await _spawn_app_server(request)
    thread_created = False
    terminal_received = False
    try:
        try:
            await _initialize(client, request.timeout)
        except CodexAdapterError as exc:
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
            stderr = b"".join(stderr_parts).decode("utf-8", errors="replace").lower()
            if proc.returncode is not None or "unrecognized subcommand" in stderr:
                raise CodexAppServerUnsupported(str(exc)) from exc
            raise

        thread_result: Any = None
        resumed = False
        if request.resume_session_id:
            try:
                resume_params = _codex_thread_params(request)
                resume_params["threadId"] = request.resume_session_id
                thread_result = await client.request(
                    "thread/resume",
                    resume_params,
                    timeout=min(request.timeout, 15.0),
                )
                resumed = True
            except CodexAdapterError as exc:
                raise RuntimeResumeRejected(str(exc)) from exc
        if thread_result is None:
            thread_result = await client.request(
                "thread/start",
                _codex_thread_params(request),
                timeout=min(request.timeout, 15.0),
            )
        thread_id = _result_id(thread_result, "threadId") or request.resume_session_id
        if not thread_id:
            raise CodexAdapterError("Codex thread/start 未返回 thread ID")
        thread_created = True
        yield ExternalStreamEvent(
            kind="session",
            session_id=thread_id,
            session_resumed=resumed,
            session_reset=False,
        )

        turn_result = await client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": request.prompt}],
                # Balanced default until Crew exposes a per-session effort selector. Codex only
                # emits display-safe reasoning summaries when both an effort and summary mode are
                # requested. These map to Crew's timeline; raw chain-of-thought is never exposed.
                "effort": "medium",
                "summary": "concise",
            },
            timeout=min(request.timeout, 15.0),
        )
        turn_id = _result_id(turn_result, "turnId")
        if not turn_id:
            raise CodexAdapterError("Codex turn/start 未返回 turn ID")

        while True:
            try:
                payload = await asyncio.wait_for(client.events.get(), timeout=request.timeout)
            except asyncio.TimeoutError as exc:
                raise CodexAdapterError("Codex 模型响应空闲超时") from exc
            method = str(payload.get("method") or "")
            params = payload.get("params")
            params = params if isinstance(params, dict) else {}
            event_thread, event_turn = _event_scope(params)
            if not event_thread or not event_turn:
                continue
            if event_thread != thread_id or event_turn != turn_id:
                continue

            if "id" in payload and method:
                if method == "item/tool/call":
                    tool_name = str(params.get("tool") or "").strip()
                    namespace = str(params.get("namespace") or "").strip()
                    display_name = f"{namespace}.{tool_name}" if namespace else tool_name
                    call_id = str(params.get("callId") or payload.get("id") or "").strip()
                    arguments = params.get("arguments")
                    arguments = arguments if isinstance(arguments, dict) else {}
                    yield ExternalStreamEvent(
                        kind="tool",
                        tool=ExternalToolEvent(
                            name=tool_name or display_name or "dynamic_tool",
                            phase="start",
                            tool_call_id=call_id,
                            args=json.dumps(arguments, ensure_ascii=False),
                        ),
                    )
                    if request.dynamic_tool_handler is None:
                        detail = f"未配置 dynamic tool handler: {display_name or '<unknown>'}"
                        await client.respond(
                            payload.get("id"),
                            {
                                "success": False,
                                "contentItems": [{"type": "inputText", "text": detail}],
                            },
                        )
                        yield ExternalStreamEvent(
                            kind="tool",
                            tool=ExternalToolEvent(
                                name=tool_name or display_name or "dynamic_tool",
                                phase="error",
                                detail=detail,
                                tool_call_id=call_id,
                            ),
                        )
                        continue
                    try:
                        result = await request.dynamic_tool_handler(
                            tool_name,
                            arguments,
                            namespace=namespace,
                        )
                        detail = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                        await client.respond(
                            payload.get("id"),
                            {
                                "success": True,
                                "contentItems": [{"type": "inputText", "text": detail}],
                            },
                        )
                        yield ExternalStreamEvent(
                            kind="tool",
                            tool=ExternalToolEvent(
                                name=tool_name or display_name or "dynamic_tool",
                                phase="result",
                                detail=detail,
                                tool_call_id=call_id,
                            ),
                        )
                    except Exception as exc:  # noqa: BLE001 - tool error must return to Codex turn
                        detail = str(exc) or type(exc).__name__
                        await client.respond(
                            payload.get("id"),
                            {
                                "success": False,
                                "contentItems": [{"type": "inputText", "text": detail}],
                            },
                        )
                        yield ExternalStreamEvent(
                            kind="tool",
                            tool=ExternalToolEvent(
                                name=tool_name or display_name or "dynamic_tool",
                                phase="error",
                                detail=detail,
                                tool_call_id=call_id,
                            ),
                        )
                    continue
                if "requestApproval" in method or method.endswith("/approval"):
                    item = params.get("item") if isinstance(params.get("item"), dict) else params
                    tool = _item_tool(item, phase="start") or ExternalToolEvent(
                        name=str(params.get("toolName") or "tool"),
                        phase="start",
                        tool_call_id=str(params.get("itemId") or payload.get("id") or ""),
                        args=json.dumps(params, ensure_ascii=False),
                    )
                    permission = ExternalPermissionRequest(
                        request_id=str(payload.get("id") or ""),
                        session_id=thread_id,
                        tool_call={
                            "toolCallId": tool.tool_call_id,
                            "title": tool.name,
                            "rawInput": {
                                "name": tool.name,
                                # 权限分类器从 arguments 里取 command/path 做判定；
                                # 审批请求的参数嵌套在 item 里（如 item.command），
                                # 直接给 params 会让分类器看不到命令内容而一律 deny。
                                "arguments": item,
                            },
                        },
                        raw_params=params,
                    )
                    decision = (
                        await request.permission_handler(permission)
                        if request.permission_handler is not None
                        else "deny"
                    )
                    await client.respond(
                        payload.get("id"),
                        {"decision": "accept" if decision == "allow" else "decline"},
                    )
                else:
                    await client.respond_error(
                        payload.get("id"),
                        -32601,
                        "Unsupported client request",
                    )
                continue

            if method == "item/agentMessage/delta":
                text = str(params.get("delta") or "")
                if text:
                    yield ExternalStreamEvent(kind="text", text=text)
            elif method in {
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
                "item/reasoning/delta",
            }:
                text = str(params.get("delta") or "")
                if text:
                    yield ExternalStreamEvent(kind="thinking", text=text)
            elif method in {"item/started", "item/completed"}:
                tool = _item_tool(
                    params.get("item"),
                    phase="start" if method == "item/started" else "result",
                )
                if tool is not None:
                    yield ExternalStreamEvent(kind="tool", tool=tool)
            elif method == "thread/tokenUsage/updated":
                usage = _usage(params)
                if usage:
                    yield ExternalStreamEvent(kind="usage", usage=usage)
            elif method == "turn/completed":
                terminal_received = True
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
                status = str(turn.get("status") or "").lower()
                if status in {"failed", "error"}:
                    error = turn.get("error")
                    detail = str(error.get("message") or error) if isinstance(error, dict) else str(error or "")
                    raise CodexAdapterError(detail or "Codex turn 执行失败")
                break
    except asyncio.CancelledError:
        await terminate_process_tree(proc)
        raise
    except CodexAppServerUnsupported:
        raise
    except Exception:
        if not thread_created and proc.returncode is not None:
            stderr = b"".join(stderr_parts).decode("utf-8", errors="replace")
            if "unrecognized subcommand" in stderr.lower():
                raise CodexAppServerUnsupported(stderr.strip())
        raise
    finally:
        await client.close()
        if terminal_received:
            await finish_process_after_terminal(proc, stdin=proc.stdin)
        else:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            await terminate_process_tree(proc)
        await asyncio.gather(stderr_task, return_exceptions=True)


async def stream_codex_events(
    request: RuntimeExecutionRequest,
) -> AsyncIterator[ExternalStreamEvent]:
    try:
        async for event in _stream_codex_app_server(request):
            yield event
    except CodexAppServerUnsupported:
        # This path is safe only because app-server failed before thread/start.
        if request.mcp_servers:
            raise CodexAdapterError(
                "当前 Codex 不支持 app-server，无法安全注入 Crew MCP；请升级 Codex Runtime"
            )
        from crew.agent.external.cli_adapter import ExternalCliConfig, run_external_cli

        output = await run_external_cli(
            ExternalCliConfig(
                provider="codex",
                executable_path=request.executable_path,
                prompt=request.prompt,
                model=request.model if request.model != "default" else "",
                cwd=request.cwd,
                system_prompt=request.system_prompt,
                custom_env=request.custom_env,
                timeout=request.timeout,
            )
        )
        if output:
            yield ExternalStreamEvent(kind="text", text=output)


class CodexAppServerAdapter:
    adapter_id = "codex-app-server"

    async def probe(
        self,
        executable_path: str,
        *,
        provider: str,
        launch_args: tuple[str, ...] = (),
        custom_env: dict[str, str] | None = None,
    ) -> RuntimeAdapterProbe:
        del provider, launch_args
        request = RuntimeExecutionRequest(
            executable_path=executable_path,
            provider="codex",
            prompt="",
            custom_env=custom_env or {},
            timeout=8.0,
        )
        proc, client, stderr_parts, stderr_task = await _spawn_app_server(request)
        app_server_supported = True
        try:
            try:
                await _initialize(client, 8.0)
            except Exception:
                # Discovery remains backward compatible with older Codex
                # builds. Execution will use the same pre-thread safe fallback.
                app_server_supported = False
        finally:
            await client.close()
            await terminate_process_tree(proc)
            await asyncio.gather(stderr_task, return_exceptions=True)

        try:
            from crew.agent.external.cli_adapter import probe_cli_runtime

            catalog = await probe_cli_runtime(
                executable_path,
                provider="codex",
                custom_env=custom_env,
            )
            models = catalog.models
            default_model_id = catalog.default_model_id
        except Exception:
            fallback = RuntimeModelProfile(
                id="default",
                label="CLI 默认模型",
                provider="openai",
                default=True,
                capabilities=("text", "tools"),
            )
            models = [fallback]
            default_model_id = fallback.id
        return RuntimeAdapterProbe(
            models=models,
            default_model_id=default_model_id,
            capabilities=RuntimeCapabilities(
                session_resume=app_server_supported,
                model_switch=True,
                images=True,
                tool_events=True,
                streaming=app_server_supported,
                usage=app_server_supported,
                approval=app_server_supported,
                mcp_servers=app_server_supported,
            ),
            source="codex_app_server" if app_server_supported else "codex_exec_compat",
        )

    def stream(self, request: RuntimeExecutionRequest) -> AsyncIterator[ExternalStreamEvent]:
        return stream_codex_events(request)


register_runtime_adapter(CodexAppServerAdapter())
