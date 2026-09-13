"""Workflow Runtime 数据模型。

把 orchestration 逻辑从 prompt context 抽离成
可持久化的 workflow definition + runtime state。一个 workflow 由若干 phase 组成，
每个 phase 内包含可并行的 agent_call；phase 之间通过 edges 形成 DAG，并支持
verification gate 与暂停恢复状态。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from crew.dynamickanban.plan_graph import validate_workflow_dag


WORKFLOW_DEFINITION_SCHEMA_VERSION = 2


class WorkflowDefinitionMigrationError(ValueError):
    """旧 Workflow definition 无法无损迁移到单一 edges DAG。"""

    def __init__(
        self,
        message: str,
        *,
        persisted_edges: list[tuple[str, str]] | None = None,
        legacy_runtime_edges: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.persisted_edges = list(persisted_edges or [])
        self.legacy_runtime_edges = list(legacy_runtime_edges or [])

    def diagnostic(self) -> dict[str, Any]:
        """返回可持久化的完整迁移冲突诊断。"""

        return {
            "error": str(self),
            "persisted_edges": [list(edge) for edge in self.persisted_edges],
            "legacy_runtime_edges": [list(edge) for edge in self.legacy_runtime_edges],
            "target_schema_version": WORKFLOW_DEFINITION_SCHEMA_VERSION,
        }


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class AgentCallInput:
    """agent_call 的输入：引用上游 phase/agent_call 的某个输出变量。"""

    source_phase_id: str
    source_call_id: str
    output_key: str
    # 如果 source 还没完成，是否允许使用空字符串降级继续
    optional: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentCall:
    """对单个 worker agent 的一次调用。"""

    id: str
    role: str
    prompt: str
    # 输入变量名 -> 引用
    inputs: dict[str, AgentCallInput] = field(default_factory=dict)
    # 期望产出的变量名列表
    outputs: list[str] = field(default_factory=list)
    # 覆盖模型（空=继承 workflow 默认模型）
    model: str = ""
    # 该 call 独占的超时（秒，0=继承）
    timeout_seconds: float = 0.0
    # 是否必须通过后继 verification gate 才算完成
    requires_verification: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "role": self.role,
            "prompt": self.prompt,
            "inputs": {k: v.to_dict() for k, v in self.inputs.items()},
            "outputs": self.outputs,
            "model": self.model,
            "timeout_seconds": self.timeout_seconds,
            "requires_verification": self.requires_verification,
        }


@dataclass
class VerificationGate:
    """phase 完成后的验证门。"""

    role: str
    prompt: str
    # 验证通过的条件变量名（gate agent 需输出该变量，值为 yes/no）
    pass_key: str = "passed"
    # 验证失败时进入的 phase_id（空=当前 phase 失败并停止推进）
    fallback_phase_id: str = ""
    max_retries: int = 2

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Phase:
    """一个执行阶段，内部 agent_call 可并行。"""

    id: str
    name: str
    description: str = ""
    agent_calls: list[AgentCall] = field(default_factory=list)
    max_concurrent: int = 3
    # phase 完成后必经的验证门（空=不验证）
    verification_gate: VerificationGate | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "agent_calls": [c.to_dict() for c in self.agent_calls],
            "max_concurrent": self.max_concurrent,
            "verification_gate": self.verification_gate.to_dict() if self.verification_gate else None,
        }


@dataclass
class WorkflowDefinition:
    """LLM / orchestrator 生成的可执行 workflow 脚本。"""

    summary: str
    phases: list[Phase]
    edges: list[tuple[str, str]] = field(default_factory=list)
    # workflow 级默认模型
    default_model: str = ""
    # 全局并发上限
    max_concurrent: int = 3
    schema_version: int = WORKFLOW_DEFINITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != WORKFLOW_DEFINITION_SCHEMA_VERSION:
            raise ValueError(
                f"仅支持 schema_version={WORKFLOW_DEFINITION_SCHEMA_VERSION} 的内存 definition"
            )
        self.edges = validate_workflow_dag(
            [phase.id for phase in self.phases],
            list(self.edges),
        )

    def predecessors(self) -> dict[str, list[str]]:
        """从唯一 edges 真源派生每个 phase 的直接前驱。"""
        result = {phase.id: [] for phase in self.phases}
        for parent, child in self.edges:
            result[child].append(parent)
        return result

    def successors(self) -> dict[str, list[str]]:
        """从唯一 edges 真源派生每个 phase 的直接后继。"""
        result = {phase.id: [] for phase in self.phases}
        for parent, child in self.edges:
            result[parent].append(child)
        return result

    def entry_phase_ids(self) -> list[str]:
        """按稳定展示顺序返回所有无前驱入口。"""
        predecessors = self.predecessors()
        return [phase.id for phase in self.phases if not predecessors[phase.id]]

    def ready_phase_ids(
        self,
        *,
        completed_phase_ids: set[str],
        terminal_phase_ids: set[str],
    ) -> list[str]:
        """按稳定展示顺序返回前驱全部完成且自身未终结的 phase。"""
        predecessors = self.predecessors()
        return [
            phase.id
            for phase in self.phases
            if phase.id not in terminal_phase_ids
            and all(parent in completed_phase_ids for parent in predecessors[phase.id])
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": WORKFLOW_DEFINITION_SCHEMA_VERSION,
            "summary": self.summary,
            "phases": [p.to_dict() for p in self.phases],
            "edges": list(self.edges),
            "default_model": self.default_model,
            "max_concurrent": self.max_concurrent,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkflowDefinition:
        """读取 v2 definition，或无损迁移 v1/无版本 definition。

        旧 Runtime 对空 next 使用 Phase 列表后继，因此迁移比较的是旧代码实际会走的
        拓扑，而不是只比较显式 next 字段。若旧实际拓扑与同时保存的 edges 冲突，
        必须隔离并等待人工确认，不能静默选择其中一份。
        """

        if not isinstance(data, dict):
            raise ValueError("workflow definition 必须是对象")
        raw_version = data.get("schema_version")
        if raw_version is not None:
            try:
                schema_version = int(raw_version)
            except (TypeError, ValueError) as exc:
                raise ValueError("workflow definition schema_version 非法") from exc
        else:
            schema_version = 1
        if schema_version > WORKFLOW_DEFINITION_SCHEMA_VERSION or schema_version < 1:
            raise ValueError(f"不支持的 workflow definition schema_version: {schema_version}")

        phases_data = data.get("phases") or []
        if not isinstance(phases_data, list):
            raise ValueError("workflow definition phases 必须是列表")
        if any(not isinstance(phase, dict) for phase in phases_data):
            raise ValueError("workflow definition phase 必须是对象")
        phase_ids = [str(phase.get("id") or "").strip() for phase in phases_data]
        persisted_edges = cls._parse_edges(data.get("edges") or [])
        if schema_version == WORKFLOW_DEFINITION_SCHEMA_VERSION:
            if any("next_phase_ids" in p for p in phases_data if isinstance(p, dict)):
                raise ValueError("v2 workflow definition 不允许持久化 next_phase_ids")
            normalized_edges = validate_workflow_dag(phase_ids, persisted_edges)
        else:
            normalized_edges = cls._migrate_legacy_edges(
                phases_data,
                phase_ids,
                persisted_edges,
            )

        phases: list[Phase] = []
        for p in phases_data:
            if not isinstance(p, dict):
                raise ValueError("workflow definition phase 必须是对象")
            calls = []
            for c in p.get("agent_calls") or []:
                inputs = {
                    k: AgentCallInput(**v)
                    for k, v in (c.get("inputs") or {}).items()
                }
                calls.append(
                    AgentCall(
                        id=str(c.get("id") or _new_id("call")),
                        role=str(c.get("role") or ""),
                        prompt=str(c.get("prompt") or ""),
                        inputs=inputs,
                        outputs=list(c.get("outputs") or []),
                        model=str(c.get("model") or ""),
                        timeout_seconds=float(c.get("timeout_seconds") or 0.0),
                        requires_verification=bool(c.get("requires_verification", True)),
                    )
                )
            gate = p.get("verification_gate")
            phases.append(
                Phase(
                    id=str(p.get("id") or _new_id("phase")),
                    name=str(p.get("name") or ""),
                    description=str(p.get("description") or ""),
                    agent_calls=calls,
                    max_concurrent=int(p.get("max_concurrent") or 3),
                    verification_gate=VerificationGate(**gate) if gate else None,
                )
            )
        return cls(
            summary=str(data.get("summary") or ""),
            phases=phases,
            edges=normalized_edges,
            default_model=str(data.get("default_model") or ""),
            max_concurrent=int(data.get("max_concurrent") or 3),
        )

    @staticmethod
    def _parse_edges(raw_edges: Any) -> list[tuple[str, str]]:
        if not isinstance(raw_edges, list):
            raise ValueError("workflow definition edges 必须是列表")
        edges: list[tuple[str, str]] = []
        for edge in raw_edges:
            if not isinstance(edge, (list, tuple)) or len(edge) != 2:
                raise ValueError("workflow definition edge 必须包含两个 phase id")
            edges.append((str(edge[0] or "").strip(), str(edge[1] or "").strip()))
        return edges

    @classmethod
    def _migrate_legacy_edges(
        cls,
        phases_data: list[Any],
        phase_ids: list[str],
        persisted_edges: list[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        legacy_runtime_edges: list[tuple[str, str]] = []
        for index, phase_data in enumerate(phases_data):
            if not isinstance(phase_data, dict):
                raise ValueError("workflow definition phase 必须是对象")
            next_ids = phase_data.get("next_phase_ids") or []
            if not isinstance(next_ids, list):
                raise ValueError("legacy next_phase_ids 必须是列表")
            if next_ids:
                legacy_runtime_edges.extend(
                    (phase_ids[index], str(next_id or "").strip()) for next_id in next_ids
                )
            elif index + 1 < len(phase_ids):
                # 旧 Runtime 对空 next 的真实行为是退回 Phase 列表中的下一项。
                legacy_runtime_edges.append((phase_ids[index], phase_ids[index + 1]))

        legacy_runtime_edges = validate_workflow_dag(phase_ids, legacy_runtime_edges)
        if persisted_edges:
            normalized_persisted = validate_workflow_dag(phase_ids, persisted_edges)
            if set(normalized_persisted) != set(legacy_runtime_edges):
                raise WorkflowDefinitionMigrationError(
                    "legacy workflow definition 的 edges 与旧 Runtime 实际拓扑冲突",
                    persisted_edges=normalized_persisted,
                    legacy_runtime_edges=legacy_runtime_edges,
                )
            return normalized_persisted
        # edges 与 next 均为空时，legacy_runtime_edges 已按旧列表顺序生成。
        return legacy_runtime_edges


@dataclass
class AgentCallResult:
    """单个 agent_call 的执行结果。"""

    call_id: str
    status: str  # done | failed | blocked
    text: str = ""
    outputs: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    error: str = ""
    finished_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PhaseResult:
    """一个 phase 的执行结果。"""

    phase_id: str
    status: str  # pending | running | done | failed | blocked
    call_results: dict[str, AgentCallResult] = field(default_factory=dict)
    verification_result: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "status": self.status,
            "call_results": {k: v.to_dict() for k, v in self.call_results.items()},
            "verification_result": self.verification_result,
            "error": self.error,
        }


@dataclass
class RuntimeState:
    """workflow 的运行时状态，可持久化后恢复。"""

    workflow_id: str
    status: str = "active"  # active | paused | done | failed
    current_phase_id: str = ""
    completed_phase_ids: list[str] = field(default_factory=list)
    phase_results: dict[str, PhaseResult] = field(default_factory=dict)
    # 跨 phase 的命名输出变量池
    variables: dict[str, Any] = field(default_factory=dict)
    pause_requested: bool = False
    pause_reason: str = ""
    loop_count: int = 0
    # 每个 phase 因验证失败被回退重试的次数，防止无限循环
    phase_retry_counts: dict[str, int] = field(default_factory=dict)
    # 失败自动 replan 已执行次数（上限由 Runtime.max_replans 控制）
    replan_count: int = 0
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "status": self.status,
            "current_phase_id": self.current_phase_id,
            "completed_phase_ids": self.completed_phase_ids,
            "phase_results": {k: v.to_dict() for k, v in self.phase_results.items()},
            "variables": self.variables,
            "pause_requested": self.pause_requested,
            "pause_reason": self.pause_reason,
            "loop_count": self.loop_count,
            "phase_retry_counts": self.phase_retry_counts,
            "replan_count": self.replan_count,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RuntimeState:
        prs = {}
        for k, v in (data.get("phase_results") or {}).items():
            prs[k] = PhaseResult(
                phase_id=str(v.get("phase_id") or k),
                status=str(v.get("status") or "pending"),
                call_results={
                    ck: AgentCallResult(**cv)
                    for ck, cv in (v.get("call_results") or {}).items()
                },
                verification_result=dict(v.get("verification_result") or {}),
                error=str(v.get("error") or ""),
            )
        return cls(
            workflow_id=str(data.get("workflow_id") or ""),
            status=str(data.get("status") or "active"),
            current_phase_id=str(data.get("current_phase_id") or ""),
            completed_phase_ids=list(data.get("completed_phase_ids") or []),
            phase_results=prs,
            variables=dict(data.get("variables") or {}),
            pause_requested=bool(data.get("pause_requested")),
            pause_reason=str(data.get("pause_reason") or ""),
            loop_count=int(data.get("loop_count") or 0),
            phase_retry_counts=dict(data.get("phase_retry_counts") or {}),
            replan_count=int(data.get("replan_count") or 0),
            updated_at=float(data.get("updated_at") or time.time()),
        )
