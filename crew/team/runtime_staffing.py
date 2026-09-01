"""Runtime 补员策略与成员能力准入。

这里处理的是一次 WorkflowRun 的临时执行决策：判断当前负责人能否执行、
是否可以改派现有成员、是否需要向用户请求补员，以及如何把用户选择的候选
转换为临时 TeamMemberSpec。它不负责节点状态持久化，也不负责创建 Team Agent。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from crew.core.errors import ToolError
from crew.core.followup import CANCELLED_MARKER
from crew.team import flow_builder
from crew.team.agent_profile import evaluate_capability_coverage
from crew.team.capabilities import normalize_capabilities
from crew.team.formation import (
    rank_staffing_candidates,
    ready_runtime_model_options,
    recommend_runtime_model,
    role_key_for_capabilities,
)
from crew.team.models import RuntimeStaffingRequest, TeamMemberSpec, TeamPlan, TeamPlanNode
from crew.team.roles import intelligent_role_markdown, role_preset


class RuntimeStaffingPolicy:
    """Runtime staffing 的纯决策和候选编译策略。"""

    def __init__(
        self,
        *,
        external_store: Any | None,
        external_store_provider: Callable[[], Any | None] | None = None,
        resolve_member_profile: Callable[[TeamMemberSpec, str], Any | None],
    ) -> None:
        self.external_store = external_store
        self.external_store_provider = external_store_provider
        self.resolve_member_profile = resolve_member_profile

    def _external_store(self) -> Any | None:
        if callable(self.external_store_provider):
            return self.external_store_provider()
        return self.external_store

    def member_capability_coverage(
        self,
        team: Any,
        member_id: str,
        required_capabilities: list[str],
        *,
        owner_account_id: str,
    ) -> Any:
        """Resolve one current Team member through the shared coverage model."""

        member = team.members.get(str(member_id or "").strip())
        if member is None:
            return evaluate_capability_coverage(
                required_capabilities,
                capability_sets={},
                assigned_agent_ids=[str(member_id or "")],
            )
        if member.executor == "external" and member.external_agent_id:
            agent_id = str(member.external_agent_id).strip()
            assigned_capabilities = normalize_capabilities(member.capabilities)
            if not assigned_capabilities:
                assigned_capabilities = normalize_capabilities(
                    flow_builder.member_node_metadata(member).get("required_capabilities") or []
                )
            profile = self.resolve_member_profile(member, owner_account_id)
            profiles = {agent_id: profile} if profile is not None else {}
            capability_sets = {agent_id: assigned_capabilities} if assigned_capabilities else None
            return evaluate_capability_coverage(
                required_capabilities,
                profiles,
                capability_sets=capability_sets,
                assigned_agent_ids=[agent_id],
                require_profile_availability=True,
            )
        capabilities = normalize_capabilities(member.capabilities)
        if not capabilities:
            capabilities = normalize_capabilities(
                flow_builder.member_node_metadata(member).get("required_capabilities") or []
            )
        return evaluate_capability_coverage(
            required_capabilities,
            capability_sets={member.member_id: capabilities},
            assigned_agent_ids=[member.member_id],
        )

    @staticmethod
    def request(node: TeamPlanNode) -> RuntimeStaffingRequest | None:
        raw = (node.metadata or {}).get("runtime_staffing")
        return RuntimeStaffingRequest.from_dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def request_id(
        plan: TeamPlan,
        node: TeamPlanNode,
        *,
        trigger_type: str,
        required_capabilities: list[str],
    ) -> str:
        identity = "\x1f".join([
            plan.plan_id,
            node.node_id,
            trigger_type,
            ",".join(normalize_capabilities(required_capabilities)),
            str(node.attempt_count),
            str(node.delegate_task_id or ""),
        ])
        return f"staffing_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}"

    def trigger(
        self,
        team: Any,
        node: TeamPlanNode,
        *,
        owner_account_id: str,
        max_attempts: int,
    ) -> dict[str, Any] | None:
        """Return one hard Runtime staffing gap; low confidence alone is not a trigger."""

        if node.assignee == "leader":
            return None
        required = normalize_capabilities((node.metadata or {}).get("required_capabilities") or [])
        if not required:
            return None

        explicit_trigger = str((node.metadata or {}).get("runtime_staffing_trigger") or "").strip()
        if explicit_trigger and explicit_trigger != "capability_gap":
            return {
                "trigger_type": explicit_trigger,
                "required_capabilities": required,
                "reason": str(
                    (node.metadata or {}).get("runtime_staffing_trigger_reason")
                    or "Leader 审阅修订已耗尽，需要更换执行成员。"
                ),
            }
        if node.assignee not in team.teammates:
            return {
                "trigger_type": "unknown_assignee",
                "required_capabilities": required,
                "reason": f"节点指向未知或不可委派成员 {node.assignee}。",
            }
        if node.attempt_count >= max_attempts:
            return {
                "trigger_type": "acceptance_exhausted",
                "required_capabilities": required,
                "reason": f"节点已连续失败 {node.attempt_count} 次，达到自动重试上限。",
            }

        coverage = self.member_capability_coverage(
            team,
            node.assignee,
            required,
            owner_account_id=owner_account_id,
        )
        if coverage.status != "covered":
            for member_id, _member in team.members.items():
                if member_id in {node.assignee, "leader"}:
                    continue
                member_coverage = self.member_capability_coverage(
                    team,
                    member_id,
                    required,
                    owner_account_id=owner_account_id,
                )
                if member_coverage.status == "covered":
                    return {
                        "trigger_type": "existing_member_reassignment",
                        "required_capabilities": list(required),
                        "replacement_assignee": member_id,
                        "reason": f"当前负责人 {node.assignee} 不具备所需能力，已有成员 {member_id} 可以承担。",
                    }
        if coverage.status in {"unavailable", "unknown"}:
            return {
                "trigger_type": "agent_unavailable",
                "required_capabilities": list(required),
                "reason": f"当前成员 {node.assignee} 的 Runtime/model 不可用或能力画像无法确认。",
            }
        if coverage.status != "covered":
            missing = list(dict.fromkeys([*coverage.missing, *coverage.unavailable, *coverage.unknown]))
            return {
                "trigger_type": "capability_gap",
                "required_capabilities": missing,
                "reason": f"当前节点负责人 {node.assignee} 未覆盖硬能力：{'、'.join(missing)}。",
            }
        return None

    def candidates(
        self,
        team: Any,
        *,
        owner_account_id: str,
        required_capabilities: list[str],
    ) -> list[dict[str, Any]]:
        external_store = self._external_store()
        if external_store is None:
            return []
        excluded_agent_ids = {
            str(spec.external_agent_id or "").strip()
            for spec in team.members.values()
            if str(spec.external_agent_id or "").strip()
        }
        candidates = rank_staffing_candidates(
            required_capabilities,
            external_store.list_agents(owner_account_id=owner_account_id, include_managed=True),
            excluded_agent_ids=excluded_agent_ids,
            limit=3,
        )
        if len(candidates) >= 3:
            return candidates
        options = ready_runtime_model_options(external_store.list_runtimes())
        recommended = recommend_runtime_model(
            options,
            required_capabilities=required_capabilities,
        )
        if recommended is None:
            return candidates
        role_key = role_key_for_capabilities(required_capabilities)
        preset = role_preset(role_key)
        candidates.append({
            "candidate_type": "runtime",
            "selection_source": "new_managed_agent",
            "runtime_id": str(recommended.get("runtime_id") or ""),
            "runtime_name": str(recommended.get("runtime_name") or recommended.get("runtime_id") or "Runtime"),
            "model_id": str(recommended.get("model_id") or ""),
            "role_key": role_key,
            "role_label": str(preset.get("label") or role_key),
            "covered_capabilities": list(required_capabilities),
            "profile_version": 0,
            "reason": "创建一个隐藏的 Runtime 托管 Agent；仅挂载到本次 WorkflowRun，后续实证继续更新其 AgentProfile。",
        })
        return candidates[:3]

    @staticmethod
    def answer(answers: list[dict[str, Any]]) -> str:
        if not answers or any(
            str(item.get("id") or "") == CANCELLED_MARKER
            for item in answers
            if isinstance(item, dict)
        ):
            return ""
        for item in answers:
            values = item.get("answers") if isinstance(item, dict) else None
            if not isinstance(values, list):
                continue
            for value in values:
                text = str(value or "").strip()
                if text:
                    return text
        return ""

    @staticmethod
    def user_reason(trigger_type: str) -> str:
        return {
            "agent_unavailable": "当前负责这项工作的成员暂时无法使用。",
            "capability_gap": "当前团队还缺少完成这项工作所需的能力。",
            "acceptance_exhausted": "这项工作已经尝试了几次仍未通过，换位助手接手会更稳妥。",
            "review_exhausted": "这项工作经过多次修改仍未通过，换位助手接手会更稳妥。",
            "unknown_assignee": "原定成员现在无法接手这项工作。",
        }.get(trigger_type, "这项工作暂时缺少合适的执行成员。")

    @staticmethod
    def candidate_option(
        candidate: dict[str, Any],
        *,
        index: int,
        role_label: str,
    ) -> dict[str, str]:
        model_label = str(candidate.get("model_id") or "").strip()
        if candidate.get("candidate_type") == "runtime":
            runtime_label = str(
                candidate.get("runtime_name") or candidate.get("runtime_id") or "可用 Runtime"
            ).strip()
            description = f"负责{role_label}；使用 {runtime_label}"
            if model_label:
                description += f" · {model_label}"
            description += "，首次参与这项任务"
            label = "新建一位协作助手"
        else:
            selection_source = str(candidate.get("selection_source") or "")
            label = (
                str(candidate.get("name") or "现有协作助手")
                if selection_source == "existing_agent"
                else "现有协作助手"
            )
            description = f"适合{role_label}，有相关能力记录，可以立即开始"
            if model_label:
                description += f" · {model_label}"
        if index == 0:
            label = f"{label}（推荐）"
        return {
            "label": label,
            "value": f"candidate:{index}",
            "description": description,
        }

    def member_spec(
        self,
        team: Any,
        plan: TeamPlan,
        request: RuntimeStaffingRequest,
        candidate: dict[str, Any],
        *,
        owner_account_id: str,
    ) -> TeamMemberSpec:
        external_store = self._external_store()
        if external_store is None:
            raise ToolError("External Agent Store 未启用")
        required = normalize_capabilities(request.required_capabilities)
        role_key = str(candidate.get("role_key") or role_key_for_capabilities(required))
        preset = role_preset(role_key)
        if candidate.get("candidate_type") == "runtime":
            runtime_id = str(candidate.get("runtime_id") or "").strip()
            model_id = str(candidate.get("model_id") or "").strip()
            managed_identity = f"{runtime_id}\x1f{model_id}\x1f{role_key}"
            managed_key = hashlib.sha256(managed_identity.encode("utf-8")).hexdigest()
            name = f"Runtime 外援·{preset.get('label') or role_key}"
            generic_prompt = intelligent_role_markdown(
                role_key=role_key,
                agent_name=name,
                team_goal="根据每次 WorkflowPlan 节点上下文完成受控任务",
                assigned_capabilities=required,
            )
            agent = external_store.get_or_create_managed_agent(
                owner_account_id=owner_account_id,
                managed_kind="runtime_staffing",
                managed_key=managed_key,
                name=name,
                runtime_id=runtime_id,
                model=model_id,
                system_prompt=generic_prompt,
            )
        else:
            agent = external_store.get_agent(
                str(candidate.get("external_agent_id") or ""),
                owner_account_id=owner_account_id,
            )
            name = str(agent.get("name") or agent.get("id") or "Runtime 外援")

        external_agent_id = str(agent.get("id") or "").strip()
        if not external_agent_id:
            raise ToolError("补员候选缺少 External Agent id")
        # Runtime identity must not depend on a mutable or duplicate display
        # name.  The name is retained for prompts and UI labels only.
        member_id = external_agent_id
        role_markdown = intelligent_role_markdown(
            role_key=role_key,
            agent_name=name,
            team_goal=plan.goal,
            assigned_capabilities=required,
        )
        return TeamMemberSpec(
            member_id=member_id,
            name=name,
            role=str(preset.get("description") or "Runtime 动态补员"),
            executor="external",
            external_agent_id=external_agent_id,
            model=str(agent.get("model") or candidate.get("model_id") or ""),
            capabilities=required,
            system_prompt=role_markdown,
            metadata={
                "role_key": role_key,
                "role_label": str(preset.get("label") or role_key),
                "workflow_lane": str(preset.get("workflow_lane") or "build"),
                "selection_source": str(candidate.get("selection_source") or "runtime_staffing"),
                "runtime_staffing": True,
                "staffing_request_id": request.request_id,
            },
        )
