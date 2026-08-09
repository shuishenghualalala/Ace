"""Generic ACP JSON-RPC adapter for external agents."""

from __future__ import annotations

import asyncio
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Literal, Protocol

from crew.agent.external.process_lifecycle import isolated_process_kwargs, terminate_process_tree
from crew.agent.external.runtime_adapter import (
    ExternalStreamEvent,
    ExternalToolEvent,
    RuntimeAdapterProbe,
    RuntimeExecutionRequest,
    RuntimeResumeRejected,
    build_external_runtime_env,
    build_managed_external_runtime_env,
    register_runtime_adapter,
)
from crew.agent.external.runtime_profile import RuntimeCapabilities, RuntimeModelProfile
from crew.security.models import AdditionalPermissionProfile
from crew.security.runtime_client import NativeRuntimeError
from crew.state.logging import get_logger


log = get_logger("agent.acp")

ACP_STREAM_LIMIT_BYTES = 64 * 1024 * 1024
ACP_RUNTIME_PROBE_TIMEOUT_SECONDS = 15.0
HERMES_RUNTIME_PROBE_TIMEOUT_SECONDS = 30.0


class AcpAdapterError(RuntimeError):
    pass


PermissionDecision = Literal["allow", "deny"]


@dataclass(frozen=True)
class AcpPermissionRequest:
    """Normalized inbound ACP permission request.

    Policy stays outside the protocol adapter.  The adapter only validates the
    runtime-advertised options and maps an allow/deny decision back to one of
    those exact option ids.
    """

    session_id: str
    tool_call: dict[str, Any]
    options: tuple[dict[str, Any], ...]
    raw_params: dict[str, Any]


PermissionHandler = Callable[[AcpPermissionRequest], Awaitable[PermissionDecision]]


@dataclass
class AcpAdapterConfig:
    executable_path: str
    provider: str = ""
    launch_args: list[str] = field(default_factory=lambda: ["acp"])
    model: str = ""
    cwd: str = "."
    system_prompt: str = ""
    custom_args: list[str] = field(default_factory=list)
    custom_env: dict[str, str] = field(default_factory=dict)
    additional_permissions: AdditionalPermissionProfile = field(
        default_factory=AdditionalPermissionProfile
    )
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    resume_session_id: str = ""
    timeout: float = 120.0
    permission_handler: PermissionHandler | None = None


AcpToolEvent = ExternalToolEvent
AcpStreamEvent = ExternalStreamEvent


class _AcpTransport(Protocol):
    async def read(self) -> bytes | None: ...

    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None: ...

    async def abort(self) -> None: ...


class _SubprocessAcpTransport:
    """Adapter for the legacy host process used only outside managed mode."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    async def read(self) -> bytes | None:
        if not self.proc.stdout:
            return None
        try:
            data = await self.proc.stdout.read(64 * 1024)
        except ValueError as exc:
            raise AcpAdapterError(
                f"ACP stdout JSONL 单行超过读取上限 ({ACP_STREAM_LIMIT_BYTES // 1024 // 1024}MB): {exc}"
            ) from exc
        return data or None

    async def write(self, data: bytes) -> None:
        if not self.proc.stdin:
            raise AcpAdapterError("ACP stdin is closed")
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def close(self) -> None:
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()

    async def abort(self) -> None:
        await terminate_process_tree(self.proc)


class _NativeAcpTransport:
    """Protocol-neutral ACP view over the managed native interactive session."""

    def __init__(self, session: Any) -> None:
        self.session = session
        self.stderr_lines = session.stderr_lines

    async def read(self) -> bytes | None:
        return await self.session.read_chunk()

    async def write(self, data: bytes) -> None:
        await self.session.write(data)

    async def close(self) -> None:
        await self.session.close()

    async def abort(self) -> None:
        await self.session.abort()


@dataclass(frozen=True)
class AcpRuntimeProbeResult:
    models: list[RuntimeModelProfile]
    default_model_id: str
    capabilities: RuntimeCapabilities


def _build_session_new_params(cwd: str, mcp_servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Build ACP session/new params with MCP field compatibility.

    Kimi accepts the camelCase ACP shape, while Hermes currently reads the
    snake_case Python signature. New ACP runtimes should support at least one.
    """
    return {
        "cwd": cwd,
        "mcpServers": mcp_servers,
        "mcp_servers": mcp_servers,
    }


def _build_session_resume_params(cwd: str, session_id: str, mcp_servers: list[dict[str, Any]]) -> dict[str, Any]:
    """Build ACP session/resume params with provider compatibility fields."""
    return {
        "cwd": cwd,
        "sessionId": session_id,
        "session_id": session_id,
        "mcpServers": mcp_servers,
        "mcp_servers": mcp_servers,
    }


