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
    "kanban", "followup_question",
    "todo_updated", "todo_reminder", "file_changes",
    "workflow_progress",
    "wiki_cards", "wiki_ingest_progress",
    "team_internal",
]
Status = Literal["in_progress", "succeeded", "failed"]
Mode = Literal["agent", "team", "dynamic_kanban"]


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _tool_event_args_for_ui(name: str, args: str) -> str:
    needs_projection = (
        name in {"file_write", "write_file", "record_replay"}
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
            name in {"file_write", "write_file", "record_replay"}
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
    def error(request_id: str, message: str, sequence: int = 0) -> "ResponseChunk":
        return ResponseChunk(
            request_id,
            kind="error",
            body={"message": message},
            sequence=sequence,
            is_final=True,
            status="failed",
        )

    @staticmethod
    def status_event(request_id: str, message: str, sequence: int = 0) -> "ResponseChunk":
        """中间状态提示（如 Team 派发进度），非最终帧。"""
        return ResponseChunk(request_id, kind="status", body={"message": message}, sequence=sequence)

    @staticmethod
    def tool_planning_event(request_id: str, sequence: int = 0) -> "ResponseChunk":
        """模型已开始推理工具选择，但 provider 尚未给出具体 tool call。"""
        return ResponseChunk(
            request_id,
            kind="status",
            body={
                "message": "正在规划工具调用…",
                "activity": "tool_planning",
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
    def kanban_event(request_id: str, event: str, payload: dict[str, Any] | None = None, sequence: int = 0) -> "ResponseChunk":
        """Dynamic Kanban 看板事件：如 workflow 启动/结束，供前端按需打开/关闭看板。"""
        return ResponseChunk(
            request_id,
            kind="kanban",
            body={"event": event, **(payload or {})},
            sequence=sequence,
        )

    @staticmethod
    def workflow_progress(
        request_id: str,
        workflow_id: str,
        *,
        status: str = "running",
        current_phase: dict[str, Any] | None = None,
        completed_phases: list[dict[str, Any]] | None = None,
        active_calls: list[dict[str, Any]] | None = None,
        message: str = "",
        sequence: int = 0,
    ) -> "ResponseChunk":
        """Dynamic Kanban 工作流进度事件：结构化地描述当前阶段、已完成阶段和正在执行的调用。

        前端把它渲染成对话中始终可见的进度面板，避免大量阶段编排 status 文本被折叠到过程区。
        """
        body: dict[str, Any] = {"workflow_id": workflow_id, "status": status}
        if current_phase is not None:
            body["current_phase"] = current_phase
        if completed_phases is not None:
            body["completed_phases"] = completed_phases
        if active_calls is not None:
            body["active_calls"] = active_calls
        if message:
            body["message"] = message
        return ResponseChunk(
            request_id,
            kind="workflow_progress",
            body=body,
            sequence=sequence,
        )
