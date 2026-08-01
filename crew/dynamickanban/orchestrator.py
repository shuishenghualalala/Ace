"""Workflow Orchestrator：把用户请求转成可执行 WorkflowDefinition。

动态工作流设计：
- 编排逻辑从 prompt context 抽离成可持久化的 workflow 脚本。
- 一个 workflow 由若干 phase 组成，phase 内 agent_call 可并行。
- phase 之间通过 WorkflowDefinition.edges 形成 DAG，支持 verification gate。

生成策略：优先让 LLM 按 schema 生成 definition，失败时使用确定性单阶段模板。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import OrderedDict
from typing import Any

from crew.core.interfaces import LLMProvider
from crew.core.types import Message
from crew.dynamickanban.runtime_models import (
    AgentCall,
    Phase,
    VerificationGate,
    WorkflowDefinition,
)
from crew.state.logging import get_logger

log = get_logger("dynamickanban.orchestrator")


class WorkflowOrchestrator:
    """根据用户请求生成 WorkflowDefinition。"""

    def __init__(
        self,
        provider: LLMProvider,
        max_retries: int = 1,
        definition_cache_size: int = 32,
    ) -> None:
        self.provider = provider
        self.max_retries = max_retries
        # 按 request_hash 缓存 LLM 生成的 WorkflowDefinition，
        # 命中后使用当前 context 重新渲染变量占位符。
        self._definition_cache: OrderedDict[str, WorkflowDefinition] = OrderedDict()
        self._definition_cache_size = max(0, definition_cache_size)

    def _definition_cache_key(self, request: str) -> str:
        norm = re.sub(r"\s+", " ", request.strip()).lower()
        return hashlib.md5(norm.encode("utf-8")).hexdigest()

    def _get_cached_definition(self, request: str) -> WorkflowDefinition | None:
        if self._definition_cache_size <= 0:
            return None
        key = self._definition_cache_key(request)
        definition = self._definition_cache.pop(key, None)
        if definition is not None:
            self._definition_cache[key] = definition
            log.info("命中 workflow definition 缓存 key=%s", key)
        return definition

    def _cache_definition(
        self,
        request: str,
        definition: WorkflowDefinition,
    ) -> None:
        if self._definition_cache_size <= 0:
            return
        key = self._definition_cache_key(request)
        if key in self._definition_cache:
            self._definition_cache.pop(key)
        self._definition_cache[key] = definition
        while len(self._definition_cache) > self._definition_cache_size:
            self._definition_cache.popitem(last=False)

    async def build_definition(
        self,
        request: str,
        context: dict[str, Any] | None = None,
    ) -> WorkflowDefinition:
        """生成 workflow definition。"""
        ctx = context or {}
        cached = self._get_cached_definition(request)
        if cached is not None:
            return self._render_definition(cached, {"request": request, **ctx})

        try:
            definition = await self._definition_from_llm(request, ctx)
            if definition.phases:
                self._cache_definition(request, definition)
                return definition
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM 生成 workflow definition 失败: %s，降级到模板", exc)

        return self._fallback_definition(request, ctx)

    # ------------------------------------------------------------------ #
    async def _definition_from_llm(
        self,
        request: str,
        context: dict[str, Any],
    ) -> WorkflowDefinition:
        system = self._build_system_prompt()
        user = self._build_user_prompt(request, context)
        start = time.time()
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self.provider.chat([Message.system(system), Message.user(user)])
                data = self._extract_json(resp.text or "")
                if data and isinstance(data, dict) and data.get("phases"):
                    definition = WorkflowDefinition.from_dict(data)
                    # 重新渲染 prompt 中的变量（LLM 可能漏掉 ${request}）
                    definition = self._render_definition(definition, {"request": request, **context})
                    log.info(
                        "[DK Orchestrator] LLM 生成 definition 成功，耗时 %.2fs，attempt=%d",
                        time.time() - start,
                        attempt + 1,
                    )
                    return definition
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM workflow definition 尝试 %d 失败: %s", attempt + 1, exc)
                if attempt == self.max_retries:
                    raise
        raise ValueError("LLM 未返回合法 workflow definition")

    def _build_system_prompt(self) -> str:
        return (
            "你是一位 workflow orchestrator。你的任务是把用户请求转换成结构化的 "
            "workflow definition（JSON），用于驱动多个 worker agent 并行执行。\n"
            "规则：\n"
            "1. 每个 phase 内可包含多个并行的 agent_call；phase 之间只通过 edges 声明依赖。\n"
            "2. 请根据任务自行设计精简、清晰的 role。\n"
            "3. prompt 中可用 ${request}、${context.key} 等占位符。\n"
            "4. 关键阶段必须配置 verification_gate，输出 JSON 包含 {\"passed\": true/false}。\n"
            "5. 根据每个角色的能力与职责分配合适的任务，不要给角色分配超出其职责的工作。\n"
            "6. 输出必须是合法 JSON，格式如下。\n"
            "schema: {\n"
            '  "schema_version": 2,\n'
            '  "summary": "简短摘要",\n'
            '  "phases": [\n'
            '    {\n'
            '      "id": "phase_id",\n'
            '      "name": "阶段名",\n'
            '      "description": "",\n'
            '      "max_concurrent": 3,\n'
            '      "agent_calls": [\n'
            '        {\n'
            '          "id": "call_id",\n'
            '          "role": "role_name",\n'
            '          "prompt": "具体指令，可包含 ${request} / ${phase_id.call_id.text}",\n'
            '          "outputs": ["text"],\n'
            '          "requires_verification": true\n'
            '        }\n'
            '      ],\n'
            '      "verification_gate": {\n'
            '        "role": "verifier_role",\n'
            '        "prompt": "验收标准...",\n'
            '        "pass_key": "passed",\n'
            '        "fallback_phase_id": "",\n'
            '        "max_retries": 1\n'
            '      }\n'
            '    }\n'
            '  ],\n'
            '  "edges": [["parent_phase_id", "child_phase_id"]],\n'
            '  "max_concurrent": 3\n'
            "}"
        )

    @staticmethod
    def _build_user_prompt(
        request: str,
        context: dict[str, Any],
    ) -> str:
        ctx_text = json.dumps(context, ensure_ascii=False, indent=2) if context else "{}"
        return (
            f"用户请求：{request}\n"
            f"上下文：{ctx_text}\n"
            "请只输出 workflow definition JSON，不要解释。"
        )

    # ------------------------------------------------------------------ #
    # Repair / steer planning
    # ------------------------------------------------------------------ #
    async def build_repair_phases(
        self,
        request: str,
        replan_context: dict[str, Any],
    ) -> list[Phase]:
        """生成 1~2 个修复性 phase（失败自动 replan 与 steer 重规划共用）。

        replan_context 描述失败原因（失败阶段、错误、验证建议）或用户 steer 指令。
        返回的 phase 尚未接入 definition edges，由调用方决定挂载位置。
        """
        system = self._build_system_prompt()
        user = (
            f"用户请求：{request}\n"
            "workflow 执行中需要动态调整。请规划 1~2 个修复/调整阶段（不是完整 workflow）。\n"
            f"调整上下文：{json.dumps(replan_context, ensure_ascii=False, indent=2)}\n"
            "要求：\n"
            "- 只输出修复阶段的 JSON：{\"phases\": [...]}，phase schema 与 workflow definition 相同。\n"
            "- 修复阶段的任务是弥补失败原因或落实用户的新指令，不要重复已成功的工作。\n"
            "- phase id 必须以 repair_ 开头。\n"
            "- 不要输出 edges，调用方负责把修复阶段接入现有 DAG。\n"
        )
        resp = await self.provider.chat([Message.system(system), Message.user(user)])
        data = self._extract_json(resp.text or "")
        if not data or not isinstance(data, dict) or not data.get("phases"):
            raise ValueError("LLM 未返回合法修复阶段")
        mini = {
            "schema_version": 2,
            "summary": "repair",
            "phases": data["phases"],
            "edges": [],
        }
        definition = WorkflowDefinition.from_dict(mini)
        return self._render_definition(definition, {"request": request}).phases

    # ------------------------------------------------------------------ #
    # Fallback deterministic definition
    # ------------------------------------------------------------------ #
    def _fallback_definition(
        self,
        request: str,
        context: dict[str, Any],
    ) -> WorkflowDefinition:
        """LLM 失败时的确定性单阶段兜底。"""
        variables = {"request": request, **context}
        roles = ["worker"]

        phases: list[Phase] = []
        edges: list[tuple[str, str]] = []
        for idx, role in enumerate(roles):
            phase_id = f"phase_{idx + 1}_{role}"
            call = AgentCall(
                id=f"{phase_id}_call",
                role=role,
                prompt=self._render(
                    "用户需求：${request}\n\n"
                    f"你是工作流中的 {role}。请根据当前需求完成本阶段任务，"
                    "并把结果写入工作目录中的对应文件。",
                    variables,
                ),
                outputs=["text"],
            )
            gate = VerificationGate(
                role=role,
                prompt="请检查本阶段产出是否满足需求：如果满足，输出 {\"passed\": true}，否则输出 {\"passed\": false, \"reason\": \"...\"}",
                fallback_phase_id="",
                max_retries=1,
            )
            phases.append(
                Phase(
                    id=phase_id,
                    name=f"阶段 {idx + 1}: {role}",
                    description=f"由 {role} 执行",
                    agent_calls=[call],
                    verification_gate=gate,
                )
            )
            if idx > 0:
                edges.append((phases[idx - 1].id, phase_id))

        return WorkflowDefinition(
            summary=self._render("${request} 的顺序执行 workflow", variables),
            phases=phases,
            edges=edges,
            max_concurrent=1,
        )

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _new_phase_id() -> str:
        import uuid

        return f"phase_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _new_call_id(phase_id: str) -> str:
        import uuid

        return f"{phase_id}_call_{uuid.uuid4().hex[:8]}"

    @staticmethod
    def _render(template: str, variables: dict[str, Any]) -> str:
        rendered = template
        for key, value in variables.items():
            for pat in [f"${{{key}}}", f"{{{{{key}}}}}"]:
                rendered = rendered.replace(pat, str(value))
        return rendered

    def _render_definition(
        self,
        definition: WorkflowDefinition,
        variables: dict[str, Any],
    ) -> WorkflowDefinition:
        for phase in definition.phases:
            phase.name = self._render(phase.name, variables)
            phase.description = self._render(phase.description, variables)
            for call in phase.agent_calls:
                call.prompt = self._render(call.prompt, variables)
            if phase.verification_gate:
                phase.verification_gate.prompt = self._render(phase.verification_gate.prompt, variables)
        definition.summary = self._render(definition.summary, variables)
        return definition

    @staticmethod
    def _extract_json(text: str) -> dict[str, Any] | None:
        if not text:
            return None
        for pat in [r"```(?:json)?\s*([\s\S]*?)```", r"\{[\s\S]*\}"]:
            m = re.search(pat, text)
            if m:
                if m.lastindex and m.group(1):
                    candidate = m.group(1).strip()
                else:
                    candidate = m.group(0).strip()
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            return None
