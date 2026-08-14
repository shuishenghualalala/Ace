"""Team 协同的轻量数据模型。

这些模型只描述 Crew 控制平面自己的状态：团队会话、成员会话、消息与产物引用。
外部 agent 的原生 session id 仍由各 Executor/Adapter 维护，Team Bus 只使用
member_session_id 做寻址，避免把 ACP/CLI 的实现细节泄露给团队通信层。
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ExecutorKind = Literal["builtin", "external", "acp", "cli", "client"]
MessageType = Literal[
    "assign",
    "accept",
    "reject",
    "question",
    "answer",
    "progress",
    "blocked",
    "result",
    "handoff",
    "decision_request",
    "permission_request",
    "artifact_update",
    "task_notification",
]

log = logging.getLogger(__name__)
TeamPlanNodeStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "failed",
    "blocked",
    "needs_info",
    "cancelled",
    "timed_out",
]
RuntimeStaffingStatus = Literal[
    "detected",
    "awaiting_confirmation",
    "approved",
    "declined",
    "applying",
    "applied",
    "failed",
]


def now_ts() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class TeamMemberSpec:
    """团队成员统一配置。

    builtin 成员直接由 Crew SingleAgent 执行；external 成员使用
    ExternalExecutor 与 external store。acp/cli 只作为旧配置兼容值。
    """

    member_id: str
    name: str
    role: str = ""
    executor: ExecutorKind = "builtin"
    external_agent_id: str = ""
    model: str = ""
    capabilities: list[str] = field(default_factory=list)
    workspace_policy: str = "shared_workspace"
    session_policy: str = "team_scoped"
    permission_policy: str = "inherit"
    system_prompt: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> "TeamMemberSpec":
        name = str(raw.get("member_id") or raw.get("name") or raw.get("id") or "").strip()
        if not name:
            raise ValueError("Team member 缺少 name/member_id")
        executor = str(raw.get("executor") or raw.get("executor_kind") or raw.get("type") or "").strip().lower()
        external_agent_id = str(raw.get("external_agent_id") or raw.get("agent_id") or "").strip()
        if not executor:
            executor = "external" if external_agent_id else "builtin"
        if executor not in {"builtin", "external", "acp", "cli", "client"}:
            raise ValueError(f"未知 Team member executor: {executor}")
        if "communication_policy" in raw:
            log.warning(
                "Team member 配置字段 communication_policy 已废弃并被忽略 member=%s",
                name,
            )
        return cls(
            member_id=name,
            name=str(raw.get("name") or name).strip(),
            role=str(raw.get("role") or raw.get("description") or "").strip(),
            executor=executor,  # type: ignore[arg-type]
            external_agent_id=external_agent_id,
            model=str(raw.get("model") or "").strip(),
            capabilities=list(raw.get("capabilities") or []),
            workspace_policy=str(raw.get("workspace_policy") or "shared_workspace").strip(),
            session_policy=str(raw.get("session_policy") or "team_scoped").strip(),
            permission_policy=str(raw.get("permission_policy") or "inherit").strip(),
            system_prompt=str(raw.get("system_prompt") or "").strip(),
            metadata=dict(raw.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemberSession:
    team_session_id: str
    member_id: str
    member_session_id: str
    executor: ExecutorKind
    external_agent_id: str = ""
    external_session_id: str = ""
    status: str = "idle"
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamSession:
    team_session_id: str
    leader_member_id: str = "leader"
    workflow_run_id: str = ""
    member_sessions: dict[str, MemberSession] = field(default_factory=dict)
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def member_session_id(self, member_id: str) -> str:
        return f"{self.team_session_id}::{member_id}"

    def ensure_member(self, spec: TeamMemberSpec) -> MemberSession:
        existing = self.member_sessions.get(spec.member_id)
        if existing is not None:
            return existing
        session = MemberSession(
            team_session_id=self.team_session_id,
            member_id=spec.member_id,
            member_session_id=self.member_session_id(spec.member_id),
            executor=spec.executor,
            external_agent_id=spec.external_agent_id,
        )
        self.member_sessions[spec.member_id] = session
        self.updated_at = now_ts()
        return session

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["member_sessions"] = {
            key: value.to_dict() for key, value in self.member_sessions.items()
        }
        return data


@dataclass
class TeamMessage:
    team_session_id: str
    sender_member_id: str
    recipient_member_ids: tuple[str, ...]
    content: str
    message_type: MessageType = "question"
    intent: str = ""
    request_id: str = ""
    node_id: str = ""
    task_id: str = ""
    thread_id: str = ""
    reply_to: str = ""
    artifact_refs: tuple[str, ...] = field(default_factory=tuple)
    workflow_run_id: str = ""
    requires_ack: bool = False
    priority: int = 0
    status: str = "unread"
    message_id: str = field(default_factory=lambda: new_id("msg"))
    created_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # 对外仍保持 JSON array 契约，冻结元组只用于防止进程内消息事实被修改。
        data["recipient_member_ids"] = list(self.recipient_member_ids)
        data["artifact_refs"] = list(self.artifact_refs)
        return data


@dataclass
class TeamArtifact:
    team_session_id: str
    owner_member_id: str
    summary: str
    scope: str = "team"
    task_id: str = ""
    content_type: str = "text/plain"
    path: str = ""
    version: int = 1
    artifact_id: str = field(default_factory=lambda: new_id("artifact"))
    created_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamPlanEdge:
    parent_id: str
    child_id: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuntimeStaffingRequest:
    """One user-governed, WorkflowRun-scoped request for an extra member."""

    request_id: str
    trigger_node_id: str
    trigger_type: str
    required_capabilities: list[str]
    reason: str
    status: RuntimeStaffingStatus = "detected"
    candidates: list[dict[str, Any]] = field(default_factory=list)
    selected_candidate: dict[str, Any] = field(default_factory=dict)
    previous_assignee: str = ""
    previous_delegate_task_id: str = ""
    previous_attempt_count: int = 0
    created_at: float = field(default_factory=now_ts)
    resolved_at: float = 0.0
    last_error: str = ""
    version: int = 1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeStaffingRequest | None:
        request_id = str(data.get("request_id") or "").strip()
        node_id = str(data.get("trigger_node_id") or "").strip()
        if not request_id or not node_id:
            return None
        status = str(data.get("status") or "detected").strip()
        allowed = {
            "detected",
            "awaiting_confirmation",
            "approved",
            "declined",
            "applying",
            "applied",
            "failed",
        }
        return cls(
            version=max(1, int(data.get("version") or 1)),
            request_id=request_id,
            trigger_node_id=node_id,
            trigger_type=str(data.get("trigger_type") or "capability_gap"),
            required_capabilities=[str(item) for item in (data.get("required_capabilities") or []) if str(item)],
            reason=str(data.get("reason") or ""),
            status=status if status in allowed else "detected",  # type: ignore[arg-type]
            candidates=[dict(item) for item in (data.get("candidates") or []) if isinstance(item, dict)],
            selected_candidate=(
                dict(data.get("selected_candidate"))
                if isinstance(data.get("selected_candidate"), dict)
                else {}
            ),
            previous_assignee=str(data.get("previous_assignee") or ""),
            previous_delegate_task_id=str(data.get("previous_delegate_task_id") or ""),
            previous_attempt_count=max(0, int(data.get("previous_attempt_count") or 0)),
            created_at=float(data.get("created_at") or now_ts()),
            resolved_at=float(data.get("resolved_at") or 0.0),
            last_error=str(data.get("last_error") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamPlanNode:
    node_id: str
    title: str
    detail: str = ""
    assignee: str = ""
    status: TeamPlanNodeStatus = "pending"
    result_summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    delegate_task_id: str = ""
    attempt_count: int = 0
    last_error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def update(
        self,
        *,
        status: str | None = None,
        result_summary: str | None = None,
        artifact_refs: list[str] | None = None,
        delegate_task_id: str | None = None,
        attempt_count: int | None = None,
        last_error: str | None = None,
        allow_reopen: bool = False,
    ) -> None:
        terminal_statuses = {"completed", "failed", "cancelled", "timed_out"}
        if (
            status in terminal_statuses
            and delegate_task_id
            and self.delegate_task_id
            and delegate_task_id != self.delegate_task_id
        ):
            return
        if status and self.status in terminal_statuses and status != self.status:
            explicit_retry = (
                self.status == "failed"
                and status == "in_progress"
                and attempt_count is not None
                and attempt_count > self.attempt_count
            )
            failed_to_blocked = self.status == "failed" and status == "blocked"
            if not allow_reopen and not explicit_retry and not failed_to_blocked:
                return
        if status:
            allowed = {
                "pending",
                "in_progress",
                "completed",
                "failed",
                "blocked",
                "needs_info",
                "cancelled",
                "timed_out",
            }
            self.status = status if status in allowed else "pending"  # type: ignore[assignment]
        if result_summary is not None:
            self.result_summary = result_summary
        if artifact_refs is not None:
            self.artifact_refs = list(artifact_refs)
        if delegate_task_id is not None:
            self.delegate_task_id = delegate_task_id
        if attempt_count is not None:
            self.attempt_count = max(0, int(attempt_count))
        if last_error is not None:
            self.last_error = last_error
        self.updated_at = now_ts()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TeamPlan:
    team_session_id: str
    goal: str
    nodes: dict[str, TeamPlanNode] = field(default_factory=dict)
    edges: list[TeamPlanEdge] = field(default_factory=list)
    status: str = "active"
    plan_id: str = field(default_factory=lambda: new_id("team_plan"))
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "team_session_id": self.team_session_id,
            "goal": self.goal,
            "status": self.status,
            "nodes": [node.to_dict() for node in self.nodes.values()],
            "edges": [edge.to_dict() for edge in self.edges],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