def _normalize_permission_request(params: dict[str, Any]) -> AcpPermissionRequest:
    tool_call = params.get("toolCall") or params.get("tool_call") or {}
    if not isinstance(tool_call, dict):
        tool_call = {}
    raw_options = params.get("options") or []
    options = tuple(item for item in raw_options if isinstance(item, dict)) if isinstance(raw_options, list) else ()
    return AcpPermissionRequest(
        session_id=str(params.get("sessionId") or params.get("session_id") or "").strip(),
        tool_call=dict(tool_call),
        options=options,
        raw_params=dict(params),
    )


def _permission_option_id(option: dict[str, Any]) -> str:
    return str(option.get("optionId") or option.get("option_id") or option.get("id") or "").strip()


def _permission_option_kind(option: dict[str, Any]) -> str:
    return str(option.get("kind") or option.get("type") or "").strip().lower()


def _select_permission_option(
    options: tuple[dict[str, Any], ...],
    decision: PermissionDecision,
) -> str:
    if decision == "allow":
        # Never manufacture or broaden a permission.  A one-shot option is the
        # only automatic grant Crew emits; session/permanent grants remain out
        # of scope even if a runtime advertises them.
        preferred_kinds = {"allow_once", "allow-once"}
        preferred_ids = {"allow_once", "approve_once"}
    else:
        preferred_kinds = {"reject_once", "deny_once", "reject-once"}
        preferred_ids = {"deny", "reject", "deny_once", "reject_once"}
    for option in options:
        option_id = _permission_option_id(option)
        if option_id and _permission_option_kind(option) in preferred_kinds:
            return option_id
    for option in options:
        option_id = _permission_option_id(option)
        if option_id.lower() in preferred_ids:
            return option_id
    return ""


def _permission_result(
    options: tuple[dict[str, Any], ...],
    decision: PermissionDecision,
) -> dict[str, Any]:
    option_id = _select_permission_option(options, decision)
    if option_id:
        return {
            "outcome": {
                "outcome": "selected",
                "optionId": option_id,
            }
        }
    if decision == "allow":
        log.warning("ACP permission request has no one-shot allow option; request cancelled")
    else:
        log.warning("ACP permission request has no reject option; request cancelled")
    return {"outcome": {"outcome": "cancelled"}}


