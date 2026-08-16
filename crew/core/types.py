"""核心数据类型：对话消息、工具调用、LLM 响应。

设计目标：贴近 OpenAI Chat Completions 的数据形状，便于 Provider 适配，
同时保持与具体 SDK 解耦（业务层只依赖这些 dataclass，不依赖 openai 包）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


_FILE_WRITE_UI_ARG_KEYS = ("path", "file_path", "append")
_BROWSER_REF_RE = re.compile(r"^p[1-9]\d*:[es][1-9]\d*$")
_FORM_FIELD_TYPES = ("textbox", "combobox", "checkbox", "radio", "slider")


def _record_replay_arguments_for_display(
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Expose replay structure without retaining runtime form values."""

    workflow_id = arguments.get("workflow_id")
    inputs = arguments.get("inputs")
    safe_inputs: dict[str, dict[str, Any]] = {}
    if isinstance(inputs, dict):
        for index, (key, value) in enumerate(inputs.items()):
            if index >= 32:
                break
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None
            ):
                continue
            if isinstance(value, list):
                safe_inputs[key] = {"type": "list", "count": len(value)}
            elif isinstance(value, str):
                safe_inputs[key] = {"type": "text"}
            elif isinstance(value, bool):
                safe_inputs[key] = {"type": "boolean"}
            else:
                safe_inputs[key] = {"type": type(value).__name__}
    result: dict[str, Any] = {}
    if isinstance(workflow_id, str) and re.fullmatch(
        r"[0-9a-f]{64}",
        workflow_id,
    ):
        result["workflow_id"] = workflow_id
    result["inputs"] = safe_inputs
    return result


def _safe_browser_ref(value: Any) -> str:
    return value if isinstance(value, str) and _BROWSER_REF_RE.fullmatch(value) else ""


def _fill_form_arguments_for_display(fields: Any) -> dict[str, Any]:
    """Project a form batch to structure only; runtime values never enter events/history."""

    if not isinstance(fields, list):
        return {"field_count": 0, "fields": [], "field_types": {}}
    projected: list[dict[str, Any]] = []
    counts = {field_type: 0 for field_type in _FORM_FIELD_TYPES}
    for index, raw in enumerate(fields[:32]):
        item: dict[str, Any] = {"index": index}
        if isinstance(raw, dict):
            field_type = raw.get("type")
            if field_type in counts:
                item["type"] = field_type
                counts[field_type] += 1
            ref = _safe_browser_ref(raw.get("ref"))
            if ref:
                item["ref"] = ref
            select_by = raw.get("select_by")
            if field_type == "combobox" and select_by in {"label", "value"}:
                item["select_by"] = select_by
        projected.append(item)
    result: dict[str, Any] = {
        "field_count": min(len(fields), 32),
        "fields": projected,
        "field_types": {key: count for key, count in counts.items() if count},
    }
    if len(fields) > 32:
        result["truncated"] = True
    return result


