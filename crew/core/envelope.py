"""内部消息信封（简化版 E2A: Everything-to-Agent）。

所有入口（CLI / Web / Gateway / 未来的 A2A 等）把外部请求统一归一成 `Envelope`
再送入内核；内核产出统一的 `ResponseChunk` 流回灌给入口。
=> 入口与内核彻底解耦，新增入口不改内核，新增内核能力不改入口。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from crew.core.types import tool_arguments_for_ui

ChunkKind = Literal[
    "delta", "tool", "task", "thinking", "final", "error", "status", "plan_review",
    "followup_question",
    "todo_updated", "todo_reminder", "file_changes",
    # 命名空间业务事件：body = {"feature", "event", "version", "payload"}。
    # 具体业务事件不进核心枚举，插件贡献的未登记事件同样以本 kind 出口。
    "feature_event",
]
Status = Literal["in_progress", "succeeded", "failed"]
Mode = str


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _tool_event_args_for_ui(name: str, args: str) -> str:
    needs_projection = (
        name in {"file_write", "write_file"}
        or name.startswith("browser_")
    )
    if not args or not needs_projection:
        return args
    try:
        parsed = json.loads(args)
    except Exception:
        return ""
    ui_args = tool_arguments_for_ui(name, parsed if isinstance(parsed, dict) else {})
    return json.dumps(ui_args, ensure_ascii=False) if ui_args else ""


@dataclass
class Envelope:
    """统一请求信封。"""

    session_id: str
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: _new_id("req"))
    channel: str = "cli"
    user_id: str = "local"
    user_type: str = "internal"  # "external" | "internal"，供 access_control 使用
    workspace_id: str = "default"  # 会话所属工作空间
    mode: Mode = "agent"
    is_stream: bool = True
    attachments: list[dict[str, Any]] = field(default_factory=list)  # 附件列表

    @property
    def query(self) -> str:
        """业务主输入文本。"""
        return self.params.get("query", "")

    @staticmethod
    def of(query: str, session_id: str, **kw: Any) -> "Envelope":
        """便捷构造：Envelope.of("你好", session_id="s1", mode="team")。"""
        params = kw.pop("params", {})
        params = {"query": query, **params}
        return Envelope(session_id=session_id, params=params, **kw)


@dataclass
class ResponseChunk:
    """统一响应帧（流式时多帧，非流式时单帧 is_final=True）。"""

    request_id: str
    kind: ChunkKind = "delta"
    body: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    is_final: bool = False
    status: Status = "in_progress"
    ts: float = field(default_factory=time.time)

    # ---- 工厂方法，业务层用这些构造，避免手填字段 ----
    @staticmethod
    def delta(request_id: str, text: str, sequence: int = 0) -> "ResponseChunk":
        return ResponseChunk(request_id, kind="delta", body={"text": text}, sequence=sequence)

    @staticmethod
    def tool_event(
        request_id: str,
        name: str,
        phase: str,
        detail: str = "",
        sequence: int = 0,
        *,
        tool_call_id: str = "",
        args: str = "",
        ui_label: str = "",
    ) -> "ResponseChunk":
        """工具活动事件：phase = generating | start | result | error。

        tool_call_id: 工具调用唯一标识，用于前端聚合 start/result。
        args: 工具调用参数 JSON 字符串（generating/start 时提供 UI-safe 参数；
              file_write/write_file 不透传完整 content）。
        """
        args = _tool_event_args_for_ui(name, args)
        if phase in {"generating", "start"} and (
            name in {"file_write", "write_file"}
            or name.startswith("browser_")
        ):
            detail = args
        body: dict[str, Any] = {"name": name, "phase": phase, "detail": detail}
        if tool_call_id:
            body["tool_call_id"] = tool_call_id
        if args:
            body["args"] = args
        if ui_label:
            body["ui_label"] = ui_label
        return ResponseChunk(request_id, kind="tool", body=body, sequence=sequence)

    @staticmethod
    def thinking_event(request_id: str, text: str, sequence: int = 0) -> "ResponseChunk":
        """推理/思考过程事件。"""
        return ResponseChunk(
            request_id,
            kind="thinking",
            body={"text": text},
            sequence=sequence,
        )

    @staticmethod
    def final(
        request_id: str,
        text: str,
        sequence: int = 0,
        *,
        replace_content: bool = False,
        reason: str | None = None,
        usage: dict[str, int] | None = None,
    ) -> "ResponseChunk":
        body: dict[str, Any] = {"text": text}
        if replace_content:
            body["replace_content"] = True
        if reason:
            body["reason"] = reason
        if usage:
            body["usage"] = usage
        return ResponseChunk(
            request_id,
            kind="final",
            body=body,
            sequence=sequence,
            is_final=True,
            status="succeeded",
        )

    @staticmethod
    def error(
        request_id: str,
        message: str,
        sequence: int = 0,
        *,
        code: str | None = None,
    ) -> "ResponseChunk":
        body = {"message": message}
        if code:
            body["code"] = code
        return ResponseChunk(
            request_id,
            kind="error",
            body=body,
            sequence=sequence,
            is_final=True,
            status="failed",
        )

    @staticmethod
    def status_event(request_id: str, message: str, sequence: int = 0) -> "ResponseChunk":
        """中间状态提示（如 Team 派发进度），非最终帧。"""
        return ResponseChunk(request_id, kind="status", body={"message": message}, sequence=sequence)

    @staticmethod
    def compaction_event(request_id: str, active: bool, sequence: int = 0) -> "ResponseChunk":
        """上下文摘要的瞬时状态，供输入框上方提示条展示。"""
        return ResponseChunk(
            request_id,
            kind="status",
            body={
                "message": "正在压缩上下文" if active else "上下文压缩完成",
                "activity": "context_compaction",
                "active": active,
            },
            sequence=sequence,
        )

    @staticmethod
    def task_event(
        request_id: str,
        task_id: str,
        task_kind: str,
        phase: str,
        *,
        status: str,
        progress: dict[str, Any] | None = None,
        output_ref: str = "",
        summary: str = "",
        sequence: int = 0,
    ) -> "ResponseChunk":
        return ResponseChunk(
            request_id,
            kind="task",
            body={
                "task_id": task_id,
                "task_kind": task_kind,
                "phase": phase,
                "status": status,
                "progress": progress or {},
                "output_ref": output_ref,
                "summary": summary,
            },
            sequence=sequence,
        )

    @staticmethod
    def plan_review(
        request_id: str,
        plan: str,
        plan_file: str,
        sequence: int = 0,
        *,
        empty: bool = False,
        phase: str = "review",
        status: str = "pending",
        options: list[dict[str, str]] | None = None,
    ) -> "ResponseChunk":
        """Plan 模式：模型已调 exit_plan_mode。

        - empty=False：计划已落盘，前端弹「批准/继续修改」审批卡。
        - empty=True：计划文件为空，前端弹「计划为空」提示卡（无审批按钮），倒逼模型先 file_write。
        """
        return ResponseChunk(
            request_id,
            kind="plan_review",
            body={
                "plan": plan,
                "plan_file": plan_file,
                "empty": empty,
                "phase": phase,
                "status": "empty" if empty else status,
                "options": options or [],
            },
            sequence=sequence,
        )

    @staticmethod
    def feature_event(
        request_id: str,
        feature: str,
        event: str,
        payload: dict[str, Any] | None = None,
        sequence: int = 0,
        *,
        version: int = 1,
    ) -> "ResponseChunk":
        """命名空间业务事件：核心协议只认识 envelope，不认识具体 Feature。

        body 固定为 {"feature", "event", "version", "payload"}，事件名形如
        team.internal_message / wiki.cards；WS 出口原样透传本帧。
        """
        return ResponseChunk(
            request_id,
            kind="feature_event",
            body={
                "feature": feature,
                "event": event,
                "version": version,
                "payload": payload or {},
            },
            sequence=sequence,
        )


def feature_event_body(
    feature: str,
    event: str,
    payload: dict[str, Any] | None = None,
    *,
    version: int = 1,
) -> dict[str, Any]:
    """构造 feature_event 的标准 body（供直接产出 WS 帧的 Host 推送点复用）。"""
    return {
        "feature": feature,
        "event": event,
        "version": version,
        "payload": payload or {},
    }