class _JsonRpcClient:
    def __init__(
        self,
        transport: _AcpTransport,
        *,
        permission_handler: PermissionHandler | None = None,
    ) -> None:
        self.transport = transport
        self.permission_handler = permission_handler
        self.next_id = 1
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.output: list[str] = []
        self.text_queue: asyncio.Queue[str] = asyncio.Queue()
        self.event_queue: asyncio.Queue[AcpStreamEvent] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._anonymous_tool_seq = 0
        self._anonymous_tools: dict[str, list[str]] = {}
        # Some ACP runtimes (notably Kimi) stream a tool's JSON input before
        # session/request_permission, but omit rawInput from the permission
        # request itself. Keep only guard-relevant fields, correlated by the
        # runtime tool-call id, so policy can verify the actual workspace path.
        self._permission_tool_inputs: dict[str, dict[str, Any]] = {}
        self.accept_stream_events = True

    async def start(self) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())

    async def close(self, *, abort: bool = False) -> None:
        if abort:
            await self.transport.abort()
        else:
            await self.transport.close()
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        msg_id = self.next_id
        self.next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[msg_id] = fut
        await self._write({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}})
        raw = await fut
        if "error" in raw:
            err = raw.get("error") or {}
            message = err.get("message") or err
            code = err.get("code")
            data = err.get("data")
            detail = f"{method}: {message}"
            if code is not None:
                detail += f" (code={code}"
                if data not in (None, ""):
                    detail += f", data={data}"
                detail += ")"
            elif data not in (None, ""):
                detail += f" (data={data})"
            raise AcpAdapterError(detail)
        return raw.get("result")

    async def _write(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        await self.transport.write(data)

    async def _read_loop(self) -> None:
        buffer = bytearray()
        try:
            while True:
                chunk = await self.transport.read()
                if not chunk:
                    break
                buffer.extend(chunk)
                if len(buffer) > ACP_STREAM_LIMIT_BYTES:
                    raise AcpAdapterError(
                        f"ACP stdout JSONL 超过读取上限 ({ACP_STREAM_LIMIT_BYTES // 1024 // 1024}MB)"
                    )
                while b"\n" in buffer:
                    index = buffer.index(b"\n")
                    line = bytes(buffer[:index])
                    del buffer[: index + 1]
                    await self._handle_line(line)
            if buffer:
                await self._handle_line(bytes(buffer))
        except (AcpAdapterError, NativeRuntimeError) as exc:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            self.pending.clear()
            await self.event_queue.put(AcpStreamEvent(kind="error", text=str(exc)))
        finally:
            if self.pending:
                err = AcpAdapterError("ACP process exited before responding")
                for fut in self.pending.values():
                    if not fut.done():
                        fut.set_exception(err)
                self.pending.clear()

    async def _handle_line(self, line: bytes) -> None:
        if not line:
            return
        try:
            raw = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        if "id" in raw and ("result" in raw or "error" in raw):
            try:
                msg_id = int(raw["id"])
            except Exception:
                return
            fut = self.pending.pop(msg_id, None)
            if fut and not fut.done():
                fut.set_result(raw)
            return
        if "id" in raw and "method" in raw:
            await self._handle_agent_request(raw)
            return
        if raw.get("method") in {"session/update", "session/notification"}:
            self._handle_notification(raw)

    async def _handle_agent_request(self, raw: dict[str, Any]) -> None:
        method = raw.get("method")
        msg_id = raw.get("id")
        if method == "session/request_permission":
            params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
            request = self._enrich_permission_request(_normalize_permission_request(params))
            decision: PermissionDecision = "deny"
            if self.permission_handler is not None:
                try:
                    decision = await self.permission_handler(request)
                except Exception as exc:  # noqa: BLE001 - permission failures must fail closed
                    log.warning("ACP permission handler failed; denying request: %s", exc)
            result = _permission_result(request.options, decision)
            outcome = result.get("outcome") if isinstance(result, dict) else {}
            log.info(
                "ACP permission resolved tool_call=%s decision=%s outcome=%s option=%s",
                _permission_tool_call_id(request.tool_call) or "unknown",
                decision,
                outcome.get("outcome") if isinstance(outcome, dict) else "unknown",
                outcome.get("optionId") if isinstance(outcome, dict) else "",
            )
            await self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
            self._permission_tool_inputs.pop(_permission_tool_call_id(request.tool_call), None)
            return
        await self._write({
            "jsonrpc": "2.0",
            "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        })

    def _handle_notification(self, raw: dict[str, Any]) -> None:
        if not self.accept_stream_events:
            return
        params = raw.get("params") or {}
        # ACP implementations differ here: the spec shape nests the payload in
        # params.update, while some Python runtimes put sessionUpdate directly
        # under params. Accept both so valid process events are not discarded.
        update = params.get("update", params)
        update_type, data = _normalize_update(update)
        if update_type == "agent_message_chunk":
            text = _extract_text(data)
            if text:
                self.output.append(text)
                self.text_queue.put_nowait(text)
                self.event_queue.put_nowait(AcpStreamEvent(kind="text", text=text))
            return
        if update_type == "agent_thought_chunk":
            text = _extract_text(data)
            if text:
                self.event_queue.put_nowait(AcpStreamEvent(kind="thinking", text=text))
            return
        tool_event = _extract_tool_event(update_type, data)
        if tool_event:
            if not tool_event.tool_call_id:
                pending = self._anonymous_tools.setdefault(tool_event.name, [])
                if tool_event.phase == "start":
                    self._anonymous_tool_seq += 1
                    tool_event.tool_call_id = f"acp_tool_{self._anonymous_tool_seq}"
                    pending.append(tool_event.tool_call_id)
                elif pending:
                    tool_event.tool_call_id = pending.pop(0)
                else:
                    self._anonymous_tool_seq += 1
                    tool_event.tool_call_id = f"acp_tool_{self._anonymous_tool_seq}"
            self._remember_permission_tool_input(tool_event)
            self.event_queue.put_nowait(AcpStreamEvent(kind="tool", tool=tool_event))

    def _remember_permission_tool_input(self, event: AcpToolEvent) -> None:
        call_id = str(event.tool_call_id or "").strip()
        if not call_id:
            return
        if event.phase in {"result", "error"}:
            self._permission_tool_inputs.pop(call_id, None)
            return
        snapshot = self._permission_tool_inputs.setdefault(call_id, {})
        if event.name and event.name != "external_tool":
            snapshot["tool"] = event.name
        arguments = _permission_arguments(event.args) or _permission_arguments(event.detail)
        if arguments:
            snapshot["arguments"] = arguments
        while len(self._permission_tool_inputs) > 256:
            self._permission_tool_inputs.pop(next(iter(self._permission_tool_inputs)))

    def _enrich_permission_request(self, request: AcpPermissionRequest) -> AcpPermissionRequest:
        call_id = _permission_tool_call_id(request.tool_call)
        snapshot = self._permission_tool_inputs.get(call_id)
        call = dict(request.tool_call)
        raw_input = call.get("rawInput") or call.get("raw_input") or {}
        raw = dict(raw_input) if isinstance(raw_input, dict) else {}
        if not raw.get("tool"):
            raw["tool"] = (
                (snapshot or {}).get("tool")
                or call.get("name")
                or call.get("toolName")
                or call.get("tool_name")
                or call.get("title")
                or ""
            )
        if not isinstance(raw.get("arguments"), dict) and isinstance((snapshot or {}).get("arguments"), dict):
            raw["arguments"] = dict(snapshot["arguments"])
        if raw:
            call["rawInput"] = raw
        return AcpPermissionRequest(
            session_id=request.session_id,
            tool_call=call,
            options=request.options,
            raw_params=request.raw_params,
        )


def _normalize_update(update: Any) -> tuple[str, Any]:
    if not isinstance(update, dict):
        return "", update
    raw_type = update.get("sessionUpdate") or update.get("type")
    if raw_type:
        return _normalize_update_type(str(raw_type)), update
    if len(update) == 1:
        key, value = next(iter(update.items()))
        return _normalize_update_type(str(key)), value
    return "", update


def _permission_tool_call_id(tool_call: dict[str, Any] | None) -> str:
    call = tool_call if isinstance(tool_call, dict) else {}
    return str(
        call.get("toolCallId")
        or call.get("tool_call_id")
        or call.get("callId")
        or call.get("id")
        or ""
    ).strip()


def _permission_arguments(value: Any) -> dict[str, Any]:
    """Extract only fields needed by workspace policy from a streamed tool input."""
    candidate = value
    if isinstance(candidate, str):
        text = candidate.strip()
        if not text or not (text.startswith("{") and text.endswith("}")):
            return {}
        try:
            candidate = json.loads(text)
        except json.JSONDecodeError:
            return {}
    if not isinstance(candidate, dict):
        return {}
    nested = candidate.get("arguments")
    if isinstance(nested, dict):
        candidate = nested
    result: dict[str, Any] = {}
    for key in ("path", "filePath", "file_path"):
        path = candidate.get(key)
        if isinstance(path, str) and path.strip():
            result["path"] = path.strip()
            break
    for key in ("command", "raw"):
        command = candidate.get(key)
        if isinstance(command, str) and command.strip():
            result[key] = command.strip()
    return result


def _normalize_update_type(value: str) -> str:
    key = value.strip().replace("_", "").replace("-", "").lower()
    if key == "agentmessagechunk":
        return "agent_message_chunk"
    if key in {"agentthoughtchunk", "agentthinkingchunk", "agentreasoningchunk"}:
        return "agent_thought_chunk"
    if key in {"toolcall", "tooluse", "toolinvocation"}:
        return "tool_start"
    if key in {"toolcallupdate", "toolupdate", "toolinvocationupdate"}:
        return "tool_update"
    if key in {"toolcallresult", "toolresult", "toolcallcomplete", "toolcomplete"}:
        return "tool_result"
    return ""


def _json_dumps(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except TypeError:
        return str(value)


def _first_str(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _extract_tool_name(data: dict[str, Any]) -> str:
    name = _first_str(data, "name", "toolName", "tool_name", "title")
    if name:
        return name
    tool = data.get("tool")
    if isinstance(tool, dict):
        return _first_str(tool, "name", "toolName", "tool_name")
    return "external_tool"


def _extract_tool_args(data: dict[str, Any]) -> str:
    for key in ("arguments", "args", "input", "parameters"):
        if key in data:
            return _json_dumps(data.get(key))
    tool = data.get("tool")
    if isinstance(tool, dict):
        for key in ("arguments", "args", "input", "parameters"):
            if key in tool:
                return _json_dumps(tool.get(key))
    return ""


def _extract_tool_detail(data: dict[str, Any]) -> str:
    for key in ("detail", "content", "output", "result", "error", "message"):
        if key not in data:
            continue
        value = data.get(key)
        if isinstance(value, dict):
            text = _extract_text({"content": value})
            return text or _json_dumps(value)
        if isinstance(value, list):
            text = _extract_text({"content": value})
            return text or _json_dumps(value)
        return _json_dumps(value)
    return ""


def _extract_tool_event(update_type: str, data: Any) -> AcpToolEvent | None:
    if update_type not in {"tool_start", "tool_update", "tool_result"}:
        return None
    if not isinstance(data, dict):
        return None
    for key in ("toolCall", "tool_call", "toolUse", "tool_use", "toolResult", "tool_result"):
        nested = data.get(key)
        if isinstance(nested, dict):
            data = {
                **nested,
                "id": nested.get("id") or data.get("id"),
                "toolCallId": nested.get("toolCallId") or data.get("toolCallId"),
                "tool_call_id": nested.get("tool_call_id") or data.get("tool_call_id"),
                "status": nested.get("status") or data.get("status"),
                "state": nested.get("state") or data.get("state"),
                "phase": nested.get("phase") or data.get("phase"),
            }
            break
    status = str(data.get("status") or data.get("state") or data.get("phase") or "").lower()
    if status in {"failed", "failure", "error", "errored"}:
        phase = "error"
    elif update_type == "tool_result" or status in {"completed", "complete", "done", "finished", "succeeded", "success"}:
        phase = "result"
    else:
        phase = "start"
    tool_call_id = _first_str(data, "toolCallId", "tool_call_id", "callId", "id")
    return AcpToolEvent(
        name=_extract_tool_name(data),
        phase=phase,
        detail=_extract_tool_detail(data),
        tool_call_id=tool_call_id,
        args=_extract_tool_args(data),
    )


def _extract_text(data: Any) -> str:
    """Extract ACP text blocks across camel/snake and nested runtime variants."""
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        return "".join(_extract_text(item) for item in data)
    if not isinstance(data, dict):
        return ""
    direct = data.get("text")
    if isinstance(direct, str):
        return direct
    content = data.get("content")
    if content is not None:
        return _extract_text(content)
    for key in ("message", "delta", "chunk"):
        if key in data:
            text = _extract_text(data.get(key))
            if text:
                return text
    return ""


def _extract_session_id(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    for key in ("sessionId", "session_id", "id"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    session = result.get("session")
    if isinstance(session, dict):
        value = session.get("id") or session.get("sessionId")
        if isinstance(value, str):
            return value
    return ""


def _extract_session_models(result: Any) -> tuple[list[RuntimeModelProfile], str]:
    if not isinstance(result, dict):
        return [], ""
    state = result.get("models")
    if not isinstance(state, dict):
        return [], ""
    raw_models = state.get("availableModels")
    if not isinstance(raw_models, list):
        raw_models = state.get("available_models")
    if not isinstance(raw_models, list):
        raw_models = []
    current = str(state.get("currentModelId") or state.get("current_model_id") or "").strip()
    models: list[RuntimeModelProfile] = []
    seen: set[str] = set()
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        model_id = str(raw.get("modelId") or raw.get("model_id") or raw.get("id") or "").strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        provider = ""
        if ":" in model_id:
            provider = model_id.split(":", 1)[0]
        elif "/" in model_id:
            provider = model_id.split("/", 1)[0]
        capabilities = raw.get("capabilities") or []
        models.append(RuntimeModelProfile(
            id=model_id,
            label=str(raw.get("name") or raw.get("label") or model_id).strip() or model_id,
            provider=provider,
            default=model_id == current,
            capabilities=tuple(str(item).strip() for item in capabilities if str(item).strip())
            if isinstance(capabilities, list) else (),
        ))
    return models, current


def _model_config_option_id(result: Any) -> str:
    """Return the ACP session config id whose category is ``model``."""

    if not isinstance(result, dict):
        return ""
    options = result.get("configOptions")
    if not isinstance(options, list):
        options = result.get("config_options")
    if not isinstance(options, list):
        return ""
    for option in options:
        if not isinstance(option, dict):
            continue
        category = option.get("category")
        category_name = (
            str(category.get("type") or category.get("id") or "")
            if isinstance(category, dict)
            else str(category or "")
        ).strip().lower()
        if category_name == "model":
            return str(option.get("id") or option.get("configId") or option.get("config_id") or "").strip()
    return ""


async def probe_acp_runtime(
    executable_path: str,
    *,
    provider: str,
    launch_args: list[str] | tuple[str, ...],
    custom_env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> AcpRuntimeProbeResult:
    """Open a throwaway ACP session and return its advertised model catalog."""

    env = build_external_runtime_env(custom_env)
    proc = await asyncio.create_subprocess_exec(
        executable_path,
        *launch_args,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        limit=ACP_STREAM_LIMIT_BYTES,
    )
    client = _JsonRpcClient(_SubprocessAcpTransport(proc))
    await client.start()
    try:
        async def _probe() -> AcpRuntimeProbeResult:
            await client.request("initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "crew-runtime-discovery", "version": "0.1.0"},
                "clientCapabilities": {},
            })
            with tempfile.TemporaryDirectory(prefix=f"crew-{provider or 'acp'}-probe-") as cwd:
                result = await client.request("session/new", _build_session_new_params(cwd, []))
            models, current = _extract_session_models(result)
            return AcpRuntimeProbeResult(
                models=models,
                default_model_id=current,
                capabilities=RuntimeCapabilities(
                    session_resume=True,
                    model_switch=bool(models),
                    mcp_servers=True,
                    images=True,
                    tool_events=True,
                    streaming=True,
                    approval=True,
                ),
            )

        return await asyncio.wait_for(_probe(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise AcpAdapterError(f"{provider or 'ACP'} 运行时探测超时") from exc
    finally:
        await client.close()
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


def _compact_timeout_detail(value: str, *, max_chars: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


def _describe_stream_activity(event: AcpStreamEvent) -> str:
    if event.kind == "session":
        return f"ACP session 已建立: {event.session_id or 'unknown'}"
    if event.kind == "text":
        return "外部模型开始输出文本"
    if event.kind == "thinking":
        return "外部模型输出 thinking"
    if event.kind == "error":
        return f"ACP 错误事件: {_compact_timeout_detail(event.text)}"
    if event.kind == "tool" and event.tool:
        detail = _compact_timeout_detail(event.tool.detail or event.tool.args)
        suffix = f": {detail}" if detail else ""
        return f"工具 {event.tool.name} {event.tool.phase}{suffix}"
    return event.kind or "unknown"


def _format_timeout_error(
    *,
    timeout_kind: str,
    stage: str,
    process_state: str,
    provider: str,
    idle_seconds: float,
    last_activity: str,
) -> str:
    provider_label = str(provider or "external").strip() or "external"
    if timeout_kind == "idle":
        headline = f"{provider_label} 模型响应空闲超时"
        hint = "ACP 子进程仍在运行，但外部模型/CLI 在等待响应期间没有继续发送文本、工具事件或 heartbeat。"
    else:
        headline = f"{provider_label} ACP 调用总时长超时"
        hint = "外部智能体调用超过硬性上限，Crew 已取消本轮 ACP 调用。"
    return (
        f"{headline}（stage={stage}, process={process_state}, idle={idle_seconds:.1f}s, "
        f"last_activity={last_activity or 'unknown'}）。{hint}"
    )


async def stream_acp_events(prompt: str, config: AcpAdapterConfig) -> AsyncIterator[AcpStreamEvent]:
    from crew.security.launch import current_process_launch

    launch = current_process_launch.get()
    if launch is None:
        raise AcpAdapterError(
            "ACP adapter 缺少安全启动上下文：当前运行时未建立 ProcessLaunch（常见于 Team "
            "委派未继承启动边界）。已拒绝在无明确启动决策时启动宿主进程。"
        )
    loop = asyncio.get_running_loop()
    total_started_at = loop.time()
    env = build_external_runtime_env(config.custom_env)
    cwd_path = Path(config.cwd or ".").expanduser().resolve(strict=True)
    cwd = str(cwd_path)
    stderr_lines: list[str] = []
    stage_state = {"name": "spawn"}
    provider_label = str(config.provider or Path(config.executable_path).name or "external").strip()
    last_activity = {"text": "准备启动 ACP 子进程"}
    native_session = None
    stderr_task: asyncio.Task[None] | None = None
    if launch.managed:
        from crew.security.broker import ExecutionRequest, SecurityExecutionBroker
        from crew.security.runtime_client import NativeRuntimeClient

        if not launch.helper_argv:
            raise AcpAdapterError("ACP adapter 缺少 managed native security runtime")
        try:
            executable = str(Path(config.executable_path).expanduser().resolve(strict=True))
        except OSError as exc:
            raise AcpAdapterError(f"找不到 ACP 可执行文件: {config.executable_path}") from exc
        managed_env = build_managed_external_runtime_env(config.custom_env)
        native_session = await SecurityExecutionBroker(
            NativeRuntimeClient(launch.helper_argv)
        ).open_interactive(
            ExecutionRequest(
                command=(executable, *config.launch_args, *config.custom_args),
                cwd=cwd_path,
                permission_profile=launch.profile,
                additional_permissions=config.additional_permissions,
                trusted_readable_roots=launch.trusted_readable_roots,
                env_overrides=managed_env,
                timeout_seconds=config.timeout,
                max_output_bytes=ACP_STREAM_LIMIT_BYTES,
            )
        )
        proc = native_session.process
        stderr_lines = native_session.stderr_lines
        transport: _AcpTransport = _NativeAcpTransport(native_session)
    else:
        # Disabled profiles are retained for existing compatibility/unit-test
        # callers. Production conversation modes compile a managed profile.
        proc = await asyncio.create_subprocess_exec(
            config.executable_path,
            *config.launch_args,
            *config.custom_args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=ACP_STREAM_LIMIT_BYTES,
            **isolated_process_kwargs(),
        )
        transport = _SubprocessAcpTransport(proc)
    log.info(
        "[PERF] acp_spawn provider=%s elapsed=%.3fs model=%s pid=%s",
        provider_label,
        loop.time() - total_started_at,
        config.model or "default",
        proc.pid,
    )
    log.info(
        "[ACP] process started pid=%s executable=%s cwd=%s timeout=%ss mcp_servers=%s custom_args=%s",
        proc.pid,
        config.executable_path,
        cwd,
        config.timeout,
        [str(item.get("name") or item.get("id") or "unnamed") for item in config.mcp_servers],
        len(config.custom_args),
    )
    client = _JsonRpcClient(transport, permission_handler=config.permission_handler)
    try:
        await client.start()
    except Exception:
        await client.close(abort=True)
        raise
    if native_session is None:
        stderr_task = asyncio.create_task(_read_stderr(proc, stderr_lines))
    was_cancelled = False
    try:
        session_state: dict[str, Any] = {
            "id": "",
            "emitted": False,
            "resumed": False,
            "reset": False,
        }

        async def _request_stage(name: str, method: str, params: dict[str, Any] | None = None) -> Any:
            stage_state["name"] = name
            if name == "session/prompt":
                last_activity["text"] = "已发送 session/prompt，等待外部模型响应"
            else:
                last_activity["text"] = f"已发送 {method} 请求"
            log.info("[ACP] stage=%s method=%s pid=%s", name, method, proc.pid)
            stage_started_at = loop.time()
            try:
                result = await client.request(method, params)
            except Exception:
                log.info(
                    "[PERF] acp_stage provider=%s stage=%s elapsed=%.3fs model=%s status=failed",
                    provider_label,
                    name,
                    loop.time() - stage_started_at,
                    config.model or "default",
                )
                raise
            log.info(
                "[PERF] acp_stage provider=%s stage=%s elapsed=%.3fs model=%s status=completed",
                provider_label,
                name,
                loop.time() - stage_started_at,
                config.model or "default",
            )
            return result

        async def _run() -> None:
            await _request_stage("initialize", "initialize", {
                "protocolVersion": 1,
                "clientInfo": {"name": "crew", "version": "0.1.0"},
                "clientCapabilities": {},
            })
            session_id = ""
            session_result: Any = None
            if config.resume_session_id:
                client.accept_stream_events = False
                try:
                    result = await _request_stage(
                        "session/resume",
                        "session/resume",
                        _build_session_resume_params(cwd, config.resume_session_id, config.mcp_servers),
                    )
                    session_result = result
                    session_id = _extract_session_id(result) or config.resume_session_id
                    session_state["resumed"] = session_id == config.resume_session_id
                    session_state["reset"] = session_id != config.resume_session_id
                except AcpAdapterError as exc:
                    raise RuntimeResumeRejected(str(exc)) from exc
                finally:
                    while not client.event_queue.empty():
                        client.event_queue.get_nowait()
                    client.accept_stream_events = True
            else:
                result = await _request_stage(
                    "session/new",
                    "session/new",
                    _build_session_new_params(cwd, config.mcp_servers),
                )
                session_result = result
                session_id = _extract_session_id(result)
            if not session_id:
                raise AcpAdapterError("ACP session/new returned no session ID")
            session_state["id"] = session_id
            requested_model = str(config.model or "").strip()
            _, current_model = _extract_session_models(session_result)
            should_set_model = bool(requested_model) and requested_model != current_model
            if requested_model and not should_set_model:
                log.info(
                    "[ACP] session model already selected; skipping redundant model update model=%s",
                    requested_model,
                )
            if should_set_model:
                config_id = _model_config_option_id(session_result)
                if config_id:
                    try:
                        await _request_stage(
                            "session/set_config_option",
                            "session/set_config_option",
                            {"sessionId": session_id, "configId": config_id, "value": requested_model},
                        )
                    except AcpAdapterError:
                        # 兼容已返回 configOptions、但仍只实现旧 set_model 的 ACP runtime。
                        await _request_stage(
                            "session/set_model",
                            "session/set_model",
                            {"sessionId": session_id, "modelId": requested_model},
                        )
                else:
                    await _request_stage(
                        "session/set_model",
                        "session/set_model",
                        {"sessionId": session_id, "modelId": requested_model},
                    )
            user_text = prompt
            if config.system_prompt:
                user_text = f"{config.system_prompt}\n\n---\n\n{prompt}"
            await _request_stage("session/prompt", "session/prompt", {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": user_text}],
            })

        task = asyncio.create_task(_run())
        idle_timeout = max(0.1, float(config.timeout or 0.0))
        started_at = loop.time()
        first_output_logged = False
        # Process/interpreter startup is not model-response idle time. Give the
        # ACP handshake a small floor, then switch to the configured idle
        # budget as soon as a session or stream event is observed.
        idle_deadline = started_at + max(idle_timeout, 2.0)
        hard_deadline = started_at + max(idle_timeout * 4, idle_timeout + 900.0)

        def _touch_activity() -> None:
            nonlocal idle_deadline
            idle_deadline = loop.time() + idle_timeout

        try:
            while not task.done():
                if session_state["id"] and not session_state["emitted"]:
                    session_state["emitted"] = True
                    _touch_activity()
                    yield AcpStreamEvent(
                        kind="session",
                        session_id=str(session_state["id"]),
                        session_resumed=bool(session_state["resumed"]),
                        session_reset=bool(session_state["reset"]),
                    )
                now = loop.time()
                idle_remaining = idle_deadline - now
                hard_remaining = hard_deadline - now
                if hard_remaining <= 0:
                    task.cancel()
                    proc_state = "running" if proc.returncode is None else f"exited({proc.returncode})"
                    raise AcpAdapterError(_format_timeout_error(
                        timeout_kind="hard",
                        stage=stage_state["name"],
                        process_state=proc_state,
                        provider=provider_label,
                        idle_seconds=loop.time() - started_at,
                        last_activity=last_activity["text"],
                    ))
                if idle_remaining <= 0:
                    task.cancel()
                    proc_state = "running" if proc.returncode is None else f"exited({proc.returncode})"
                    raise AcpAdapterError(_format_timeout_error(
                        timeout_kind="idle",
                        stage=stage_state["name"],
                        process_state=proc_state,
                        provider=provider_label,
                        idle_seconds=idle_timeout,
                        last_activity=last_activity["text"],
                    ))
                try:
                    event = await asyncio.wait_for(
                        client.event_queue.get(),
                        timeout=min(0.2, idle_remaining, hard_remaining),
                    )
                except asyncio.TimeoutError:
                    continue
                _touch_activity()
                last_activity["text"] = _describe_stream_activity(event)
                if not first_output_logged and event.kind in {"text", "thinking", "tool", "error"}:
                    first_output_logged = True
                    log.info(
                        "[PERF] acp_first_output provider=%s elapsed=%.3fs model=%s kind=%s pid=%s",
                        provider_label,
                        loop.time() - total_started_at,
                        config.model or "default",
                        event.kind,
                        proc.pid,
                    )
                yield event
            while not client.event_queue.empty():
                event = client.event_queue.get_nowait()
                _touch_activity()
                last_activity["text"] = _describe_stream_activity(event)
                yield event
            await task
            if session_state["id"] and not session_state["emitted"]:
                _touch_activity()
                yield AcpStreamEvent(
                    kind="session",
                    session_id=str(session_state["id"]),
                    session_resumed=bool(session_state["resumed"]),
                    session_reset=bool(session_state["reset"]),
                )
        except (AcpAdapterError, NativeRuntimeError) as exc:
            if isinstance(exc, NativeRuntimeError):
                exc = AcpAdapterError(str(exc))
            stderr_tail = "\n".join(stderr_lines[-8:]).strip()
            if stderr_tail and stderr_tail not in str(exc):
                raise AcpAdapterError(f"{exc}\nstderr: {stderr_tail}") from exc
            raise
        except Exception:
            if not task.done():
                task.cancel()
            raise
        if not "".join(client.output).strip():
            yield AcpStreamEvent(kind="text", text="ACP 智能体已完成，但没有返回文本输出。")
    except asyncio.CancelledError:
        was_cancelled = True
        raise
    finally:
        if "task" in locals() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if stderr_task is not None:
            stderr_task.cancel()
            try:
                await stderr_task
            except asyncio.CancelledError:
                pass
        await client.close(abort=was_cancelled)
        if native_session is None and not was_cancelled:
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                await terminate_process_tree(proc)
        log.info(
            "[PERF] acp_total provider=%s elapsed=%.3fs model=%s returncode=%s",
            provider_label,
            loop.time() - total_started_at,
            config.model or "default",
            proc.returncode,
        )


async def stream_acp_prompt(prompt: str, config: AcpAdapterConfig) -> AsyncIterator[str]:
    async for event in stream_acp_events(prompt, config):
        if event.kind == "text" and event.text:
            yield event.text


async def run_acp_prompt(prompt: str, config: AcpAdapterConfig) -> str:
    parts: list[str] = []
    async for chunk in stream_acp_prompt(prompt, config):
        parts.append(chunk)
    output = "".join(parts).strip()
    if output:
        return output
    return "ACP 智能体已完成，但没有返回文本输出。"


class AcpRuntimeAdapter:
    """Generic stdio ACP driver shared by compatible runtime descriptors."""

    adapter_id = "acp-stdio"

    async def probe(
        self,
        executable_path: str,
        *,
        provider: str,
        launch_args: tuple[str, ...] = (),
        custom_env: dict[str, str] | None = None,
    ) -> RuntimeAdapterProbe:
        result = await probe_acp_runtime(
            executable_path,
            provider=provider,
            launch_args=launch_args,
            custom_env=custom_env,
            timeout=(
                HERMES_RUNTIME_PROBE_TIMEOUT_SECONDS
                if provider == "hermes"
                else ACP_RUNTIME_PROBE_TIMEOUT_SECONDS
            ),
        )
        models = result.models
        default_model_id = result.default_model_id
        source = "acp_session_new"
        # Kimi 0.26 may omit models from ACP session/new. Keep this narrow
        # compatibility behavior inside the protocol driver, not detector.
        if provider == "kimi" and not models:
            from crew.agent.external.cli_adapter import probe_kimi_model_catalog

            models, default_model_id = await probe_kimi_model_catalog(
                executable_path,
                custom_env=custom_env,
            )
            source = "acp_session_new+kimi_provider_list"
        return RuntimeAdapterProbe(
            models=models,
            default_model_id=default_model_id,
            capabilities=result.capabilities,
            source=source,
        )

    def stream(self, request: RuntimeExecutionRequest) -> AsyncIterator[ExternalStreamEvent]:
        return stream_acp_events(
            request.prompt,
            AcpAdapterConfig(
                executable_path=request.executable_path,
                provider=request.provider,
                launch_args=request.launch_args,
                model=request.model,
                cwd=request.cwd,
                system_prompt=request.system_prompt,
                custom_args=request.custom_args,
                custom_env=request.custom_env,
                additional_permissions=request.additional_permissions,
                mcp_servers=[
                    server.stdio_config(env_as_list=True)
                    for server in request.mcp_servers
                ],
                resume_session_id=request.resume_session_id,
                timeout=request.timeout,
                permission_handler=request.permission_handler,
            ),
        )


register_runtime_adapter(AcpRuntimeAdapter())


async def _read_stderr(proc: asyncio.subprocess.Process, lines: list[str]) -> None:
    if not proc.stderr:
        return
    while True:
        line = await proc.stderr.readline()
        if not line:
            break
        text = line.decode("utf-8", errors="replace").strip()
        if text:
            lines.append(text)