def _select_arguments_for_display(arguments: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    ref = _safe_browser_ref(arguments.get("ref"))
    if ref:
        result["ref"] = ref
    values = arguments.get("values")
    result["value_count"] = min(len(values), 32) if isinstance(values, list) else 0
    if isinstance(values, list) and len(values) > 32:
        result["truncated"] = True
    return result


def tool_arguments_for_ui(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Return tool arguments safe for frontend display."""
    if not isinstance(arguments, dict):
        return {}
    if name in {"file_write", "write_file"}:
        return {key: arguments[key] for key in _FILE_WRITE_UI_ARG_KEYS if key in arguments}
    if name == "record_replay":
        return _record_replay_arguments_for_display(arguments)
    if name in {"browser_type", "browser_dialog"}:
        return {key: value for key, value in arguments.items() if key not in {"text", "value", "password"}}
    if name == "browser_fill_form":
        return _fill_form_arguments_for_display(arguments.get("fields"))
    if name == "browser_select":
        return _select_arguments_for_display(arguments)
    if name == "browser_use":
        # 单一 browser_use 同时承载 url（navigate/tab_new）与 text（type/dialog_accept）。
        action = arguments.get("action")
        if action == "fill_form":
            return {
                "action": "fill_form",
                **_fill_form_arguments_for_display(arguments.get("fields")),
            }
        if action == "select":
            return {
                "action": "select",
                **_select_arguments_for_display(arguments),
            }
        safe = {key: value for key, value in arguments.items() if key != "text"}
        if "url" in safe:
            from crew.tools.redact import redact_url_for_display

            safe["url"] = redact_url_for_display(str(safe.get("url") or ""))
        return safe
    if name in {"browser_navigate", "browser_tabs"} and "url" in arguments:
        # Local import avoids a core.types -> crew.tools package -> registry ->
        # core.types cycle during application bootstrap.
        from crew.tools.redact import redact_url_for_display

        safe = dict(arguments)
        safe["url"] = redact_url_for_display(str(arguments.get("url") or ""))
        return safe
    return arguments


def tool_arguments_for_history(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Return tool arguments safe to retain in canonical conversation history.

    Runtime execution still receives the original arguments.  Only the durable
    replay/audit representation is reduced, so typed values and URL credentials
    cannot be recovered from a later session-history response.
    """
    if not isinstance(arguments, dict):
        return {}
    if name in {
        "browser_type",
        "browser_dialog",
        "browser_fill_form",
        "browser_navigate",
        "browser_select",
        "browser_tabs",
        "browser_use",
        "record_replay",
    }:
        return tool_arguments_for_ui(name, arguments)
    return arguments


@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    started_at: float | None = None
    duration: float | None = None
    result: str = ""
    status: Literal["running", "done", "error"] = "done"
    ui_label: str = ""


@dataclass
class Message:
    """一条对话消息，覆盖 system/user/assistant/tool 四种角色。"""

    role: Role
    content: str = ""
    # assistant 消息可能携带工具调用
    tool_calls: list[ToolCall] = field(default_factory=list)
    # tool 消息必须回填对应的 tool_call_id
    tool_call_id: str | None = None
    # 可选：消息发送者名称（多智能体场景标识 leader/teammate）
    name: str | None = None
    # 实际生成本条 assistant 消息的模型。用于历史回放时保留逐回合模型，
    # 避免 Session 后续切换模型后把旧气泡统一改成当前模型。
    model: str | None = None
    # 元信息消息：发送给 LLM 但前端不渲染（如 system-reminder、skill 展开内容）
    is_meta: bool = False
    timestamp: float | None = None
    turn_started_at: float | None = None
    turn_duration: float | None = None
    # 本轮文件改动摘要（供桌面端历史回放「已编辑文件」卡；不含 diff 正文）
    # 形如 [{path, name, added, removed, status}, ...]；旧消息缺省为 None。
    turn_file_changes: list[dict[str, Any]] | None = None
    # 思考/推理过程（如 DeepSeek 的 reasoning_content）
    thinking: str | None = None
    # 多模态内容（OpenAI vision 格式）：[{type:"text",text:"..."}, {type:"image_url",image_url:{url:"data:image/png;base64,..."}}]
    # 设置后 to_openai() 优先使用此字段，content 保留纯文本用于向后兼容和文本检索。
    content_parts: list[dict[str, Any]] | None = None
    # 隐藏 attachment 元数据：消息本体仍按 user/system-reminder 发给 LLM，
    # 该字段只用于历史扫描与节流，不暴露给 provider。
    attachment_type: str | None = None
    attachment_data: dict[str, Any] | None = None
    # Team 通信回合的历史关联元数据。仅用于回放、刷新和重连，不进入 provider 消息。
    communication_kind: str | None = None
    communication_status: str | None = None
    request_id: str | None = None
    reply_to: str | None = None
    communication_request_text: str | None = None

    def to_openai(self) -> dict[str, Any]:
        """转换为 OpenAI Chat Completions 的 message 字典。

        is_meta 不影响 API 调用——它只控制前端渲染和历史回放过滤。
        """
        import json

        msg: dict[str, Any] = {"role": self.role}
        if self.role == "tool":
            msg["content"] = self.content
            msg["tool_call_id"] = self.tool_call_id
            return msg

        # 多模态内容优先（OpenAI Vision 格式）
        if self.content_parts:
            msg["content"] = self.content_parts
        elif self.role in ("user", "system"):
            # user/system 绝不能发 content:null：OpenAI SDK 联合校验与 MiniMax 等网关都会 400。
            # 空串仍可能被部分网关拒绝，调用方应再过滤；此处只保证序列化形状合法。
            msg["content"] = self.content or ""
        elif self.tool_calls:
            # assistant + tool_calls：OpenAI 惯例允许 content 为 null
            msg["content"] = self.content or None
        else:
            msg["content"] = self.content or ""
        if self.name:
            msg["name"] = self.name
        if self.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                    },
                }
                for tc in self.tool_calls
            ]
        return msg

    @staticmethod
    def system(content: str) -> "Message":
        return Message(role="system", content=content)

    @staticmethod
    def user(content: str, *, is_meta: bool = False) -> "Message":
        return Message(role="user", content=content, is_meta=is_meta)

    @staticmethod
    def assistant(
        content: str = "",
        tool_calls: list[ToolCall] | None = None,
        *,
        model: str | None = None,
    ) -> "Message":
        return Message(role="assistant", content=content, tool_calls=tool_calls or [], model=model)

    @staticmethod
    def tool(tool_call_id: str, content: str, name: str | None = None) -> "Message":
        return Message(role="tool", content=content, tool_call_id=tool_call_id, name=name)

    @property
    def text_content(self) -> str:
        """提取纯文本内容（兼容 content 为 str 或 content_parts 多模态场景）。"""
        if isinstance(self.content, str):
            return self.content
        if self.content_parts:
            return "\n".join(
                p.get("text", "") for p in self.content_parts if p.get("type") == "text"
            )
        return ""

    @staticmethod
    def system_reminder(content: str) -> "Message":
        """创建一条 ``<system-reminder>`` 包裹的 user 消息。

        用于注入动态上下文（记忆、日期、项目文件等），
        不放入 system prompt 以避免破坏 KV Cache。
        标记 is_meta=True 使前端不渲染、历史回放时过滤。
        """
        wrapped = (
            "<system-reminder>\n"
            "以下上下文信息可能对当前任务有帮助。"
            "请根据相关性决定是否参考，不需要响应所有内容。\n"
            f"{content}\n"
            "</system-reminder>"
        )
        return Message(role="user", content=wrapped, is_meta=True)


@dataclass
class MediaPart:
    """Binary media attached after a tool result for multimodal providers."""

    mime_type: str
    path: str = ""
    data_url: str = ""
    alt: str = ""
    detail: Literal["auto", "low", "high"] = "auto"


@dataclass
class ToolOutput:
    """Rich tool handler output; text remains the canonical tool result."""

    content: str
    media: list[MediaPart] = field(default_factory=list)


@dataclass
class ToolPermissionDecision:
    """Optional per-call permission override supplied by a tool."""

    behavior: Literal["allow", "ask", "deny"]
    reason: str = ""
    allow_always: bool = True
    approval_token: str = ""


@dataclass
class ToolResult:
    """工具执行结果。"""

    tool_call_id: str
    name: str
    content: str
    is_error: bool = False
    media: list[MediaPart] = field(default_factory=list)


@dataclass
class ChatResponse:
    """LLM 一次（非流式）调用的归一化返回。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str = ""  # 推理/思考过程（DeepSeek 等模型返回）

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class StreamChunk:
    """LLM 流式调用的增量帧。"""

    delta_text: str = ""
    done: bool = False
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning_content: str = ""
    # 本轮 usage（prompt/completion/cache_* 字段），仅在 done 帧填充；透传到前端 Inspector。
    usage: dict[str, int] = field(default_factory=dict)
    # 流式工具派发（Crew StreamingToolExecutor 思路）：某个工具的参数在流中刚拼完、
    # 可立即派发执行时，provider 单独 yield 一帧带上它（近似 Anthropic 的
    # content_block_stop 信号）。done 帧仍携带完整 tool_calls，供组装 assistant 消息。
    ready_tool_call: ToolCall | None = None
    # Legacy 兼容字段：旧 provider 在工具 name 一出现即 yield。executor 现在把它映射
    # 为 tool_call_generating，只显示参数生成中的卡片，不触发真正 start/执行。
    tool_call_seen: ToolCall | None = None
    # Crew tool generation signal：模型已经开始生成工具调用参数，但完整
    # arguments 尚未就绪。展示层可先渲染「正在准备/写入文件」，执行层不能据此执行工具。
    tool_call_generating: ToolCall | None = None
