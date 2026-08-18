"""多智能体 Team 协同 + 任务管理。"""

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from crew.agent.executor import BuiltinExecutor
from crew.agent.executor.external import AcpExecutor
from crew.agent.external.store import ExternalAgentStore
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ToolError
from crew.core.interfaces import LLMProvider
from crew.core.mocks import InMemorySessionStore, NullMemory
from crew.core.runctx import current_agent_id, current_agent_workdir
from crew.core.types import ChatResponse, Message, StreamChunk, ToolCall
from crew.dynamickanban.store import SQLiteKanbanStore
from crew.gateway.auth import AccountContext
from crew.gateway.routers.sessions import create_sessions_router
from crew.plugins.manager import PluginManager
from crew.providers.openai_provider import OpenAIProvider
from crew.state.config import Config, load_config
from crew.tasks.runtime import TaskRuntime
from crew.tasks.task_manager import InMemoryTaskManager, LegacyTaskManagerAdapter
from crew.team.delegate_tool import run_delegate_to_teammate
from crew.team.bus import TeamBus
from crew.security.launch import ProcessLaunch, current_process_launch
from crew.security.models import (
    FilesystemAccess,
    FilesystemEntry,
    PermissionProfile,
    PermissionProfileKind,
)
from crew.team.graph_planner import (
    DEFAULT_PLANNING_DECISION_TIMEOUT,
    PLANNING_DECISION_MAX_TOKENS,
    TeamGraphPlanner,
    _normalize_nodes_with_graph,
    schedule_planning_provider_warmup,
)
from crew.team.agent_profile import (
    AgentProfile,
    CapabilityAssessment,
    evaluate_capability_coverage,
)
from crew.team.history_projection import team_internal_history_items
from crew.team.models import RuntimeStaffingRequest, TeamPlan, TeamPlanEdge, TeamPlanNode
from crew.team.result_presenter import (
    assignment_text,
    node_dict_assignment_text,
    node_dict_should_show_assignment,
    node_display_progress,
    result_projection,
    should_show_assignment,
)
from crew.team.roles import CREW_BUILTIN_AGENT_ID
from crew.team.team_manager import (
    InProcessTeamManager,
    NodeExecutionAssessment,
    _join_stream_fragments,
    _normalize_legacy_chunked_thinking,
)
from crew.team.team_spec import build_team_spec
from crew.team.turn_decision import (
    TeamTurnDecision,
    coerce_team_turn_decision,
    new_workflow_decision,
)
from crew.team.turn_router import TeamTurnRouter
from crew.team.workflow_plan import coerce_planning_decision
from crew.team.workspace_guard import check_workspace_guard, classify_external_permission
from crew.tools.registry import Registry, register_builtin_tools


def _structured_team_spec(
    goal: str,
    *,
    capabilities: list[str] | tuple[str, ...] = (),
    intent: str = "implementation",
    complexity: str = "focused",
    workflow_lanes: list[str] | tuple[str, ...] = (),
) -> dict:
    return {
        "goal": goal,
        "task_profile": {
            "intent": intent,
            "complexity": complexity,
        },
        "team_requirements": {
            "capabilities": list(capabilities),
            "workflow_lanes": list(workflow_lanes),
        },
    }


class RoleProvider(LLMProvider):
    """Leader 委派给 coder；coder 调 terminal 后给出结果。"""

    async def chat(self, messages, tools=None):
        sys = messages[0].content
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if "真实审阅" in last_user:
            return ChatResponse(text="Leader验收通过：检查实现结果、风险和交付条件，结论可交付。")
        if "最终汇总" in last_user:
            return ChatResponse(text="团队最终答案：2")
        if "真实验收" in last_user:
            return ChatResponse(text="Leader验收通过：检查实现结果、风险和交付条件，结论可交付。")
        has_tool_result = any(m.role == "tool" for m in messages)
        if "Leader（队长）" in sys:
            if not has_tool_result:
                return ChatResponse(tool_calls=[
                    ToolCall("d1", "delegate_to_teammate", {"member": "coder", "instruction": "算 1+1"})
                ])
            return ChatResponse(text="团队最终答案：2")
        # teammate
        if not has_tool_result:
            return ChatResponse(tool_calls=[ToolCall("t1", "terminal", {"command": "echo 2"})])
        return ChatResponse(text="coder算出：2")

    async def stream_chat(self, messages, tools=None):
        resp = await self.chat(messages, tools)
        if resp.text:
            yield StreamChunk(delta_text=resp.text)
        yield StreamChunk(done=True, tool_calls=resp.tool_calls, finish_reason=resp.finish_reason)


def test_team_planning_progress_uses_agent_turn_timing_contract(monkeypatch):
    monkeypatch.setattr('crew.team.team_manager.time.time', lambda: 1_700_000_010.0)
    envelope = Envelope.of('开发贪吃蛇', session_id='team-planning', request_id='req-planning')

    running = InProcessTeamManager._planning_progress_chunk(envelope, {
        'phase': 'reasoning',
        'status': 'running',
        'label': '正在识别工作单元',
        'elapsed_ms': 2_500,
    })
    done = InProcessTeamManager._planning_progress_chunk(envelope, {
        'phase': 'compiled',
        'status': 'done',
        'label': '团队执行图已就绪',
        'elapsed_ms': 4_200,
    })

    assert running.kind == 'team_internal'
    assert running.body['event_type'] == 'team_planning_progress'
    assert running.body['display_mode'] == 'stream'
    assert running.body['turn_started_at'] == pytest.approx(1_700_000_007.5)
    assert 'turn_duration' not in running.body
    assert done.body['display_mode'] == 'collapsible'
    assert done.body['collapsed_title'] == 'Crew 已生成团队执行图'
    assert '- 准备团队执行：完成' in done.body['process_text']
    assert '进行中' not in done.body['process_text']
    assert done.body['turn_started_at'] == pytest.approx(1_700_000_005.8)
    assert done.body['turn_duration'] == pytest.approx(4.2)


class JsonGraphProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        return ChatResponse(text="""
{
  "nodes": [
    {"id": "leader_plan", "title": "Leader 定义接口目标", "detail": "确认登录接口边界、成员分工和验收标准。", "assignee": "leader", "workflow_lane": "lead"},
    {"id": "api_build", "title": "实现登录接口", "detail": "完成接口实现、自测和风险说明。", "assignee": "dev", "workflow_lane": "build", "required_capabilities": ["backend", "implementation"]},
    {"id": "qa_verify", "title": "验证登录接口", "detail": "验证核心路径、失败路径和回归风险。", "assignee": "qa", "workflow_lane": "verify", "required_capabilities": ["testing", "verification"]},
    {"id": "leader_summary", "title": "Leader 汇总结论", "detail": "汇总实现、验证和交付建议。", "assignee": "leader", "workflow_lane": "summary"}
  ],
  "edges": [["leader_plan", "api_build"], ["api_build", "qa_verify"], ["qa_verify", "leader_summary"]],
  "notes": ["单方案 DAG"]
}
""")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class SemanticPlanningProvider(LLMProvider):
    def __init__(self):
        self.calls = 0
        self.messages = []

    async def chat(self, messages, tools=None, *, max_tokens=None):
        self.calls += 1
        self.messages = list(messages)
        return ChatResponse(text="""
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "parallel_merge",
  "quality_policy": "independent_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "medium",
  "semantic_uncertainty": "low",
  "work_units": [
    {
      "id": "research_architecture_a",
      "objective": "调研架构 A",
      "display_title": "调研架构 A",
      "kind": "research",
      "required_capabilities": ["research", "analysis"],
      "depends_on": [],
      "expected_output": "架构 A 调研摘要"
    },
    {
      "id": "research_jiuwen",
      "objective": "调研 JiuwenSwarm",
      "display_title": "调研 Jiuwen",
      "kind": "research",
      "required_capabilities": ["research", "analysis"],
      "depends_on": [],
      "expected_output": "JiuwenSwarm 调研摘要"
    },
    {
      "id": "synthesis",
      "objective": "综合比较并形成综述",
      "display_title": "综合综述",
      "kind": "docs",
      "required_capabilities": ["analysis", "synthesis", "documentation"],
      "depends_on": ["research_architecture_a", "research_jiuwen"],
      "expected_output": "架构综述",
      "needs_independent_review": true
    }
  ]
}
""")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class ParallelResearchPlanningProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        return ChatResponse(text="""
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "parallel_merge",
  "quality_policy": "leader_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "low",
  "semantic_uncertainty": "low",
  "work_units": [
    {
      "id": "research_city1",
      "objective": "调研城市 1 的隐藏小吃",
      "display_title": "调研城市 1",
      "kind": "research",
      "required_capabilities": ["research", "analysis"],
      "depends_on": [],
      "expected_output": "城市 1 小吃摘要"
    },
    {
      "id": "research_city2",
      "objective": "调研城市 2 的隐藏小吃",
      "display_title": "调研城市 2",
      "kind": "research",
      "required_capabilities": ["research", "analysis"],
      "depends_on": [],
      "expected_output": "城市 2 小吃摘要"
    },
    {
      "id": "research_city3",
      "objective": "调研城市 3 的隐藏小吃",
      "display_title": "调研城市 3",
      "kind": "research",
      "required_capabilities": ["research", "analysis"],
      "depends_on": [],
      "expected_output": "城市 3 小吃摘要"
    },
    {
      "id": "compile_results",
      "objective": "整合三个城市的小吃结果",
      "display_title": "整合结果",
      "kind": "docs",
      "required_capabilities": ["documentation", "synthesis"],
      "depends_on": ["research_city1", "research_city2", "research_city3"],
      "expected_output": "最终小吃清单"
    }
  ]
}
""")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class DisplayMappingProvider(RoleProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if "最终对象映射提取器" in last_user:
            return ChatResponse(text="""
{
  "updates": [
    {"node_id": "research_city1", "display_subject": "四川", "display_title": "调研城市 1 - 四川"}
  ]
}
""")
        return await super().chat(messages, tools)


P0_STANDARD_SEMANTIC_SCENARIOS = {
    "parallel_research": {
        "goal": "找中国3个不同城市的隐藏小吃，并配一句本地话做点评",
        "decision": {
            "goal_clarity": "high",
            "critical_missing_info": [],
            "dependency_pattern": "parallel_merge",
            "quality_policy": "leader_review",
            "dynamic_discovery": False,
            "conditional_branching": False,
            "iteration_until_convergence": False,
            "risk_level": "low",
            "semantic_uncertainty": "low",
            "work_units": [
                {
                    "id": "research_city1",
                    "objective": "调研第一个城市的隐藏小吃",
                    "display_title": "调研城市 1",
                    "kind": "research",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": [],
                    "expected_output": "城市 1 小吃摘要",
                },
                {
                    "id": "research_city2",
                    "objective": "调研第二个城市的隐藏小吃",
                    "display_title": "调研城市 2",
                    "kind": "research",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": [],
                    "expected_output": "城市 2 小吃摘要",
                },
                {
                    "id": "research_city3",
                    "objective": "调研第三个城市的隐藏小吃",
                    "display_title": "调研城市 3",
                    "kind": "research",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": [],
                    "expected_output": "城市 3 小吃摘要",
                },
                {
                    "id": "compile_foods",
                    "objective": "整合小吃清单和点评",
                    "display_title": "整合小吃",
                    "kind": "docs",
                    "required_capabilities": ["synthesis", "documentation"],
                    "depends_on": ["research_city1", "research_city2", "research_city3"],
                    "expected_output": "最终小吃清单",
                },
            ],
        },
        "required_nodes": {"research_city1", "research_city2", "research_city3", "compile_foods"},
        "required_edges": {("research_city1", "compile_foods"), ("research_city2", "compile_foods"), ("research_city3", "compile_foods")},
    },
    "synthesis": {
        "goal": "对比三种动态工作流，输出 Crew 可复用的协作设计综述",
        "decision": {
            "goal_clarity": "high",
            "critical_missing_info": [],
            "dependency_pattern": "parallel_merge",
            "quality_policy": "independent_review",
            "dynamic_discovery": False,
            "conditional_branching": False,
            "iteration_until_convergence": False,
            "risk_level": "medium",
            "semantic_uncertainty": "low",
            "work_units": [
                {
                    "id": "research_architecture_a",
                    "objective": "梳理架构 A 的模块边界和可复用点",
                    "display_title": "调研架构 A",
                    "kind": "research",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": [],
                    "expected_output": "架构 A 摘要",
                },
                {
                    "id": "research_architecture_b",
                    "objective": "梳理架构 B 的协作流程和可复用点",
                    "display_title": "调研协作流",
                    "kind": "research",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": [],
                    "expected_output": "架构 B 摘要",
                },
                {
                    "id": "synthesize_architecture",
                    "objective": "综合形成 Crew 协作设计建议",
                    "display_title": "综合架构",
                    "kind": "docs",
                    "required_capabilities": ["synthesis", "documentation"],
                    "depends_on": ["research_architecture_a", "research_architecture_b"],
                    "expected_output": "协作设计综述",
                },
            ],
        },
        "required_nodes": {"research_architecture_a", "research_architecture_b", "synthesize_architecture", "independent_review"},
        "required_edges": {("research_architecture_a", "synthesize_architecture"), ("research_architecture_b", "synthesize_architecture")},
    },
    "legal_consultation": {
        "goal": "分析一个中国生成式 AI 数据合规问题，给出风险点和整改建议",
        "decision": {
            "goal_clarity": "high",
            "critical_missing_info": [],
            "dependency_pattern": "staged",
            "quality_policy": "independent_review",
            "dynamic_discovery": False,
            "conditional_branching": False,
            "iteration_until_convergence": False,
            "risk_level": "high",
            "semantic_uncertainty": "medium",
            "work_units": [
                {
                    "id": "facts_scope",
                    "objective": "整理用户事实、问题边界和待判断事项",
                    "display_title": "事实边界",
                    "kind": "analysis",
                    "required_capabilities": ["analysis"],
                    "depends_on": [],
                    "expected_output": "事实与问题清单",
                },
                {
                    "id": "law_research",
                    "objective": "检索并整理相关监管要求",
                    "display_title": "法规检索",
                    "kind": "research",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": ["facts_scope"],
                    "expected_output": "法规依据摘要",
                },
                {
                    "id": "risk_advice",
                    "objective": "形成风险分析和整改建议",
                    "display_title": "风险建议",
                    "kind": "docs",
                    "required_capabilities": ["synthesis", "documentation"],
                    "depends_on": ["law_research"],
                    "expected_output": "风险和整改建议",
                },
            ],
        },
        "required_nodes": {"facts_scope", "law_research", "risk_advice", "independent_review"},
        "required_edges": {("facts_scope", "law_research"), ("law_research", "risk_advice")},
    },
    "history_query": {
        "goal": "根据已有团队事件，回答上一轮每个成员分别做了什么、用了多久、哪个节点最慢",
        "decision": {
            "goal_clarity": "high",
            "critical_missing_info": [],
            "dependency_pattern": "sequential",
            "quality_policy": "none",
            "dynamic_discovery": False,
            "conditional_branching": False,
            "iteration_until_convergence": False,
            "risk_level": "low",
            "semantic_uncertainty": "low",
            "work_units": [
                {
                    "id": "collect_events",
                    "objective": "读取并整理已有团队事件和节点耗时",
                    "display_title": "整理事件",
                    "kind": "analysis",
                    "required_capabilities": ["analysis"],
                    "depends_on": [],
                    "expected_output": "成员动作和耗时摘要",
                },
                {
                    "id": "answer_status",
                    "objective": "汇总回答用户的团队状态问题",
                    "display_title": "回答状态",
                    "kind": "docs",
                    "required_capabilities": ["synthesis", "documentation"],
                    "depends_on": ["collect_events"],
                    "expected_output": "团队状态答复",
                },
            ],
        },
        "required_nodes": {"collect_events", "answer_status"},
        "required_edges": {("collect_events", "answer_status")},
    },
    "dev_test_loop": {
        "goal": "开发一个登录接口并完成测试验收",
        "decision": {
            "goal_clarity": "high",
            "critical_missing_info": [],
            "dependency_pattern": "staged",
            "quality_policy": "leader_review",
            "dynamic_discovery": False,
            "conditional_branching": False,
            "iteration_until_convergence": False,
            "risk_level": "medium",
            "semantic_uncertainty": "low",
            "work_units": [
                {
                    "id": "api_design",
                    "objective": "确认登录接口边界、输入输出和验收标准",
                    "display_title": "接口设计",
                    "kind": "design",
                    "required_capabilities": ["analysis", "design"],
                    "depends_on": [],
                    "expected_output": "接口设计方案",
                },
                {
                    "id": "api_build",
                    "objective": "实现登录接口并自测核心路径",
                    "display_title": "接口实现",
                    "kind": "build",
                    "required_capabilities": ["implementation"],
                    "depends_on": ["api_design"],
                    "expected_output": "接口实现结果",
                },
                {
                    "id": "qa_verify",
                    "objective": "验证登录接口成功、失败和回归风险",
                    "display_title": "接口验证",
                    "kind": "verify",
                    "required_capabilities": ["verification", "testing"],
                    "depends_on": ["api_build"],
                    "expected_output": "测试验证结论",
                },
            ],
        },
        "required_nodes": {"api_design", "api_build", "qa_verify"},
        "required_edges": {("api_design", "api_build"), ("api_build", "qa_verify")},
    },
}


class P0ScenarioPlanningProvider(LLMProvider):
    def __init__(self, scenario: str):
        self.scenario = scenario
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=None):
        self.chat_calls += 1
        decision = P0_STANDARD_SEMANTIC_SCENARIOS[self.scenario]["decision"]
        return ChatResponse(text=json.dumps(decision, ensure_ascii=False))

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        decision = P0_STANDARD_SEMANTIC_SCENARIOS[self.scenario]["decision"]
        yield StreamChunk(delta_text=json.dumps(decision, ensure_ascii=False))
        yield StreamChunk(done=True)


class StreamingPlanningProvider(LLMProvider):
    def __init__(self):
        self.stream_calls = 0
        self.chat_calls = 0
        self.stream_max_tokens = None
        self.chat_max_tokens = None

    async def chat(self, messages, tools=None, *, max_tokens=None):
        self.chat_calls += 1
        self.chat_max_tokens = max_tokens
        await asyncio.sleep(1.0)
        return ChatResponse(text="")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        self.stream_max_tokens = max_tokens
        text = """
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "sequential",
  "quality_policy": "leader_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "low",
  "semantic_uncertainty": "low",
  "work_units": [{
    "id": "draft_answer",
    "objective": "整理答案",
    "kind": "docs",
    "required_capabilities": ["documentation"],
    "depends_on": [],
    "expected_output": "答案"
  }]
}
"""
        midpoint = len(text) // 2
        yield StreamChunk(delta_text=text[:midpoint])
        yield StreamChunk(delta_text=text[midpoint:])
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class ReasoningThenPlanningProvider(StreamingPlanningProvider):
    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        yield StreamChunk(reasoning_content="先分析任务")
        yield StreamChunk(delta_text="""
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "sequential",
  "quality_policy": "leader_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "low",
  "semantic_uncertainty": "low",
  "work_units": [{
    "id": "draft_answer",
    "objective": "整理答案",
    "kind": "docs",
    "required_capabilities": ["documentation"],
    "depends_on": [],
    "expected_output": "答案"
  }]
}
""")
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class ReasoningOnlyPlanningProvider(LLMProvider):
    def __init__(self):
        self.stream_calls = 0
        self.chat_calls = 0
        self.stream_max_tokens = None
        self.chat_max_tokens = None

    async def chat(self, messages, tools=None, *, max_tokens=None):
        self.chat_calls += 1
        self.chat_max_tokens = max_tokens
        return ChatResponse(text="", reasoning_content="继续推演但没有输出 JSON", finish_reason="length")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        self.stream_max_tokens = max_tokens
        yield StreamChunk(reasoning_content="先分析任务结构")
        yield StreamChunk(delta_text="", done=True, finish_reason="length")


class ChatWinsPlanningProvider(LLMProvider):
    def __init__(self):
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=None):
        self.chat_calls += 1
        return ChatResponse(text=json.dumps(P0_STANDARD_SEMANTIC_SCENARIOS["synthesis"]["decision"], ensure_ascii=False))

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        yield StreamChunk(reasoning_content="持续推演但暂不输出 JSON")
        await asyncio.sleep(1.0)
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class StreamReasoningGracePlanningProvider(LLMProvider):
    def __init__(self):
        self.stream_calls = 0
        self.chat_calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=None):
        self.chat_calls += 1
        await asyncio.sleep(1.0)
        return ChatResponse(text=json.dumps(P0_STANDARD_SEMANTIC_SCENARIOS["synthesis"]["decision"], ensure_ascii=False))

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        yield StreamChunk(reasoning_content="持续推演规划")
        await asyncio.sleep(0.3)
        yield StreamChunk(delta_text=json.dumps(P0_STANDARD_SEMANTIC_SCENARIOS["synthesis"]["decision"], ensure_ascii=False))
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class CachePlanningProvider(SemanticPlanningProvider):
    pass


class WarmupPlanningProvider(LLMProvider):
    def __init__(self):
        self.stream_calls = 0

    async def chat(self, messages, tools=None, *, max_tokens=None):
        return ChatResponse(text='{"ok":true}')

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        self.stream_calls += 1
        yield StreamChunk(delta_text='{"ok":true}')
        yield StreamChunk(delta_text="", done=True, finish_reason="stop")


class MissingInfoPlanningProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        return ChatResponse(text="""
{
  "goal_clarity": "low",
  "critical_missing_info": ["需要分析的合同正文或文件"],
  "dependency_pattern": "sequential",
  "quality_policy": "leader_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "medium",
  "semantic_uncertainty": "high",
  "work_units": [{
    "id": "analyze_contract",
    "objective": "分析合同风险",
    "kind": "analysis",
    "required_capabilities": ["analysis", "review"],
    "depends_on": [],
    "expected_output": "合同风险分析"
  }]
}
""")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class DefaultableMissingInfoPlanningProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        return ChatResponse(text="""
{
  "goal_clarity": "medium",
  "critical_missing_info": ["技术栈偏好未说明"],
  "dependency_pattern": "sequential",
  "quality_policy": "leader_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "low",
  "semantic_uncertainty": "medium",
  "work_units": [{
    "id": "build_snake_frontend",
    "objective": "实现一个可运行的贪吃蛇前端",
    "kind": "build",
    "required_capabilities": ["implementation"],
    "depends_on": [],
    "expected_output": "可运行的前端代码"
  }]
}
""")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class JsonProfileProvider(RoleProvider):
    def __init__(self):
        self.profile_calls = 0

    async def chat(self, messages, tools=None):
        system = messages[0].content if messages else ""
        if "轻量执行画像器" in system:
            self.profile_calls += 1
            return ChatResponse(text="""
{
  "task_profile": {"intent": "implementation", "complexity": "focused", "deliverable_shape": "artifact"},
  "execution_profile": {"requested_mode": "fast", "budget": {"max_nodes": 3}},
  "team_requirements": {"workflow_lanes": ["build"]}
}
""")
        return await super().chat(messages, tools)


class FailingGraphProvider(RoleProvider):
    async def chat(self, messages, tools=None):
        raise RuntimeError("planner timeout")


class SlowPlanningProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        await asyncio.sleep(0.25)
        return ChatResponse(text="{}")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class InvalidJsonPlanningProvider(LLMProvider):
    async def chat(self, messages, tools=None, *, max_tokens=None):
        return ChatResponse(text="这不是 JSON {{{")

    async def stream_chat(self, messages, tools=None, *, max_tokens=None):
        yield StreamChunk(delta_text="", done=True)


class LeaderQuestionProvider(RoleProvider):
    async def chat(self, messages, tools=None):
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        if "直接处理当前节点" in last_user:
            return ChatResponse(text="天气需要补充位置才能查询；团队成员 kk 已在位，Leader 可继续协调。")
        if "最终汇总" in last_user:
            assert "天气需要补充位置才能查询" in last_user
            return ChatResponse(text="天气需要补充位置才能查询；团队成员 kk 已准备好，我会继续协调。")
        return await super().chat(messages, tools)


def _team(provider=None, config=None, kanban_store=None):
    reg = Registry()
    register_builtin_tools(reg)
    tasks = InMemoryTaskManager()
    tm = InProcessTeamManager(
        provider=provider or RoleProvider(),
        registry=reg,
        session_store=InMemorySessionStore(),
        memory=NullMemory(),
        plugins=PluginManager(),
        tasks=tasks,
        config=config or Config(max_iterations=5),
        kanban_store=kanban_store,
    )
    return tm, tasks


def test_team_result_projection_builds_structured_contract():
    projection = result_projection(
        "结论：可以验收。\n关键依据：核心路径、回归用例和安全检查均已通过。\n风险：未发现阻断问题。\n建议：进入交付。"
    )

    assert projection["result_contract"]["answer"] == "可以验收"
    assert projection["result_contract"]["evidence"] == "核心路径、回归用例和安全检查均已通过"
    assert projection["result_contract"]["risk"] == "未发现阻断问题"
    assert projection["result_contract"]["next_action"] == "进入交付"
    assert projection["result_contract"]["status_signal"] == "pass"
    assert projection["summary_items"] == [
        "结论：可以验收",
        "依据：核心路径、回归用例和安全检查均已通过",
        "风险：未发现阻断问题",
        "建议：进入交付",
    ]


def test_team_result_projection_keeps_useful_command_evidence_untruncated():
    projection = result_projection(
        "验证情况：JS 语法已通过 `node --check index.html`，核心交互通过浏览器手动检查，未发现阻断问题。"
    )

    joined = "\n".join(projection["summary_items"])
    assert "node --check index.html" in joined
    assert "..." not in joined


def test_team_result_projection_does_not_cut_inside_code_span():
    projection = result_projection(
        "验证情况："
        + "已完成基础检查，" * 20
        + "关键命令 `node --check index.html && npm run build` 已通过，未发现阻断问题。"
    )

    joined = "\n".join(projection["summary_items"])
    assert joined.count("`") % 2 == 0
    assert "`node --check index.html && npm run build`" in joined or "`node --check" not in joined


def test_team_presenter_does_not_infer_lane_from_node_title_or_id():
    without_contract = node_display_progress(
        node_id="qa_verify_1",
        title="测试验证：登录接口",
        assignee="qa",
    )
    with_contract = node_display_progress(
        node_id="opaque_1",
        title="普通节点",
        assignee="qa",
        metadata={"workflow_lane": "verify"},
    )

    assert without_contract["workflow_lane"] == "other"
    assert with_contract["workflow_lane"] == "verify"


def test_team_decision_presenter_uses_same_rules_for_dict_and_plan_nodes():
    plan_node = TeamPlanNode(
        node_id="qa_engineer_plan_1",
        title="测试方案：覆盖关键链路",
        assignee="qa",
        metadata={"workflow_lane": "plan"},
    )
    raw_node = {
        "node_id": plan_node.node_id,
        "title": plan_node.title,
        "assignee": plan_node.assignee,
        "metadata": dict(plan_node.metadata),
    }

    assert assignment_text(plan_node) == node_dict_assignment_text(raw_node)
    assert assignment_text(plan_node) == "@qa 测试方案：覆盖关键链路：请只写方案，先不要执行验证。"

    verify_node = TeamPlanNode(
        node_id="qa_verify_1",
        title="测试验证：执行回归",
        assignee="qa",
        metadata={"workflow_lane": "verify"},
    )
    plan = TeamPlan(team_session_id="team_1", goal="测试目标")
    plan.edges = [TeamPlanEdge(parent_id="leader_review", child_id=verify_node.node_id)]
    raw_verify_node = {
        "node_id": verify_node.node_id,
        "title": verify_node.title,
        "assignee": verify_node.assignee,
        "metadata": dict(verify_node.metadata),
    }
    raw_edges = [{"parent_id": "leader_review", "child_id": verify_node.node_id}]

    assert should_show_assignment(plan, verify_node) is True
    assert node_dict_should_show_assignment(raw_verify_node, raw_edges) is True


def test_leader_review_decision_parser_supports_structured_and_plain_results():
    structured = InProcessTeamManager._parse_leader_review_decision(
        '```json\n{"action":"revise","target_node_id":"build_1","message":"实现不完整","instructions":"补齐错误处理"}\n```'
    )
    plain = InProcessTeamManager._parse_leader_review_decision("需要用户补充生产环境权限信息。")

    assert structured == {
        "action": "revise",
        "target_node_id": "build_1",
        "message": "实现不完整",
        "instructions": "补齐错误处理",
    }
    assert plain["action"] == "ask_user"


def test_team_user_input_signal_ignores_generic_pending_confirmation():
    assert InProcessTeamManager._result_requires_user_input(
        "提交方案、风险和待确认问题给 Leader 审阅",
        {"status_signal": "pass"},
    ) is False
    assert InProcessTeamManager._result_requires_user_input(
        "缺少生产环境权限，需要用户补充授权信息",
        {"status_signal": "fail"},
    ) is True


def test_leader_review_rejects_missing_plan_claim_when_upstream_is_present():
    plan = TeamPlan(team_session_id="review_conflict", goal="开发小游戏")
    design = TeamPlanNode(
        node_id="build_design_1",
        title="实现方案",
        assignee="kk",
        status="completed",
        result_summary="已提交完整实现方案",
        artifact_refs=["/tmp/实现方案.md"],
    )
    review = TeamPlanNode(node_id="leader_review", title="Leader 审阅", assignee="leader")
    plan.nodes = {design.node_id: design, review.node_id: review}
    plan.edges = [TeamPlanEdge(parent_id=design.node_id, child_id=review.node_id)]

    assert InProcessTeamManager._leader_review_decision_conflicts(
        plan,
        review,
        {
            "action": "ask_user",
            "message": "当前仅包含成员角色，缺少实现方案，无法审阅。",
            "instructions": "请用户重新提供方案。",
        },
    ) is True


@pytest.mark.parametrize("answers", [[], [{"id": "__cancelled__", "answers": []}]])
def test_leader_review_followup_timeout_or_cancel_is_not_answered(answers):
    assert InProcessTeamManager._review_followup_answered(answers) is False


async def test_leader_review_followup_answer_reopens_and_reexecutes(monkeypatch):
    tm, _ = _team()
    team = tm._build_team("review_followup")
    plan = TeamPlan(team_session_id="review_followup", goal="开发小游戏")
    design = TeamPlanNode(
        node_id="build_design_1",
        title="实现方案",
        assignee="coder",
        status="completed",
        result_summary="方案完整，但需要用户确认是否保留撤销功能。",
    )
    review = TeamPlanNode(node_id="leader_review", title="Leader 审阅", assignee="leader")
    summary = TeamPlanNode(node_id="leader_summary", title="Leader 总结", assignee="leader")
    plan.nodes = {node.node_id: node for node in (design, review, summary)}
    plan.edges = [
        TeamPlanEdge(parent_id=design.node_id, child_id=review.node_id),
        TeamPlanEdge(parent_id=review.node_id, child_id=summary.node_id),
    ]
    tm._plans[tm._key(plan.team_session_id, "owner")] = plan

    async def fake_ensure(*args, **kwargs):
        return plan

    review_calls = 0

    async def fake_run_leader(envelope, *, node, **kwargs):
        nonlocal review_calls
        if node.node_id == "leader_review":
            review_calls += 1
            if review_calls == 1:
                return '{"action":"ask_user","message":"是否保留撤销功能？","target_node_id":"","instructions":""}'
            assert (node.metadata or {}).get("user_followup_answers")
            return '{"action":"approve","message":"用户已确认，方案通过。","target_node_id":"","instructions":""}'
        return "已完成最终汇总。"

    sent: dict[str, object] = {}

    async def fake_send(session_id, questions, **kwargs):
        sent["session_id"] = session_id
        sent["questions"] = questions
        return session_id, "question_1"

    async def fake_wait(session_id, question_id):
        return [{"id": "leader_review_decision", "answers": ["确认并继续"]}]

    monkeypatch.setattr(tm, "_ensure_runtime_plan_async", fake_ensure)
    monkeypatch.setattr(tm, "_run_leader_node", fake_run_leader)
    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)
    monkeypatch.setattr("crew.team.team_manager.wait_for_answer", fake_wait)

    chunks = [
        chunk async for chunk in tm._run_required_workflow(
            Envelope.of("开发小游戏", session_id="review_followup", user_id="owner"),
            team=team,
            external_team_id="",
        )
    ]

    assert sent["session_id"] == "review_followup"
    assert review_calls == 2
    assert review.status == "completed"
    assert review.metadata["user_followup_answers"][0]["answers"] == ["确认并继续"]
    assert any(chunk.kind == "team_internal" and chunk.body.get("event_type") == "team_summary" for chunk in chunks)


async def test_leader_node_streams_thinking_tools_and_delta_before_result(monkeypatch):
    tm, _ = _team()
    team = tm._build_team("leader_live_stream")
    plan = TeamPlan(team_session_id="leader_live_stream", goal="审阅方案")
    review = TeamPlanNode(node_id="leader_review", title="Leader 审阅", assignee="leader")

    async def fake_run(envelope, *, on_chunk, **kwargs):
        on_chunk(ResponseChunk.thinking_event(envelope.request_id, "先核对方案完整性。"))
        on_chunk(ResponseChunk.tool_event(
            envelope.request_id,
            tool_call_id="tool_review",
            name="team_read_messages",
            phase="start",
        ))
        on_chunk(ResponseChunk.delta(envelope.request_id, "正在形成审阅结论"))
        return '{"action":"approve","message":"方案通过。","target_node_id":"","instructions":""}'

    monkeypatch.setattr(tm, "_run_leader_node", fake_run)

    streamed = [
        item async for item in tm._stream_leader_node(
            Envelope.of("审阅方案", session_id="leader_live_stream"),
            team=team,
            plan=plan,
            node=review,
            attempt=1,
        )
    ]

    chunks = [chunk for chunk, _ in streamed if chunk is not None]
    final_results = [result for _, result in streamed if result is not None]
    assert chunks[0].body["thinking"] == "先核对方案完整性。"
    assert chunks[1].body["tool_calls"][0]["status"] == "running"
    assert chunks[2].body["text"] == "正在形成审阅结论"
    assert final_results == ['{"action":"approve","message":"方案通过。","target_node_id":"","instructions":""}']


def test_leader_review_revise_reopens_member_then_approve_releases_review():
    tm, _ = _team()
    plan = TeamPlan(team_session_id="review_loop", goal="实现登录接口")
    build = TeamPlanNode(
        node_id="build_1",
        title="实现登录接口",
        assignee="coder",
        status="completed",
        result_summary="仅实现成功路径",
        artifact_refs=["/tmp/login.py"],
        attempt_count=1,
    )
    review = TeamPlanNode(node_id="leader_review", title="Leader 审阅", assignee="leader")
    summary = TeamPlanNode(node_id="leader_summary", title="Leader 总结", assignee="leader")
    plan.nodes = {node.node_id: node for node in (build, review, summary)}
    plan.edges = [
        TeamPlanEdge(parent_id=build.node_id, child_id=review.node_id),
        TeamPlanEdge(parent_id=review.node_id, child_id=summary.node_id),
    ]
    tm._plans[tm._key(plan.team_session_id, "owner")] = plan

    revised = tm._apply_leader_review_decision(
        plan,
        review,
        {
            "action": "revise",
            "target_node_id": build.node_id,
            "message": "审阅未通过",
            "instructions": "补齐失败路径并增加测试。",
        },
        owner_account_id="owner",
    )

    assert revised["action"] == "revise"
    assert build.status == "pending"
    assert build.attempt_count == 0
    assert build.artifact_refs == []
    assert build.metadata["revision_instructions"] == "补齐失败路径并增加测试。"
    assert review.status == "pending"
    assert review.metadata["revision_count"] == 1
    assert InProcessTeamManager._node_ready(plan, summary) is False

    build.update(status="completed", result_summary="失败路径和测试已补齐")
    approved = tm._apply_leader_review_decision(
        plan,
        review,
        {"action": "approve", "message": "修订符合要求，可以继续。"},
        owner_account_id="owner",
    )

    assert approved["action"] == "approve"
    assert review.status == "completed"
    assert InProcessTeamManager._node_ready(plan, summary) is True


def test_external_agent_profile_observation_uses_final_leader_review_attempt(tmp_path):
    store = ExternalAgentStore(str(tmp_path / "crew.db"))
    runtime = store.upsert_runtime({
        "id": "profile-runtime",
        "provider": "custom",
        "name": "Profile Runtime",
        "executable_path": "/bin/sh",
    })
    agent = store.create_agent(
        owner_account_id="owner-a",
        name="Worker A",
        runtime_id=runtime["id"],
    )
    external_team = store.create_team(
        owner_account_id="owner-a",
        name="Profile Team",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[
            {"agent_id": CREW_BUILTIN_AGENT_ID, "role": "Leader"},
            {"agent_id": agent["id"], "role": "负责实现"},
        ],
    )
    baseline = agent["profile"]["capabilities"]["backend"]["score"]
    tm, _ = _team(config=Config(max_iterations=3))
    tm.external_store = store
    team = tm._build_team(
        "profile-review",
        external_team_id=external_team["id"],
        owner_account_id="owner-a",
    )
    tm._teams[tm._key("profile-review", "owner-a")] = team
    member_id = next(iter(team.members))
    plan = TeamPlan(team_session_id="profile-review", goal="实现接口")
    builds = [
        TeamPlanNode(
            node_id=f"build-{index}",
            title=f"实现接口 {index}",
            assignee=member_id,
            status="completed",
            delegate_task_id=f"task-{index}-first",
            metadata={"required_capabilities": ["backend"]},
        )
        for index in range(1, 4)
    ]
    review = TeamPlanNode(node_id="leader-review", title="Leader 审阅", assignee="leader")
    plan.nodes = {node.node_id: node for node in [*builds, review]}
    plan.edges = [TeamPlanEdge(parent_id=node.node_id, child_id=review.node_id) for node in builds]
    tm._plans[tm._key(plan.team_session_id, "owner-a")] = plan

    tm._apply_leader_review_decision(
        plan,
        review,
        {"action": "revise", "target_node_id": builds[0].node_id, "message": "补齐错误处理"},
        owner_account_id="owner-a",
    )
    builds[0].update(status="completed", delegate_task_id="task-1-second", result_summary="错误处理已补齐")
    tm._apply_leader_review_decision(
        plan,
        review,
        {"action": "approve", "message": "通过"},
        owner_account_id="owner-a",
    )

    observations = store.list_agent_profile_observations(agent["id"], owner_account_id="owner-a")
    by_attempt = {item["source_attempt_id"]: item for item in observations}
    assert set(by_attempt) == {
        "task-1-first",
        "task-1-second",
        "task-2-first",
        "task-3-first",
    }
    assert by_attempt["task-1-first"]["outcome"] == "revise"
    assert by_attempt["task-1-first"]["quality_weight"] == 0.5
    assert all(by_attempt[attempt]["outcome"] == "success" for attempt in set(by_attempt) - {"task-1-first"})
    assert all(by_attempt[attempt]["quality_weight"] == 1.0 for attempt in set(by_attempt) - {"task-1-first"})
    profile = store.get_agent(agent["id"], owner_account_id="owner-a")["profile"]
    assert profile["capabilities"]["backend"]["score"] > baseline
    assert any(
        item["source"] == "execution_observation" and "samples=4" in item["value"]
        for item in profile["capabilities"]["backend"]["evidence"]
    )

    tm._apply_leader_review_decision(
        plan,
        review,
        {"action": "approve", "message": "重复消费"},
        owner_account_id="owner-a",
    )
    assert len(store.list_agent_profile_observations(agent["id"], owner_account_id="owner-a")) == 4


def test_agent_profile_execution_outcome_weights_material_evidence():
    material_pass = NodeExecutionAssessment(
        execution_status="completed",
        acceptance_status="pass",
        reason="有产物",
        changed_file_count=1,
    )
    text_only_pass = NodeExecutionAssessment(
        execution_status="completed",
        acceptance_status="pass",
        reason="文本结果",
    )
    acceptance_failure = NodeExecutionAssessment(
        execution_status="completed",
        acceptance_status="fail",
        reason="验收失败",
    )
    runtime_failure = NodeExecutionAssessment(
        execution_status="failed",
        acceptance_status="unknown",
        reason="运行时失败",
        failed_tools=("terminal",),
    )

    assert InProcessTeamManager._profile_outcome_from_execution(material_pass) == ("success", 0.8, "")
    assert InProcessTeamManager._profile_outcome_from_execution(text_only_pass) == ("success", 0.4, "")
    assert InProcessTeamManager._profile_outcome_from_execution(acceptance_failure) == (
        "failure",
        0.8,
        "acceptance",
    )
    assert InProcessTeamManager._profile_outcome_from_execution(runtime_failure) == ("neutral", 0.0, "tool")


def test_leader_review_revise_infers_target_from_multi_parent_feedback():
    tm, _ = _team()
    plan = TeamPlan(team_session_id="review_infer_target", goal="开发小游戏")
    design = TeamPlanNode(
        node_id="build_design_1",
        title="实现方案：2048小游戏",
        assignee="kk",
        status="completed",
        result_summary="初版实现方案",
        metadata={"workflow_lane": "design", "role_label": "技术负责人"},
    )
    test_plan = TeamPlanNode(
        node_id="qa_engineer_plan_1",
        title="测试方案：2048小游戏",
        assignee=CREW_BUILTIN_AGENT_ID,
        status="completed",
        result_summary="初版测试方案",
        metadata={"workflow_lane": "plan", "role_label": "测试工程师"},
    )
    review = TeamPlanNode(node_id="leader_review", title="Leader 审阅", assignee="leader")
    plan.nodes = {node.node_id: node for node in (design, test_plan, review)}
    plan.edges = [
        TeamPlanEdge(parent_id=design.node_id, child_id=review.node_id),
        TeamPlanEdge(parent_id=test_plan.node_id, child_id=review.node_id),
    ]
    tm._plans[tm._key(plan.team_session_id, "owner")] = plan

    decision = tm._apply_leader_review_decision(
        plan,
        review,
        {
            "action": "revise",
            "target_node_id": "",
            "message": "实现方案过于简略，缺少技术栈、文件结构、算法伪代码和动画方案。",
            "instructions": "补齐实现方案，并说明与测试方案的对应关系。",
        },
        owner_account_id="owner",
    )

    assert decision["action"] == "revise"
    assert decision["target_node_id"] == "build_design_1"
    assert design.status == "pending"
    assert design.metadata["revision_instructions"].startswith("补齐实现方案")
    assert test_plan.status == "completed"
    assert review.status == "pending"


def test_leader_review_stops_when_revision_budget_is_exhausted():
    tm, _ = _team()
    plan = TeamPlan(team_session_id="review_budget", goal="实现登录接口")
    build = TeamPlanNode(node_id="build_1", title="实现", assignee="coder", status="completed")
    review = TeamPlanNode(
        node_id="leader_review",
        title="Leader 审阅",
        assignee="leader",
        metadata={"revision_count": 2},
    )
    plan.nodes = {node.node_id: node for node in (build, review)}
    plan.edges = [TeamPlanEdge(parent_id=build.node_id, child_id=review.node_id)]
    tm._plans[tm._key(plan.team_session_id, "owner")] = plan

    decision = tm._apply_leader_review_decision(
        plan,
        review,
        {"action": "revise", "target_node_id": build.node_id, "message": "仍需修改"},
        owner_account_id="owner",
        max_revisions=2,
    )

    assert decision["action"] == "block"
    assert review.status == "blocked"
    assert build.status == "completed"
    assert "最多 2 次" in review.result_summary


def test_leader_review_exhaustion_opens_runtime_staffing_trigger_when_external_store_exists(tmp_path):
    tm, _ = _team()
    tm.external_store = ExternalAgentStore(str(tmp_path / "external.db"))
    plan = TeamPlan(team_session_id="review_staffing", goal="实现登录接口")
    build = TeamPlanNode(
        node_id="build_1",
        title="实现",
        assignee="coder",
        status="completed",
        metadata={"required_capabilities": ["backend"]},
    )
    review = TeamPlanNode(
        node_id="leader_review",
        title="Leader 审阅",
        assignee="leader",
        metadata={"revision_count": 2},
    )
    plan.nodes = {node.node_id: node for node in (build, review)}
    plan.edges = [TeamPlanEdge(parent_id=build.node_id, child_id=review.node_id)]
    tm._plans[tm._key(plan.team_session_id, "owner")] = plan

    decision = tm._apply_leader_review_decision(
        plan,
        review,
        {"action": "revise", "target_node_id": build.node_id, "message": "仍需修改"},
        owner_account_id="owner",
        max_revisions=2,
    )

    assert decision["action"] == "block"
    assert review.status == "blocked"
    assert build.status == "failed"
    assert build.metadata["runtime_staffing_trigger"] == "review_exhausted"


def test_team_delegate_output_contract_stops_empty_context_search():
    contract = InProcessTeamManager._delegate_output_contract("isolated_turn_workspace")

    assert "工作区范围：isolated_turn_workspace" in contract
    assert "搜索/枚举工具会被限制" in contract
    assert "如果确实缺少关键输入" in contract
    assert "缺失项和建议动作" in contract


def test_team_markdown_artifact_uses_business_filename(tmp_path):
    cases = [
        (
            TeamPlanNode(node_id="qa_engineer_plan_2", title="测试方案：测试一下团队协作吧"),
            "# 功能测试方案\n\n完整内容",
            "功能测试方案.md",
        ),
        (
            TeamPlanNode(node_id="legal_review_1", title="法务审查：检查广告合规"),
            "# 广告合规审查意见\n\n完整内容",
            "广告合规审查意见.md",
        ),
        (
            TeamPlanNode(node_id="video_script_1", title="视频脚本：生成宣传片脚本"),
            "# 宣传片视频脚本\n\n完整内容",
            "宣传片视频脚本.md",
        ),
        (
            TeamPlanNode(node_id="security_engineer_plan_1", title="安全方案：测试一下团队协作吧"),
            "没有 H1 时使用节点短标题",
            "安全方案.md",
        ),
        (
            TeamPlanNode(node_id="unknown_node_1", title="处理一下这个需求：用户原始 prompt 不应进文件名"),
            "没有 H1 且标题太泛时使用兜底",
            "团队产物.md",
        ),
    ]
    for node, content, expected in cases:
        assert InProcessTeamManager._artifact_filename(node, content) == expected

    existing = tmp_path / "功能测试方案.md"
    existing.write_text("old", encoding="utf-8")
    assert InProcessTeamManager._unique_artifact_path(tmp_path, "功能测试方案.md").name == "功能测试方案-2.md"


def test_team_node_owned_artifacts_filters_concurrent_member_artifacts():
    tm, _ = _team()
    node = TeamPlanNode(
        node_id="build_1",
        title="实现：小游戏",
        detail="实现小游戏",
        assignee="kk",
    )
    artifacts = [
        {
            "artifact_id": "a-crew",
            "owner_member_id": CREW_BUILTIN_AGENT_ID,
            "task_id": "task-qa",
            "path": "/tmp/test-plan.md",
        },
        {
            "artifact_id": "a-kk",
            "owner_member_id": "kk",
            "task_id": "task-build",
            "path": "/tmp/tetris.html",
        },
    ]

    owned = tm._node_owned_artifacts(artifacts, node=node, task_id="task-build")

    assert [item["artifact_id"] for item in owned] == ["a-kk"]


@pytest.fixture
def auto_artifact_ctx(tmp_path, monkeypatch):
    """auto-artifact 用例共享 setup：task_workspace_path 指向 tmp_path，附 node/envelope 构造器。"""
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / str(workspace_id or "default"),
    )
    tm, _ = _team()
    team = tm._build_team("auto_artifact_s1")

    def _node(title: str, detail: str) -> TeamPlanNode:
        return TeamPlanNode(node_id="build_1", title=title, detail=detail, assignee="kk")

    def _envelope(query: str, session_id: str) -> Envelope:
        return Envelope.of(query, session_id=session_id, user_id="owner", workspace_id="default")

    return SimpleNamespace(tm=tm, team=team, node=_node, envelope=_envelope)


def test_team_auto_file_artifacts_from_node_result(tmp_path):
    tm, _ = _team()
    team = tm._build_team("auto_artifact_s1")
    node = TeamPlanNode(
        node_id="build_1",
        title="实现：小游戏",
        detail="实现小游戏",
        assignee="kk",
    )
    html = tmp_path / "tetris.html"
    html.write_text("<html>ok</html>", encoding="utf-8")
    envelope = Envelope.of("做小游戏", session_id="auto_artifact_s1", user_id="owner")

    artifacts = tm._auto_file_artifacts_from_result(
        envelope,
        team=team,
        node=node,
        task_id="task-build",
        text=f"交付产物：`{html}`",
        existing_artifacts=[],
    )

    assert len(artifacts) == 1
    assert artifacts[0]["owner_member_id"] == "kk"
    assert artifacts[0]["task_id"] == "task-build"
    assert artifacts[0]["path"] == str(html)


def test_team_auto_file_artifacts_resolves_relative_turn_workspace_paths(auto_artifact_ctx):
    ctx = auto_artifact_ctx
    node = ctx.node("实现：2048", "实现 2048")
    envelope = ctx.envelope("做 2048", "web_a::turn::req_2048")
    workspace = Path(ctx.tm._team_delegate_cwd(envelope, "做一个2048小游戏"))
    (workspace / "index.html").write_text("<html>2048</html>", encoding="utf-8")
    (workspace / "README.md").write_text("# 2048", encoding="utf-8")

    artifacts = ctx.tm._auto_file_artifacts_from_result(
        envelope,
        team=ctx.team,
        node=node,
        task_id="task-build",
        text="交付物：`index.html` 和 `README.md`；Wrote 10552 bytes to index.html",
        existing_artifacts=[],
    )

    assert {Path(item["path"]).name for item in artifacts} == {"index.html", "README.md"}
    assert {item["owner_member_id"] for item in artifacts} == {"kk"}


def test_team_auto_file_artifacts_resolve_relative_node_workspace_paths(auto_artifact_ctx):
    ctx = auto_artifact_ctx
    node = ctx.node("实现：节点隔离产物", "实现节点隔离产物")
    envelope = ctx.envelope("测试团队协作", "web_a::turn::req_node")
    workspace = Path(ctx.tm._team_delegate_cwd(
        envelope,
        envelope.query,
        node_id=node.node_id,
        agent_id=node.assignee,
    ))
    output = workspace / "result.md"
    output.write_text("# 节点产物", encoding="utf-8")

    artifacts = ctx.tm._auto_file_artifacts_from_result(
        envelope,
        team=ctx.team,
        node=node,
        task_id="task-build",
        text="交付物：`result.md`",
        existing_artifacts=[],
        changed_paths={str(output.resolve())},
        workspace_root=str(workspace),
    )

    assert [item["path"] for item in artifacts] == [str(output.resolve())]


def test_team_auto_artifacts_register_changed_output_directories(auto_artifact_ctx):
    ctx = auto_artifact_ctx
    node = ctx.node("实现：体检报告平台", "实现前后端")
    envelope = ctx.envelope("实现体检报告平台", "web_f9x8m9::turn::req_f4834b83cea8")
    workspace = Path(ctx.tm._team_delegate_cwd(envelope, envelope.query))
    api_file = workspace / "apps" / "api" / "src" / "main.ts"
    web_file = workspace / "apps" / "web" / "src" / "App.tsx"
    api_file.parent.mkdir(parents=True)
    web_file.parent.mkdir(parents=True)
    api_file.write_text("export {};", encoding="utf-8")
    web_file.write_text("export default function App() {}", encoding="utf-8")

    artifacts = ctx.tm._auto_file_artifacts_from_result(
        envelope,
        team=ctx.team,
        node=node,
        task_id="task-build",
        text="产物位于当前工作区 `apps/api` 与 `apps/web`。",
        existing_artifacts=[],
        changed_paths={str(api_file.resolve()), str(web_file.resolve())},
    )

    assert {item["path"] for item in artifacts} == {
        str((workspace / "apps" / "api").resolve()),
        str((workspace / "apps" / "web").resolve()),
    }
    assert {item["content_type"] for item in artifacts} == {"inode/directory"}


def test_team_thinking_preserves_raw_delta_boundaries_and_repairs_legacy_history():
    node = TeamPlanNode(node_id="build_1", title="实现", assignee="kk")
    event = InProcessTeamManager._child_chunk_execution_event(
        node,
        "kk",
        ResponseChunk.thinking_event("req", " plan"),
    )

    assert event is not None
    assert event["event_text"] == " plan"
    assert _join_stream_fragments(["The", " user", " has", " given", " me", " a", " task."]) == "The user has given me a task."
    assert _normalize_legacy_chunked_thinking("The\n\nplan\n\nfile\n\nhas\n\nbeen\n\nwritten\n\n.") == "The plan file has been written."


def test_team_auto_file_artifacts_ignore_relative_paths_outside_turn_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / "workspaces" / str(workspace_id or "default"),
    )
    repo_web = tmp_path / "repo" / "web"
    repo_web.mkdir(parents=True)
    (repo_web / "index.html").write_text("<html>wrong file</html>", encoding="utf-8")
    monkeypatch.chdir(repo_web)
    tm, _ = _team()
    team = tm._build_team("auto_artifact_ignore_cwd_s1")
    node = TeamPlanNode(
        node_id="build_1",
        title="实现：2048",
        detail="实现 2048",
        assignee="kk",
    )
    envelope = Envelope.of(
        "做 2048",
        session_id="web_a::turn::req_2048",
        user_id="owner",
        workspace_id="default",
    )

    artifacts = tm._auto_file_artifacts_from_result(
        envelope,
        team=team,
        node=node,
        task_id="task-build",
        text="交付物：`index.html`",
        existing_artifacts=[],
    )

    assert artifacts == []


def test_team_owned_artifacts_normalize_relative_paths_to_turn_workspace(tmp_path):
    tm, _ = _team()
    node = TeamPlanNode(
        node_id="qa_engineer_plan_1",
        title="测试方案",
        detail="输出测试方案",
        assignee=CREW_BUILTIN_AGENT_ID,
    )
    workspace = tmp_path / "team_turn"
    workspace.mkdir()
    report = workspace / "2048_测试方案.md"
    report.write_text("# 测试方案", encoding="utf-8")

    artifacts = tm._node_owned_artifacts(
        [{
            "artifact_id": "artifact-relative",
            "owner_member_id": CREW_BUILTIN_AGENT_ID,
            "task_id": "task-plan",
            "path": "2048_测试方案.md",
        }],
        node=node,
        task_id="task-plan",
        workspace_root=workspace,
    )

    assert artifacts[0]["path"] == str(report.resolve())


def test_team_owned_artifacts_reject_relative_paths_outside_turn_workspace(tmp_path):
    tm, _ = _team()
    node = TeamPlanNode(
        node_id="build_1",
        title="实现",
        detail="实现",
        assignee="kk",
    )
    workspace = tmp_path / "team_turn"
    workspace.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")

    artifacts = tm._node_owned_artifacts(
        [{
            "artifact_id": "artifact-outside",
            "owner_member_id": "kk",
            "task_id": "task-build",
            "path": "../outside.md",
        }],
        node=node,
        task_id="task-build",
        workspace_root=workspace,
    )

    assert artifacts == []


def test_team_auto_file_artifacts_do_not_reown_upstream_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / str(workspace_id or "default"),
    )
    tm, _ = _team()
    team = tm._build_team("auto_artifact_upstream_s1")
    build_node = TeamPlanNode(
        node_id="build_1",
        title="实现：2048",
        detail="实现 2048",
        assignee="kk",
    )
    verify_node = TeamPlanNode(
        node_id="qa_engineer_verify_1",
        title="测试验证：2048",
        detail="验证 2048",
        assignee=CREW_BUILTIN_AGENT_ID,
    )
    envelope = Envelope.of(
        "做 2048",
        session_id="web_a::turn::req_2048",
        user_id="owner",
        workspace_id="default",
    )
    workspace = Path(tm._team_delegate_cwd(envelope, "做一个2048小游戏"))
    html = workspace / "index.html"
    html.write_text("<html>2048</html>", encoding="utf-8")
    team.bus.add_artifact(
        team_session_id=envelope.session_id,
        owner_member_id=build_node.assignee,
        task_id="task-build",
        summary="index.html",
        scope="node-output",
        content_type="text/html",
        path=str(html),
    )

    artifacts = tm._auto_file_artifacts_from_result(
        envelope,
        team=team,
        node=verify_node,
        task_id="task-verify",
        text=f"验证对象：`{html}`，测试报告另行生成。",
        existing_artifacts=[],
    )

    assert artifacts == []


def test_team_auto_file_artifacts_require_current_node_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / str(workspace_id or "default"),
    )
    tm, _ = _team()
    team = tm._build_team("auto_artifact_changed_s1")
    node = TeamPlanNode(
        node_id="build_1",
        title="实现：2048",
        detail="实现 2048",
        assignee="kk",
    )
    envelope = Envelope.of(
        "做 2048",
        session_id="web_a::turn::req_2048",
        user_id="owner",
        workspace_id="default",
    )
    workspace = Path(tm._team_delegate_cwd(envelope, "做一个2048小游戏"))
    html = workspace / "index.html"
    html.write_text("<html>old</html>", encoding="utf-8")
    before = tm._workspace_file_snapshot(workspace)

    unchanged = tm._auto_file_artifacts_from_result(
        envelope,
        team=team,
        node=node,
        task_id="task-build",
        text="交付物：`index.html`",
        existing_artifacts=[],
        changed_paths=tm._changed_workspace_files(workspace, before),
    )

    html.write_text("<html>new version</html>", encoding="utf-8")
    changed = tm._auto_file_artifacts_from_result(
        envelope,
        team=team,
        node=node,
        task_id="task-build",
        text="交付物：`index.html`",
        existing_artifacts=[],
        changed_paths=tm._changed_workspace_files(workspace, before),
    )

    assert unchanged == []
    assert len(changed) == 1
    assert changed[0]["owner_member_id"] == "kk"
    assert changed[0]["task_id"] == "task-build"
    assert changed[0]["path"] == str(html)


@pytest.mark.asyncio
async def test_team_leader_fallback_file_uses_turn_workspace_and_registers_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / str(workspace_id or "default"),
    )
    tm, _ = _team()
    envelope = Envelope.of(
        "实现一个贪吃蛇小游戏",
        session_id="leader_fallback_snake",
        user_id="owner",
        workspace_id="default",
    )
    team = tm._build_team(envelope.session_id)
    node = TeamPlanNode(
        node_id="leader_summary",
        title="Leader 汇总并兜底交付",
        detail="确保小游戏可以直接运行",
        assignee="leader",
    )
    plan = TeamPlan(
        plan_id="plan_leader_fallback",
        team_session_id=envelope.session_id,
        goal=envelope.query,
        nodes={node.node_id: node},
    )
    tm._plans[tm._key(envelope.session_id, envelope.user_id)] = plan
    observed_cwd = ""

    async def fake_leader_run(leader_envelope):
        nonlocal observed_cwd
        observed_cwd = str(leader_envelope.params.get("cwd") or "")
        output = Path(observed_cwd) / "snake_game.html"
        output.write_text("<html><title>Snake</title></html>", encoding="utf-8")
        yield ResponseChunk.final(leader_envelope.request_id, "已生成 `snake_game.html`，可以直接运行。")

    monkeypatch.setattr(team.leader, "run", fake_leader_run)

    result = await tm._run_leader_node(
        envelope,
        team=team,
        plan=plan,
        node=node,
        attempt=1,
    )

    expected = tmp_path / "default" / "team_turns" / "leader_fallback_snake" / "snake_game.html"
    assert result == "已生成 `snake_game.html`，可以直接运行。"
    assert Path(observed_cwd) == expected.parent
    assert expected.is_file()
    assert node.artifact_refs == [str(expected)]
    artifacts = team.bus.list_artifacts(envelope.session_id)
    assert len(artifacts) == 1
    assert artifacts[0]["owner_member_id"] == "leader"
    assert artifacts[0]["path"] == str(expected)


def test_team_full_result_is_stored_as_node_owned_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / str(workspace_id or "default"),
    )
    tm, _ = _team()
    envelope = Envelope.of(
        "实现登录接口",
        session_id="full_result_ref",
        user_id="owner",
        workspace_id="workspace",
    )
    node = TeamPlanNode(
        node_id="build_login",
        title="实现登录接口",
        assignee="coder",
        metadata={"workflow_lane": "build"},
    )
    content = "完整实现过程\n包含详细日志，但 Leader 只消费结构化摘要。"

    result_ref, result_bytes = tm._persist_node_full_result(envelope, node, content)

    result_path = Path(result_ref)
    assert result_path.is_file()
    assert result_path.read_text(encoding="utf-8") == content
    assert result_bytes == len(content.encode("utf-8"))
    assert ".crew/node-results" in result_ref


def test_team_child_tool_chunk_becomes_node_execution_event():
    node = TeamPlanNode(node_id="build_1", title="实现：小游戏", assignee="kk")
    chunk = ResponseChunk.tool_event(
        "req_1",
        "file_write",
        "result",
        "Wrote 529 bytes to README.md",
        tool_call_id="tool_1",
        args='{"path":"README.md"}',
    )

    event = InProcessTeamManager._child_chunk_execution_event(node, "kk", chunk)

    assert event is not None
    assert event["event_type"] == "tool"
    assert event["event_icon"] == "tool"
    assert "README.md" in event["event_text"]


def test_team_verify_node_receives_upstream_artifact_refs():
    plan = TeamPlan(team_session_id="team_1", goal="开发小游戏")
    build = TeamPlanNode(
        node_id="build_1",
        title="实现：小游戏",
        assignee="kk",
        status="completed",
        artifact_refs=["/tmp/game/index.html", "/tmp/game/README.md"],
    )
    review = TeamPlanNode(
        node_id="leader_review",
        title="Leader 审阅方案：小游戏",
        assignee="leader",
        status="completed",
    )
    verify = TeamPlanNode(
        node_id="qa_engineer_verify_1",
        title="测试验证：小游戏",
        assignee=CREW_BUILTIN_AGENT_ID,
    )
    plan.nodes = {item.node_id: item for item in [build, review, verify]}
    plan.edges = [
        TeamPlanEdge(parent_id="build_1", child_id="qa_engineer_verify_1"),
        TeamPlanEdge(parent_id="leader_review", child_id="qa_engineer_verify_1"),
    ]

    refs = InProcessTeamManager._node_upstream_artifact_refs(plan, verify)
    formatted = InProcessTeamManager._format_upstream_artifacts(refs)
    contract = InProcessTeamManager._delegate_output_contract("isolated_turn_workspace")

    assert refs == ["/tmp/game/index.html", "/tmp/game/README.md"]
    assert "/tmp/game/index.html" in formatted
    assert "验证节点需要先基于实际产物复核" in contract
    assert "搜索/枚举工具会被限制" in contract


def test_team_workspace_guard_blocks_parent_task_workspace_search(tmp_path):
    root = tmp_path / "task_workspaces" / "default" / "team_turns" / "turn_1"
    root.mkdir(parents=True)
    guard = {"enabled": True, "root": str(root), "allowed_roots": [str(root)]}
    parent = tmp_path / "task_workspaces"

    decision = check_workspace_guard(
        "terminal",
        {"command": f"find {parent} -maxdepth 5 -type f"},
        guard,
        cwd=str(root),
    )

    assert decision.allowed is False
    assert "当前 Session 授权范围外" in decision.reason

    raw_decision = check_workspace_guard(
        "terminal",
        {"raw": f"find {parent} -maxdepth 5 -type f"},
        guard,
        cwd=str(root),
    )
    assert raw_decision.allowed is False


def test_workspace_guard_does_not_treat_shell_launcher_as_business_file(tmp_path):
    root = tmp_path / "session"
    skill_root = tmp_path / "skills" / "callassistant"
    root.mkdir()
    script = skill_root / "scripts" / "call.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root), str(skill_root)],
        "writable_roots": [str(root)],
    }
    command = f"/bin/zsh -lc 'sh {script}'"

    checked = check_workspace_guard(
        "terminal",
        {"command": command},
        guard,
        cwd=str(root),
    )
    classified = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {"command": command},
        },
    }, guard, cwd=str(root))

    assert checked.allowed is True
    assert classified.action == "allow"
    assert classified.operation == "execute"


def test_workspace_guard_still_classifies_dangerous_shell_payload(tmp_path):
    root = tmp_path / "session"
    root.mkdir()
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
    }

    delete = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {"command": "/bin/zsh -lc 'rm -rf .'"},
        },
    }, guard, cwd=str(root))
    network = classify_external_permission({
        "kind": "execute",
        "rawInput": {
            "name": "shell",
            "arguments": {"command": "/bin/zsh -lc 'curl https://example.com'"},
        },
    }, guard, cwd=str(root))

    assert delete.action == "deny"
    assert network.action == "ask"
    assert network.operation == "network"


def test_team_workspace_guard_allows_current_workspace_and_upstream_artifact(tmp_path):
    root = tmp_path / "task_workspaces" / "default" / "team_turns" / "turn_1"
    artifact_dir = tmp_path / "task_workspaces" / "default" / "team_turns" / "turn_0"
    root.mkdir(parents=True)
    artifact_dir.mkdir(parents=True)
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root), str(artifact_dir)],
        "writable_roots": [str(root)],
        "allowed_roots": [str(root), str(artifact_dir)],
    }

    assert check_workspace_guard("terminal", {"command": "find . -maxdepth 2 -type f"}, guard, cwd=str(root)).allowed
    assert check_workspace_guard(
        "terminal",
        {"command": f"rg Tetris {artifact_dir}"},
        guard,
        cwd=str(root),
    ).allowed
    assert check_workspace_guard(
        "file_read",
        {"path": str(artifact_dir / "plan.md")},
        guard,
        cwd=str(root),
    ).allowed
    assert not check_workspace_guard(
        "file_write",
        {"path": str(artifact_dir / "plan.md"), "content": "overwrite"},
        guard,
        cwd=str(root),
    ).allowed
    assert not check_workspace_guard(
        "terminal",
        {"command": f"echo overwrite > {artifact_dir / 'plan.md'}"},
        guard,
        cwd=str(root),
    ).allowed
    assert check_workspace_guard(
        "file_write",
        {"path": "result.md", "content": "ok"},
        guard,
        cwd=str(root),
    ).allowed
    assert check_workspace_guard("search_files", {"query": "Tetris", "path": "."}, guard, cwd=str(root)).allowed


def test_team_workspace_guard_allows_exact_attachment_read_only(tmp_path):
    root = tmp_path / "team_turn"
    uploads = tmp_path / "uploads"
    root.mkdir()
    uploads.mkdir()
    attachment = uploads / "template.png"
    sibling = uploads / "private.png"
    attachment.write_bytes(b"image")
    sibling.write_bytes(b"private")
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "readable_files": [str(attachment)],
        "writable_roots": [str(root)],
    }

    assert check_workspace_guard(
        "file_read",
        {"path": str(attachment)},
        guard,
        cwd=str(root),
    ).allowed
    assert not check_workspace_guard(
        "file_read",
        {"path": str(sibling)},
        guard,
        cwd=str(root),
    ).allowed
    assert not check_workspace_guard(
        "file_write",
        {"path": str(attachment), "content": "overwrite"},
        guard,
        cwd=str(root),
    ).allowed

    read_permission = classify_external_permission({
        "rawInput": {
            "tool": "read_file",
            "arguments": {"path": str(attachment)},
        },
    }, guard, cwd=str(root))
    write_permission = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "tool": "write_file",
            "arguments": {"path": str(attachment), "content": "overwrite"},
        },
    }, guard, cwd=str(root))

    assert read_permission.action == "allow"
    assert write_permission.action == "ask"


def test_external_permission_allows_local_edit_and_single_file_delete_but_asks_for_directory(tmp_path):
    root = tmp_path / "team_turn"
    root.mkdir()
    directory = root / "build-cache"
    directory.mkdir()
    obsolete = root / "obsolete.txt"
    obsolete.write_text("old", encoding="utf-8")
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
    }

    edit = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "tool": "write_file",
            "arguments": {"path": "index.html", "content": "<html></html>"},
        },
    }, guard, cwd=str(root))
    delete = classify_external_permission({
        "kind": "execute",
        "rawInput": {"command": "rm -rf build-cache"},
    }, guard, cwd=str(root))
    delete_tool = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "name": "delete_file",
            "arguments": {"path": "obsolete.txt"},
        },
    }, guard, cwd=str(root))

    assert edit.action == "allow"
    assert edit.target == str((root / "index.html").resolve())
    assert delete.action == "ask"
    assert delete.operation == "write"
    assert delete_tool.action == "allow"
    assert delete_tool.target == str((root / "obsolete.txt").resolve())


def test_external_permission_asks_before_deleting_user_bound_project_file(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    source = root / "main.py"
    source.write_text("print('ok')", encoding="utf-8")
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
        "confirm_delete_roots": [str(root)],
    }

    decision = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "name": "delete_file",
            "arguments": {"path": "main.py"},
        },
    }, guard, cwd=str(root))

    assert decision.action == "ask"
    assert decision.operation == "write"
    assert decision.target == str(source.resolve())


def test_external_permission_normalizes_runtime_write_and_requires_a_real_path(tmp_path):
    root = tmp_path / "team_turn"
    root.mkdir()
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
    }

    local_write = classify_external_permission({
        "title": "Write",
        "rawInput": {
            "tool": "Write",
            "arguments": {"path": "game.js"},
        },
    }, guard, cwd=str(root))
    delete = classify_external_permission({
        "kind": "execute",
        "rawInput": {"command": "rm -rf build-cache"},
    }, guard, cwd=str(root))
    missing_path = classify_external_permission({
        "title": "Write",
        "rawInput": {"tool": "Write"},
    }, guard, cwd=str(root))
    delete_tool = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "name": "delete_file",
            "arguments": {"path": "obsolete.txt"},
        },
    }, guard, cwd=str(root))

    assert local_write.action == "allow"
    assert local_write.tool_name == "file_write"
    assert local_write.target == str((root / "game.js").resolve())
    assert missing_path.action == "ask"
    assert missing_path.target == ""
    assert delete.action == "ask"
    assert delete_tool.action == "allow"
    assert delete_tool.target == str((root / "obsolete.txt").resolve())


def test_external_permission_auto_allows_governed_interaction_mcp_tools(tmp_path):
    root = tmp_path / "team_turn"
    root.mkdir()
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
    }

    for tool_name in (
        "mcp_crew_interaction_ask_followup_question",
        "mcp__crew-interaction__ask_followup_question",
    ):
        decision = classify_external_permission({
            "kind": "other",
            "rawInput": {"tool": tool_name, "arguments": {"questions": []}},
        }, guard, cwd=str(root))
        assert decision.action == "allow"
        assert decision.tool_name == tool_name

    governed_team_tool = classify_external_permission({
        "kind": "other",
        "rawInput": {
            "tool": "mcp_crew_interaction_team_plan_update",
            "arguments": {"node_id": "build"},
        },
    }, guard, cwd=str(root))
    assert governed_team_tool.action == "allow"


def test_external_permission_asks_outside_workspace_and_for_network_side_effect(tmp_path):
    root = tmp_path / "team_turn"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
    }

    outside_edit = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "tool": "write_file",
            "arguments": {"path": str(outside), "content": "x"},
        },
    }, guard, cwd=str(root))
    network = classify_external_permission({
        "kind": "execute",
        "rawInput": {"command": "npm install"},
    }, guard, cwd=str(root))

    assert outside_edit.action == "ask"
    assert outside_edit.target == str(outside.resolve())
    assert network.action == "ask"


def test_external_permission_denies_workspace_control_data_and_root_delete(tmp_path):
    root = tmp_path / "team_turn"
    root.mkdir()
    guard = {
        "enabled": True,
        "root": str(root),
        "readable_roots": [str(root)],
        "writable_roots": [str(root)],
    }

    crew_state = classify_external_permission({
        "kind": "edit",
        "rawInput": {
            "tool": "write_file",
            "arguments": {"path": ".crew/state.json", "content": "{}"},
        },
    }, guard, cwd=str(root))
    root_delete = classify_external_permission({
        "kind": "execute",
        "rawInput": {"command": "rm -rf ."},
    }, guard, cwd=str(root))

    assert crew_state.action == "deny"
    assert root_delete.action == "deny"


def test_team_workspace_guard_config_only_for_isolated_workspace(tmp_path):
    root = tmp_path / "turn"
    artifact = tmp_path / "previous" / "game.html"
    root.mkdir()
    artifact.parent.mkdir()
    artifact.write_text("<html></html>", encoding="utf-8")

    guard = InProcessTeamManager._workspace_guard_config(
        "isolated_turn_workspace",
        str(root),
        [str(artifact)],
    )

    assert guard is not None
    assert guard["enabled"] is True
    assert str(root.resolve()) in guard["allowed_roots"]
    assert str(root.resolve()) in guard["writable_roots"]
    assert str(artifact.resolve()) in guard["readable_files"]
    assert str(artifact.parent.resolve()) not in guard["allowed_roots"]
    assert str(artifact.parent.resolve()) not in guard["readable_roots"]
    assert str(artifact.parent.resolve()) not in guard["writable_roots"]
    assert InProcessTeamManager._workspace_guard_config("shared_workspace", str(root), [str(artifact)]) is None


async def test_team_leader_delegates_to_teammate():
    tm, tasks = _team()
    final = None
    team_internal = []
    async for ch in tm.interact(Envelope.of(
        "组队算1+1",
        session_id="t1",
        mode="team",
        params={"team_spec": _structured_team_spec("组队算1+1", capabilities=["planning", "implementation"], workflow_lanes=("build",))},
    )):
        if ch.kind == "team_internal":
            team_internal.append(ch)
        if ch.kind == "final":
            final = ch.body["text"]
    assert final is not None
    assert "团队最终答案：2" in final
    assert any(ch.body.get("event_type") == "team_assign" and "@coder" in str(ch.body.get("text") or "") for ch in team_internal)
    assert any("coder算出" in str(ch.body.get("text") or "") for ch in team_internal)
    assert all("正在使用工具" not in str(ch.body.get("text") or "") for ch in team_internal)
    assert all("调用工具" not in str(ch.body.get("text") or "") for ch in team_internal)
    assert any(ch.body.get("event_type") == "team_submit" and ch.body.get("mention_intent") == "handoff" for ch in team_internal)
    result_chunks = [
        ch for ch in team_internal
        if ch.body.get("event_type") == "team_submit" and ch.body.get("mention_intent") == "handoff"
    ]
    assert result_chunks
    assert all(ch.body.get("display_mode") != "collapsible" for ch in result_chunks)
    assert all(ch.body.get("node_id") for ch in result_chunks)
    board = tasks.list("t1")
    team_node_tasks = [
        item for item in board
        if str(item.get("title") or "").startswith(("规划：", "实现："))
    ]
    assert len(team_node_tasks) == 2
    assert {item["assignee"] for item in team_node_tasks} == {"researcher", "coder"}
    assert {item["status"] for item in team_node_tasks} == {"done"}
    assert all("coder算出" in item["result"] for item in team_node_tasks)
    team = tm._teams[("local", "t1")]
    assert team.session.member_sessions["coder"].member_session_id == "t1::coder"
    messages = team.bus.list_messages("t1")
    assert [m["message_type"] for m in messages[:4]] == [
        "assign",
        "task_notification",
        "assign",
        "task_notification",
    ]
    assert messages[0]["recipient_member_ids"] == ["researcher"]
    assert messages[1]["recipient_member_ids"] == ["leader"]
    assert messages[2]["recipient_member_ids"] == ["coder"]
    assert messages[3]["recipient_member_ids"] == ["leader"]
    plan = tm.read_plan("t1")["plan"]
    assert plan["status"] == "completed"
    assert [node["node_id"] for node in plan["nodes"]] == [
        "leader_plan",
        "plan_1",
        "build_design_1",
        "leader_review",
        "build_1",
        "leader_summary",
    ]
    assert [node["status"] for node in plan["nodes"]] == ["completed"] * 6
    build_design = next(node for node in plan["nodes"] if node["node_id"] == "build_design_1")
    assert build_design["assignee"] == "coder"
    assert build_design["metadata"]["workflow_lane"] == "design"
    review = next(node for node in plan["nodes"] if node["node_id"] == "leader_review")
    assert "Leader验收通过" in review["result_summary"]
    assert "结论可交付" in review["result_summary"]


async def test_team_plan_persists_to_kanban_store_and_history_events(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "team-kanban.db")
    owner_store = store.for_owner("local")
    tm, _ = _team(kanban_store=store)
    chunks = [ch async for ch in tm.interact(Envelope.of(
        "组队算1+1",
        session_id="persist-team",
        mode="team",
        params={"team_spec": _structured_team_spec("组队算1+1", capabilities=["planning", "implementation"], workflow_lanes=("build",))},
    ))]
    assert any(ch.kind == "team_internal" for ch in chunks)

    plan = tm.read_plan("persist-team", owner_account_id="local")["plan"]
    workflow = owner_store.get_latest_workflow_by_session("persist-team")
    assert workflow is not None
    assert workflow.context["source"] == "team"
    assert workflow.context["team_plan_id"] == plan["plan_id"]
    workflow_plan = workflow.context["workflow_plan"]
    assert workflow_plan["version"] == 1
    assert workflow_plan["revision"] == 1
    assert workflow_plan["task"]["turn_id"] == "persist-team"
    assert workflow.context["current_revision"] == 1

    board = owner_store.get_board_state(workflow.id)
    assert board["workflow_plan"] == workflow_plan
    assert len(board["tasks"]) == len(plan["nodes"])
    assert board["dependencies"]
    assert any(task["status"] == "done" for task in board["tasks"])
    events = board["events"]
    assert any(event["event_type"] == "team_plan_created" for event in events)
    assert any(event["event_type"] == "team_assign" for event in events)
    assert any(event["event_type"] == "team_submit" for event in events)
    assert any((event.get("payload") or {}).get("mention_intent") == "handoff" for event in events)

    history = tm.event_history_for_session("persist-team", owner_account_id="local")
    assert any(item["role"] == "team_internal" and "@coder" in item["content"] for item in history)
    assert any(item["role"] == "team_internal" and ("团队最终答案" in item["content"] or "coder算出" in item["content"]) for item in history)
    assert any("coder算出" in str(node.get("result_summary") or "") for node in plan["nodes"])
    assert all("正在使用工具" not in item["content"] for item in history)
    assert all("调用工具" not in item["content"] for item in history)

    tm._plan_workflows.clear()
    restored_history = tm.event_history_for_session("persist-team", owner_account_id="local")
    assert any(item["role"] == "team_internal" and "@coder" in item["content"] for item in restored_history)
    assert any(item["role"] == "team_internal" and ("团队最终答案" in item["content"] or "coder算出" in item["content"]) for item in restored_history)

    projected = tm.task_projection_for_session("persist-team", owner_account_id="local")
    assert len(projected) == len(plan["nodes"])
    assert {item["progress"]["source"] for item in projected} == {"team_kanban"}
    assert any(item["progress"]["plan_node_id"] == "build_1" for item in projected)
    projected_by_node = {item["progress"]["plan_node_id"]: item for item in projected}
    assert projected_by_node["build_1"]["progress"]["workflow_lane"] == "build"
    assert projected_by_node["build_1"]["progress"]["display_order"] == 40
    assert projected_by_node["build_1"]["progress"]["role_label"] == "负责编写代码、执行命令、文件读写等工程操作"
    assert projected_by_node["build_1"]["progress"]["result_contract"]["answer"]
    assert projected_by_node["build_1"]["progress"]["summary_items"]
    created_event = next(event for event in events if event["event_type"] == "team_plan_created")
    created_nodes = {
        node["node_id"]: node
        for node in (created_event.get("payload") or {}).get("nodes") or []
    }
    assert created_nodes["build_1"]["metadata"]["workflow_lane"] == "build"


def test_workflow_plan_revision_updates_snapshot_and_event_atomically(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "workflow-revision.db").for_owner("local")
    workflow = store.create_workflow(
        "session-revision",
        "任务",
        context={"source": "team", "workflow_plan": {"version": 1, "revision": 1}},
    )

    updated = store.save_workflow_plan_revision(
        workflow.id,
        {"version": 1, "revision": 2, "task": {"goal": "任务"}, "nodes": [], "edges": []},
        reason="新增独立核验节点",
        delta={"added_nodes": ["independent_review"]},
    )

    assert updated.context["current_revision"] == 2
    assert updated.context["workflow_plan"]["revision"] == 2
    event = next(item for item in store.list_events(workflow.id) if item.event_type == "workflow_plan_revised")
    assert event.payload["revision"] == 2
    assert event.payload["delta"]["added_nodes"] == ["independent_review"]


async def test_final_summary_refreshes_display_metadata_revision_once(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "display-mapping.db")
    owner_store = store.for_owner("local")
    tm, _ = _team(provider=DisplayMappingProvider(), kanban_store=store)
    plan = TeamPlan(team_session_id="display-mapping", goal="找不同城市的隐藏小吃")
    plan.nodes = {
        "leader_plan": TeamPlanNode(
            node_id="leader_plan",
            title="Leader 拆分任务",
            assignee="leader",
            status="completed",
        ),
        "research_city1": TeamPlanNode(
            node_id="research_city1",
            title="搜索中国 1 个不同城市的隐藏小吃",
            assignee="kk",
            status="completed",
            result_summary="结论：四川成都的洞子口张老二凉粉值得推荐。",
            metadata={
                "workflow_lane": "research",
                "display_title": "调研城市 1",
                "full_title": "搜索中国 1 个不同城市的隐藏小吃",
            },
        ),
        "leader_summary": TeamPlanNode(
            node_id="leader_summary",
            title="Leader 汇总",
            assignee="leader",
            status="completed",
        ),
    }
    plan.edges = [
        TeamPlanEdge(parent_id="leader_plan", child_id="research_city1"),
        TeamPlanEdge(parent_id="research_city1", child_id="leader_summary"),
    ]
    tm._persist_team_plan(
        plan,
        owner_account_id="local",
        workflow_plan={
            "version": 1,
            "revision": 1,
            "task": {"goal": plan.goal},
            "nodes": [
                {"id": "leader_plan", "title": "Leader 拆分任务", "display_title": "Leader 拆分"},
                {"id": "research_city1", "title": "搜索中国 1 个不同城市的隐藏小吃", "display_title": "调研城市 1"},
                {"id": "leader_summary", "title": "Leader 汇总", "display_title": "汇总"},
            ],
            "edges": [{"from": "leader_plan", "to": "research_city1"}, {"from": "research_city1", "to": "leader_summary"}],
        },
    )

    await tm._refresh_final_display_metadata(
        plan,
        owner_account_id="local",
        final_summary="最终包含四川成都的洞子口张老二凉粉。",
    )

    assert plan.nodes["research_city1"].metadata["display_subject"] == "四川"
    assert plan.nodes["research_city1"].metadata["display_title"] == "调研城市 1 - 四川"
    workflow = owner_store.get_latest_workflow_by_session("display-mapping")
    assert workflow is not None
    assert workflow.context["workflow_plan"]["revision"] == 2
    event = next(item for item in owner_store.list_events(workflow.id) if item.event_type == "workflow_plan_revised")
    assert event.payload["delta"]["updated_node_metadata"]["research_city1"]["display_subject"] == "四川"

    projected = tm.task_projection_for_session("display-mapping", owner_account_id="local")
    projected_by_node = {item["progress"]["plan_node_id"]: item for item in projected}
    assert projected_by_node["research_city1"]["progress"]["display_subject"] == "四川"
    assert projected_by_node["research_city1"]["progress"]["display_title"] == "调研城市 1 - 四川"


async def test_team_history_does_not_synthesize_missing_assignment_events(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "team-kanban.db")
    owner_store = store.for_owner("local")
    tm, _ = _team(kanban_store=store)
    chunks = [ch async for ch in tm.interact(Envelope.of("组队算1+1", session_id="restore-assign-team", mode="team"))]
    assert any(
        ch.kind == "team_internal"
        and ch.body.get("event_type") == "team_assign"
        and ch.body.get("mention_intent") == "assign"
        for ch in chunks
    )

    workflow = owner_store.get_latest_workflow_by_session("restore-assign-team")
    assert workflow is not None
    assign_event_ids = [
        event.id for event in owner_store.list_events(workflow.id, limit=500)
        if event.event_type == "team_assign"
        and (event.payload or {}).get("mention_intent") == "assign"
    ]
    assert assign_event_ids
    for event_id in assign_event_ids:
        store._conn.execute("DELETE FROM kanban_events WHERE id = ?", (event_id,))
    store._conn.commit()

    tm._plan_workflows.clear()
    tm._plan_node_tasks.clear()
    restored_history = tm.event_history_for_session("restore-assign-team", owner_account_id="local")
    restored_assigns = [
        item for item in restored_history
        if item.get("event_type") == "team_assign"
        and item.get("mention_intent") == "assign"
    ]
    assert restored_assigns == []


async def test_team_bus_tool_results_are_not_rendered_as_chat_noise(tmp_path):
    class BusReadingProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            sys = messages[0].content
            has_tool_result = any(m.role == "tool" for m in messages)
            if "Leader（队长）" in sys:
                return await super().chat(messages, tools)
            if not has_tool_result:
                return ChatResponse(tool_calls=[
                    ToolCall("r1", "team_read_messages", {"limit": 20, "consume": False})
                ])
            return ChatResponse(
                text="我读完团队消息：当前协作展示可以按成员发言理解。",
                reasoning_content="INTERNAL THINKING SHOULD NOT RENDER",
            )

    store = SQLiteKanbanStore(tmp_path / "team-bus-noise.db")
    tm, _ = _team(provider=BusReadingProvider(), kanban_store=store)
    chunks = [ch async for ch in tm.interact(Envelope.of("组队做一次展示自检", session_id="bus-team", mode="team"))]
    internal_texts = [
        str(ch.body.get("text") or "")
        for ch in chunks
        if ch.kind == "team_internal"
    ]
    rendered = "\n".join(internal_texts)
    assert "工具 team_read_messages 返回" not in rendered
    assert '"messages"' not in rendered
    assert "INTERNAL THINKING SHOULD NOT RENDER" not in rendered
    assert "我读完团队消息" in rendered


async def test_team_direct_leader_reply_renders_once_as_team_internal():
    class SimpleLeaderProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            if "Leader（队长）" in messages[0].content:
                return ChatResponse(text="你好，我是 hh。", reasoning_content="我在判断这是轻量团队聊天。")
            return await super().chat(messages, tools)

    tm, _ = _team(SimpleLeaderProvider())
    chunks = [ch async for ch in tm.interact(Envelope.of("你好", session_id="direct-leader-team", mode="team"))]
    internal = [ch for ch in chunks if ch.kind == "team_internal"]

    stream = next(ch for ch in internal if ch.body.get("event_type") == "team_stream")
    result = next(ch for ch in internal if ch.body.get("event_type") == "team_summary")
    assert stream.body["node_id"] == result.body["node_id"]
    assert stream.body["source_session_id"] == result.body["source_session_id"]
    assert result.body["agent_id"] == "leader"
    assert result.body["is_leader"] is True
    assert "你好，我是 hh" in result.body["text"]
    assert "process_text" not in result.body
    assert any(ch.kind == "final" for ch in chunks)


def test_team_plans_are_scoped_by_owner_for_same_session_id():
    tm, _ = _team()
    tm.create_plan(
        "same-team",
        goal="A 的任务",
        nodes=[{"id": "a", "title": "A 节点", "assignee": "coder"}],
        owner_account_id="A:uid-a",
    )
    tm.create_plan(
        "same-team",
        goal="B 的任务",
        nodes=[{"id": "b", "title": "B 节点", "assignee": "coder"}],
        owner_account_id="B:uid-b",
    )

    plan_a = tm.read_plan("same-team", owner_account_id="A:uid-a")["plan"]
    plan_b = tm.read_plan("same-team", owner_account_id="B:uid-b")["plan"]
    assert plan_a["goal"] == "A 的任务"
    assert plan_b["goal"] == "B 的任务"
    assert [plan["goal"] for plan in tm.plans_for_session("same-team", owner_account_id="A:uid-a")] == ["A 的任务"]


async def test_team_required_workflow_dispatches_when_leader_does_not_delegate():
    class PassiveLeaderProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            sys = messages[0].content
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "真实验收" in last_user:
                return ChatResponse(text="Leader验收通过：被动 Leader 仍完成质量验收。")
            if "Leader（队长）" in sys:
                return ChatResponse(text="我只做总结，不主动派活")
            return await super().chat(messages, tools)

    tm, tasks = _team(PassiveLeaderProvider())
    chunks = [ch async for ch in tm.interact(Envelope.of(
        "实现一个小功能",
        session_id="workflow_s1",
        mode="team",
        params={"team_spec": _structured_team_spec("实现一个小功能", capabilities=["implementation"], workflow_lanes=("build",))},
    ))]
    final = next(ch.body["text"] for ch in chunks if ch.kind == "final")

    assert "本次团队任务已完成" in final
    board = tasks.list("workflow_s1")
    assert len(board) == 3
    assert {task["assignee"] for task in board} == {"researcher", "coder"}
    assert {task["status"] for task in board} == {"done"}
    plan = tm.read_plan("workflow_s1")["plan"]
    assert plan["status"] == "completed"
    assert [node["assignee"] for node in plan["nodes"]] == ["leader", "researcher", "coder", "leader", "coder", "leader"]
    assert [node["status"] for node in plan["nodes"]] == ["completed"] * 6


def test_team_required_workflow_single_member_title_uses_goal():
    config = Config(
        max_iterations=3,
        team_config={
            "members": [
                {"member_id": "hh", "name": "hh", "role": "负责编码实现", "executor": "builtin"}
            ]
        },
    )
    tm, _ = _team(config=config)
    team = tm._build_team("single_member_s1")
    nodes, edges = tm._default_workflow_nodes(
        team,
        "写一个贪吃蛇小游戏，像素风",
        team_spec=_structured_team_spec("写一个贪吃蛇小游戏，像素风", capabilities=["design", "implementation"], workflow_lanes=("build",)),
    )

    assert [node["id"] for node in nodes] == ["leader_plan", "build_design_1", "build_1", "leader_review", "leader_summary"]
    assert ["leader_plan", "build_design_1"] in edges
    assert ["build_design_1", "leader_review"] in edges
    assert ["leader_review", "build_1"] in edges
    assert ["leader_review", "leader_summary"] in edges
    assert nodes[1]["title"] == "实现方案：贪吃蛇小游戏，像素风"
    assert "先不要编码或改文件" in nodes[1]["detail"]
    assert nodes[1]["assignee"] == "hh"
    assert nodes[1]["metadata"]["workflow_lane"] == "design"
    assert nodes[1]["metadata"]["build_plan_mode"] == "auto"
    assert nodes[2]["title"] == "实现：贪吃蛇小游戏，像素风"
    assert nodes[2]["metadata"]["workflow_lane"] == "build"
    assert nodes[3]["title"] == "Leader 审阅方案：贪吃蛇小游戏，像素风"
    assert nodes[3]["assignee"] == "leader"
    assert nodes[3]["metadata"]["workflow_lane"] == "lead"


def test_team_workflow_lane_reuses_role_catalog_without_member_metadata():
    config = Config(
        max_iterations=3,
        team_config={
            "members": [
                {"member_id": "researcher", "name": "researcher", "role": "负责查资料、读取文件、搜集与整理信息", "executor": "builtin"},
                {"member_id": "coder", "name": "coder", "role": "负责编写代码、执行命令、文件读写等工程操作", "executor": "builtin"},
            ]
        },
    )
    tm, _ = _team(config=config)
    team = tm._build_team("catalog_lane_s1")
    nodes, edges = tm._default_workflow_nodes(
        team,
        "组队算1+1",
        team_spec=_structured_team_spec("组队算1+1", capabilities=["planning", "implementation"], workflow_lanes=("build",)),
    )

    assert [node["id"] for node in nodes] == [
        "leader_plan",
        "plan_1",
        "build_design_1",
        "build_1",
        "leader_review",
        "leader_summary",
    ]
    assert ["plan_1", "build_design_1"] in edges
    assert ["build_design_1", "leader_review"] in edges
    assert ["leader_review", "build_1"] in edges
    assert nodes[1]["assignee"] == "researcher"
    assert nodes[2]["assignee"] == "coder"
    assert nodes[3]["assignee"] == "coder"


def test_team_required_workflow_generates_role_lane_dag():
    config = Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "ui",
                    "name": "ui",
                    "role": "负责像素风 UI",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "design"},
                    "capabilities": ["ui", "visual"],
                },
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责前端开发",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["frontend"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify"},
                    "capabilities": ["test"],
                },
                {
                    "member_id": "doc",
                    "name": "doc",
                    "role": "负责文档记录",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["docs"],
                },
            ]
        },
    )
    tm, _ = _team(config=config)
    team = tm._build_team("lane_dag_s1")
    nodes, edges = tm._default_workflow_nodes(
        team,
        "写一个贪吃蛇小游戏，像素风",
        team_spec=_structured_team_spec(
            "写一个贪吃蛇小游戏，像素风",
            capabilities=["design", "implementation", "testing", "verification", "documentation"],
            workflow_lanes=("build", "verify", "docs"),
        ),
    )

    assert [node["id"] for node in nodes] == [
        "leader_plan",
        "design_1",
        "build_design_1",
        "build_1",
        "verify_qa_plan_1",
        "leader_review",
        "verify_qa_refine_1",
        "verify_qa_verify_1",
        "handoff_1",
        "leader_summary",
    ]
    assert ["leader_plan", "design_1"] in edges
    assert ["leader_plan", "verify_qa_plan_1"] in edges
    assert ["design_1", "build_design_1"] in edges
    assert ["build_design_1", "leader_review"] in edges
    assert ["leader_review", "build_1"] in edges
    assert ["build_1", "verify_qa_refine_1"] in edges
    assert ["verify_qa_plan_1", "leader_review"] in edges
    assert ["leader_review", "verify_qa_refine_1"] in edges
    assert ["verify_qa_refine_1", "verify_qa_verify_1"] in edges
    assert ["verify_qa_verify_1", "handoff_1"] in edges
    assert ["leader_plan", "leader_summary"] in edges
    assert ["handoff_1", "leader_summary"] in edges


def test_team_spec_does_not_infer_workflow_from_testing_words():
    goals = [
        "帮我测试一下之前开发的贪吃蛇，不需要开发新功能",
        "帮我测试一下开发的俄罗斯方块小游戏",
        "测试之前开发好的俄罗斯方块游戏",
    ]

    with pytest.raises(TypeError, match="不再接受字符串目标"):
        build_team_spec(goals[0])

    for goal in goals:
        spec = build_team_spec({"goal": goal})
        assert spec.task_profile["intent"] == "mixed"
        assert spec.team_requirements["workflow_lanes"] == []
        assert spec.team_requirements["roles"] == []
        assert spec.team_requirements["capabilities"] == []
        assert spec.deliverables == []
        assert spec.uncertainty == "high"


def test_team_delegate_workspace_isolates_abstract_team_turns(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.team.team_manager.task_workspace_path",
        lambda workspace_id: tmp_path / str(workspace_id or "default"),
    )
    tm, _ = _team()

    assert tm._team_goal_uses_shared_workspace("测试一下之前开发的贪吃蛇是否可验收")
    assert tm._team_goal_uses_shared_workspace("检查 snake.html 是否可验收")
    assert not tm._team_goal_uses_shared_workspace("测试一下团队协作吧")
    assert not tm._team_goal_uses_shared_workspace("测试一下团队协作是否正常运行")

    shared = tm._team_delegate_cwd(
        Envelope.of("测试一下之前开发的贪吃蛇是否可验收", session_id="web_a::turn::req_1", mode="team"),
        "测试一下之前开发的贪吃蛇是否可验收",
    )
    isolated = tm._team_delegate_cwd(
        Envelope.of("测试一下团队协作吧", session_id="web_a::turn::req_2", mode="team"),
        "测试一下团队协作吧",
    )

    assert shared == ""
    shared_cwd = tm._team_shared_cwd(
        Envelope.of(
            "测试一下之前开发的贪吃蛇是否可验收",
            session_id="web_a::turn::req_1",
            mode="team",
            workspace_id="default",
        ),
    )
    assert shared_cwd == str((tmp_path / "default").resolve())
    assert Path(isolated).exists()
    assert str(tmp_path / "default" / "team_turns") in isolated
    assert "req_2" in isolated

    node_a = tm._team_delegate_cwd(
        Envelope.of("测试团队协作", session_id="web_a::turn::req_2", mode="team"),
        "测试团队协作",
        node_id="frontend",
        agent_id="agent-a",
    )
    node_b = tm._team_delegate_cwd(
        Envelope.of("测试团队协作", session_id="web_a::turn::req_2", mode="team"),
        "测试团队协作",
        node_id="backend",
        agent_id="agent-b",
    )
    assert node_a != node_b
    assert "frontend" in node_a and "agent-a" in node_a
    assert "backend" in node_b and "agent-b" in node_b


def test_team_internal_chunk_carries_member_turn_file_changes(tmp_path):
    tm, _ = _team()
    changed = tmp_path / "member.txt"
    chunk = tm._team_internal_chunk(
        "req",
        agent_id="coder",
        text="完成",
        node_id="node-1",
        event_type="team_submit",
        turn_file_changes=[{
            "path": str(changed),
            "name": changed.name,
            "added": 2,
            "removed": 1,
            "status": "modified",
        }],
    )

    assert chunk.kind == "team_internal"
    assert chunk.body["turn_file_changes"] == [{
        "path": str(changed),
        "name": changed.name,
        "added": 2,
        "removed": 1,
        "status": "modified",
    }]


def test_team_workspace_file_changes_include_deletions(tmp_path):
    tm, _ = _team()
    deleted = tmp_path / "removed.txt"
    deleted.write_text("old\n", encoding="utf-8")
    before = tm._workspace_file_snapshot(tmp_path)
    deleted.unlink()

    changes = tm._workspace_file_changes(tmp_path, before)

    assert changes == [{
        "path": str(deleted.resolve()),
        "name": deleted.name,
        "added": 0,
        "removed": 0,
        "status": "deleted",
        "diff": [],
        "revision": f"deleted:{before[str(deleted.resolve())][0]}:{before[str(deleted.resolve())][1]}",
    }]


async def test_delegate_tool_rejects_non_team_leader_context():
    tm, _ = _team()
    team = tm._build_team("guard_s1")
    token = current_agent_id.set("coder")
    try:
        result = await team.leader.registry.execute(
            ToolCall("guard", "delegate_to_teammate", {"member": "coder", "instruction": "算 1+1"})
        )
    finally:
        current_agent_id.reset(token)
    assert result.is_error
    assert "只允许 Crew Team 内部 Leader" in result.content


async def test_delegate_tool_requires_existing_plan_node_when_team_plan_exists():
    tm, tasks = _team()
    team = tm._build_team("guard_plan_s1")
    tm.create_plan(
        "guard_plan_s1",
        goal="实现并验收",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "build_1", "title": "实现", "assignee": "coder"},
            {"id": "leader_summary", "title": "Leader 总结", "assignee": "leader"},
        ],
        edges=[["leader_plan", "build_1"], ["build_1", "leader_summary"]],
    )

    token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall("guard-missing-node", "delegate_to_teammate", {"member": "coder", "instruction": "算 1+1"})
        )
    finally:
        current_agent_id.reset(token)

    assert result.is_error
    assert "必须绑定现有 plan_node_id" in result.content
    assert tasks.list("guard_plan_s1") == []


async def test_delegate_tool_rejects_leader_plan_node():
    tm, tasks = _team()
    team = tm._build_team("guard_plan_s2")
    tm.create_plan(
        "guard_plan_s2",
        goal="实现并验收",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "build_1", "title": "实现", "assignee": "coder"},
            {"id": "leader_summary", "title": "Leader 总结", "assignee": "leader"},
        ],
        edges=[["leader_plan", "build_1"], ["build_1", "leader_summary"]],
    )

    token = current_agent_id.set("leader")
    try:
        leader_node = await team.leader.registry.execute(
            ToolCall(
                "guard-leader-node",
                "delegate_to_teammate",
                {"member": "coder", "instruction": "算 1+1", "plan_node_id": "leader_summary"},
            )
        )
    finally:
        current_agent_id.reset(token)

    assert leader_node.is_error
    assert "Leader 控制节点" in leader_node.content
    assert tasks.list("guard_plan_s2") == []


async def test_delegate_tool_allows_pending_member_plan_node():
    tm, tasks = _team()
    team = tm._build_team("guard_plan_s3")
    tm.create_plan(
        "guard_plan_s3",
        goal="实现并验收",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "build_1", "title": "实现", "assignee": "coder"},
            {"id": "leader_summary", "title": "Leader 总结", "assignee": "leader"},
        ],
        edges=[["leader_plan", "build_1"], ["build_1", "leader_summary"]],
    )

    token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall(
                "guard-valid-node",
                "delegate_to_teammate",
                {"member": "coder", "instruction": "算 1+1", "plan_node_id": "build_1"},
            )
        )
    finally:
        current_agent_id.reset(token)

    assert not result.is_error
    assert "coder算出：2" in result.content
    assert [task["assignee"] for task in tasks.list("guard_plan_s3")] == ["coder"]
    nodes = {node.get("node_id") or node.get("id"): node for node in tm.read_plan("guard_plan_s3")["plan"]["nodes"]}
    assert nodes["build_1"]["status"] == "completed"

    token = current_agent_id.set("leader")
    try:
        repeated = await team.leader.registry.execute(
            ToolCall(
                "guard-repeat-node",
                "delegate_to_teammate",
                {"member": "coder", "instruction": "再算一次", "plan_node_id": "build_1"},
            )
        )
    finally:
        current_agent_id.reset(token)

    assert repeated.is_error
    assert "不能重复委派" in repeated.content
    assert len(tasks.list("guard_plan_s3")) == 1


async def test_legacy_delegate_entry_uses_unified_assignment_events(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "legacy-delegate-kanban.db")
    owner_store = store.for_owner("local")
    tm, tasks = _team(kanban_store=store)
    team = tm._build_team("legacy_delegate_s1", owner_account_id="local")
    tm.create_plan(
        "legacy_delegate_s1",
        goal="实现并验收",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "build_1", "title": "实现", "assignee": "coder"},
            {"id": "leader_summary", "title": "Leader 总结", "assignee": "leader"},
        ],
        edges=[["leader_plan", "build_1"], ["build_1", "leader_summary"]],
        owner_account_id="local",
    )

    token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall(
                "legacy-delegate-valid-node",
                "delegate_to_teammate",
                {"member": "coder", "instruction": "算 1+1", "plan_node_id": "build_1"},
            )
        )
    finally:
        current_agent_id.reset(token)

    assert not result.is_error
    assert "coder算出：2" in result.content
    assert [task["assignee"] for task in tasks.list("legacy_delegate_s1")] == ["coder"]
    workflow = owner_store.get_latest_workflow_by_session("legacy_delegate_s1")
    assert workflow is not None
    events = owner_store.get_board_state(workflow.id)["events"]
    assign = next(event for event in events if event["event_type"] == "team_assign")
    submit = next(event for event in events if event["event_type"] == "team_submit")
    assert assign["payload"]["assignment_source"] == "legacy_delegate"
    assert submit["payload"]["assignment_source"] == "legacy_delegate"
    assert assign["payload"]["mention_to"] == ["coder"]
    assert submit["payload"]["mention_from"] == "coder"


async def test_team_mention_tool_routes_and_guards_user_mentions():
    tm, tasks = _team()
    team = tm._build_team("mention_s1")
    assert "team_send_message" not in team.leader.registry.names()
    assert "team_read_messages" in team.leader.registry.names()
    assert "team_send_message" not in team.teammates["coder"].registry.names()
    assert "team_read_messages" in team.teammates["coder"].registry.names()
    assert "ask_followup_question" in team.leader.registry.names()
    assert "ask_followup_question" not in team.teammates["coder"].registry.names()
    leader_visible_tool_names = {
        schema["function"]["name"]
        for schema in team.leader.registry.list_schemas(team.leader.tool_filter)
    }
    assert "team_mention" in leader_visible_tool_names
    assert "request_plan_change" in leader_visible_tool_names
    assert "delegate_to_teammate" not in leader_visible_tool_names
    member_mention_schema = team.teammates["coder"].registry.get("team_mention").parameters
    member_targets = member_mention_schema["properties"]["to"]["items"]["enum"]
    assert "user" not in member_targets
    tm.create_plan(
        "mention_s1",
        goal="补充测试方案",
        nodes=[{"id": "qa_plan", "title": "补充测试方案", "detail": "请补充测试方案", "assignee": "coder"}],
        edges=[],
    )

    leader_token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall(
                "mention-assign",
                "team_mention",
                {
                    "to": ["coder"],
                    "intent": "assign",
                    "content": "请补充测试方案",
                    "node_id": "qa_plan",
                },
            )
        )
    finally:
        current_agent_id.reset(leader_token)
    assert not result.is_error
    assert "[@coder](mention://member/coder)" in result.content
    assert "coder算出" in result.content
    assert [task["assignee"] for task in tasks.list("mention_s1")] == ["coder"]
    assert tm.read_plan("mention_s1")["plan"]["nodes"][0]["status"] == "completed"
    messages = team.bus.read(team_session_id="mention_s1", member_id="coder", consume=False)
    assert messages and messages[0].sender_member_id == "leader"

    member_token = current_agent_id.set("coder")
    try:
        blocked = await team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-user",
                "team_mention",
                {
                    "to": ["user"],
                    "intent": "user_followup",
                    "content": "请用户确认范围",
                },
            )
        )
    finally:
        current_agent_id.reset(member_token)
    assert blocked.is_error
    assert "user" in blocked.content
    assert "参数校验失败" in blocked.content

    member_token = current_agent_id.set("coder")
    try:
        missing_status = await team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-submit-missing-status",
                "team_mention",
                {
                    "to": ["leader"],
                    "intent": "submit",
                    "content": "测试已完成",
                    "node_id": "qa_plan",
                },
            )
        )
        submitted = await team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-submit-pass",
                "team_mention",
                {
                    "to": ["leader"],
                    "intent": "submit",
                    "content": "测试已完成",
                    "node_id": "qa_plan",
                    "result_status": "pass",
                },
            )
        )
    finally:
        current_agent_id.reset(member_token)
    assert missing_status.is_error
    assert "result_status" in missing_status.content
    assert not submitted.is_error

    leader_token = current_agent_id.set("leader")
    try:
        broadcast = await team.leader.registry.execute(
            ToolCall(
                "mention-all",
                "team_mention",
                {
                    "to": ["all"],
                    "intent": "broadcast",
                    "content": "同步一下当前进度",
                },
            )
        )
    finally:
        current_agent_id.reset(leader_token)
    assert not broadcast.is_error
    assert "[@all](mention://team/all)" in broadcast.content
    assert team.bus.read(team_session_id="mention_s1", member_id="coder", consume=False)


async def test_team_mention_assign_requires_existing_plan_node():
    tm, tasks = _team()
    team = tm._build_team("mention_guard_s1")
    tm.create_plan(
        "mention_guard_s1",
        goal="补充测试方案",
        nodes=[{"id": "qa_plan", "title": "补充测试方案", "detail": "请补充测试方案", "assignee": "coder"}],
        edges=[],
    )

    leader_token = current_agent_id.set("leader")
    try:
        missing_node = await team.leader.registry.execute(
            ToolCall(
                "mention-assign-missing-node",
                "team_mention",
                {"to": ["coder"], "intent": "assign", "content": "请补充测试方案"},
            )
        )
        mismatch = await team.leader.registry.execute(
            ToolCall(
                "mention-assign-mismatch",
                "team_mention",
                {
                    "to": ["researcher"],
                    "intent": "assign",
                    "content": "请补充测试方案",
                    "node_id": "qa_plan",
                },
            )
        )
    finally:
        current_agent_id.reset(leader_token)

    assert missing_node.is_error
    assert "必须绑定现有 TeamPlan node_id" in missing_node.content
    assert mismatch.is_error
    assert "不能委派给 researcher" in mismatch.content
    assert tasks.list("mention_guard_s1") == []
    assert tm.read_plan("mention_guard_s1")["plan"]["nodes"][0]["status"] == "pending"


def test_team_bus_message_contract_keeps_request_reply_context():
    bus = TeamBus()
    message = bus.send(
        team_session_id="communication_contract_s1",
        sender_member_id="coder",
        recipient_member_ids=["leader"],
        content="请确认方案范围",
        message_type="decision_request",
        intent="ask",
        request_id="comm_ask_1",
        node_id="game_design",
        task_id="task_42",
    )

    assert message.intent == "ask"
    assert message.request_id == "comm_ask_1"
    assert message.node_id == "game_design"
    assert message.task_id == "task_42"
    assert message.thread_id == "game_design"
    assert message.to_dict()["recipient_member_ids"] == ["leader"]


@pytest.mark.asyncio
async def test_builtin_and_external_mentions_share_communication_router_contract():
    tm, _tasks = _team()
    builtin_team = tm._build_team("communication_builtin_s1")

    member_token = current_agent_id.set("coder")
    try:
        result = await builtin_team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-contract",
                "team_mention",
                {
                    "to": ["leader"],
                    "intent": "ask",
                    "content": "用户只需要方案吗？",
                    "node_id": "game_design",
                    "task_id": "task_42",
                },
            )
        )
    finally:
        current_agent_id.reset(member_token)

    assert not result.is_error
    builtin_payload = json.loads(result.content)
    builtin_message = builtin_payload["mention"]["message"]
    assert builtin_message["intent"] == "ask"
    assert builtin_message["request_id"]
    assert builtin_message["node_id"] == "game_design"
    assert builtin_message["task_id"] == "task_42"
    assert builtin_message["message_type"] == "decision_request"

    external_result = await tm.external_team_mention(
        "communication_external_s1",
        member_id="coder",
        to=["leader"],
        intent="ask",
        content="请确认是否只输出方案",
        node_id="game_design",
        task_id="task_43",
    )
    assert external_result["status"] == "answered"
    external_team = tm._get_or_create("communication_external_s1")
    external_message = external_team.bus.read(
        team_session_id="communication_external_s1",
        member_id="leader",
        consume=False,
    )[0].to_dict()
    assert external_message["intent"] == "ask"
    assert external_message["request_id"]
    assert external_message["node_id"] == "game_design"
    assert external_message["task_id"] == "task_43"
    assert external_message["message_type"] == "decision_request"


@pytest.mark.asyncio
async def test_team_ask_runs_target_agent_and_publishes_reply():
    class AskAnswerProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                return ChatResponse(text="只输出小游戏方案，不进入开发实现。")
            return await super().chat(messages, tools)

    tm, _tasks = _team(provider=AskAnswerProvider())
    team = tm._build_team("communication_ask_s1")
    member_token = current_agent_id.set("coder")
    try:
        result = await team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-execute",
                "team_mention",
                {
                    "to": ["leader"],
                    "intent": "ask",
                    "content": "用户只需要小游戏方案吗？",
                    "node_id": "game_design",
                    "task_id": "task_42",
                },
            )
        )
    finally:
        current_agent_id.reset(member_token)

    assert not result.is_error
    payload = json.loads(result.content)
    answer = payload["result"]["answer"]
    assert payload["result"]["status"] == "answered"
    assert answer == "只输出小游戏方案，不进入开发实现。"
    messages = team.bus.list_messages("communication_ask_s1")
    request = next(item for item in messages if item["message_type"] == "decision_request")
    reply = next(item for item in messages if item["message_type"] == "answer")
    assert reply["reply_to"] == request["message_id"]
    assert reply["request_id"] == request["request_id"]
    assert reply["sender_member_id"] == "leader"
    assert reply["recipient_member_ids"] == ["coder"]


@pytest.mark.asyncio
async def test_team_ask_serializes_same_target_and_records_queue_status():
    class SlowAskProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                await asyncio.sleep(0.04)
                return ChatResponse(text=f"已回答：{last_user.split('问题：', 1)[-1].splitlines()[0]}")
            return await super().chat(messages, tools)

    tm, _tasks = _team(provider=SlowAskProvider())
    team = tm._build_team("communication_queue_s1")
    coordinator = team.communication_router.ask_coordinator
    assert coordinator is not None
    coordinator.timeout_seconds = 1.0
    member_token = current_agent_id.set("coder")
    try:
        first = asyncio.create_task(team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-queue-1",
                "team_mention",
                {"to": ["leader"], "intent": "ask", "content": "问题一"},
            )
        ))
        await asyncio.sleep(0.005)
        second = asyncio.create_task(team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-queue-2",
                "team_mention",
                {"to": ["leader"], "intent": "ask", "content": "问题二"},
            )
        ))
        first_result, second_result = await asyncio.gather(first, second)
    finally:
        current_agent_id.reset(member_token)

    assert not first_result.is_error
    assert not second_result.is_error
    assert json.loads(first_result.content)["result"]["status"] == "answered"
    assert json.loads(second_result.content)["result"]["status"] == "answered"
    messages = team.bus.list_messages("communication_queue_s1")
    requests = [item for item in messages if item["message_type"] == "decision_request"]
    assert len(requests) == 2
    assert all(item["status"] == "answered" for item in requests)
    assert any(
        item["type"] == "message_status_changed" and item["status"] == "queued"
        for item in team.bus.events("communication_queue_s1")
    )


@pytest.mark.asyncio
async def test_team_ask_timeout_returns_expired_and_replies():
    class TimeoutAskProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                await asyncio.sleep(0.05)
                return ChatResponse(text="不会及时返回")
            return await super().chat(messages, tools)

    tm, _tasks = _team(provider=TimeoutAskProvider())
    team = tm._build_team("communication_timeout_s1")
    coordinator = team.communication_router.ask_coordinator
    assert coordinator is not None
    coordinator.timeout_seconds = 0.01
    member_token = current_agent_id.set("coder")
    try:
        result = await team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-timeout",
                "team_mention",
                {"to": ["leader"], "intent": "ask", "content": "请快速回答"},
            )
        )
    finally:
        current_agent_id.reset(member_token)

    assert not result.is_error
    payload = json.loads(result.content)["result"]
    assert payload["status"] == "expired"
    messages = team.bus.list_messages("communication_timeout_s1")
    request = next(item for item in messages if item["message_type"] == "decision_request")
    reply = next(item for item in messages if item["message_type"] == "answer")
    assert request["status"] == "expired"
    assert reply["reply_to"] == request["message_id"]
    expired_messages = tm.session_store.load(
        f"communication_timeout_s1::turn::{request['request_id']}::leader",
        owner_account_id="local",
    )
    expired_answer = next(
        message for message in reversed(expired_messages)
        if message.role == "assistant" and message.content
    )
    assert expired_answer.communication_kind == "ask_answer"
    assert expired_answer.communication_status == "expired"
    assert expired_answer.request_id == request["request_id"]
    assert expired_answer.reply_to == request["message_id"]


@pytest.mark.asyncio
async def test_team_ask_cancellation_persists_cancelled_status():
    class SlowAskProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                await asyncio.sleep(1)
                return ChatResponse(text="不会返回")
            return await super().chat(messages, tools)

    tm, _tasks = _team(provider=SlowAskProvider())
    team = tm._build_team("communication_cancel_s1")
    member_token = current_agent_id.set("coder")
    task = asyncio.create_task(
        team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-cancel",
                "team_mention",
                {"to": ["leader"], "intent": "ask", "content": "请回答后取消"},
            )
        )
    )
    try:
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        current_agent_id.reset(member_token)

    messages = team.bus.list_messages("communication_cancel_s1")
    request = next(item for item in messages if item["message_type"] == "decision_request")
    assert request["status"] == "cancelled"
    cancelled_messages = tm.session_store.load(
        f"communication_cancel_s1::turn::{request['request_id']}::leader",
        owner_account_id="local",
    )
    cancelled_answer = next(
        message for message in reversed(cancelled_messages)
        if message.role == "assistant" and message.content
    )
    assert cancelled_answer.communication_kind == "ask_answer"
    assert cancelled_answer.communication_status == "cancelled"
    assert cancelled_answer.request_id == request["request_id"]
    assert cancelled_answer.reply_to == request["message_id"]


@pytest.mark.asyncio
async def test_team_ask_history_projects_request_status_and_reply_without_new_node_or_artifact(tmp_path):
    class AskAnswerProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                return ChatResponse(text="只回答当前问题，不创建工作流节点。")
            return await super().chat(messages, tools)

    store = SQLiteKanbanStore(tmp_path / "communication-history.db")
    tm, _tasks = _team(provider=AskAnswerProvider(), kanban_store=store)
    team = tm._build_team("communication_history_s1", owner_account_id="local")
    tm.create_plan(
        "communication_history_s1",
        goal="团队通信测试",
        nodes=[{
            "id": "game_design",
            "title": "方案确认",
            "detail": "确认方案边界",
            "assignee": "coder",
        }],
        owner_account_id="local",
    )
    before_nodes = {
        str(node.get("node_id") or node.get("id") or "")
        for node in tm.read_plan("communication_history_s1", owner_account_id="local")["plan"]["nodes"]
    }

    member_token = current_agent_id.set("coder")
    try:
        result = await team.teammates["coder"].registry.execute(
            ToolCall(
                "mention-ask-history",
                "team_mention",
                {
                    "to": ["leader"],
                    "intent": "ask",
                    "content": "只确认方案，不要继续开发。",
                    "node_id": "game_design",
                },
            )
        )
    finally:
        current_agent_id.reset(member_token)

    assert not result.is_error
    history = tm.event_history_for_session("communication_history_s1", owner_account_id="local")
    communication = [item for item in history if item["event_type"] == "team_communication"]
    assert [item["communication_kind"] for item in communication] == ["ask_request", "ask_answer"]
    assert communication[0]["communication_status"] == "answered"
    assert communication[1]["request_id"] == communication[0]["request_id"]
    assert communication[1]["reply_to"]
    after_nodes = {
        str(node.get("node_id") or node.get("id") or "")
        for node in tm.read_plan("communication_history_s1", owner_account_id="local")["plan"]["nodes"]
    }
    assert after_nodes == before_nodes
    assert team.bus.list_artifacts("communication_history_s1") == []


@pytest.mark.asyncio
async def test_user_agent_mention_wakes_selected_member_without_workflow_or_artifact():
    class UserMentionProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                assert "发起成员：user" in last_user
                return ChatResponse(text="coder 当前使用 K3 模型。")
            return await super().chat(messages, tools)

    tm, tasks = _team(provider=UserMentionProvider())
    process_launch = ProcessLaunch(PermissionProfile(PermissionProfileKind.DISABLED))
    team = tm._get_or_create("user_mention_s1", owner_account_id="local")
    original_run = team.teammates["coder"].run
    seen_launch: list[object] = []

    async def capture_launch(member_envelope):
        seen_launch.append(member_envelope.params.get("_security_process_launch"))
        async for chunk in original_run(member_envelope):
            yield chunk

    team.teammates["coder"].run = capture_launch
    envelope = Envelope.of(
        "你使用的是什么模型？",
        session_id="user_mention_s1",
        request_id="user_mention_req",
        mode="team",
        user_id="local",
        params={
            "user_mentions": [{"kind": "team_member", "member_id": "coder"}],
            "_security_process_launch": process_launch,
        },
    )

    chunks = [chunk async for chunk in tm.interact(envelope)]

    assert chunks[0].kind == "status"
    assert chunks[1].kind == "team_internal"
    waiting = chunks[1]
    assert waiting.body["communication_status"] == "waiting_reply"
    assert waiting.body["communication_request_text"] == envelope.query
    answer = next(
        chunk for chunk in chunks
        if chunk.kind == "team_internal" and chunk.body.get("communication_status") == "answered"
    )
    assert answer.body["agent_id"] == "coder"
    assert answer.body["mention_intent"] == "answer"
    assert answer.body["communication_kind"] == "user_mention_answer"
    assert answer.body["communication_status"] == "answered"
    assert answer.body["request_id"] == envelope.request_id
    assert answer.body["communication_request_text"] == envelope.query
    assert chunks[-1].body["text"] == "coder 当前使用 K3 模型。"
    assert any(
        chunk.kind == "team_internal" and chunk.body.get("event_type") == "team_stream"
        for chunk in chunks
    )
    assert seen_launch == [process_launch]
    assert tasks.list("user_mention_s1") == []
    assert ("local", "user_mention_s1") not in tm._plans

    team = tm._get_or_create("user_mention_s1", owner_account_id="local")
    messages = team.bus.list_messages("user_mention_s1")
    request = next(item for item in messages if item["message_type"] == "decision_request")
    reply = next(item for item in messages if item["message_type"] == "answer")
    assert request["sender_member_id"] == "user"
    assert request["recipient_member_ids"] == ["coder"]
    assert request["intent"] == "ask"
    assert reply["reply_to"] == request["message_id"]
    assert reply["sender_member_id"] == "coder"
    assert reply["recipient_member_ids"] == ["user"]
    assert answer.body["reply_to"] == request["message_id"]
    child_messages = tm.session_store.load(
        f"user_mention_s1::turn::{envelope.request_id}::coder",
        owner_account_id="local",
    )
    child_answer = next(
        message for message in reversed(child_messages)
        if message.role == "assistant" and message.content
    )
    assert child_answer.communication_kind == "user_mention_answer"
    assert child_answer.communication_status == "answered"
    assert child_answer.request_id == envelope.request_id
    assert child_answer.reply_to == request["message_id"]


@pytest.mark.asyncio
async def test_user_agent_mention_request_id_is_idempotent_and_new_id_retries():
    calls: list[str] = []

    class UserMentionProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "这是一次团队内部通信回合" in last_user:
                calls.append(last_user)
                await asyncio.sleep(0.02)
                return ChatResponse(text="当前使用 K3 模型。")
            return await super().chat(messages, tools)

    tm, _tasks = _team(provider=UserMentionProvider())
    base_params = {
        "user_mentions": [{"kind": "team_member", "member_id": "coder"}],
    }

    async def run(request_id: str):
        envelope = Envelope.of(
            "你使用的是什么模型？",
            session_id="user_mention_idempotent_s1",
            request_id=request_id,
            mode="team",
            user_id="local",
            params=base_params,
        )
        return [chunk async for chunk in tm.interact(envelope)]

    first, duplicate = await asyncio.gather(
        run("user_mention_once"),
        run("user_mention_once"),
    )
    for result in (first, duplicate):
        assert result[0].kind == "status"
        assert result[1].body["communication_status"] == "waiting_reply"
        assert any(
            chunk.kind == "team_internal" and chunk.body.get("communication_status") == "answered"
            for chunk in result
        )
        assert result[-1].kind == "final"
    assert len(calls) == 1

    team = tm._get_or_create("user_mention_idempotent_s1", owner_account_id="local")
    messages = team.bus.list_messages("user_mention_idempotent_s1")
    assert len([item for item in messages if item["message_type"] == "decision_request"]) == 1
    assert len([item for item in messages if item["message_type"] == "answer"]) == 1

    retried = await run("user_mention_retry")
    assert retried[0].kind == "status"
    assert retried[1].body["communication_status"] == "waiting_reply"
    assert any(
        chunk.kind == "team_internal" and chunk.body.get("communication_status") == "answered"
        for chunk in retried
    )
    assert retried[-1].kind == "final"
    assert len(calls) == 2
    messages = team.bus.list_messages("user_mention_idempotent_s1")
    assert len([item for item in messages if item["message_type"] == "decision_request"]) == 2
    assert len([item for item in messages if item["message_type"] == "answer"]) == 2


@pytest.mark.asyncio
async def test_user_agent_mention_streams_thinking_and_tools_without_workflow():
    tm, tasks = _team()
    team = tm._get_or_create("user_mention_stream_s1", owner_account_id="local")

    async def streaming_run(member_envelope):
        yield ResponseChunk.thinking_event(member_envelope.request_id, "先确认模型配置。", sequence=1)
        yield ResponseChunk.tool_event(
            member_envelope.request_id,
            "runtime_info",
            "start",
            sequence=2,
            tool_call_id="runtime-info-1",
        )
        yield ResponseChunk.delta(member_envelope.request_id, "coder 当前使用 ", sequence=3)
        yield ResponseChunk.tool_event(
            member_envelope.request_id,
            "runtime_info",
            "result",
            detail="provider=kimi model=kimi-code/k3",
            sequence=4,
            tool_call_id="runtime-info-1",
        )
        yield ResponseChunk.final(member_envelope.request_id, "coder 当前使用 Kimi Code/K3。", sequence=5)

    team.teammates["coder"].run = streaming_run
    envelope = Envelope.of(
        "你现在使用什么模型？",
        session_id="user_mention_stream_s1",
        request_id="user_mention_stream_req",
        mode="team",
        user_id="local",
        params={
            "user_mentions": [{"kind": "team_member", "member_id": "coder"}],
        },
    )

    chunks = [chunk async for chunk in tm.interact(envelope)]
    stream_chunks = [
        chunk for chunk in chunks
        if chunk.kind == "team_internal" and chunk.body.get("event_type") == "team_stream"
    ]
    assert any(chunk.body.get("thinking") == "先确认模型配置。" for chunk in stream_chunks)
    assert any(chunk.body.get("tool_calls") for chunk in stream_chunks)
    assert any(chunk.body.get("text") == "coder 当前使用 " for chunk in stream_chunks)
    terminal = next(
        chunk for chunk in chunks
        if chunk.kind == "team_internal" and chunk.body.get("communication_status") == "answered"
    )
    assert terminal.body["text"] == "coder 当前使用 Kimi Code/K3。"
    assert chunks[-1].kind == "final"
    assert tasks.list("user_mention_stream_s1") == []
    assert ("local", "user_mention_stream_s1") not in tm._plans


@pytest.mark.asyncio
async def test_user_agent_mention_converts_agent_exception_to_failed_terminal_state():
    tm, tasks = _team()
    envelope = Envelope.of(
        "@coder 你现在用什么模型？",
        session_id="user_mention_exception_s1",
        request_id="user_mention_exception_req",
        mode="team",
        user_id="local",
        params={
            "user_mentions": [{"kind": "team_member", "member_id": "coder"}],
        },
    )
    team = tm._get_or_create("user_mention_exception_s1", owner_account_id="local")
    calls = 0

    async def broken_run(_envelope):
        nonlocal calls
        calls += 1
        raise RuntimeError("provider exploded")
        yield  # pragma: no cover - keep this function an async generator

    team.teammates["coder"].run = broken_run

    first = [chunk async for chunk in tm.interact(envelope)]
    duplicate = [chunk async for chunk in tm.interact(envelope)]

    assert [chunk.kind for chunk in first] == ["status", "team_internal", "team_internal", "error"]
    assert first[2].body["communication_status"] == "failed"
    assert "稍后重试" in first[2].body["text"]
    assert first[-1].body["message"] == first[2].body["text"]
    assert [chunk.kind for chunk in duplicate] == ["status", "team_internal", "team_internal", "error"]
    assert calls == 1

    messages = team.bus.list_messages("user_mention_exception_s1")
    assert len([item for item in messages if item["message_type"] == "decision_request"]) == 1
    assert len([item for item in messages if item["message_type"] == "answer"]) == 1
    child_messages = tm.session_store.load(
        f"user_mention_exception_s1::turn::{envelope.request_id}::coder",
        owner_account_id="local",
    )
    child_answer = next(
        message for message in reversed(child_messages)
        if message.role == "assistant" and message.content
    )
    assert child_answer.communication_status == "failed"
    assert tasks.list("user_mention_exception_s1") == []


@pytest.mark.asyncio
async def test_user_agent_mention_rejects_unselected_or_unknown_target_without_fallback():
    tm, tasks = _team()
    envelope = Envelope.of(
        "请直接回答",
        session_id="user_mention_invalid_s1",
        request_id="user_mention_invalid_req",
        mode="team",
        user_id="local",
        params={
            "user_mentions": [{"kind": "team_member", "member_id": "not_in_team"}],
        },
    )

    chunks = [chunk async for chunk in tm.interact(envelope)]

    assert chunks[-1].kind == "error"
    assert "不是当前团队成员" in chunks[-1].body["message"]
    assert tasks.list("user_mention_invalid_s1") == []
    team = tm._get_or_create("user_mention_invalid_s1", owner_account_id="local")
    assert team.bus.list_messages("user_mention_invalid_s1") == []


@pytest.mark.asyncio
async def test_user_agent_mention_exposes_terminal_failure_state_before_error_frame():
    tm, tasks = _team()
    envelope = Envelope.of(
        "@coder 你现在用什么模型？",
        session_id="user_mention_failed_s1",
        request_id="user_mention_failed_req",
        mode="team",
        user_id="local",
        params={
            "user_mentions": [{"kind": "team_member", "member_id": "coder"}],
        },
    )
    team = tm._get_or_create("user_mention_failed_s1", owner_account_id="local")

    async def failed_route_user_mention(**_kwargs):
        return {
            "status": "expired",
            "target": "coder",
            "answer": "coder 的回答已超时。",
            "reply_to": "bus_failed",
        }

    team.communication_router.route_user_mention = failed_route_user_mention
    chunks = [chunk async for chunk in tm.interact(envelope)]

    assert [chunk.kind for chunk in chunks] == [
        "status",
        "team_internal",
        "team_internal",
        "error",
    ]
    terminal = chunks[2]
    assert terminal.body["communication_kind"] == "user_mention_answer"
    assert terminal.body["communication_status"] == "expired"
    assert terminal.body["request_id"] == envelope.request_id
    assert terminal.body["reply_to"] == "bus_failed"
    assert terminal.body["communication_request_text"] == envelope.query
    assert chunks[-1].body["message"] == "coder 的回答已超时。"
    assert tasks.list("user_mention_failed_s1") == []
    assert ("local", "user_mention_failed_s1") not in tm._plans


async def test_external_team_mention_propagates_current_active_skill(monkeypatch):
    tm, _tasks = _team()
    tm._get_or_create("external_skill_team")
    tm.create_plan(
        "external_skill_team",
        goal="查询资料",
        nodes=[{
            "id": "search",
            "title": "查询资料",
            "detail": "查询资料",
            "assignee": "coder",
        }],
        edges=[],
    )
    seen = {}

    async def fake_delegate(*_args, **kwargs):
        seen["task_payload_meta"] = kwargs.get("task_payload_meta")
        return "done"

    monkeypatch.setattr(
        "crew.team.team_manager.run_delegate_to_teammate",
        fake_delegate,
    )
    result = await tm.external_team_mention(
        "external_skill_team",
        member_id="leader",
        to=["coder"],
        intent="assign",
        content="查询资料",
        node_id="search",
        task_payload_meta={
            "active_skills": [{
                "skill_id": "directory-search",
                "name": "统一搜索",
            }],
        },
    )

    assert result["assigned"] is True
    assert seen["task_payload_meta"]["active_skills"][0]["skill_id"] == "directory-search"


async def test_team_request_delegate_control_plane_entry():
    tm, tasks = _team()
    result = await tm.request_delegate(
        "mcp_team_s1",
        member="coder",
        instruction="算 1+1",
        requester_member_id="external_agent",
    )
    assert result["ok"] is True
    assert result["member"] == "coder"
    assert result["status"] == "in_progress"
    assert result["task_id"]
    for _ in range(20):
        board = tasks.list("mcp_team_s1")
        if board and board[0]["status"] == "done":
            break
        await asyncio.sleep(0.01)
    board = tasks.list("mcp_team_s1")
    assert len(board) == 1
    assert board[0]["assignee"] == "coder"
    assert board[0]["status"] == "done"
    messages = tm._teams[("", "mcp_team_s1")].bus.list_messages("mcp_team_s1")
    assert messages[0]["sender_member_id"] == "external_agent"


async def test_team_request_delegate_can_wait_for_result():
    tm, tasks = _team()
    result = await tm.request_delegate(
        "mcp_team_sync_s1",
        member="coder",
        instruction="算 1+1",
        requester_member_id="external_agent",
        wait_for_result=True,
    )
    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["task_id"]
    assert "coder算出" in result["output"]
    assert tasks.list("mcp_team_sync_s1")[0]["status"] == "done"


async def test_team_delegate_propagates_current_turn_attachments(tmp_path):
    seen: list[Envelope] = []

    class RecordingAgent:
        async def run(self, envelope):
            seen.append(envelope)
            yield ResponseChunk.final(envelope.request_id, "已读取附件")

    attachment = tmp_path / "用户模板.png"
    attachment.write_bytes(b"image")
    tasks = InMemoryTaskManager()

    output = await run_delegate_to_teammate(
        {"designer": RecordingAgent()},
        tasks,
        "team_attachment_s1",
        member="designer",
        instruction="根据模板设计页面",
        owner_account_id="owner-a",
        attachments=[{
            "name": "用户模板.png",
            "path": str(attachment),
            "type": "image",
        }],
    )

    assert output == "已读取附件"
    assert len(seen) == 1
    assert seen[0].attachments == [{
        "name": "用户模板.png",
        "path": str(attachment),
        "type": "image",
    }]
    assert seen[0].user_id == "owner-a"


async def test_team_delegate_propagates_security_launch_context():
    seen: list[Envelope] = []

    class RecordingAgent:
        async def run(self, envelope):
            seen.append(envelope)
            yield ResponseChunk.final(envelope.request_id, "已继承安全边界")

    launch = ProcessLaunch(
        PermissionProfile(PermissionProfileKind.MANAGED),
        ("native-runtime",),
    )
    token = current_process_launch.set(launch)
    try:
        output = await run_delegate_to_teammate(
            {"coder": RecordingAgent()},
            InMemoryTaskManager(),
            "team-security-context-s1",
            member="coder",
            instruction="执行受控外援任务",
        )
    finally:
        current_process_launch.reset(token)

    assert output == "已继承安全边界"
    assert seen[0].params["_security_process_launch"] is launch


async def test_team_delegate_inherits_workspace_root_from_security_launch(tmp_path):
    session_cwd = tmp_path / "external-session"
    session_cwd.mkdir()
    seen: list[Envelope] = []

    class RecordingAgent:
        async def run(self, envelope):
            seen.append(envelope)
            yield ResponseChunk.final(envelope.request_id, "已继承工作空间根")

    launch = ProcessLaunch(
        PermissionProfile(
            PermissionProfileKind.MANAGED,
            filesystem=(FilesystemEntry(tmp_path, FilesystemAccess.READ_WRITE),),
        ),
        ("native-runtime",),
    )
    launch_token = current_process_launch.set(launch)
    cwd_token = current_agent_workdir.set(str(session_cwd))
    try:
        output = await run_delegate_to_teammate(
            {"coder": RecordingAgent()},
            InMemoryTaskManager(),
            "team-workspace-context-s1",
            member="coder",
            instruction="执行受控外援任务",
        )
    finally:
        current_agent_workdir.reset(cwd_token)
        current_process_launch.reset(launch_token)

    assert output == "已继承工作空间根"
    assert seen[0].params["workspace_root_path"] == str(tmp_path.resolve())


async def test_required_workflow_delegate_waits_for_structured_acceptance_before_completion():
    tm, _ = _team()
    tm.create_plan(
        "acceptance-owned-s1",
        goal="实现功能",
        nodes=[{"id": "build_1", "title": "编码实现", "assignee": "coder"}],
        edges=[],
    )

    await tm.request_delegate(
        "acceptance-owned-s1",
        member="coder",
        instruction="算 1+1",
        plan_node_id="build_1",
        wait_for_result=True,
        finalize_plan_node=False,
    )

    node = tm.read_plan("acceptance-owned-s1")["plan"]["nodes"][0]
    assert node["status"] == "in_progress"
    assert node["delegate_task_id"]


async def test_team_request_delegate_returns_before_slow_worker_finishes():
    gate = asyncio.Event()

    class SlowControlPlaneProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            sys = messages[0].content
            if "Leader（队长）" in sys:
                return ChatResponse(text="unused")
            await gate.wait()
            return ChatResponse(text="coder慢任务完成")

    tm, tasks = _team(SlowControlPlaneProvider())
    result = await asyncio.wait_for(
        tm.request_delegate(
            "mcp_team_async_s1",
            member="coder",
            instruction="慢任务",
            requester_member_id="external_agent",
        ),
        timeout=0.5,
    )
    assert result["ok"] is True
    assert result["status"] == "in_progress"
    assert tasks.list("mcp_team_async_s1")[0]["status"] == "in_progress"

    gate.set()
    for _ in range(30):
        board = tasks.list("mcp_team_async_s1")
        if board and board[0]["status"] == "done":
            break
        await asyncio.sleep(0.01)
    assert tasks.list("mcp_team_async_s1")[0]["status"] == "done"


async def test_team_plan_create_and_delegate_binding():
    tm, tasks = _team()
    created = tm.create_plan(
        "plan_team_s1",
        goal="完成一个小功能",
        nodes=[
            {"id": "design", "title": "设计方案", "detail": "输出方案", "assignee": "researcher"},
            {"id": "code", "title": "编码实现", "detail": "实现代码", "assignee": "coder"},
        ],
        edges=[["design", "code"]],
    )
    assert created["ok"] is True
    plan = created["plan"]
    assert [node["node_id"] for node in plan["nodes"]] == ["design", "code"]
    assert plan["edges"] == [{"parent_id": "design", "child_id": "code"}]

    result = await tm.request_delegate(
        "plan_team_s1",
        member="coder",
        instruction="算 1+1",
        requester_member_id="external_agent",
        plan_node_id="code",
    )
    assert result["ok"] is True
    assert result["status"] == "in_progress"
    board = tasks.list("plan_team_s1")
    assert len(board) == 1
    for _ in range(20):
        current = tm.read_plan("plan_team_s1")["plan"]
        code_node = next(node for node in current["nodes"] if node["node_id"] == "code")
        if code_node["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    current = tm.read_plan("plan_team_s1")["plan"]
    code_node = next(node for node in current["nodes"] if node["node_id"] == "code")
    assert code_node["status"] == "completed"
    assert code_node["delegate_task_id"] == board[0]["id"]
    assert "coder算出" in code_node["result_summary"]


async def test_leader_request_plan_change_adds_dag_node_and_requeues_summary():
    tm, _ = _team()
    team = tm._build_team("plan_change_s1")
    tm.create_plan(
        "plan_change_s1",
        goal="补充调研后汇总",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "leader_summary", "title": "Leader 汇总", "assignee": "leader"},
        ],
        edges=[["leader_plan", "leader_summary"]],
    )
    tm.update_plan_node("plan_change_s1", node_id="leader_plan", status="completed", result_summary="已承接")
    tm.update_plan_node("plan_change_s1", node_id="leader_summary", status="in_progress")

    assert "request_plan_change" in team.leader.registry.names()
    assert "request_plan_change" not in team.direct_leader.registry.names()
    assert "request_plan_change" not in team.teammates["researcher"].registry.names()

    token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall(
                "plan-change-1",
                "request_plan_change",
                {
                    "change_type": "add_node",
                    "node_id": "extra_research",
                    "title": "补充调研",
                    "detail": "补充必要事实并提交给 Leader 汇总。",
                    "assignee": "researcher",
                    "required_capabilities": ["research", "analysis"],
                    "depends_on": ["leader_plan"],
                    "before": ["leader_summary"],
                    "reason": "当前 DAG 缺少调研事实。",
                },
            )
        )
    finally:
        current_agent_id.reset(token)

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["ok"] is True
    assert payload["node"]["node_id"] == "extra_research"
    assert payload["requeued_nodes"] == ["leader_summary"]

    plan = tm.read_plan("plan_change_s1")["plan"]
    nodes = {node["node_id"]: node for node in plan["nodes"]}
    assert nodes["extra_research"]["status"] == "pending"
    assert nodes["extra_research"]["metadata"]["required_capabilities"] == ["research", "analysis"]
    assert nodes["extra_research"]["metadata"]["capability_source"] == "leader_plan_change"
    assert nodes["leader_summary"]["status"] == "pending"
    assert {"parent_id": "leader_plan", "child_id": "extra_research"} in plan["edges"]
    assert {"parent_id": "extra_research", "child_id": "leader_summary"} in plan["edges"]


async def test_leader_request_plan_change_rejects_unknown_member_without_mutation():
    tm, _ = _team()
    team = tm._build_team("plan_change_s2")
    tm.create_plan(
        "plan_change_s2",
        goal="补充调研后汇总",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "leader_summary", "title": "Leader 汇总", "assignee": "leader"},
        ],
        edges=[["leader_plan", "leader_summary"]],
    )

    token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall(
                "plan-change-bad-member",
                "request_plan_change",
                {
                    "change_type": "add_node",
                    "title": "补充调研",
                    "detail": "补充必要事实。",
                    "assignee": "ghost",
                    "required_capabilities": ["research"],
                    "depends_on": ["leader_plan"],
                    "before": ["leader_summary"],
                },
            )
        )
    finally:
        current_agent_id.reset(token)

    assert result.is_error
    assert "assignee" in result.content
    assert "ghost" in result.content
    assert "not one of" in result.content
    plan = tm.read_plan("plan_change_s2")["plan"]
    assert [node["node_id"] for node in plan["nodes"]] == ["leader_plan", "leader_summary"]
    assert plan["edges"] == [{"parent_id": "leader_plan", "child_id": "leader_summary"}]


async def test_leader_request_plan_change_requires_capability_contract():
    tm, _ = _team()
    team = tm._build_team("plan_change_missing_capabilities")
    tm.create_plan(
        "plan_change_missing_capabilities",
        goal="补充调研后汇总",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "leader_summary", "title": "Leader 汇总", "assignee": "leader"},
        ],
        edges=[["leader_plan", "leader_summary"]],
    )

    token = current_agent_id.set("leader")
    try:
        result = await team.leader.registry.execute(
            ToolCall(
                "plan-change-missing-capabilities",
                "request_plan_change",
                {
                    "change_type": "add_node",
                    "title": "补充调研",
                    "detail": "补充必要事实。",
                    "assignee": "researcher",
                },
            )
        )
    finally:
        current_agent_id.reset(token)

    assert result.is_error
    assert "required_capabilities" in result.content
    plan = tm.read_plan("plan_change_missing_capabilities")["plan"]
    assert [node["node_id"] for node in plan["nodes"]] == ["leader_plan", "leader_summary"]


async def test_runtime_executes_added_node_after_leader_plan_change():
    class PlanChangeProvider(LLMProvider):
        def __init__(self):
            self.summary_calls = 0

        async def chat(self, messages, tools=None):
            sys = messages[0].content if messages else ""
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "Leader（队长）" in sys:
                if "Leader 汇总" in last_user:
                    self.summary_calls += 1
                    if self.summary_calls == 1:
                        return ChatResponse(tool_calls=[
                            ToolCall(
                                "pc1",
                                "request_plan_change",
                                {
                                    "change_type": "add_node",
                                    "node_id": "extra_research",
                                    "title": "补充调研",
                                    "detail": "补充必要事实并提交给 Leader 汇总。",
                                    "assignee": "researcher",
                                    "required_capabilities": ["research", "analysis"],
                                    "depends_on": ["leader_plan"],
                                    "before": ["leader_summary"],
                                    "reason": "当前 DAG 缺少调研事实。",
                                },
                            )
                        ])
                    if self.summary_calls == 2:
                        return ChatResponse(text="已新增补充调研节点，等待 Runtime 继续执行。")
                    return ChatResponse(text="最终汇总：已纳入 researcher 调研完成。")
                return ChatResponse(text="Leader 控制节点完成。")
            return ChatResponse(text="researcher 调研完成")

        async def stream_chat(self, messages, tools=None):
            resp = await self.chat(messages, tools)
            if resp.text:
                yield StreamChunk(delta_text=resp.text)
            yield StreamChunk(done=True, tool_calls=resp.tool_calls, finish_reason=resp.finish_reason)

    provider = PlanChangeProvider()
    tm, tasks = _team(provider)
    team = tm._build_team("plan_change_runtime_s1")
    plan = TeamPlan(team_session_id="plan_change_runtime_s1", goal="补充调研后汇总")
    plan.nodes["leader_plan"] = TeamPlanNode(
        node_id="leader_plan",
        title="Leader 拆解",
        assignee="leader",
        status="completed",
        result_summary="已承接",
    )
    plan.nodes["leader_summary"] = TeamPlanNode(
        node_id="leader_summary",
        title="Leader 汇总",
        assignee="leader",
    )
    plan.edges = [TeamPlanEdge(parent_id="leader_plan", child_id="leader_summary")]
    tm._plans[tm._key("plan_change_runtime_s1", "local")] = plan

    chunks = [
        chunk async for chunk in tm._run_required_workflow(
            Envelope.of("补充调研后汇总", session_id="plan_change_runtime_s1", mode="team"),
            team=team,
            external_team_id="",
        )
    ]

    final = next(chunk.body["text"] for chunk in chunks if chunk.kind == "final")
    assert "最终汇总" in final
    assert provider.summary_calls >= 3
    current = tm.read_plan("plan_change_runtime_s1")["plan"]
    nodes = {node["node_id"]: node for node in current["nodes"]}
    assert nodes["extra_research"]["status"] == "completed"
    assert nodes["leader_summary"]["status"] == "completed"
    assert "researcher 调研完成" in nodes["extra_research"]["result_summary"]
    assert [task["assignee"] for task in tasks.list("plan_change_runtime_s1")] == ["researcher"]


def test_team_builds_heterogeneous_member_with_member_session_binding():
    class DummyExternalStore:
        pass

    config = Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "kimi",
                    "name": "kimi",
                    "role": "ACP 外部智能体",
                    "executor": "acp",
                    "external_agent_id": "agent_kimi",
                }
            ]
        },
    )
    tm, _ = _team(config=config)
    tm.external_store = DummyExternalStore()

    team = tm._build_team("team_s1")
    kimi = team.teammates["kimi"]

    assert team.session.member_sessions["kimi"].member_session_id == "team_s1::kimi"
    assert isinstance(kimi.executor, AcpExecutor)
    assert kimi.executor.config.external_agent_id == "agent_kimi"
    assert kimi.executor.config.crew_session_id == "team_s1::kimi"
    assert kimi.executor.config.display_session_id == "team_s1"
    assert kimi.executor.config.control_session_id == "team_s1"

    turn_team = tm._build_team("web_s1::turn::req_abc")
    turn_kimi = turn_team.teammates["kimi"]
    assert turn_kimi.executor.config.crew_session_id == "web_s1::turn::req_abc::kimi"
    assert turn_kimi.executor.config.display_session_id == "web_s1"
    assert turn_kimi.executor.config.control_session_id == "web_s1::turn::req_abc"


def test_team_uses_external_team_selected_leader():
    class DummyExternalStore:
        def get_team(self, team_id: str, *, owner_account_id: str = ""):
            assert team_id == "team_ext"
            return {
                "id": "team_ext",
                "leader_agent_id": "agent_kk",
                "members": [
                    {
                        "agent_id": "agent_hh",
                        "agent_name": "hh",
                        "role": "负责编码实现",
                    },
                    {
                        "agent_id": "agent_kk",
                        "agent_name": "kk",
                        "role": "负责统筹推进与测试验收",
                    },
                ],
            }

    tm, _ = _team()
    tm.external_store = DummyExternalStore()

    team = tm._build_team("team_selected_leader", external_team_id="team_ext")

    assert team.session.leader_member_id == "leader"
    assert "hh" in team.teammates
    assert "kk" not in team.teammates
    assert isinstance(team.leader.executor, AcpExecutor)
    assert team.leader.executor.config.external_agent_id == "agent_kk"
    assert team.leader.executor.config.crew_session_id == "team_selected_leader::leader"
    assert team.leader.executor.config.display_session_id == "team_selected_leader"
    assert team.leader.executor.config.control_session_id == "team_selected_leader"

    nodes, edges = tm._default_workflow_nodes(
        team,
        "开发一个贪吃蛇游戏",
        team_spec=_structured_team_spec("开发一个贪吃蛇游戏", capabilities=["implementation"], workflow_lanes=("build",)),
    )
    assert [node["id"] for node in nodes] == ["leader_plan", "build_design_1", "build_1", "leader_review", "leader_summary"]
    assert nodes[1]["assignee"] == "hh"
    assert nodes[1]["metadata"]["workflow_lane"] == "design"
    assert nodes[3]["title"] == "Leader 审阅方案：贪吃蛇游戏"
    assert ["build_design_1", "leader_review"] in edges
    assert ["leader_review", "build_1"] in edges


def test_external_team_projects_confirmed_formation_responsibility_into_runtime():
    responsibility = {
        "mission": "承担团队的需求范围、规则定义和验收标准工作",
        "boundaries": ["不改变用户确认范围"],
        "deliverables": ["需求范围", "玩法规则", "验收清单"],
        "collaboration": "向 Leader 提交需求和验收边界",
    }

    class DummyExternalStore:
        def get_team(self, team_id: str, *, owner_account_id: str = ""):
            assert team_id == "team_ext"
            return {
                "id": "team_ext",
                "leader_agent_id": CREW_BUILTIN_AGENT_ID,
                "formation_plan": {
                    "version": 1,
                    "leader_agent_id": CREW_BUILTIN_AGENT_ID,
                    "members": [
                        {
                            "agent_id": CREW_BUILTIN_AGENT_ID,
                            "role_key": "project_manager",
                            "responsibility": {
                                "mission": "负责团队规划与验收",
                                "boundaries": [],
                                "deliverables": ["团队计划"],
                                "collaboration": "负责团队控制面",
                            },
                        },
                        {
                            "agent_id": "agent_cc",
                            "role_key": "product_manager",
                            "responsibility": responsibility,
                            "responsibility_markdown": "### 产品经理 - cc",
                            "locked": True,
                        },
                    ],
                },
                "members": [
                    {
                        "agent_id": CREW_BUILTIN_AGENT_ID,
                        "agent_name": "Crew 内置智能体",
                        "role": "负责团队规划与验收",
                        "role_key": "project_manager",
                        "role_label": "项目经理",
                        "workflow_lane": "lead",
                        "capabilities": ["planning"],
                    },
                    {
                        "agent_id": "agent_cc",
                        "agent_name": "cc",
                        "role": "### 产品经理 - cc",
                        "role_key": "product_manager",
                        "role_label": "产品经理",
                        "workflow_lane": "plan",
                        "capabilities": ["requirements", "analysis"],
                    },
                ],
            }

    tm, _ = _team()
    tm.external_store = DummyExternalStore()

    team = tm._build_team("formation-runtime", external_team_id="team_ext")
    cc_spec = team.members["cc"]
    assert cc_spec.metadata["formation_plan_version"] == 1
    assert cc_spec.metadata["formation_responsibility"] == responsibility
    assert cc_spec.metadata["formation_locked"] is True

    nodes, _ = tm._default_workflow_nodes(team, "设计一个贪吃蛇游戏")
    plan_node = next(node for node in nodes if node["assignee"] == "cc")
    assert plan_node["title"].startswith("需求与验收：")
    assert plan_node["metadata"]["expected_outputs"] == ["需求范围", "玩法规则", "验收清单"]


def test_team_external_team_load_failure_does_not_fallback_to_default_team():
    class BrokenExternalStore:
        def get_team(self, team_id: str, *, owner_account_id: str = ""):
            assert team_id == "team_ext"
            raise RuntimeError("schema drift")

    tm, _ = _team()
    tm.external_store = BrokenExternalStore()

    with pytest.raises(ToolError, match="读取外部团队失败"):
        tm._build_team("team_selected_leader", external_team_id="team_ext")


def test_team_uses_crew_builtin_as_regular_member():
    class DummyExternalStore:
        def get_team(self, team_id: str, *, owner_account_id: str = ""):
            assert team_id == "team_ext"
            return {
                "id": "team_ext",
                "leader_agent_id": "agent_kk",
                "members": [
                    {
                        "agent_id": "agent_kk",
                        "agent_name": "kk",
                        "role": "负责统筹推进与测试验收",
                    },
                    {
                        "agent_id": CREW_BUILTIN_AGENT_ID,
                        "agent_name": "Crew 内置智能体",
                        "agent_provider": "crew",
                        "role": "负责内部拆解补位和汇总辅助",
                    },
                ],
            }

    tm, _ = _team()
    tm.external_store = DummyExternalStore()

    team = tm._build_team("team_builtin_member", external_team_id="team_ext")

    assert team.session.leader_member_id == "leader"
    assert CREW_BUILTIN_AGENT_ID in team.teammates
    crew_member = team.teammates[CREW_BUILTIN_AGENT_ID]
    assert isinstance(crew_member.executor, BuiltinExecutor)
    assert "ask_followup_question" not in crew_member.registry.names()
    assert "team_mention" in crew_member.registry.names()
    assert isinstance(team.leader.executor, AcpExecutor)
    assert team.leader.executor.config.external_agent_id == "agent_kk"


def test_team_interrupt_only_stops_current_team_session_children():
    tm, _ = _team()

    class InterruptibleAgent:
        def __init__(self):
            self.messages: list[str | None] = []

        def interrupt(self, message=None):
            self.messages.append(message)

    leader_a = InterruptibleAgent()
    leader_b = InterruptibleAgent()
    child_a = InterruptibleAgent()
    child_b = InterruptibleAgent()
    tm._teams[tm._key("team-a", "local")] = SimpleNamespace(leader=leader_a)
    tm._teams[tm._key("team-b", "local")] = SimpleNamespace(leader=leader_b)
    tm._mark_child_active({
        "child_id": "task-a::kk",
        "parent_session_id": "team-a",
        "owner_account_id": "local",
        "member": "kk",
        "agent": child_a,
    })
    tm._mark_child_active({
        "child_id": "task-b::kk",
        "parent_session_id": "team-b",
        "owner_account_id": "local",
        "member": "kk",
        "agent": child_b,
    })

    assert tm.interrupt("team-a", "stop current", owner_account_id="local") is True

    assert leader_a.messages == ["stop current"]
    assert child_a.messages == ["stop current"]
    assert leader_b.messages == []
    assert child_b.messages == []
    assert tm.active_children("team-a", owner_account_id="local") == []
    remaining = tm.active_children("team-b", owner_account_id="local")
    assert len(remaining) == 1
    assert "agent" not in remaining[0]
    assert "owner_account_id" not in remaining[0]


def test_team_member_switch_state_is_scoped_to_member_and_visible_session():
    tm, _ = _team()
    child = object()
    tm._mark_child_active({
        "child_id": "task-a::coder",
        "parent_session_id": "team-a::turn::req_1",
        "owner_account_id": "local",
        "member": "coder",
        "agent": child,
    })
    tm._mark_child_active({
        "child_id": "task-b::reviewer",
        "parent_session_id": "team-b",
        "owner_account_id": "local",
        "member": "reviewer",
        "agent": object(),
    })

    coder = tm.team_member_switch_state("team-a", "coder", owner_account_id="local")
    reviewer = tm.team_member_switch_state("team-a", "reviewer", owner_account_id="local")

    assert coder["status"] == "running"
    assert coder["active_task_count"] == 1
    assert coder["active_children"][0]["member"] == "coder"
    assert "agent" not in coder["active_children"][0]
    assert reviewer == {"status": "idle", "active_task_count": 0, "active_children": []}


def test_team_interrupt_parent_cancels_sidechain_plan():
    tm, _ = _team()
    session_id = "web_parent::turn::req_1"
    tm.create_plan(
        session_id,
        goal="测试停止",
        nodes=[
            {"id": "leader_plan", "title": "Leader 拆解", "assignee": "leader"},
            {"id": "build_1", "title": "实现", "assignee": "coder"},
        ],
        edges=[["leader_plan", "build_1"]],
    )

    assert tm.interrupt("web_parent", "已停止当前回复") is True
    plan = tm.read_plan(session_id)["plan"]
    assert plan["status"] == "cancelled"
    by_id = {node["node_id"]: node for node in plan["nodes"]}
    assert by_id["leader_plan"]["status"] == "cancelled"
    assert by_id["build_1"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_team_interrupt_cancels_all_tracked_member_tasks_for_session():
    tm, _ = _team()

    class InterruptibleAgent:
        def __init__(self):
            self.messages: list[str | None] = []

        def interrupt(self, message=None):
            self.messages.append(message)

    leader = InterruptibleAgent()
    direct_leader = InterruptibleAgent()
    tm._teams[tm._key("team-stop", "local")] = SimpleNamespace(
        leader=leader,
        direct_leader=direct_leader,
    )
    stopped = asyncio.Event()

    async def member_work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    member_task = asyncio.create_task(member_work())
    other_task = asyncio.create_task(asyncio.Event().wait())
    tm._track_delegate_task("team-stop", "local", member_task)
    tm._track_delegate_task("other-team", "local", other_task)
    try:
        await asyncio.sleep(0)
        assert tm.interrupt("team-stop", "用户停止", owner_account_id="local") is True
        await asyncio.sleep(0)
        assert member_task.cancelled()
        assert stopped.is_set()
        assert not other_task.done()
        assert leader.messages == ["用户停止"]
        assert direct_leader.messages == ["用户停止"]
    finally:
        other_task.cancel()
        await asyncio.gather(other_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_team_interrupt_cancels_tracked_task_even_if_team_object_was_released():
    tm, _ = _team()
    stopped = asyncio.Event()

    async def member_work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    member_task = asyncio.create_task(member_work())
    tm._track_delegate_task("released-team", "local", member_task)
    await asyncio.sleep(0)

    assert tm.interrupt("released-team", "用户停止", owner_account_id="local") is True
    await asyncio.sleep(0)
    assert member_task.cancelled()
    assert stopped.is_set()


def test_node_execution_assessment_rejects_unrecovered_tool_failure_without_artifact():
    node = TeamPlanNode(
        node_id="build_1",
        title="实现小游戏",
        assignee="agent-a",
        metadata={"workflow_lane": "build"},
    )
    assessment = InProcessTeamManager._assess_node_execution(
        node,
        runtime_events=[{
            "event_type": "tool",
            "tool_call": {
                "name": "write_file",
                "status": "error",
                "result": "permission denied",
            },
        }],
        artifact_refs=[],
        changed_paths=set(),
        result_contract={"status_signal": "unknown"},
    )

    assert assessment.execution_status == "blocked"
    assert assessment.acceptance_status == "blocked"
    assert assessment.failed_tools == ("write_file",)


def test_node_execution_assessment_accepts_recovered_write_with_material_evidence():
    node = TeamPlanNode(
        node_id="build_1",
        title="实现小游戏",
        assignee="agent-a",
        metadata={"workflow_lane": "build"},
    )
    assessment = InProcessTeamManager._assess_node_execution(
        node,
        runtime_events=[{
            "event_type": "tool",
            "tool_call": {"name": "write_file", "status": "error", "result": "first attempt failed"},
        }],
        artifact_refs=["/tmp/snake.html"],
        changed_paths={"/tmp/snake.html"},
        result_contract={"status_signal": "unknown"},
    )

    assert assessment.execution_status == "completed"
    assert assessment.acceptance_status == "pass"
    assert assessment.artifact_count == 1


def test_node_execution_assessment_uses_structured_runtime_submission_status():
    node = TeamPlanNode(
        node_id="verify_1",
        title="运行验证",
        assignee="agent-a",
        metadata={"workflow_lane": "verify"},
    )
    events = [
        {
            "event_type": "tool",
            "tool_call": {
                "id": "mention-1",
                "name": "mcp__crew-interaction__team_mention",
                "status": "running",
                "arguments": json.dumps({
                    "to": ["leader"],
                    "intent": "submit",
                    "content": "本轮通过；历史失败已修复。",
                    "node_id": "verify_1",
                    "result_status": "pass",
                }),
            },
        },
        {
            "event_type": "tool",
            "tool_call": {
                "id": "mention-1",
                "name": "mcp__crew-interaction__team_mention",
                "status": "done",
                "result": '{"ok":true}',
            },
        },
    ]

    result_status = InProcessTeamManager._runtime_result_status(node, events)
    assessment = InProcessTeamManager._assess_node_execution(
        node,
        runtime_events=events,
        artifact_refs=[],
        changed_paths=set(),
        result_contract={"status_signal": result_status or "unknown"},
    )

    assert result_status == "pass"
    assert assessment.execution_status == "completed"
    assert assessment.acceptance_status == "pass"


def test_node_execution_assessment_does_not_block_plan_artifact_for_risk_section():
    node = TeamPlanNode(
        node_id="qa_refine_1",
        title="测试方案复核",
        assignee="agent-a",
        metadata={"workflow_lane": "plan"},
    )
    assessment = InProcessTeamManager._assess_node_execution(
        node,
        runtime_events=[],
        artifact_refs=["/tmp/测试方案_复核版.md"],
        changed_paths=set(),
        result_contract={
            "status_signal": "blocked",
            "risk": "风险/阻塞：后续验证节点需依赖真实回传。",
        },
    )

    assert assessment.execution_status == "completed"
    assert assessment.acceptance_status == "pass"
    assert assessment.artifact_count == 1


def test_testing_goal_without_build_member_does_not_create_build_node():
    tm, _ = _team(config=Config(
        max_iterations=5,
        team_config={
            "members": [
                {
                    "member_id": "qa",
                    "name": "QA",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                },
                {
                    "member_id": "security",
                    "name": "Security",
                    "role": "负责安全测试",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "security_engineer"},
                },
            ]
        },
    ))
    team = tm._build_team("test-only")

    collaboration_spec = build_team_spec(_structured_team_spec(
        "测试一下团队协作",
        capabilities=["testing", "verification"],
        intent="testing",
        workflow_lanes=("verify",),
    ))
    assert collaboration_spec.task_profile["intent"] == "testing"
    assert collaboration_spec.team_requirements["workflow_lanes"] == ["verify"]

    nodes, edges = tm._default_workflow_nodes(
        team,
        "测试一下团队协作",
        team_spec=collaboration_spec.to_dict(),
    )

    node_ids = {node["id"] for node in nodes}
    assert not any(node_id.startswith("build_") for node_id in node_ids)
    assert {
        "qa_engineer_plan_1",
        "security_engineer_plan_2",
        "leader_review",
        "qa_engineer_verify_1",
        "security_engineer_verify_2",
        "leader_summary",
    } <= node_ids
    by_id = {node["id"]: node for node in nodes}
    assert by_id["qa_engineer_plan_1"]["title"].startswith("测试方案：")
    assert by_id["security_engineer_plan_2"]["title"].startswith("安全方案：")
    assert by_id["qa_engineer_verify_1"]["title"].startswith("测试验证：")
    assert by_id["security_engineer_verify_2"]["title"].startswith("安全验证：")
    assert ["qa_engineer_plan_1", "leader_review"] in edges
    assert ["security_engineer_plan_2", "leader_review"] in edges
    assert ["leader_review", "qa_engineer_verify_1"] in edges
    assert ["leader_review", "security_engineer_verify_2"] in edges
    assert ["qa_engineer_verify_1", "leader_summary"] in edges
    assert ["security_engineer_verify_2", "leader_summary"] in edges


def test_unstructured_goal_profile_does_not_embed_execution_mode():
    spec = build_team_spec({"goal": "有贪吃蛇小游戏么"})

    assert spec.task_profile["intent"] == "mixed"
    assert spec.execution_profile == {}
    assert spec.team_requirements["workflow_lanes"] == []
    assert spec.uncertainty == "high"


def test_team_spec_rejects_legacy_execution_flags():
    with pytest.raises(ValueError, match="needs_build"):
        build_team_spec({
            "goal": "完成实现、验证和文档交付",
            "execution_profile": {"needs_build": True},
        })


def test_team_spec_keeps_task_semantics_out_of_execution_profile():
    spec = build_team_spec({
        "goal": "输出研究方案",
        "task_profile": {
            "intent": "research",
            "complexity": "multi_role",
            "deliverable_shape": "proposal",
        },
        "execution_profile": {
            "requested_mode": "standard",
            "budget": {"max_nodes": 5},
        },
        "team_requirements": {"workflow_lanes": ["plan", "docs"]},
    })

    assert spec.task_profile == {
        "intent": "research",
        "complexity": "multi_role",
        "deliverable_shape": "proposal",
    }
    assert spec.execution_profile == {
        "requested_mode": "standard",
        "budget": {"max_nodes": 5},
    }
    assert spec.team_requirements["workflow_lanes"] == ["plan", "docs"]


def test_team_graph_planner_emits_runtime_only_execution_profile():
    tm, _ = _team(config=Config(
        team_config={
            "members": [{
                "member_id": "dev",
                "name": "dev",
                "role": "负责开发实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
            }],
        },
    ))
    team = tm._build_team("runtime-profile-contract")
    plan = TeamGraphPlanner().plan(
        team,
        "开发一个小工具",
        execution_profile={
            "requested_mode": "fast",
        },
        team_spec=_structured_team_spec(
            "开发一个小工具",
            capabilities=["implementation"],
            workflow_lanes=("build",),
        ),
    )

    assert plan.spec.task_profile["intent"] == "implementation"
    assert plan.spec.team_requirements["workflow_lanes"] == ["build"]
    assert plan.spec.execution_profile == {
        "requested_mode": "fast",
        "selected_mode": "fast",
        "budget": {},
    }


def test_planning_decision_rejects_work_unit_without_capability_contract():
    with pytest.raises(ValueError, match="missing required_capabilities"):
        coerce_planning_decision({
            "work_units": [{
                "id": "build_api",
                "objective": "实现接口",
                "expected_output": "可运行接口",
                "required_capabilities": [],
            }],
        })


def test_capability_coverage_is_shared_and_deterministic():
    profile = AgentProfile(
        agent_id="kk",
        capabilities={
            "implementation": CapabilityAssessment(0.9, 0.9),
            "testing": CapabilityAssessment(0.2, 0.8),
        },
    )

    covered = evaluate_capability_coverage(
        ["implementation"],
        {"kk": profile},
        assigned_agent_ids=["kk"],
    )
    missing = evaluate_capability_coverage(
        ["testing"],
        {"kk": profile},
        assigned_agent_ids=["kk"],
    )

    assert covered.status == "covered"
    assert covered.covered_by == {"implementation": ["kk"]}
    assert missing.status == "missing"
    assert missing.missing == ["testing"]


def test_dag_admission_reassigns_node_to_existing_member_with_full_coverage():
    tm, _ = _team(config=Config(
        team_config={
            "members": [
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责开发",
                    "executor": "builtin",
                    "capabilities": ["implementation"],
                    "metadata": {"workflow_lane": "build"},
                },
                {
                    "member_id": "hh",
                    "name": "hh",
                    "role": "负责测试",
                    "executor": "builtin",
                    "capabilities": ["testing"],
                    "metadata": {"workflow_lane": "verify"},
                },
            ],
        },
    ))
    team = tm._build_team("capability-admission")
    graph_plan = TeamGraphPlanner().plan(
        team,
        "执行测试",
        execution_profile={"requested_mode": "ai"},
        team_spec=_structured_team_spec(
            "执行测试",
            capabilities=["testing"],
            workflow_lanes=("verify",),
        ),
    )

    # Fast/Standard compilers choose by the member capability assignment. The
    # direct admission helper is also covered by the AI planner path below;
    # this assertion protects the persisted node contract.
    verify_nodes = [
        node for node in graph_plan.nodes
        if node["metadata"].get("required_capabilities") == ["testing"]
    ]
    assert verify_nodes
    assert all(node["assignee"] == "hh" for node in verify_nodes)


def test_dag_admission_records_uncovered_node_instead_of_hiding_it_in_runtime():
    tm, _ = _team(config=Config(
        team_config={
            "members": [
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责开发",
                    "executor": "builtin",
                    "capabilities": ["implementation"],
                    "metadata": {"workflow_lane": "build"},
                },
                {
                    "member_id": "hh",
                    "name": "hh",
                    "role": "负责分析",
                    "executor": "builtin",
                    "capabilities": ["analysis"],
                    "metadata": {"workflow_lane": "plan"},
                },
            ],
        },
    ))
    team = tm._build_team("capability-admission-gap")
    graph_plan = TeamGraphPlanner().plan(
        team,
        "做测试",
        execution_profile={"requested_mode": "fast"},
        team_spec=_structured_team_spec(
            "做测试",
            capabilities=["testing"],
            workflow_lanes=("verify",),
        ),
    )

    execute = next(node for node in graph_plan.nodes if node["id"] == "fast_execute")
    assert execute["metadata"]["capability_status"] == "missing"
    assert execute["metadata"]["capability_gap_source"] == "dag_admission"
    assert "testing" in execute["metadata"]["capability_coverage"]["missing"]


def test_dag_admission_reassigns_existing_member_and_refreshes_role_metadata():
    nodes, _, _ = _normalize_nodes_with_graph(
        goal="执行测试",
        raw_nodes=[{
            "id": "verify",
            "title": "验证实现",
            "detail": "验证实现结果",
            "assignee": "kk",
            "metadata": {
                "workflow_lane": "verify",
                "role_label": "开发成员",
                "role_key": "implementation",
                "required_capabilities": ["testing"],
            },
        }],
        raw_edges=[],
        valid_roles=["kk", "hh"],
        member_capabilities={"kk": ["implementation"], "hh": ["testing"]},
        member_metadata={
            "kk": {"role_label": "开发成员", "role_key": "implementation"},
            "hh": {"role_label": "测试成员", "role_key": "testing"},
        },
    )

    node = nodes[0]
    assert node["assignee"] == "hh"
    assert node["metadata"]["role_label"] == "测试成员"
    assert node["metadata"]["role_key"] == "testing"
    assert node["metadata"]["assignment_source"] == "existing_member_reassignment"
    assert node["metadata"]["previous_assignee"] == "kk"


def test_runtime_staffing_does_not_trigger_when_assigned_member_is_covered():
    tm, _ = _team(config=Config(
        team_config={
            "members": [{
                "member_id": "kk",
                "name": "kk",
                "role": "负责实现",
                "executor": "builtin",
                "capabilities": ["implementation"],
                "metadata": {"workflow_lane": "build"},
            }],
        },
    ))
    team = tm._build_team("runtime-covered")
    node = TeamPlanNode(
        node_id="build",
        title="实现",
        assignee="kk",
        metadata={
            "required_capabilities": ["implementation"],
            "runtime_staffing_trigger": "capability_gap",
        },
    )
    assert tm._runtime_staffing_trigger(
        team,
        node,
        owner_account_id="",
        max_attempts=2,
    ) is None


def test_runtime_blocking_marks_only_dependency_chain_and_clears_assignee():
    tm, _ = _team()
    blocked = TeamPlanNode(
        node_id="build",
        title="实现",
        assignee="kk",
        metadata={"required_capabilities": ["testing"]},
    )
    dependent = TeamPlanNode(node_id="verify", title="验证", assignee="hh")
    independent = TeamPlanNode(node_id="docs", title="整理说明", assignee="hh")
    plan = TeamPlan(
        team_session_id="runtime-blocking-scope",
        goal="完成任务",
        nodes={node.node_id: node for node in (blocked, dependent, independent)},
        edges=[TeamPlanEdge(parent_id="build", child_id="verify")],
    )
    request = RuntimeStaffingRequest(
        request_id="staffing_declined_scope",
        trigger_node_id="build",
        trigger_type="capability_gap",
        required_capabilities=["testing"],
        reason="缺少 testing",
        status="declined",
    )

    tm._mark_runtime_blocked(
        plan,
        blocked,
        owner_account_id="local",
        request=request,
        result_summary="用户拒绝补员，当前节点没有可执行主责，已阻塞。",
    )

    assert blocked.status == "blocked"
    assert blocked.assignee == ""
    assert blocked.metadata["previous_assignee"] == "kk"
    assert plan.status == "blocked"
    feasibility = blocked.metadata["runtime_blocking"]["feasibility"]
    assert feasibility["blocking_nodes"] == ["build"]
    assert feasibility["blocked_dependency_nodes"] == ["verify"]
    assert "docs" in feasibility["runnable_nodes"]
    assert dependent.status == "pending"
    assert independent.status == "pending"


def test_runtime_blocking_persists_unassigned_owner_to_team_board(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "runtime-blocked-board.db")
    tm, _ = _team(kanban_store=store)
    node = TeamPlanNode(
        node_id="build",
        title="实现",
        assignee="kk",
        metadata={"required_capabilities": ["testing"]},
    )
    plan = TeamPlan(team_session_id="runtime-blocked-board", goal="执行测试", nodes={"build": node})
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    tm._persist_team_plan(
        plan,
        owner_account_id="local",
        workflow_plan={
            "version": 1,
            "revision": 1,
            "nodes": [{"id": "build", "title": "实现", "assignee_id": "kk"}],
            "edges": [],
        },
    )
    request = RuntimeStaffingRequest(
        request_id="staffing_declined_board",
        trigger_node_id="build",
        trigger_type="capability_gap",
        required_capabilities=["testing"],
        reason="缺少 testing",
        status="declined",
    )

    tm._mark_runtime_blocked(
        plan,
        node,
        owner_account_id="local",
        request=request,
        result_summary="用户拒绝补员，当前节点没有可执行主责，已阻塞。",
    )

    projected = tm.task_projection_for_session("runtime-blocked-board", owner_account_id="local")
    assert projected[0]["assignee"] == ""
    assert projected[0]["status"] == "blocked"
    assert projected[0]["progress"]["runtime_blocking"]["status"] == "blocked"
    assert projected[0]["progress"]["previous_assignee"] == "kk"


def test_blocked_workflow_result_does_not_claim_completion():
    tm, _ = _team()
    plan = TeamPlan(team_session_id="blocked-result", goal="执行测试", status="blocked")
    plan.nodes = {
        "verify": TeamPlanNode(
            node_id="verify",
            title="测试验证",
            assignee="",
            status="blocked",
            result_summary="用户拒绝补员，当前节点没有可执行主责。",
            metadata={"runtime_blocking": {"status": "blocked"}},
        ),
    }

    result = tm._format_workflow_result(plan)

    assert result.startswith("团队工作流已阻塞")
    assert "团队工作流完成" not in result
    assert "主责：待分配" in result


@pytest.mark.asyncio
async def test_runtime_staffing_decline_blocks_node_without_fake_continuation(monkeypatch):
    tm, _ = _team()
    node = TeamPlanNode(
        node_id="build",
        title="实现",
        assignee="kk",
        metadata={"required_capabilities": ["testing"]},
    )
    plan = TeamPlan(team_session_id="runtime-decline", goal="执行测试", nodes={"build": node})
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    team = tm._build_team(plan.team_session_id)
    trigger = {
        "trigger_type": "capability_gap",
        "required_capabilities": ["testing"],
        "reason": "当前负责人 kk 未覆盖 testing",
    }

    async def fake_send(session_id, questions, **kwargs):
        return session_id, "runtime-decline-question"

    async def fake_wait(session_id, question_id, **kwargs):
        return [{"id": "runtime-decline-question", "answers": ["decline"]}]

    async def fake_status(*args, **kwargs):
        return None

    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)
    monkeypatch.setattr("crew.team.team_manager.wait_for_answer", fake_wait)
    monkeypatch.setattr("crew.team.team_manager.send_followup_status_to", fake_status)
    monkeypatch.setattr(
        tm,
        "_runtime_staffing_candidates",
        lambda *args, **kwargs: [{"candidate_type": "runtime", "model_id": "testing-model"}],
    )

    _, status = await tm._handle_runtime_staffing(
        Envelope.of("执行测试", session_id=plan.team_session_id, mode="team", user_id="local"),
        plan,
        node,
        team,
        trigger,
    )

    assert status == "declined"
    assert node.status == "blocked"
    assert node.assignee == ""
    assert node.metadata["runtime_staffing"]["status"] == "declined"
    assert node.metadata["runtime_blocking"]["reason"] == "staffing_declined"
    assert plan.status == "blocked"


def test_runtime_staffing_reassigns_existing_member_before_prompting_user():
    tm, _ = _team(config=Config(
        team_config={
            "members": [
                {"member_id": "kk", "name": "kk", "executor": "builtin", "capabilities": ["implementation"]},
                {"member_id": "hh", "name": "hh", "executor": "builtin", "capabilities": ["testing"]},
            ],
        },
    ))
    team = tm._build_team("runtime-reassign")
    node = TeamPlanNode(
        node_id="verify",
        title="验证",
        assignee="kk",
        metadata={"required_capabilities": ["testing"]},
    )
    plan = TeamPlan(team_session_id="runtime-reassign", goal="验证", nodes={"verify": node})
    tm._plans[tm._key(plan.team_session_id, "local")] = plan

    trigger = tm._runtime_staffing_trigger(team, node, owner_account_id="local", max_attempts=2)

    assert trigger is not None
    assert trigger["trigger_type"] == "existing_member_reassignment"
    assert trigger["replacement_assignee"] == "hh"


def _recovery_team_config() -> Config:
    return Config(team_config={
        "members": [
            {
                "member_id": "kk",
                "name": "kk",
                "executor": "builtin",
                "capabilities": ["implementation"],
            },
            {
                "member_id": "hh",
                "name": "hh",
                "executor": "builtin",
                "capabilities": ["testing"],
            },
        ],
    })


def _blocked_recovery_plan(session_id: str) -> TeamPlan:
    blocked = TeamPlanNode(
        node_id="verify",
        title="验证",
        detail="验证实现结果",
        assignee="kk",
        metadata={"required_capabilities": ["testing"]},
    )
    dependent = TeamPlanNode(node_id="summary", title="汇总", assignee="hh")
    independent = TeamPlanNode(node_id="docs", title="整理说明", assignee="hh")
    plan = TeamPlan(
        team_session_id=session_id,
        goal="完成验证和说明",
        nodes={node.node_id: node for node in (blocked, dependent, independent)},
        edges=[TeamPlanEdge(parent_id="verify", child_id="summary")],
    )
    return plan


def test_recover_plan_node_reassigns_persisted_blocked_node(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "team-recovery.db")
    tm, _ = _team(config=_recovery_team_config(), kanban_store=store)
    plan = _blocked_recovery_plan("persisted-recovery")
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    tm._persist_team_plan(
        plan,
        owner_account_id="local",
        workflow_plan={
            "version": 1,
            "revision": 1,
            "task": {"goal": plan.goal},
            "nodes": [
                {
                    "id": "verify",
                    "title": "验证",
                    "assignee_id": "kk",
                    "required_capabilities": ["testing"],
                },
                {"id": "summary", "title": "汇总", "assignee_id": "hh"},
                {"id": "docs", "title": "整理说明", "assignee_id": "hh"},
            ],
            "edges": [{"from": "verify", "to": "summary"}],
        },
    )
    tm._mark_runtime_blocked(
        plan,
        plan.nodes["verify"],
        owner_account_id="local",
        request=RuntimeStaffingRequest(
            request_id="recovery-staffing",
            trigger_node_id="verify",
            trigger_type="capability_gap",
            required_capabilities=["testing"],
            reason="kk 不具备 testing",
            status="declined",
        ),
        result_summary="用户拒绝补员，当前节点已阻塞。",
    )

    recovered_tm, _ = _team(config=_recovery_team_config(), kanban_store=store)
    result = recovered_tm.recover_plan_node(
        "persisted-recovery",
        node_id="verify",
        action="reassign",
        replacement_assignee="hh",
        owner_account_id="local",
    )

    node = result["node"]
    assert result["recovery_scheduled"] is False
    assert node["status"] == "pending"
    assert node["assignee"] == "hh"
    assert "runtime_blocking" not in node["metadata"]
    assert "blocked_by_nodes" not in result["plan"]["nodes"][1]["metadata"]
    projected = recovered_tm.task_projection_for_session("persisted-recovery", owner_account_id="local")
    verify = next(item for item in projected if item["progress"].get("plan_node_id") == "verify")
    assert verify["assignee"] == "hh"
    assert verify["status"] == "pending"


def test_recover_plan_node_reuses_capability_coverage_and_rejects_invalid_member():
    tm, _ = _team(config=_recovery_team_config())
    plan = _blocked_recovery_plan("recovery-capability")
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    tm._mark_runtime_blocked(
        plan,
        plan.nodes["verify"],
        owner_account_id="local",
        request=RuntimeStaffingRequest(
            request_id="recovery-capability-staffing",
            trigger_node_id="verify",
            trigger_type="capability_gap",
            required_capabilities=["testing"],
            reason="kk 不具备 testing",
            status="declined",
        ),
        result_summary="用户拒绝补员，当前节点已阻塞。",
    )

    with pytest.raises(ValueError, match="testing"):
        tm.recover_plan_node(
            "recovery-capability",
            node_id="verify",
            action="reassign",
            replacement_assignee="kk",
            owner_account_id="local",
        )


def test_recovery_retry_clears_stale_staffing_request_for_fresh_runtime_decision():
    tm, _ = _team(config=Config(team_config={
        "members": [{
            "member_id": "kk",
            "name": "kk",
            "executor": "builtin",
            "capabilities": ["implementation"],
        }],
    }))
    plan = _blocked_recovery_plan("recovery-fresh-staffing")
    node = plan.nodes["verify"]
    node.assignee = ""
    stale_request = RuntimeStaffingRequest(
        request_id="stale-declined-request",
        trigger_node_id=node.node_id,
        trigger_type="capability_gap",
        required_capabilities=["testing"],
        reason="kk 不具备 testing",
        status="declined",
        previous_assignee="kk",
    )
    node.metadata = {
        **dict(node.metadata),
        "previous_assignee": "kk",
        "runtime_staffing": stale_request.to_dict(),
        "runtime_blocking": {
            "status": "blocked",
            "reason": "staffing_declined",
            "previous_assignee": "kk",
        },
    }
    tm._plans[tm._key(plan.team_session_id, "local")] = plan

    result = tm.recover_plan_node(
        "recovery-fresh-staffing",
        node_id="verify",
        action="retry",
        owner_account_id="local",
    )

    recovered = result["node"]
    assert recovered["assignee"] == "kk"
    assert recovered["status"] == "pending"
    assert "runtime_staffing" not in recovered["metadata"]
    assert tm._runtime_staffing_request(plan.nodes["verify"]) is None
    trigger = tm._runtime_staffing_trigger(
        tm._build_team("recovery-fresh-staffing"),
        plan.nodes["verify"],
        owner_account_id="local",
        max_attempts=2,
    )
    assert trigger is not None
    assert trigger["trigger_type"] == "capability_gap"


@pytest.mark.asyncio
async def test_recovered_runtime_reuses_existing_plan_without_replanning(monkeypatch):
    tm, _ = _team(config=_recovery_team_config())
    plan = _blocked_recovery_plan("recovery-no-replan")
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    team = tm._build_team(plan.team_session_id, owner_account_id="local")

    async def fail_replan(*args, **kwargs):
        raise AssertionError("恢复执行不应重新调用 Planner")

    monkeypatch.setattr(tm.graph_planner, "plan_async", fail_replan)
    reused = await tm._ensure_runtime_plan_async(
        plan.team_session_id,
        team,
        "恢复执行",
        "",
        owner_account_id="local",
    )

    assert reused is plan
    assert list(reused.nodes) == ["verify", "summary", "docs"]


def test_abandon_recovery_keeps_dependent_node_blocked_but_preserves_independent_branch():
    tm, _ = _team(config=_recovery_team_config())
    plan = _blocked_recovery_plan("recovery-abandon")
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    tm._mark_runtime_blocked(
        plan,
        plan.nodes["verify"],
        owner_account_id="local",
        request=RuntimeStaffingRequest(
            request_id="recovery-abandon-staffing",
            trigger_node_id="verify",
            trigger_type="capability_gap",
            required_capabilities=["testing"],
            reason="kk 不具备 testing",
            status="declined",
        ),
        result_summary="用户拒绝补员，当前节点已阻塞。",
    )

    result = tm.recover_plan_node(
        "recovery-abandon",
        node_id="verify",
        action="abandon",
        owner_account_id="local",
    )

    assert result["node"]["metadata"]["runtime_blocking"]["reason"] == "node_abandoned"
    assert plan.status == "blocked"
    assert plan.nodes["summary"].metadata["blocked_by_nodes"] == ["verify"]
    assert "blocked_by_nodes" not in plan.nodes["docs"].metadata


@pytest.mark.asyncio
async def test_recovery_schedules_team_runtime_resume(monkeypatch):
    tm, _ = _team(config=_recovery_team_config())
    plan = _blocked_recovery_plan("recovery-schedule")
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    tm._mark_runtime_blocked(
        plan,
        plan.nodes["verify"],
        owner_account_id="local",
        request=RuntimeStaffingRequest(
            request_id="recovery-schedule-staffing",
            trigger_node_id="verify",
            trigger_type="capability_gap",
            required_capabilities=["testing"],
            reason="kk 不具备 testing",
            status="declined",
        ),
        result_summary="用户拒绝补员，当前节点已阻塞。",
    )
    resumed: list[str] = []

    async def fake_resume(session_id: str, owner_account_id: str) -> None:
        resumed.append(f"{owner_account_id}:{session_id}")

    monkeypatch.setattr(tm, "_resume_recovered_plan", fake_resume)
    result = tm.recover_plan_node(
        "recovery-schedule",
        node_id="verify",
        action="reassign",
        replacement_assignee="hh",
        owner_account_id="local",
    )
    await asyncio.sleep(0)

    assert result["recovery_scheduled"] is True
    assert resumed == ["local:recovery-schedule"]


def test_team_turn_router_returns_team_turn_decision():
    router = TeamTurnRouter()

    chat_decision = router.route("你好")
    fast_decision = router.route("有贪吃蛇小游戏么")

    assert isinstance(chat_decision, TeamTurnDecision)
    assert chat_decision.is_direct_chat is True
    assert chat_decision.diagnostics["turn_source"] == "simple_chat"
    assert isinstance(fast_decision, TeamTurnDecision)
    assert fast_decision.is_new_workflow is True
    assert fast_decision.execution_mode == "standard"
    assert fast_decision.diagnostics["turn_source"] == "task_profile"


async def test_ai_planner_uses_llm_single_dag():
    tm, _ = _team(JsonGraphProvider(), config=Config(
        max_iterations=5,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build", "role_key": "fullstack_developer"},
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                },
            ]
        },
    ))
    team = tm._build_team("standard-llm")

    graph_plan = await tm.graph_planner.plan_async(
        team,
        "开发登录接口",
        execution_profile={"requested_mode": "ai"},
        provider=tm.provider,
    )

    assert [node["id"] for node in graph_plan.nodes] == ["leader_plan", "api_build", "qa_verify", "leader_summary"]
    assert (graph_plan.nodes[0]["metadata"] or {})["plan_strategy"] == "ai_single_dag"
    assert (graph_plan.nodes[0]["metadata"] or {})["llm_planning_status"] == "success"
    assert isinstance((graph_plan.nodes[0]["metadata"] or {})["llm_planning_elapsed_ms"], int)
    assert graph_plan.workflow_plan["planning"]["requested_mode"] == "ai"
    assert graph_plan.workflow_plan["planning"]["selected_mode"] == "ai"
    assert graph_plan.nodes[1]["metadata"]["required_capabilities"] == ["backend", "implementation"]
    assert graph_plan.nodes[1]["metadata"]["capability_source"] == "ai_planner"
    assert graph_plan.workflow_plan["nodes"][1]["capability_source"] == "ai_planner"
    assert any("AI Planner" in note for note in graph_plan.planner_notes)


def test_team_spec_v3_records_requirements_deliverables_and_consent_policy():
    spec = build_team_spec({
        "goal": "指定当前团队开发登录接口并做安全测试，不要自动换人",
            "task_profile": {"intent": "implementation", "complexity": "multi_role"},
            "team_requirements": {
                "capabilities": ["backend", "implementation", "testing", "verification"],
                "workflow_lanes": ["build", "verify"],
        },
        "deliverables": [
            {"type": "code", "description": "可运行的接口实现"},
            {"type": "test_report", "description": "安全测试结果"},
        ],
        "success_criteria": ["登录接口可运行", "安全测试结果可追踪"],
        "policy": {
            "user_team_locked": True,
            "staffing_strategy": "suggest_only",
            "constraints": ["不得绕过用户指定团队直接换人或补员"],
            "consent_required_actions": ["不得绕过用户指定团队直接换人或补员"],
        },
        "risk_level": "high",
        "uncertainty": "low",
    })
    payload = spec.to_dict()

    assert spec.version == 3
    assert spec.policy["user_team_locked"] is True
    assert spec.policy["staffing_strategy"] == "suggest_only"
    assert "backend" in spec.team_requirements["capabilities"]
    assert spec.policy["consent_required_actions"]
    assert any("不得绕过用户指定团队" in item for item in spec.policy["consent_required_actions"])
    assert payload["task_profile"]["intent"] == "implementation"
    assert payload["team_requirements"]["workflow_lanes"] == ["build", "verify"]
    assert "backend" in payload["team_requirements"]["capabilities"]
    assert payload["policy"]["staffing_strategy"] == "suggest_only"
    assert {item["type"] for item in payload["deliverables"]} >= {"code", "test_report"}
    assert payload["success_criteria"]
    assert payload["risk_level"] == "high"
    assert payload["uncertainty"] == "low"
    assert "task_kind" not in payload
    assert "required_roles" not in payload
    assert "needs_build" not in payload


def test_team_spec_normalizes_explicit_capability_aliases_only():
    spec = build_team_spec({
        "goal": "输出一份可审阅的方案",
        "team_requirements": {"capabilities": ["qa", "docs", "qa"]},
        "deliverables": [{"type": "proposal", "description": "方案"}],
    })

    assert spec.team_requirements["capabilities"] == ["testing", "documentation"]
    assert spec.task_profile["intent"] == "mixed"
    assert spec.execution_profile == {}
    assert spec.deliverables == [{"type": "proposal", "description": "方案"}]


def test_team_graph_planner_warns_without_mutating_user_team():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["backend"],
                },
            ]
        },
    ))
    team = tm._build_team("graph-policy")
    before_members = list(team.members)

    graph_plan = TeamGraphPlanner().plan(
        team,
        "开发一个登录接口并完成测试验收",
        team_spec=_structured_team_spec(
            "开发一个登录接口并完成测试验收",
            capabilities=["backend", "implementation", "testing", "verification"],
            workflow_lanes=("build", "verify"),
        ),
    )

    assert list(team.members) == before_members
    assert graph_plan.policy_report.user_team_locked is True
    assert any(item.code in {"missing_role", "leader_testing_conflict"} for item in graph_plan.policy_report.warnings)
    assert graph_plan.nodes
    assert graph_plan.nodes[0]["metadata"]["execution_events"]
    assert all(
        event.get("event_title") != "节点承接"
        for node in graph_plan.nodes
        for event in node["metadata"].get("execution_events", [])
    )
    assert graph_plan.nodes[0]["metadata"]["agent_log_style"] == "agent_turn"


def test_team_graph_planner_standard_profile_uses_default_role_dag():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "planner",
                    "name": "planner",
                    "role": "负责方案规划",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["planning"],
                },
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
            ]
        },
    ))
    team = tm._build_team("standard-graph")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "开发一个登录接口并完成测试验收",
        execution_profile={"requested_mode": "standard"},
        team_spec=_structured_team_spec(
            "开发一个登录接口并完成测试验收",
            capabilities=["planning", "backend", "implementation", "testing", "verification"],
            workflow_lanes=("build", "verify"),
        ),
    )

    node_ids = {node["id"] for node in graph_plan.nodes}
    assert {"leader_plan", "plan_1", "build_1", "qa_engineer_verify_1", "leader_summary"} <= node_ids
    assert graph_plan.nodes[0]["metadata"]["plan_strategy"] == "standard_role_dag"
    assert graph_plan.workflow_plan["planning"]["requested_mode"] == "standard"
    assert any("Standard Team" in note for note in graph_plan.planner_notes)


def test_standard_role_dag_uses_distinct_confirmed_formation_responsibilities():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "cc",
                    "name": "cc",
                    "role": "产品经理",
                    "executor": "builtin",
                    "metadata": {
                        "workflow_lane": "plan",
                        "role_key": "product_manager",
                        "role_label": "产品经理",
                        "formation_plan_version": 1,
                        "formation_responsibility": {
                            "mission": "承担团队的需求范围、规则定义和验收标准工作",
                            "boundaries": ["不改变用户确认范围"],
                            "deliverables": ["需求范围", "玩法规则", "验收清单"],
                            "collaboration": "向 Leader 提交需求和验收边界",
                        },
                    },
                    "capabilities": ["requirements", "analysis"],
                },
                {
                    "member_id": "kimi",
                    "name": "kimi",
                    "role": "研究分析",
                    "executor": "builtin",
                    "metadata": {
                        "workflow_lane": "plan",
                        "role_key": "research_analyst",
                        "role_label": "研究分析",
                        "formation_plan_version": 1,
                        "formation_responsibility": {
                            "mission": "承担团队的信息调研、方案比较和风险分析工作",
                            "boundaries": ["不改变用户确认范围"],
                            "deliverables": ["调研结论", "参考依据", "风险与建议"],
                            "collaboration": "向 Leader 提交分析结果",
                        },
                    },
                    "capabilities": ["information_retrieval", "research", "analysis"],
                },
            ]
        },
    ))
    team = tm._build_team("formation-standard-distinct")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "设计一个贪吃蛇游戏",
        execution_profile={"requested_mode": "standard"},
    )

    plan_nodes = [node for node in graph_plan.nodes if node["id"].startswith("plan_")]
    assert len(plan_nodes) == 2
    by_assignee = {node["assignee"]: node for node in plan_nodes}
    assert by_assignee["cc"]["title"].startswith("需求与验收：")
    assert "验收清单" in by_assignee["cc"]["detail"]
    assert by_assignee["kimi"]["title"].startswith("调研与风险分析：")
    assert "调研结论" in by_assignee["kimi"]["detail"]
    assert by_assignee["cc"]["detail"] != by_assignee["kimi"]["detail"]
    assert graph_plan.nodes[0]["metadata"]["plan_strategy"] == "standard_role_dag"


def test_standard_role_dag_does_not_create_duplicate_nodes_for_same_formation_scope():
    responsibility = {
        "mission": "承担团队的需求范围、规则定义和验收标准工作",
        "boundaries": ["不改变用户确认范围"],
        "deliverables": ["需求范围", "业务或玩法规则", "验收清单"],
        "collaboration": "向 Leader 提交需求和验收边界",
    }
    members = [
        {
            "member_id": member_id,
            "name": member_id,
            "role": "产品经理",
            "executor": "builtin",
            "metadata": {
                "workflow_lane": "plan",
                "role_key": "product_manager",
                "role_label": "产品经理",
                "formation_plan_version": 1,
                "formation_responsibility": responsibility,
            },
            "capabilities": ["requirements", "analysis"],
        }
        for member_id in ("pm_a", "pm_b")
    ]
    tm, _ = _team(config=Config(max_iterations=3, team_config={"members": members}))
    team = tm._build_team("formation-standard-dedup")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "定义产品需求和验收标准",
        execution_profile={"requested_mode": "standard"},
    )

    plan_nodes = [node for node in graph_plan.nodes if node["id"].startswith("plan_")]
    assert len(plan_nodes) == 1
    assert plan_nodes[0]["assignee"] == "pm_a"
    assert "pm_b" in team.teammates


def test_team_graph_planner_standard_question_stays_role_dag_without_inquiry_or_qa_plan():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": CREW_BUILTIN_AGENT_ID,
                    "name": "crew",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责技术方案和团队协作",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "lead", "role_key": "tech_lead"},
                    "capabilities": ["planning", "architecture"],
                },
            ]
        },
    ))
    team = tm._build_team("standard-question-role-dag")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "你们团队现在有哪些成员",
        execution_profile={
            "requested_mode": "standard",
        },
        team_spec=_structured_team_spec("你们团队现在有哪些成员", intent="question", complexity="simple"),
    )

    node_ids = [node["id"] for node in graph_plan.nodes]
    assert node_ids == ["leader_plan", "leader_summary"]
    assert "standard_inquiry" not in node_ids
    assert not any("qa_plan" in node_id or node_id.endswith("_plan_1") for node_id in node_ids)
    assert graph_plan.nodes[0]["metadata"]["plan_strategy"] == "standard_role_dag"


def test_team_graph_planner_standard_budget_trims_optional_nodes():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "planner",
                    "name": "planner",
                    "role": "负责方案规划",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["planning"],
                },
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
                {
                    "member_id": "docs",
                    "name": "docs",
                    "role": "负责交付整理",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["documentation"],
                },
            ]
        },
    ))
    team = tm._build_team("standard-budget")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "开发一个登录接口并完成测试验收",
        execution_profile={
            "requested_mode": "standard",
            "budget": {"max_nodes": 5},
        },
        team_spec=_structured_team_spec(
            "开发一个登录接口并完成测试验收",
            capabilities=["planning", "backend", "implementation", "testing", "verification"],
            workflow_lanes=("build", "verify"),
        ),
    )

    node_ids = [node["id"] for node in graph_plan.nodes]
    assert len(node_ids) <= 5
    assert "leader_plan" in node_ids
    assert "build_1" in node_ids
    assert "qa_engineer_verify_1" in node_ids
    assert "leader_summary" in node_ids
    assert not any(node_id.startswith("handoff_") for node_id in node_ids)
    assert any("max_nodes" in note for note in graph_plan.planner_notes)
    assert all(parent in node_ids and child in node_ids for parent, child in graph_plan.edges)


async def test_team_graph_planner_standard_compiles_semantic_parallel_work_units():
    provider = SemanticPlanningProvider()
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "researcher",
                    "name": "researcher",
                    "role": "负责检索研究与分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis", "information_retrieval"],
                },
                {
                    "member_id": "writer",
                    "name": "writer",
                    "role": "负责综合写作",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["analysis", "synthesis", "documentation"],
                },
                {
                    "member_id": "reviewer",
                    "name": "reviewer",
                    "role": "负责独立核验",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify"},
                    "capabilities": ["review", "verification"],
                },
            ]
        },
    ))
    team = tm._build_team("semantic-standard")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "调研架构 A 和 JiuwenSwarm 并形成架构综述",
        execution_profile={"requested_mode": "auto", "budget": {"standard_max_work_units": 8}},
        provider=provider,
    )

    assert provider.calls == 1
    prompt_body = provider.messages[-1].content
    assert "formation_members" not in prompt_body
    assert "team_requirements" not in prompt_body
    assert "planner_notes" not in prompt_body
    assert "metadata" not in prompt_body
    assert '"max_work_units":8' in prompt_body
    node_ids = [node["id"] for node in graph_plan.nodes]
    assert node_ids == [
        "leader_plan",
        "research_architecture_a",
        "research_jiuwen",
        "synthesis",
        "independent_review",
        "leader_summary",
    ]
    assert ["research_architecture_a", "synthesis"] in graph_plan.edges
    assert ["research_jiuwen", "synthesis"] in graph_plan.edges
    assert next(node for node in graph_plan.nodes if node["id"] == "synthesis")["assignee"] == "writer"
    assert next(node for node in graph_plan.nodes if node["id"] == "independent_review")["assignee"] == "reviewer"
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"
    assert graph_plan.nodes[1]["metadata"]["required_capabilities"] == ["research", "analysis"]
    assert graph_plan.nodes[1]["metadata"]["capability_source"] == "work_unit"
    assert graph_plan.nodes[1]["metadata"]["display_title"] == "调研架构 A"
    assert graph_plan.workflow_plan["version"] == 1
    assert graph_plan.workflow_plan["revision"] == 1
    assert graph_plan.workflow_plan["planning"]["selected_mode"] == "standard"
    assert graph_plan.workflow_plan["planning"]["dependency_pattern"] == "parallel_merge"
    assert graph_plan.workflow_plan["planning"]["quality_policy"] == "independent_review"
    assert graph_plan.workflow_plan["planning"]["planning_decision"]["status"] == "success"
    assert isinstance(graph_plan.workflow_plan["planning"]["planning_decision"]["elapsed_ms"], int)
    assert set(graph_plan.spec.team_requirements["capabilities"]) >= {
        "research", "analysis", "synthesis", "review", "verification",
    }
    assert set(graph_plan.spec.team_requirements["workflow_lanes"]) >= {"plan", "verify"}
    assert graph_plan.spec.deliverables


async def test_team_graph_planner_drops_reserved_leader_control_work_units():
    class LeaderControlPlanningProvider(LLMProvider):
        async def chat(self, messages, tools=None, *, max_tokens=None):
            return ChatResponse(text="""
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "staged",
  "quality_policy": "leader_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "low",
  "semantic_uncertainty": "low",
  "work_units": [
    {
      "id": "leader_create_team_plan",
      "objective": "Leader 创建 TeamPlan 并派活",
      "display_title": "创建 TeamPlan",
      "kind": "plan",
      "required_capabilities": ["planning"],
      "depends_on": [],
      "expected_output": "TeamPlan"
    },
    {
      "id": "calculate",
      "objective": "计算 17×19",
      "display_title": "计算",
      "kind": "build",
      "required_capabilities": ["implementation"],
      "depends_on": ["leader_create_team_plan"],
      "expected_output": "计算结果"
    },
    {
      "id": "leader_summarize",
      "objective": "Leader 汇总成员回传",
      "display_title": "汇总",
      "kind": "docs",
      "required_capabilities": ["documentation"],
      "depends_on": ["calculate"],
      "expected_output": "最终总结"
    }
  ]
}
""")

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            yield StreamChunk(delta_text="", done=True)

    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "implementer",
                "name": "implementer",
                "role": "负责实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
                "capabilities": ["implementation"],
            }]
        },
    ))
    team = tm._build_team("semantic-leader-control")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "Leader 派活，成员计算后由 Leader 汇总",
        execution_profile={"requested_mode": "standard"},
        provider=LeaderControlPlanningProvider(),
    )

    assert [node["id"] for node in graph_plan.nodes] == [
        "leader_plan",
        "calculate",
        "leader_summary",
    ]
    assert graph_plan.edges == [
        ["leader_plan", "calculate"],
        ["calculate", "leader_summary"],
    ]
    assert any("leader_create_team_plan、leader_summarize" in note for note in graph_plan.planner_notes)


async def test_team_graph_planner_renames_repeated_review_and_keeps_summary_terminal():
    class RepeatedReviewPlanningProvider(LLMProvider):
        async def chat(self, messages, tools=None, *, max_tokens=None):
            return ChatResponse(text="""
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "staged",
  "quality_policy": "independent_review",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "medium",
  "semantic_uncertainty": "low",
  "work_units": [
    {
      "id": "draft_synthesis",
      "objective": "撰写架构综述初稿",
      "display_title": "初稿",
      "kind": "docs",
      "required_capabilities": ["synthesis", "documentation"],
      "depends_on": [],
      "expected_output": "综述初稿"
    },
    {
      "id": "independent_review",
      "objective": "审阅初稿事实与结构",
      "display_title": "初稿审阅",
      "kind": "verify",
      "required_capabilities": ["review", "verification"],
      "depends_on": ["draft_synthesis"],
      "expected_output": "初稿审阅意见"
    },
    {
      "id": "finalize_report",
      "objective": "根据审阅意见形成最终稿",
      "display_title": "最终稿",
      "kind": "docs",
      "required_capabilities": ["documentation", "writing"],
      "depends_on": ["independent_review"],
      "expected_output": "最终交付稿"
    }
  ]
}
""")

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            yield StreamChunk(delta_text="", done=True)

    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "writer",
                    "name": "writer",
                    "role": "负责综合写作",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["synthesis", "documentation", "writing"],
                },
                {
                    "member_id": "reviewer",
                    "name": "reviewer",
                    "role": "负责独立核验",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify"},
                    "capabilities": ["review", "verification"],
                },
            ]
        },
    ))
    team = tm._build_team("semantic-repeated-review")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "形成 Crew 多智能体协作模块的简短综述，并由独立成员核验结论。",
        execution_profile={"requested_mode": "standard", "budget": {"standard_max_work_units": 8}},
        provider=RepeatedReviewPlanningProvider(),
    )

    node_ids = [node["id"] for node in graph_plan.nodes]
    assert len(node_ids) == len(set(node_ids))
    assert "independent_review" in node_ids
    assert "independent_review_2" in node_ids
    assert ["draft_synthesis", "independent_review"] in graph_plan.edges
    assert ["independent_review", "finalize_report"] in graph_plan.edges
    assert ["finalize_report", "independent_review_2"] in graph_plan.edges
    assert ["independent_review_2", "leader_summary"] in graph_plan.edges
    assert not any(edge[0] == "leader_summary" for edge in graph_plan.edges)
    assert any("重复节点 ID independent_review" in note for note in graph_plan.planner_notes)


@pytest.mark.parametrize(
    "scenario",
    ["parallel_research", "synthesis", "legal_consultation", "history_query", "dev_test_loop"],
)
async def test_team_graph_planner_p0_standard_semantic_dag_scenarios(scenario):
    case = P0_STANDARD_SEMANTIC_SCENARIOS[scenario]
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "researcher_a",
                    "name": "researcher_a",
                    "role": "负责检索研究、事实整理和分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis", "information_retrieval", "design"],
                },
                {
                    "member_id": "researcher_b",
                    "name": "researcher_b",
                    "role": "负责并行检索研究与交叉分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis", "information_retrieval"],
                },
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责工程实现、接口开发和自测",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation", "coding", "development"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证、回归检查和独立核验",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify"},
                    "capabilities": ["testing", "verification", "review"],
                },
                {
                    "member_id": "writer",
                    "name": "writer",
                    "role": "负责综合写作、文档整理和最终表达",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["synthesis", "documentation", "writing"],
                },
            ]
        },
    ))
    team = tm._build_team(f"p0-standard-{scenario}")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        str(case["goal"]),
        execution_profile={"requested_mode": "standard", "budget": {"standard_max_work_units": 8}},
        provider=P0ScenarioPlanningProvider(scenario),
    )

    node_ids = {node["id"] for node in graph_plan.nodes}
    edge_pairs = {tuple(edge) for edge in graph_plan.edges}
    assert set(case["required_nodes"]) <= node_ids
    assert set(case["required_edges"]) <= edge_pairs
    assert {"leader_plan", "leader_summary"} <= node_ids
    assert all(parent in node_ids and child in node_ids for parent, child in edge_pairs)
    assert graph_plan.workflow_plan["planning"]["selected_mode"] == "standard"
    assert graph_plan.workflow_plan["planning"]["planning_decision"]["status"] == "success"
    assert graph_plan.workflow_plan["planning"]["planning_decision"]["transport"] in {
        "stream_race_won",
        "chat_race_won",
        "stream_reasoning_grace",
        "cache",
    }
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"
    assert not graph_plan.workflow_plan["planning"].get("fallback_from")

    if scenario == "parallel_research":
        by_id = {node["id"]: node for node in graph_plan.nodes}
        assignees = {
            by_id["research_city1"]["assignee"],
            by_id["research_city2"]["assignee"],
            by_id["research_city3"]["assignee"],
        }
        assert len(assignees) >= 2
    if scenario == "dev_test_loop":
        by_id = {node["id"]: node for node in graph_plan.nodes}
        assert by_id["api_build"]["assignee"] == "dev"
        assert by_id["qa_verify"]["assignee"] == "qa"


@pytest.mark.skipif(
    os.getenv("CREW_REAL_PROVIDER_E2E") != "1",
    reason="set CREW_REAL_PROVIDER_E2E=1 to run real provider PlanningDecision E2E",
)
async def test_team_graph_planner_real_provider_standard_semantic_dag_smoke():
    cfg = load_config()
    profile = cfg.active_model
    if not profile.api_key:
        pytest.skip(f"active model profile {profile.id} has no API key")
    provider = OpenAIProvider(
        api_key=profile.api_key,
        base_url=profile.base_url or None,
        model=profile.model,
        temperature=0.1,
        timeout=max(60.0, float(profile.timeout or 60.0)),
    )
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "researcher",
                    "name": "researcher",
                    "role": "负责资料检索、架构研究和事实分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis", "information_retrieval"],
                },
                {
                    "member_id": "writer",
                    "name": "writer",
                    "role": "负责综合、文档整理和最终表达",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["synthesis", "documentation", "writing"],
                },
                {
                    "member_id": "reviewer",
                    "name": "reviewer",
                    "role": "负责独立核验、风险检查和质量审阅",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify"},
                    "capabilities": ["review", "verification", "testing"],
                },
            ]
        },
    ))
    team = tm._build_team("real-provider-standard-semantic")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "调研两种多智能体协作架构，形成 Crew 协作模块的简短综述，并由独立成员核验结论。",
        execution_profile={
            "requested_mode": "standard",
            "budget": {
                "planning_decision_timeout": 30,
                "standard_max_work_units": 8,
            },
        },
        provider=provider,
    )

    node_ids = {node["id"] for node in graph_plan.nodes}
    node_id_list = [node["id"] for node in graph_plan.nodes]
    edge_pairs = {tuple(edge) for edge in graph_plan.edges}
    terminal_nodes = {
        node_id
        for node_id in node_ids
        if node_id != "leader_summary" and not any(parent == node_id for parent, _ in edge_pairs)
    }
    assert {"leader_plan", "leader_summary"} <= node_ids
    assert len(node_id_list) == len(node_ids)
    assert len(node_ids) >= 4
    assert all(parent in node_ids and child in node_ids for parent, child in edge_pairs)
    assert not any(parent == "leader_summary" for parent, _ in edge_pairs)
    assert terminal_nodes == set()
    assert graph_plan.workflow_plan["planning"]["selected_mode"] == "standard"
    assert graph_plan.workflow_plan["planning"]["planning_decision"]["status"] == "success"
    planning_decision = graph_plan.workflow_plan["planning"]["planning_decision"]
    assert planning_decision["status"] == "success", planning_decision
    assert planning_decision["transport"] in {
        "stream_race_won",
        "chat_race_won",
        "stream_reasoning_grace",
        "cache",
    }
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"
    assert not graph_plan.workflow_plan["planning"].get("fallback_from")


async def test_team_graph_planner_balances_parallel_semantic_research_units():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "hh",
                    "name": "hh",
                    "role": "负责检索研究与分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis"],
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责检索研究与分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis"],
                },
                {
                    "member_id": "writer",
                    "name": "writer",
                    "role": "负责综合写作",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["documentation", "synthesis"],
                },
            ]
        },
    ))
    team = tm._build_team("semantic-balanced")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "找中国3个不同城市的隐藏小吃，并配一句本地话做点评",
        execution_profile={"requested_mode": "standard"},
        provider=ParallelResearchPlanningProvider(),
    )

    by_id = {node["id"]: node for node in graph_plan.nodes}
    research_assignees = [
        by_id["research_city1"]["assignee"],
        by_id["research_city2"]["assignee"],
        by_id["research_city3"]["assignee"],
    ]
    assert set(research_assignees) == {"hh", "kk"}
    assert by_id["compile_results"]["assignee"] == "writer"
    assert ["research_city1", "compile_results"] in graph_plan.edges
    assert ["research_city2", "compile_results"] in graph_plan.edges
    assert ["research_city3", "compile_results"] in graph_plan.edges
    assert by_id["research_city1"]["metadata"]["parallel_assignment"] is True
    assert by_id["research_city2"]["metadata"]["assignment_group"] == by_id["research_city1"]["metadata"]["assignment_group"]


async def test_team_graph_planner_does_not_balance_to_weaker_parallel_candidate():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "hh",
                    "name": "hh",
                    "role": "负责检索研究与分析",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan"},
                    "capabilities": ["research", "analysis"],
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责写作整理",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "docs"},
                    "capabilities": ["documentation"],
                },
            ]
        },
    ))
    team = tm._build_team("semantic-no-forced-balance")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "找中国3个不同城市的隐藏小吃，并配一句本地话做点评",
        execution_profile={"requested_mode": "standard"},
        provider=ParallelResearchPlanningProvider(),
    )

    by_id = {node["id"]: node for node in graph_plan.nodes}
    assert {
        by_id["research_city1"]["assignee"],
        by_id["research_city2"]["assignee"],
        by_id["research_city3"]["assignee"],
    } == {"hh"}
    assert "parallel_assignment" not in by_id["research_city1"]["metadata"]


async def test_team_graph_planner_streams_planning_decision_and_records_diagnostics():
    provider = StreamingPlanningProvider()
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "writer",
                "name": "writer",
                "role": "负责综合写作",
                "executor": "builtin",
                "metadata": {"workflow_lane": "docs"},
                "capabilities": ["documentation"],
            }]
        },
    ))
    team = tm._build_team("streaming-planning")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "整理一份小吃清单",
        execution_profile={"requested_mode": "standard"},
        provider=provider,
    )

    planning_decision = graph_plan.workflow_plan["planning"]["planning_decision"]
    assert provider.stream_calls == 1
    assert provider.chat_calls <= 1
    assert provider.stream_max_tokens == PLANNING_DECISION_MAX_TOKENS == 4096
    assert planning_decision["status"] == "success"
    assert planning_decision["transport"] == "stream_race_won"
    assert planning_decision["race_winner"] == "stream"
    assert planning_decision["cache_hit"] is False
    assert isinstance(planning_decision["prompt_bytes"], int)
    assert isinstance(planning_decision["first_token_ms"], int)
    assert planning_decision["partial_chars"] > 0
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"


async def test_team_graph_planner_uses_chat_when_chat_wins_planning_race():
    provider = ChatWinsPlanningProvider()
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "writer",
                "name": "writer",
                "role": "负责综合写作",
                "executor": "builtin",
                "metadata": {"workflow_lane": "docs"},
                "capabilities": ["documentation", "synthesis"],
            }]
        },
    ))
    team = tm._build_team("chat-wins-planning")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "整理一份架构综述",
        execution_profile={"requested_mode": "standard", "budget": {"planning_decision_timeout": 0.5}},
        provider=provider,
    )

    planning_decision = graph_plan.workflow_plan["planning"]["planning_decision"]
    assert provider.stream_calls == 1
    assert provider.chat_calls == 1
    assert planning_decision["status"] == "success"
    assert planning_decision["transport"] == "chat_race_won"
    assert planning_decision["race_winner"] == "chat"
    assert planning_decision["cancelled_transport"] == "stream"
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"


async def test_team_graph_planner_keeps_waiting_stream_when_reasoning_after_chat_race_window():
    provider = StreamReasoningGracePlanningProvider()
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "writer",
                "name": "writer",
                "role": "负责综合写作",
                "executor": "builtin",
                "metadata": {"workflow_lane": "docs"},
                "capabilities": ["documentation", "synthesis"],
            }]
        },
    ))
    team = tm._build_team("stream-reasoning-grace")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "整理一份架构综述",
        execution_profile={
            "requested_mode": "standard",
            "budget": {
                "planning_decision_timeout": 0.05,
                "planning_decision_reasoning_grace_timeout": 0.8,
            },
        },
        provider=provider,
    )

    planning_decision = graph_plan.workflow_plan["planning"]["planning_decision"]
    assert provider.stream_calls == 1
    assert provider.chat_calls == 1
    assert planning_decision["status"] == "success"
    assert planning_decision["transport"] == "stream_reasoning_grace"
    assert planning_decision["race_winner"] == "stream"
    assert planning_decision["reasoning_grace_used"] is True
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"


async def test_team_graph_planner_reports_reasoning_progress_before_content():
    provider = ReasoningThenPlanningProvider()
    events: list[dict[str, object]] = []
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "writer",
                "name": "writer",
                "role": "负责综合写作",
                "executor": "builtin",
                "metadata": {"workflow_lane": "docs"},
                "capabilities": ["documentation"],
            }]
        },
    ))
    team = tm._build_team("reasoning-planning")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "整理一份小吃清单",
        execution_profile={"requested_mode": "standard"},
        provider=provider,
        planning_progress=events.append,
    )

    planning_decision = graph_plan.workflow_plan["planning"]["planning_decision"]
    phases = [event.get("phase") for event in events]
    assert "started" in phases
    assert "connected" in phases
    assert "reasoning" in phases
    assert "content" in phases
    assert "parsed" in phases
    assert "compiled" in phases
    assert planning_decision["first_chunk_ms"] is not None
    assert planning_decision["first_reasoning_ms"] is not None
    assert planning_decision["first_content_ms"] is not None
    assert planning_decision["reasoning_chars"] > 0
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"


async def test_team_graph_planner_classifies_reasoning_only_without_calling_stream_empty():
    provider = ReasoningOnlyPlanningProvider()
    events: list[dict[str, object]] = []
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "writer",
                "name": "writer",
                "role": "负责综合写作",
                "executor": "builtin",
                "metadata": {"workflow_lane": "docs"},
                "capabilities": ["documentation"],
            }]
        },
    ))
    team = tm._build_team("reasoning-only-planning")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "整理一份小吃清单",
        execution_profile={"requested_mode": "standard"},
        provider=provider,
        planning_progress=events.append,
    )

    planning_decision = graph_plan.workflow_plan["planning"]["planning_decision"]
    labels = [str(event.get("label") or "") for event in events]
    assert provider.stream_calls == 1
    assert provider.chat_calls == 1
    assert provider.stream_max_tokens == PLANNING_DECISION_MAX_TOKENS == 4096
    assert provider.chat_max_tokens == PLANNING_DECISION_MAX_TOKENS == 4096
    assert planning_decision["status"] == "fallback"
    assert planning_decision["error_type"] == "reasoning_only_length"
    assert planning_decision["first_reasoning_ms"] is not None
    assert planning_decision["first_content_ms"] is None
    assert planning_decision["partial_chars"] == 0
    assert planning_decision["reasoning_chars"] > 0
    assert planning_decision["chat_reasoning_chars"] > 0
    assert graph_plan.nodes[0]["metadata"]["llm_planning_error_type"] == "reasoning_only_length"
    assert graph_plan.nodes[0]["metadata"]["plan_strategy"] == "standard_role_dag"
    assert any("尚未输出结构化结果" in label for label in labels)
    assert all("流式结果为空" not in label for label in labels)


async def test_team_graph_planner_reuses_planning_decision_cache_for_same_inputs():
    provider = CachePlanningProvider()
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "writer",
                "name": "writer",
                "role": "负责综合写作",
                "executor": "builtin",
                "metadata": {"workflow_lane": "docs"},
                "capabilities": ["documentation", "analysis"],
            }]
        },
    ))
    team = tm._build_team("cached-planning")
    execution_profile = {
        "requested_mode": "standard",
        "budget": {"standard_max_work_units": 8, "planning_decision_cache_ttl": 600},
    }

    first = await TeamGraphPlanner().plan_async(
        team,
        "缓存测试：调研两个架构并写综述",
        execution_profile=execution_profile,
        provider=provider,
    )
    second = await TeamGraphPlanner().plan_async(
        team,
        "缓存测试：调研两个架构并写综述",
        execution_profile=execution_profile,
        provider=provider,
    )

    assert provider.calls == 1
    assert first.workflow_plan["planning"]["planning_decision"]["cache_hit"] is False
    assert second.workflow_plan["planning"]["planning_decision"]["cache_hit"] is True
    assert second.workflow_plan["planning"]["planning_decision"]["transport"] == "cache"
    assert second.nodes[1]["metadata"]["plan_strategy"] == "standard_semantic_dag"


async def test_planning_provider_warmup_is_scheduled_without_blocking():
    provider = WarmupPlanningProvider()

    schedule_planning_provider_warmup(provider)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert provider.stream_calls == 1


async def test_team_graph_planner_surfaces_critical_missing_user_facts():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "analyst",
                "name": "analyst",
                "role": "负责分析与审阅",
                "executor": "builtin",
                "metadata": {"workflow_lane": "plan"},
                "capabilities": ["analysis", "review"],
            }]
        },
    ))
    team = tm._build_team("missing-planning-input")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "帮我分析合同风险",
        execution_profile={"requested_mode": "auto"},
        provider=MissingInfoPlanningProvider(),
    )

    assert graph_plan.critical_missing_info == ["需要分析的合同正文或文件"]
    assert "需要分析的合同正文或文件" in graph_plan.workflow_plan["warnings"]
    assert graph_plan.workflow_plan["planning"]["confidence"] <= 0.45


@pytest.mark.asyncio
async def test_team_runtime_planning_defaultable_missing_info_still_creates_plan():
    tm, _ = _team(DefaultableMissingInfoPlanningProvider(), config=Config(
        max_iterations=3,
        team_config={
            "execution_profile": {"requested_mode": "standard", "profile_source": "test"},
            "members": [{
                "member_id": "dev",
                "name": "dev",
                "role": "负责开发实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
                "capabilities": ["implementation"],
            }],
        },
    ))
    team = tm._build_team("defaultable-missing-info")

    plan = await tm._ensure_runtime_plan_async(
        "defaultable-missing-info",
        team,
        "帮我做一个贪吃蛇的前端",
        external_team_id="",
        owner_account_id="owner",
        execution_profile={"requested_mode": "standard"},
    )

    assert plan is not None
    assert tm._planning_missing_info.get(("owner", "defaultable-missing-info")) is None


@pytest.mark.asyncio
async def test_team_runtime_planning_missing_info_uses_text_followup(monkeypatch):
    tm, _ = _team(MissingInfoPlanningProvider(), config=Config(
        max_iterations=3,
        team_config={
            "execution_profile": {"requested_mode": "standard", "profile_source": "test"},
            "members": [{
                "member_id": "analyst",
                "name": "analyst",
                "role": "负责分析与审阅",
                "executor": "builtin",
                "metadata": {"workflow_lane": "plan"},
                "capabilities": ["analysis", "review"],
            }],
        },
    ))
    team = tm._build_team("missing-info-followup")
    captured: dict[str, object] = {}

    async def fake_send(session_id, questions, **kwargs):
        captured["session_id"] = session_id
        captured["questions"] = questions
        captured["origin"] = kwargs.get("origin")
        return session_id, "missing_info_q1"

    async def fake_wait(session_id, question_id):
        return [{"question_id": "workflow_planning_missing_info", "answers": ["合同正文在 /tmp/contract.txt"]}]

    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)
    monkeypatch.setattr("crew.team.team_manager.wait_for_answer", fake_wait)

    chunks = [
        chunk async for chunk in tm._run_required_workflow(
            Envelope.of("帮我分析合同风险", session_id="missing-info-followup", user_id="owner"),
            team=team,
            external_team_id="",
            execution_profile={"requested_mode": "standard"},
        )
    ]

    question = captured["questions"][0]
    assert question["id"] == "workflow_planning_missing_info"
    assert question["inputMode"] == "text"
    assert captured["origin"]["agent_id"] == "leader"
    assert any(chunk.kind == "final" for chunk in chunks)


@pytest.mark.asyncio
async def test_team_runtime_planning_missing_info_followup_failure_is_terminal(monkeypatch):
    tm, _ = _team(MissingInfoPlanningProvider(), config=Config(
        max_iterations=3,
        team_config={
            "execution_profile": {"requested_mode": "standard", "profile_source": "test"},
            "members": [{
                "member_id": "analyst",
                "name": "analyst",
                "role": "负责分析与审阅",
                "executor": "builtin",
                "metadata": {"workflow_lane": "plan"},
                "capabilities": ["analysis", "review"],
            }],
        },
    ))
    team = tm._build_team("missing-info-followup-failure")

    async def fake_send(*args, **kwargs):
        raise ToolError("questions[0].options 必须是非空数组")

    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)

    chunks = [
        chunk async for chunk in tm._run_required_workflow(
            Envelope.of("帮我分析合同风险", session_id="missing-info-followup-failure", user_id="owner"),
            team=team,
            external_team_id="",
            execution_profile={"requested_mode": "standard"},
        )
    ]

    assert chunks[-1].kind == "error"
    assert chunks[-1].is_final is True


async def test_team_graph_planner_ai_async_uses_llm_single_dag():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
            ]
        },
    ))
    team = tm._build_team("standard-llm")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "开发一个登录接口并完成测试验收",
        execution_profile={"requested_mode": "ai", "budget": {"max_nodes": 6}},
        provider=JsonGraphProvider(),
    )

    assert [node["id"] for node in graph_plan.nodes] == [
        "leader_plan",
        "api_build",
        "qa_verify",
        "leader_summary",
    ]
    assert graph_plan.edges == [["leader_plan", "api_build"], ["api_build", "qa_verify"], ["qa_verify", "leader_summary"]]
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "ai_single_dag"
    assert graph_plan.nodes[0]["metadata"]["llm_planning_status"] == "success"
    assert isinstance(graph_plan.nodes[0]["metadata"]["llm_planning_elapsed_ms"], int)
    assert graph_plan.nodes[1]["metadata"]["workflow_lane"] == "build"
    assert graph_plan.nodes[1]["metadata"]["required_capabilities"] == ["backend", "implementation"]
    assert graph_plan.nodes[1]["metadata"]["capability_source"] == "ai_planner"
    assert any("AI Planner" in note for note in graph_plan.planner_notes)


async def test_team_graph_planner_ai_async_records_fallback_timing():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                },
            ]
        },
    ))
    team = tm._build_team("standard-fallback-timing")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "开发一个登录接口",
        execution_profile={"requested_mode": "ai", "budget": {"ai_planning_timeout": 0.2}},
        provider=FailingGraphProvider(),
    )

    metadata = graph_plan.nodes[0]["metadata"]
    assert metadata["plan_strategy"] == "standard_role_dag"
    assert metadata["llm_planning_status"] == "fallback"
    assert isinstance(metadata["llm_planning_elapsed_ms"], int)
    assert "planner timeout" in metadata["llm_planning_error"]
    assert graph_plan.workflow_plan["planning"]["fallback_from"] == "ai"
    assert any("AI Planner 失败" in note for note in graph_plan.planner_notes)


async def test_team_graph_planner_ai_missing_capability_contract_falls_back_to_standard():
    class MissingCapabilityGraphProvider(LLMProvider):
        async def chat(self, messages, tools=None, *, max_tokens=None):
            return ChatResponse(text="""
{
  "nodes": [
    {"id": "leader_plan", "title": "规划", "assignee": "leader", "workflow_lane": "lead"},
    {"id": "api_build", "title": "实现接口", "assignee": "dev", "workflow_lane": "build"},
    {"id": "leader_summary", "title": "汇总", "assignee": "leader", "workflow_lane": "summary"}
  ],
  "edges": [["leader_plan", "api_build"], ["api_build", "leader_summary"]]
}
""")

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            yield StreamChunk(delta_text="", done=True)

    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "dev",
                "name": "dev",
                "role": "负责开发实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
            }],
        },
    ))

    graph_plan = await TeamGraphPlanner().plan_async(
        tm._build_team("ai-missing-capabilities"),
        "开发一个登录接口",
        execution_profile={"requested_mode": "ai"},
        team_spec=_structured_team_spec(
            "开发一个登录接口",
            capabilities=["backend", "implementation"],
            workflow_lanes=("build",),
        ),
        provider=MissingCapabilityGraphProvider(),
    )

    assert graph_plan.workflow_plan["planning"]["fallback_from"] == "ai"
    assert graph_plan.nodes[0]["metadata"]["plan_strategy"] == "standard_role_dag"
    assert "missing required_capabilities" in graph_plan.nodes[0]["metadata"]["llm_planning_error"]
    executable = next(node for node in graph_plan.nodes if node["assignee"] == "dev")
    assert executable["metadata"]["required_capabilities"]
    assert executable["metadata"]["capability_source"] == "role_catalog"


async def test_team_graph_planner_standard_async_falls_back_to_role_dag_when_decision_fails():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "dev",
                "name": "dev",
                "role": "负责开发实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
            }],
        },
    ))
    team = tm._build_team("standard-role-only")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "开发一个登录接口",
        execution_profile={"requested_mode": "standard"},
        team_spec=_structured_team_spec(
            "开发一个登录接口",
            capabilities=["backend", "implementation"],
            workflow_lanes=("build",),
        ),
        provider=FailingGraphProvider(),
    )

    assert graph_plan.nodes[0]["metadata"]["plan_strategy"] == "standard_role_dag"
    assert graph_plan.nodes[0]["metadata"]["llm_planning_status"] == "fallback"
    assert graph_plan.nodes[0]["metadata"]["llm_planning_error_type"] == "provider_error"
    assert graph_plan.workflow_plan["planning"]["selected_mode"] == "standard"
    assert graph_plan.workflow_plan["planning"]["fallback_from"] == "planning_decision"
    assert graph_plan.workflow_plan["planning"]["engine"] == "legacy_role_compiler"
    assert graph_plan.workflow_plan["planning"]["planning_decision"]["error_type"] == "provider_error"
    executable = next(node for node in graph_plan.nodes if node["assignee"] == "dev")
    assert executable["metadata"]["required_capabilities"]
    assert executable["metadata"]["capability_source"] == "role_catalog"


@pytest.mark.parametrize(
    "provider,error_type,execution_profile",
    [
        (
            SlowPlanningProvider(),
            "timeout",
            {"requested_mode": "standard", "budget": {"planning_decision_timeout": 0.001}},
        ),
        (InvalidJsonPlanningProvider(), "invalid_json", {"requested_mode": "standard"}),
    ],
    ids=["timeout", "invalid_json"],
)
async def test_team_graph_planner_standard_decision_failure_is_classified(
    provider, error_type, execution_profile,
):
    assert DEFAULT_PLANNING_DECISION_TIMEOUT == 30.0
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "dev",
                "name": "dev",
                "role": "负责开发实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
            }],
        },
    ))
    team = tm._build_team("standard-decision-classification")

    graph_plan = await TeamGraphPlanner().plan_async(
        team,
        "开发一个登录接口",
        execution_profile=execution_profile,
        provider=provider,
    )

    metadata = graph_plan.nodes[0]["metadata"]
    planning = graph_plan.workflow_plan["planning"]
    planning_decision = planning["planning_decision"]
    assert metadata["plan_strategy"] == "standard_role_dag"
    assert metadata["llm_planning_error_type"] == error_type
    assert planning["engine"] == "legacy_role_compiler"
    assert planning["fallback_from"] == "planning_decision"
    assert planning_decision["status"] == "fallback"
    assert planning_decision["error_type"] == error_type
    assert planning_decision["fallback_from"] == "planning_decision"


def test_team_graph_planner_fast_profile_builds_minimal_dag():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build", "role_key": "fullstack_developer"},
                    "capabilities": ["frontend", "implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
            ]
        },
    ))
    team = tm._build_team("fast-graph")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "做一个可运行的小工具",
        execution_profile={
            "requested_mode": "fast",
            "budget": {"max_retries": 1, "max_nodes": 3},
        },
        team_spec=_structured_team_spec(
            "做一个可运行的小工具",
            capabilities=["implementation", "testing", "verification"],
            workflow_lanes=("build",),
        ),
    )

    assert [node["id"] for node in graph_plan.nodes] == ["leader_plan", "fast_execute", "leader_summary"]
    assert graph_plan.edges == [["leader_plan", "fast_execute"], ["fast_execute", "leader_summary"]]
    assert graph_plan.nodes[1]["assignee"] == "dev"
    assert graph_plan.nodes[1]["metadata"]["plan_strategy"] == "fast_minimal_path"
    assert graph_plan.nodes[1]["metadata"]["execution_budget"]["max_nodes"] == 3
    assert graph_plan.nodes[1]["metadata"]["required_capabilities"] == [
        "implementation",
        "testing",
        "verification",
    ]
    assert graph_plan.nodes[1]["metadata"]["capability_source"] == "team_spec"
    assert graph_plan.workflow_plan["planning"]["requested_mode"] == "fast"


def test_team_graph_planner_fast_mode_overrides_default_build_verification():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build", "role_key": "fullstack_developer"},
                    "capabilities": ["frontend", "implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
            ]
        },
    ))
    team = tm._build_team("fast-build-no-verify")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "做一个2048小游戏",
        execution_profile={"requested_mode": "fast"},
    )

    assert [node["id"] for node in graph_plan.nodes] == ["leader_plan", "fast_execute", "leader_summary"]
    assert graph_plan.edges == [["leader_plan", "fast_execute"], ["fast_execute", "leader_summary"]]
    assert graph_plan.spec.team_requirements["workflow_lanes"] == []


async def test_team_graph_planner_auto_fast_uses_work_unit_capability_contract():
    class OneUnitPlanningProvider(LLMProvider):
        async def chat(self, messages, tools=None, *, max_tokens=None):
            return ChatResponse(text="""
{
  "goal_clarity": "high",
  "critical_missing_info": [],
  "dependency_pattern": "sequential",
  "quality_policy": "none",
  "dynamic_discovery": false,
  "conditional_branching": false,
  "iteration_until_convergence": false,
  "risk_level": "low",
  "semantic_uncertainty": "low",
  "work_units": [{
    "id": "implement_api",
    "objective": "实现登录接口",
    "kind": "build",
    "required_capabilities": ["backend", "implementation"],
    "depends_on": [],
    "expected_output": "可运行接口"
  }]
}
""")

        async def stream_chat(self, messages, tools=None, *, max_tokens=None):
            yield StreamChunk(delta_text="", done=True)

    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [{
                "member_id": "dev",
                "name": "dev",
                "role": "负责开发实现",
                "executor": "builtin",
                "metadata": {"workflow_lane": "build"},
                "capabilities": ["backend", "implementation"],
            }],
        },
    ))

    graph_plan = await TeamGraphPlanner().plan_async(
        tm._build_team("auto-fast-capabilities"),
        "实现登录接口",
        execution_profile={"requested_mode": "auto"},
        provider=OneUnitPlanningProvider(),
    )

    assert graph_plan.workflow_plan["planning"]["selected_mode"] == "fast"
    assert graph_plan.nodes[1]["metadata"]["required_capabilities"] == ["backend", "implementation"]
    assert graph_plan.nodes[1]["metadata"]["capability_source"] == "work_unit_summary"


def test_team_graph_planner_fast_question_prefers_plan_or_docs_before_verify():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": CREW_BUILTIN_AGENT_ID,
                    "name": "Crew 内置智能体",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责技术方案和团队能力说明",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "plan", "role_key": "tech_lead"},
                    "capabilities": ["planning", "architecture"],
                },
            ]
        },
    ))
    team = tm._build_team("fast-question-primary")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "你们团队擅长做什么工作",
        execution_profile={
            "requested_mode": "fast",
        },
        team_spec=_structured_team_spec("你们团队擅长做什么工作", intent="question", complexity="simple"),
    )

    assert [node["id"] for node in graph_plan.nodes] == ["leader_plan", "fast_execute", "leader_summary"]
    assert graph_plan.nodes[1]["assignee"] == "kk"
    assert graph_plan.nodes[1]["metadata"]["workflow_lane"] == "plan"


def test_team_graph_planner_fast_question_uses_leader_before_build_when_no_docs_or_plan():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build", "role_key": "fullstack_developer"},
                    "capabilities": ["implementation"],
                },
            ]
        },
    ))
    team = tm._build_team("fast-question-leader")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "今天天气怎么样，你的团队成员都准备好了么",
        execution_profile={
            "requested_mode": "fast",
        },
        team_spec=_structured_team_spec("今天天气怎么样，你的团队成员都准备好了么", intent="question", complexity="simple"),
    )

    assert [node["id"] for node in graph_plan.nodes] == ["leader_plan", "fast_execute", "leader_summary"]
    assert graph_plan.nodes[1]["assignee"] == "leader"
    assert graph_plan.nodes[1]["metadata"]["workflow_lane"] == "lead"


def test_team_graph_planner_fast_profile_adds_lightweight_verify_when_requested():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build", "role_key": "fullstack_developer"},
                    "capabilities": ["frontend", "implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                    "capabilities": ["qa", "test"],
                },
            ]
        },
    ))
    team = tm._build_team("fast-verify")

    graph_plan = TeamGraphPlanner().plan(
        team,
        "快速完成当前页面调整",
        execution_profile={
            "requested_mode": "fast",
        },
        team_spec=_structured_team_spec("快速完成当前页面调整", workflow_lanes=("verify",)),
    )

    assert [node["id"] for node in graph_plan.nodes] == [
        "leader_plan",
        "fast_execute",
        "fast_verify",
        "leader_summary",
    ]
    assert ["fast_execute", "fast_verify"] in graph_plan.edges
    assert ["fast_verify", "leader_summary"] in graph_plan.edges
    assert graph_plan.nodes[2]["assignee"] == "qa"


@pytest.mark.asyncio
async def test_team_fast_question_leader_runs_model_and_summarizes_user_goal():
    tm, _ = _team(LeaderQuestionProvider(), config=Config(
        max_iterations=5,
        team_config={
            "members": [
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build", "role_key": "fullstack_developer"},
                    "capabilities": ["implementation"],
                },
            ]
        },
    ))
    chunks = []
    final = ""
    envelope = Envelope.of(
        "今天天气怎么样，你的团队成员都准备好了么",
        session_id="fast-weather-team",
        mode="team",
        params={
            "team_execution_profile": {
                "requested_mode": "fast",
            },
            "team_spec": _structured_team_spec(
                "今天天气怎么样，你的团队成员都准备好了么",
                intent="question",
                complexity="simple",
            ),
        },
    )

    async for chunk in tm.interact(envelope):
        chunks.append(chunk)
        if chunk.kind == "final":
            final = str(chunk.body.get("text") or "")

    assert "天气需要补充位置才能查询" in final
    assert "kk 已准备好" in final
    assert "本次团队任务已完成" not in final
    team_internal_text = "\n".join(
        str(chunk.body.get("text") or "")
        for chunk in chunks
        if chunk.kind == "team_internal"
    )
    assert "天气需要补充位置才能查询" in team_internal_text
    assert "已收到「快速执行" not in team_internal_text
    plan = next(plan for key, plan in tm._plans.items() if key[1] == "fast-weather-team")
    assert plan.nodes["fast_execute"].assignee == "leader"
    assert plan.nodes["leader_summary"].result_summary == final


def test_team_runtime_accepts_fast_execution_profile():
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation"],
                },
            ]
        },
    ))
    team = tm._build_team("runtime-fast")

    plan = tm._ensure_runtime_plan(
        "runtime-fast",
        team,
        "快速完成一个 demo",
        "",
        owner_account_id="local",
        execution_profile={"requested_mode": "fast", "budget": {"max_retries": 1}},
        team_spec={"goal": "快速完成一个 demo"},
    )

    assert plan is not None
    assert list(plan.nodes) == ["leader_plan", "fast_execute", "leader_summary"]
    assert plan.nodes["fast_execute"].metadata["execution_mode"] == "fast"


@pytest.mark.asyncio
async def test_auto_fast_turn_decision_creates_fast_teamplan_without_planning_decision():
    class FastQuestionProvider(RoleProvider):
        def __init__(self):
            self.planning_decision_calls = 0

        async def chat(self, messages, tools=None, *, max_tokens=None):
            system = messages[0].content if messages else ""
            if "PlanningDecision" in system:
                self.planning_decision_calls += 1
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "直接处理当前节点" in last_user:
                return ChatResponse(text="有，贪吃蛇小游戏可以实现。")
            if "最终汇总" in last_user:
                return ChatResponse(text="团队最终答案：有，贪吃蛇小游戏可以实现。")
            return await super().chat(messages, tools)

    provider = FastQuestionProvider()
    tm, _ = _team(provider)

    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of(
            "有贪吃蛇小游戏么",
            session_id="auto-fast-turn",
            mode="team",
            params={"team_spec": _structured_team_spec(
                "有贪吃蛇小游戏么",
                intent="question",
                complexity="simple",
            )},
        ))
    ]

    plan = tm.read_plan("auto-fast-turn")["plan"]
    assert plan is not None
    assert [node["node_id"] for node in plan["nodes"]] == ["leader_plan", "fast_execute", "leader_summary"]
    fast_node = next(node for node in plan["nodes"] if node["node_id"] == "fast_execute")
    assert fast_node["metadata"]["execution_mode"] == "fast"
    assert provider.planning_decision_calls == 0
    assert any(chunk.kind == "final" and "贪吃蛇小游戏" in str(chunk.body.get("text") or "") for chunk in chunks)


@pytest.mark.asyncio
async def test_explicit_fast_turn_decision_overrides_direct_chat_route():
    class FastModeProvider(RoleProvider):
        def __init__(self):
            self.planning_decision_calls = 0

        async def chat(self, messages, tools=None, *, max_tokens=None):
            system = messages[0].content if messages else ""
            if "PlanningDecision" in system:
                self.planning_decision_calls += 1
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "直接处理当前节点" in last_user:
                return ChatResponse(text="Fast 模式已处理问候。")
            if "最终汇总" in last_user:
                return ChatResponse(text="团队最终答案：Fast 模式已处理问候。")
            return await super().chat(messages, tools)

    provider = FastModeProvider()
    tm, _ = _team(provider)

    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of(
            "你好",
            session_id="explicit-fast-turn",
            mode="team",
            params={"team_execution_profile": {"requested_mode": "fast"}},
        ))
    ]

    plan = tm.read_plan("explicit-fast-turn")["plan"]
    assert plan is not None
    assert [node["node_id"] for node in plan["nodes"]] == ["leader_plan", "fast_execute", "leader_summary"]
    assert provider.planning_decision_calls == 0
    assert not any(chunk.kind == "status" and "直接回复" in str(chunk.body.get("message") or "") for chunk in chunks)
    assert any(chunk.kind == "final" and "Fast 模式已处理问候" in str(chunk.body.get("text") or "") for chunk in chunks)


@pytest.mark.asyncio
async def test_team_mode_confirmation_followup_sets_execution_profile(monkeypatch):
    tm, _ = _team(config=Config(
        max_iterations=3,
        team_config={
            "execution_profile": {"requested_mode": "standard", "profile_source": "config"},
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation"],
                },
            ]
        },
    ))
    team = tm._build_team("mode-confirm")
    captured: dict[str, object] = {}

    async def fake_send(session_id, questions, **kwargs):
        captured["session_id"] = session_id
        captured["questions"] = questions
        captured["origin"] = kwargs.get("origin")
        return session_id, "mode_question_1"

    async def fake_wait(session_id, question_id):
        return [{"question_id": "team_execution_mode", "answers": ["fast"]}]

    async def fake_ensure(*args, **kwargs):
        captured["execution_profile"] = kwargs.get("execution_profile")
        return None

    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)
    monkeypatch.setattr("crew.team.team_manager.wait_for_answer", fake_wait)
    monkeypatch.setattr(tm, "_ensure_runtime_plan_async", fake_ensure)

    chunks = [
        chunk async for chunk in tm._run_required_workflow(
            Envelope.of(
                "帮我开发一个2048小游戏",
                session_id="mode-confirm",
                user_id="owner",
                params={"team_confirm_execution_mode": True},
            ),
            team=team,
            external_team_id="",
        )
    ]

    assert not any(chunk.kind == "status" for chunk in chunks)
    assert captured["session_id"] == "mode-confirm"
    assert captured["origin"]["agent_id"] == "leader"
    question = captured["questions"][0]
    assert question["id"] == "team_execution_mode"
    labels = "\n".join(option["label"] for option in question["options"])
    assert "自动（默认）" in labels
    assert "AI 深度规划" in labels
    assert captured["execution_profile"] == {
        "requested_mode": "fast",
        "profile_source": "user_followup",
    }


def test_team_mode_confirmation_cancel_defaults_to_auto():
    assert InProcessTeamManager._team_mode_from_followup_answers([]) == "auto"
    assert InProcessTeamManager._team_mode_from_followup_answers(
        [{"id": "__cancelled__", "answers": []}]
    ) == "auto"


async def test_team_runtime_auto_falls_back_to_standard_when_planning_decision_is_invalid():
    provider = JsonProfileProvider()
    tm, _ = _team(provider, config=Config(
        max_iterations=3,
        team_config={
            "members": [
                {
                    "member_id": "dev",
                    "name": "dev",
                    "role": "负责开发实现",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "build"},
                    "capabilities": ["implementation"],
                },
                {
                    "member_id": "qa",
                    "name": "qa",
                    "role": "负责测试验证",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify"},
                    "capabilities": ["qa"],
                },
            ]
        },
    ))
    team = tm._build_team("runtime-profile")

    plan = await tm._ensure_runtime_plan_async(
        "runtime-profile",
        team,
        "帮我开发一个2048小游戏",
        "",
        owner_account_id="local",
        team_spec=_structured_team_spec(
            "帮我开发一个2048小游戏",
            capabilities=["implementation", "testing", "verification"],
            workflow_lanes=("build", "verify"),
        ),
    )

    assert plan is not None
    assert provider.profile_calls == 0
    assert "leader_review" in plan.nodes
    assert "leader_summary" in plan.nodes
    assert "fast_execute" not in plan.nodes
    assert any(node.assignee == "dev" for node in plan.nodes.values())
    assert any(node.assignee == "qa" for node in plan.nodes.values())


def test_team_runtime_reflection_records_event_and_retry_guidance():
    tm, _ = _team()
    team = tm._build_team("runtime-reflect")
    plan = tm._ensure_runtime_plan("runtime-reflect", team, "开发一个登录接口", "", owner_account_id="local")
    assert plan is not None
    node = next(item for item in plan.nodes.values() if item.assignee != "leader")

    tm._reflect_plan_node(
        plan,
        node,
        reason="工具执行失败",
        decision="补充失败上下文后按原成员重试。",
        retryable=True,
    )

    assert "Runtime reflection" in node.detail
    assert "工具执行失败" in node.detail
    assert node.metadata["runtime_reflections"][-1]["retryable"] is True
    assert any(item["event_type"] == "reflection" for item in node.metadata["execution_events"])


def test_team_runtime_reflection_inserts_diagnostic_replan_node():
    tm, _ = _team()
    team = tm._build_team("runtime-replan")
    plan = tm._ensure_runtime_plan(
        "runtime-replan",
        team,
        "开发一个登录接口",
        "",
        owner_account_id="local",
        execution_profile={"requested_mode": "fast"},
        team_spec={"goal": "开发一个登录接口"},
    )
    assert plan is not None
    node = plan.nodes["fast_execute"]
    parent_ids = tm._node_dependencies(plan, node.node_id)

    diagnostic = tm._insert_runtime_diagnostic_node(
        plan,
        node,
        reason="工具执行失败",
        owner_account_id="local",
    )

    assert diagnostic is not None
    assert diagnostic.node_id == "runtime_diagnosis_fast_execute"
    assert diagnostic.assignee == "leader"
    assert plan.nodes[diagnostic.node_id] is diagnostic
    assert tm._node_dependencies(plan, diagnostic.node_id) == parent_ids
    assert tm._node_dependencies(plan, node.node_id) == [diagnostic.node_id]
    assert node.metadata["runtime_diagnostic_node_id"] == diagnostic.node_id
    assert any(item["event_type"] == "replan" for item in node.metadata["execution_events"])


def test_runtime_diagnostic_node_persists_workflow_revision_and_replaces_dependencies(tmp_path):
    store = SQLiteKanbanStore(tmp_path / "runtime-revision.db")
    owner_store = store.for_owner("local")
    tm, _ = _team(kanban_store=store)
    team = tm._build_team("runtime-revision", owner_account_id="local")
    plan = tm._ensure_runtime_plan(
        "runtime-revision",
        team,
        "快速完成一个小工具",
        "",
        owner_account_id="local",
        execution_profile={"requested_mode": "fast"},
        team_spec={"goal": "快速完成一个小工具"},
    )
    assert plan is not None

    diagnostic = tm._insert_runtime_diagnostic_node(
        plan,
        plan.nodes["fast_execute"],
        owner_account_id="local",
        reason="工具执行失败",
    )

    assert diagnostic is not None
    workflow = owner_store.get_latest_workflow_by_session("runtime-revision")
    assert workflow is not None
    assert workflow.context["workflow_plan"]["revision"] == 2
    board = owner_store.get_board_state(workflow.id)
    revised_event = next(event for event in board["events"] if event["event_type"] == "workflow_plan_revised")
    node_task_ids = revised_event["payload"]["node_task_ids"]
    dependency_pairs = {
        (item["parent_task_id"], item["child_task_id"])
        for item in board["dependencies"]
    }
    assert (node_task_ids["leader_plan"], node_task_ids[diagnostic.node_id]) in dependency_pairs
    assert (node_task_ids[diagnostic.node_id], node_task_ids["fast_execute"]) in dependency_pairs
    assert (node_task_ids["leader_plan"], node_task_ids["fast_execute"]) not in dependency_pairs


@pytest.mark.asyncio
async def test_runtime_staffing_e2e_reassigns_executes_and_learns_without_mutating_external_team(
    tmp_path,
    monkeypatch,
):
    external_store = ExternalAgentStore(str(tmp_path / "external.db"))
    runtime = external_store.upsert_runtime({
        "id": "runtime-ready",
        "provider": "custom",
        "name": "Ready Runtime",
        "executable_path": "/bin/sh",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "backend-model",
            "models": [{
                "id": "backend-model",
                "label": "Backend Model",
                "capabilities": ["backend", "implementation"],
            }],
        },
    })
    worker = external_store.create_agent(
        owner_account_id="local",
        name="原后端",
        runtime_id=runtime["id"],
        model="backend-model",
        system_prompt="负责后端实现",
    )
    reserve = external_store.create_agent(
        owner_account_id="local",
        name="后端外援",
        runtime_id=runtime["id"],
        model="backend-model",
        system_prompt="负责后端接口实现、调试和交付",
    )
    external_team = external_store.create_team(
        owner_account_id="local",
        name="接口团队",
        leader_agent_id=CREW_BUILTIN_AGENT_ID,
        members=[
            {"agent_id": CREW_BUILTIN_AGENT_ID, "role": "Leader"},
            {"agent_id": worker["id"], "role": "后端开发", "capabilities": ["backend"]},
        ],
    )
    initial_member_ids = {
        item["agent_id"]
        for item in external_store.get_team(external_team["id"], owner_account_id="local")["members"]
    }
    kanban_store = SQLiteKanbanStore(tmp_path / "kanban.db")
    tm, _ = _team(kanban_store=kanban_store)
    tm.external_store = external_store
    session_id = "runtime-staffing-e2e"
    team = tm._build_team(
        session_id,
        external_team_id=external_team["id"],
        owner_account_id="local",
    )
    tm._teams[tm._key(session_id, "local")] = team
    original_member = next(
        spec.member_id
        for spec in team.members.values()
        if spec.external_agent_id == worker["id"]
    )
    node = TeamPlanNode(
        node_id="build",
        title="实现接口",
        detail="完成后端接口实现",
        assignee=original_member,
        status="failed",
        attempt_count=2,
        delegate_task_id="attempt-2",
        last_error="连续验收失败",
        metadata={"required_capabilities": ["backend"]},
    )
    plan = TeamPlan(team_session_id=session_id, goal="实现接口", nodes={node.node_id: node})
    tm._plans[tm._key(session_id, "local")] = plan
    tm._persist_team_plan(
        plan,
        owner_account_id="local",
        external_team_id=external_team["id"],
        workflow_plan={
            "version": 1,
            "revision": 1,
            "nodes": [{
                "id": node.node_id,
                "title": node.title,
                "assignee_id": node.assignee,
                "required_capabilities": ["backend"],
            }],
            "edges": [],
            "budget_snapshot": {"max_retries": 2},
        },
    )
    trigger = tm._runtime_staffing_trigger(
        team,
        node,
        owner_account_id="local",
        max_attempts=2,
    )
    assert trigger is not None
    assert trigger["trigger_type"] == "acceptance_exhausted"

    captured_questions = []
    captured_followup = {}

    async def fake_send(session_id, questions, **kwargs):
        captured_questions.extend(questions)
        captured_followup.update(kwargs)
        return session_id, "staffing-question"

    async def fake_wait(session_id, question_id, **kwargs):
        return [{"id": captured_questions[0]["id"], "answers": ["candidate:0"]}]

    delegated_members = []

    async def fake_request_delegate(session_id, *, member, **kwargs):
        delegated_members.append(member)
        on_child_chunk = kwargs["on_child_chunk"]
        submission_args = json.dumps({
            "to": ["leader"],
            "intent": "submit",
            "content": "后端接口已实现，成功路径与异常路径均已验证。",
            "node_id": "build",
            "result_status": "pass",
        })
        on_child_chunk(member, ResponseChunk.tool_event(
            "staffed-attempt-1",
            "team_mention",
            "start",
            tool_call_id="submit-staffed-attempt-1",
            args=submission_args,
        ))
        on_child_chunk(member, ResponseChunk.tool_event(
            "staffed-attempt-1",
            "team_mention",
            "result",
            "提交成功",
            tool_call_id="submit-staffed-attempt-1",
        ))
        return {
            "task_id": "staffed-attempt-1",
            "output": "结论：后端接口已经实现并通过验收。\n依据：成功路径与异常路径均已验证。\n风险：无阻断。",
        }

    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)
    monkeypatch.setattr("crew.team.team_manager.wait_for_answer", fake_wait)
    monkeypatch.setattr(tm, "request_delegate", fake_request_delegate)

    chunks = [
        chunk async for chunk in tm._run_required_workflow(
            Envelope.of("实现接口", session_id=session_id, mode="team", user_id="local"),
            team=team,
            external_team_id=external_team["id"],
            execution_profile={"budget": {"max_retries": 2}},
        )
    ]

    rebuilt = tm._teams[tm._key(session_id, "local")]
    assert chunks[-1].kind == "final"
    assert any("协作助手已加入本次任务" in str(chunk.body.get("message") or "") for chunk in chunks)
    assert captured_followup["note"] == "仅用于本次任务，不会加入或修改原团队。"
    assert captured_followup["origin"]["mention_intent"] == "runtime_staffing"
    assert captured_followup["origin"]["type"] == "team_control"
    assert "Runtime 补员" not in captured_questions[0]["question"]
    assert captured_questions[0]["options"][0]["value"] == "candidate:0"
    assert captured_questions[0]["options"][-1]["label"] == "这次先不添加"
    assert delegated_members == [reserve["name"]]
    assert node.assignee == reserve["name"]
    assert node.status == "completed"
    assert node.attempt_count == 1
    assert node.delegate_task_id == "staffed-attempt-1"
    assert rebuilt.runtime_members[node.assignee].external_agent_id == reserve["id"]
    assert node.metadata["runtime_staffing"]["status"] == "applied"
    assert node.metadata["runtime_assignment_history"][-1]["previous_assignee"] == original_member
    assert {
        item["agent_id"]
        for item in external_store.get_team(external_team["id"], owner_account_id="local")["members"]
    } == initial_member_ids

    owner_kanban = kanban_store.for_owner("local")
    workflow = owner_kanban.get_latest_workflow_by_session(session_id)
    assert workflow is not None
    assert workflow.context["workflow_plan"]["revision"] == 2
    assert workflow.context["workflow_plan"]["runtime_members"][0]["external_agent_id"] == reserve["id"]
    board = owner_kanban.get_board_state(workflow.id)
    assert board["tasks"][0]["assignee"] == reserve["name"]
    assert board["tasks"][0]["status"] == "done"
    _, _, recovered_metadata = tm._node_event_index(board["events"])
    assert recovered_metadata[node.node_id]["runtime_staffing"]["status"] == "applied"
    observations = external_store.list_agent_profile_observations(
        reserve["id"],
        owner_account_id="local",
    )
    assert len(observations) == 1
    assert observations[0]["source_attempt_id"] == "staffed-attempt-1"
    assert observations[0]["outcome"] == "success"
    assert observations[0]["capabilities"] == ["backend"]


@pytest.mark.asyncio
async def test_runtime_staffing_timeout_never_auto_approves(tmp_path, monkeypatch):
    external_store = ExternalAgentStore(str(tmp_path / "external.db"))
    runtime = external_store.upsert_runtime({
        "id": "runtime-ready",
        "provider": "custom",
        "name": "Ready Runtime",
        "executable_path": "/bin/sh",
        "metadata": {
            "availability_status": "ready",
            "default_model_id": "backend-model",
            "models": [{"id": "backend-model", "capabilities": ["backend"]}],
        },
    })
    external_store.create_agent(
        owner_account_id="local",
        name="候选外援",
        runtime_id=runtime["id"],
        model="backend-model",
        system_prompt="负责后端实现",
    )
    tm, _ = _team()
    tm.external_store = external_store
    team = tm._build_team("runtime-staffing-timeout", owner_account_id="local")
    node = TeamPlanNode(
        node_id="build",
        title="实现接口",
        assignee="不存在的成员",
        metadata={"required_capabilities": ["backend"]},
    )
    plan = TeamPlan(team_session_id="runtime-staffing-timeout", goal="实现接口", nodes={"build": node})
    tm._plans[tm._key(plan.team_session_id, "local")] = plan
    send_calls = 0

    async def fake_send(session_id, questions, **kwargs):
        nonlocal send_calls
        send_calls += 1
        return session_id, "staffing-question"

    async def fake_wait(session_id, question_id, **kwargs):
        return []

    monkeypatch.setattr("crew.team.team_manager.send_followup_question_to", fake_send)
    monkeypatch.setattr("crew.team.team_manager.wait_for_answer", fake_wait)
    trigger = tm._runtime_staffing_trigger(team, node, owner_account_id="local", max_attempts=2)
    assert trigger is not None

    unchanged, status = await tm._handle_runtime_staffing(
        Envelope.of("实现接口", session_id=plan.team_session_id, mode="team", user_id="local"),
        plan,
        node,
        team,
        trigger,
    )

    assert unchanged is team
    assert status == "awaiting_confirmation"
    assert node.status == "needs_info"
    assert node.assignee == "不存在的成员"
    assert not team.runtime_members
    assert node.metadata["runtime_staffing"]["status"] == "awaiting_confirmation"

    _, repeated_status = await tm._handle_runtime_staffing(
        Envelope.of("实现接口", session_id=plan.team_session_id, mode="team", user_id="local"),
        plan,
        node,
        team,
        trigger,
    )
    assert repeated_status == "awaiting_confirmation"
    assert send_calls == 1


def test_leader_summary_uses_current_summary_contract_without_history_fulltext_compat():
    long_answer = (
        "根据当前系统信息，团队有以下成员：\n\n"
        "| 成员标识 | 角色 |\n"
        "|---------|------|\n"
        "| leader | 团队 Leader，负责任务分配、计划编排与协作驱动 |\n"
        "| crew | 内置智能体，负责测试、QA、回归、Bug 验证 |\n"
        "| kk | 另一团队成员，负责测试设计 |\n\n"
        "此外，系统中还注册了 Explore、general-purpose、Plan、verification 等外部子智能体。"
        "这些不是 Team 成员本身，而是可供调用的辅助能力。"
    )
    plan = TeamPlan(
        team_session_id="summary-preserve",
        goal="团队有什么成员",
        nodes={
            "fast_execute": TeamPlanNode(
                node_id="fast_execute",
                title="快速执行：团队有什么成员",
                assignee=CREW_BUILTIN_AGENT_ID,
                status="completed",
                result_summary=long_answer,
                metadata={"workflow_lane": "docs"},
            ),
            "leader_summary": TeamPlanNode(
                node_id="leader_summary",
                title="Leader 汇总：团队有什么成员",
                assignee="leader",
                status="pending",
                metadata={"workflow_lane": "summary"},
            ),
        },
    )

    summary = InProcessTeamManager._leader_control_text(plan, plan.nodes["leader_summary"])

    assert "根据当前系统信息，团队有以下成员" in summary
    assert "Explore、general-purpose、Plan、verification" not in summary
    assert "..." in summary


def test_leader_summary_fallback_without_member_results_does_not_claim_done():
    plan = TeamPlan(
        team_session_id="summary-no-member-results",
        goal="找中国2个不同城市的隐藏小吃，并配一句本地话做点评",
        nodes={
            "leader_plan": TeamPlanNode(
                node_id="leader_plan",
                title="Leader 拆分任务",
                assignee="leader",
                status="completed",
                result_summary="收到，我来处理",
            ),
            "leader_summary": TeamPlanNode(
                node_id="leader_summary",
                title="Leader 汇总",
                assignee="leader",
                status="pending",
                metadata={"workflow_lane": "summary"},
            ),
        },
    )

    summary = InProcessTeamManager._leader_control_text(
        plan,
        plan.nodes["leader_summary"],
        fallback_error="LLM 流式调用失败: insufficient balance",
    )

    assert "本次团队任务已完成" not in summary
    assert "团队最终汇总没有生成答案" in summary
    assert "暂无可汇总的成员结果" in summary
    assert "insufficient balance" in summary


def test_leader_summary_uses_compact_runtime_summary_not_member_process_tail():
    plan = TeamPlan(
        team_session_id="summary-compact-runtime",
        goal="你们团队擅长做什么工作",
        nodes={
            "fast_execute": TeamPlanNode(
                node_id="fast_execute",
                title="快速执行：你们团队擅长做什么工作",
                assignee=CREW_BUILTIN_AGENT_ID,
                status="completed",
                result_summary="快速执行：团队擅长像素游戏需求分析、技术方案、测试验证和交付风险说明。",
                metadata={
                    "workflow_lane": "verify",
                    "full_result_ref": "/tmp/team-results/fast_execute.txt",
                },
            ),
            "leader_summary": TeamPlanNode(
                node_id="leader_summary",
                title="Leader 汇总：你们团队擅长做什么工作",
                assignee="leader",
                status="pending",
                metadata={"workflow_lane": "summary"},
            ),
        },
    )

    summary = InProcessTeamManager._leader_control_text(plan, plan.nodes["leader_summary"])

    assert "团队擅长像素游戏" in summary
    assert "下一负责人" not in summary
    assert "下一动作" not in summary


async def test_team_runtime_display_end_to_end_for_parallel_qa_and_security(tmp_path):
    class TeamE2EProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            current_title = last_user.splitlines()[0] if last_user else ""
            if "测试方案" in current_title:
                return ChatResponse(text=(
                    "测试方案已完成。\n\n"
                    "### 缺陷记录方式\n"
                    "- 统一 Issue 模板：`标题 | 环境 | 复现步骤 | 期望结果 | 实际结果 | 日志/截图 | 严重级别 | 指派 | 关闭标准`\n"
                    "- 结尾完整性标记：FULL_MARKDOWN_TAIL"
                ))
            if "安全方案" in current_title:
                return ChatResponse(text="安全方案已完成，覆盖权限、隐私和异常输入，提交给 Leader 审阅。")
            if "测试验证" in current_title:
                return ChatResponse(text="测试验证已完成，核心功能与回归路径通过。")
            if "安全验证" in current_title:
                return ChatResponse(text="安全验证已完成，未发现权限、隐私或工具输出暴露风险。")
            return ChatResponse(text="节点完成。")

        async def stream_chat(self, messages, tools=None):
            resp = await self.chat(messages, tools)
            if resp.text:
                yield StreamChunk(delta_text=resp.text)
            else:
                yield StreamChunk(delta_text="我先处理当前节点。")
            yield StreamChunk(done=True, finish_reason=resp.finish_reason)

    store = SQLiteKanbanStore(tmp_path / "team-e2e.db")
    tm, _ = _team(provider=TeamE2EProvider(), kanban_store=store, config=Config(
        max_iterations=5,
        team_config={
            "members": [
                {
                    "member_id": CREW_BUILTIN_AGENT_ID,
                    "name": "Crew 内置智能体",
                    "role": "负责安全测试，输出格式包含风险/阻塞。",
                    "executor": "builtin",
                    "metadata": {
                        "workflow_lane": "verify",
                        "role_key": "security_engineer",
                        "role_label": "安全工程师",
                    },
                    "capabilities": ["security", "privacy", "permission", "review", "risk"],
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责测试验证，输出格式包含风险/阻塞。",
                    "executor": "builtin",
                    "metadata": {
                        "workflow_lane": "verify",
                        "role_key": "qa_engineer",
                        "role_label": "测试工程师",
                    },
                    "capabilities": ["test", "qa", "regression", "bug", "quality"],
                },
            ]
        },
    ))

    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of(
            "测试一下贪吃蛇",
            session_id="team-e2e-display",
            mode="team",
            params={"team_spec": _structured_team_spec(
                "测试一下贪吃蛇",
                capabilities=["testing", "verification", "review"],
                intent="testing",
                workflow_lanes=("verify",),
            )},
        ))
    ]
    internal = [chunk for chunk in chunks if chunk.kind == "team_internal"]
    final_text = next(chunk.body["text"] for chunk in chunks if chunk.kind == "final")

    plan = tm.read_plan("team-e2e-display", owner_account_id="local")["plan"]
    by_id = {node["node_id"]: node for node in plan["nodes"]}
    assert by_id["qa_engineer_plan_2"]["title"].startswith("测试方案：")
    assert by_id["security_engineer_plan_1"]["title"].startswith("安全方案：")
    assert by_id["qa_engineer_verify_2"]["title"].startswith("测试验证：")
    assert by_id["security_engineer_verify_1"]["title"].startswith("安全验证：")
    assert "先不要执行" in by_id["qa_engineer_plan_2"]["detail"]
    assert "先不要执行" in by_id["security_engineer_plan_1"]["detail"]
    assert "可以验收" in final_text
    assert "核心功能与回归路径通过" in final_text
    assert "未发现权限、隐私或工具输出暴露风险" in final_text

    submit_chunks = [
        chunk for chunk in internal
        if chunk.body.get("event_type") == "team_submit" and chunk.body.get("mention_intent") == "submit"
    ]
    result_chunks = [
        chunk for chunk in internal
        if chunk.body.get("event_type") == "team_submit" and chunk.body.get("mention_intent") == "handoff"
    ]
    assert {chunk.body["node_id"] for chunk in submit_chunks} == {"security_engineer_plan_1", "qa_engineer_plan_2"}
    assert {chunk.body["node_id"] for chunk in result_chunks} == {"security_engineer_verify_1", "qa_engineer_verify_2"}
    assert all(chunk.body.get("display_mode") != "collapsible" for chunk in [*submit_chunks, *result_chunks])
    assert all("用户审阅" not in str(chunk.body.get("text") or "") for chunk in [*submit_chunks, *result_chunks])
    assert all("@leader" in str(chunk.body.get("text") or "") for chunk in submit_chunks)
    leader_review = next(
        chunk for chunk in internal
        if chunk.body.get("event_type") == "team_review"
    )
    leader_review_text = str(leader_review.body.get("text") or "")
    assert "@crew" in leader_review_text
    assert "@kk" in leader_review_text
    assert "方案已通过 Leader 审阅，开始验证" in leader_review_text
    assign_chunks = [
        chunk for chunk in internal
        if chunk.body.get("event_type") == "team_assign" and chunk.body.get("mention_intent") == "assign"
    ]
    assign_by_node = {str(chunk.body.get("node_id") or ""): chunk for chunk in assign_chunks}
    plan_assignments = [
        chunk
        for node_id, chunk in assign_by_node.items()
        if (by_id[node_id].get("metadata") or {}).get("workflow_lane") in {"plan", "design"}
    ]
    verify_assignments = [
        chunk
        for node_id, chunk in assign_by_node.items()
        if (by_id[node_id].get("metadata") or {}).get("workflow_lane") == "verify"
    ]
    assert plan_assignments
    assert verify_assignments
    assert all("先不要执行验证" in str(chunk.body.get("text") or "") for chunk in plan_assignments)
    assert all("先不要执行验证" not in str(chunk.body.get("text") or "") for chunk in verify_assignments)
    qa_submit = next(chunk for chunk in submit_chunks if chunk.body.get("node_id") == "qa_engineer_plan_2")
    assert str(qa_submit.body.get("text") or "").startswith("@leader 测试方案：")
    assert "请审阅" in str(qa_submit.body.get("text") or "")
    assert "FULL_MARKDOWN_TAIL" not in str(qa_submit.body.get("text") or "")
    qa_result = next(chunk for chunk in result_chunks if chunk.body.get("node_id") == "qa_engineer_verify_2")
    qa_result_text = str(qa_result.body.get("text") or "")
    assert "核心功能与回归路径通过" in qa_result_text
    assert "测试验证已完成" in qa_result_text
    security_result = next(chunk for chunk in result_chunks if chunk.body.get("node_id") == "security_engineer_verify_1")
    assert "未发现权限、隐私或工具输出暴露风险" in str(security_result.body.get("text") or "")
    qa_artifacts = qa_submit.body.get("artifacts") or []
    assert qa_artifacts
    qa_artifact_path = Path(str(qa_artifacts[0].get("path") or ""))
    assert qa_artifact_path.suffix == ".md"
    assert qa_artifact_path.exists()
    assert "FULL_MARKDOWN_TAIL" in qa_artifact_path.read_text(encoding="utf-8")

    visible_messages: list[dict[str, object]] = []
    for chunk in internal:
        body = chunk.body
        event_type = body.get("event_type")
        matching_index = next((
            index for index in range(len(visible_messages) - 1, -1, -1)
            if visible_messages[index].get("source_session_id") == body.get("source_session_id")
            and visible_messages[index].get("agent_id") == body.get("agent_id")
            and visible_messages[index].get("node_id") == body.get("node_id")
        ), -1)
        if (
            event_type in {"team_submit", "team_summary"}
            and matching_index >= 0
        ):
            if visible_messages[matching_index].get("display_mode") in {"stream", "collapsible"}:
                process_text = visible_messages[matching_index].get("process_text") or visible_messages[matching_index].get("text")
                visible_messages[matching_index].update(dict(body))
                visible_messages[matching_index]["display_mode"] = body.get("display_mode") or "chat"
                visible_messages[matching_index]["process_text"] = process_text
            else:
                visible_messages[matching_index] = dict(body)
        elif body.get("append") and matching_index >= 0 and visible_messages[matching_index].get("display_mode") in {"stream", "collapsible"}:
            visible_messages[matching_index]["text"] = f"{visible_messages[matching_index].get('text') or ''}{body.get('text') or ''}"
        else:
            visible_messages.append(dict(body))
    completed_node_ids = {
        *[chunk.body["node_id"] for chunk in submit_chunks],
        *[chunk.body["node_id"] for chunk in result_chunks],
    }
    for message in visible_messages:
        if message.get("node_id") in completed_node_ids:
            assert message.get("event_type") != "team_stream"
            assert message.get("display_mode") != "collapsible"

    restored_history = tm.event_history_for_session("team-e2e-display", owner_account_id="local")
    restored_qa = next(
        item for item in restored_history
        if item.get("node_id") == "qa_engineer_verify_2"
        and item.get("mention_intent") == "handoff"
    )
    assert "核心功能与回归路径通过" in str(restored_qa.get("content") or "")
    assert "测试验证已完成" in str(restored_qa.get("process_text") or "")

    projected = tm.task_projection_for_session("team-e2e-display", owner_account_id="local")
    projected_by_node = {item["progress"]["plan_node_id"]: item for item in projected}
    assert projected_by_node["qa_engineer_plan_2"]["progress"]["parent_node_ids"] == projected_by_node["security_engineer_plan_1"]["progress"]["parent_node_ids"]
    assert projected_by_node["qa_engineer_verify_2"]["progress"]["parent_node_ids"] == projected_by_node["security_engineer_verify_1"]["progress"]["parent_node_ids"]


async def test_simple_team_message_goes_to_direct_leader_without_teamplan():
    class GreetingProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "你好" in last_user:
                return ChatResponse(text="你好，我在。")
            return await super().chat(messages, tools)

    tm, _ = _team(GreetingProvider())
    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("你好", session_id="simple-team", mode="team"))
    ]

    assert tm.read_plan("simple-team")["plan"] is None
    assert any(chunk.kind == "status" and "直接回复" in str(chunk.body.get("message") or "") for chunk in chunks)
    assert any(chunk.kind == "final" and "你好" in str(chunk.body.get("text") or "") for chunk in chunks)


def test_team_turn_decision_accepts_direct_chat_kind():
    decision = coerce_team_turn_decision(
        {
            "turn_kind": "direct_chat",
            "execution_mode": "standard",
            "reason": "轻量聊天",
        },
        has_existing_workflow=True,
    )

    assert decision.turn_kind == "direct_chat"
    assert decision.execution_mode == "direct"
    assert decision.is_direct_chat is True
    assert decision.is_status_query is False
    assert decision.status_query is None


def test_team_turn_decision_accepts_fast_workflow_kind():
    decision = new_workflow_decision("fast", "轻量问题", source="team_spec_auto_fast")

    assert decision.turn_kind == "new_workflow"
    assert decision.execution_mode == "fast"
    assert decision.is_direct_chat is False
    assert decision.is_status_query is False
    assert decision.diagnostics["source"] == "team_spec_auto_fast"


async def test_simple_message_direct_for_test_team_roles():
    class GreetingProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "你好" in last_user:
                return ChatResponse(text="你好，我在。")
            return await super().chat(messages, tools)

    tm, tasks = _team(GreetingProvider(), config=Config(
        max_iterations=5,
        team_config={
            "members": [
                {
                    "member_id": CREW_BUILTIN_AGENT_ID,
                    "name": "Crew 内置智能体",
                    "role": "负责安全测试",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "security_engineer"},
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责测试设计",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                },
            ]
        },
    ))

    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("你好", session_id="test-team-simple", mode="team"))
    ]

    assert tm.read_plan("test-team-simple")["plan"] is None
    assert tasks.list("test-team-simple") == []
    assert tm._build_team("test-team-simple").bus.list_artifacts("test-team-simple") == []
    assert any(chunk.kind == "status" and "直接回复" in str(chunk.body.get("message") or "") for chunk in chunks)
    assert any(chunk.kind == "final" and "你好" in str(chunk.body.get("text") or "") for chunk in chunks)


async def test_team_info_question_keeps_team_bubble_process_and_full_summary():
    seen_messages: list[list[Message]] = []

    class TeamInfoProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            seen_messages.append(messages)
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "团队成员" in last_user:
                return ChatResponse(text="当前团队成员：Leader、Crew 内置智能体、kk。")
            return await super().chat(messages, tools)

    tm, tasks = _team(TeamInfoProvider(), config=Config(
        max_iterations=5,
        team_config={
            "members": [
                {
                    "member_id": CREW_BUILTIN_AGENT_ID,
                    "name": "Crew 内置智能体",
                    "role": "负责安全测试",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "security_engineer"},
                },
                {
                    "member_id": "kk",
                    "name": "kk",
                    "role": "负责测试设计",
                    "executor": "builtin",
                    "metadata": {"workflow_lane": "verify", "role_key": "qa_engineer"},
                },
            ]
        },
    ))

    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("你的团队成员有哪些", session_id="team-info", mode="team"))
    ]

    plan = tm.read_plan("team-info")["plan"]
    assert plan is not None
    assert not tasks.list("team-info")
    final_text = next(chunk.body["text"] for chunk in chunks if chunk.kind == "final")
    assert "当前团队成员：Leader、Crew 内置智能体、kk" in final_text
    assert "..." not in final_text
    leader_answer = next(
        chunk for chunk in chunks
        if chunk.kind == "team_internal"
        and chunk.body.get("agent_id") == "leader"
        and "当前团队成员" in str(chunk.body.get("text") or "")
    )
    finished_node = next(
        node for node in plan["nodes"]
        if node["node_id"] == leader_answer.body.get("node_id") and node["status"] == "completed"
    )
    assert "当前团队成员：Leader、Crew 内置智能体、kk" in str(finished_node.get("result_summary") or "")
    prompt_text = "\n".join(message.content for messages in seen_messages for message in messages)
    assert "Crew 内置智能体" in prompt_text
    assert "kk" in prompt_text


async def test_team_status_question_keeps_full_result_without_direct_route_rule():
    class StatusProvider(RoleProvider):
        def __init__(self):
            self.turn_decision_calls = 0

        async def chat(self, messages, tools=None):
            if "TeamTurnDecision" in messages[0].content:
                self.turn_decision_calls += 1
                return ChatResponse(text='{"turn_kind":"status_query","execution_mode":"direct","reason":"status","status_query":{"question":"运行状态","scope":"latest_turn","needs":["nodes"]}}')
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "还在运行" in last_user:
                return ChatResponse(text="我来查看当前团队运行状态。")
            return await super().chat(messages, tools)

    provider = StatusProvider()
    tm, tasks = _team(provider)
    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("你还在运行么", session_id="team-status", mode="team"))
    ]

    assert provider.turn_decision_calls == 0
    assert tm.read_plan("team-status")["plan"] is not None
    assert tasks.list("team-status")
    final_text = next(chunk.body["text"] for chunk in chunks if chunk.kind == "final")
    assert "我来查看当前团队运行状态" in final_text


async def test_team_status_duration_query_reads_snapshot_without_new_workflow(tmp_path):
    class StatusQueryProvider(RoleProvider):
        def __init__(self):
            self.turn_decision_calls = 0
            self.status_summary_calls = 0

        async def chat(self, messages, tools=None, *, max_tokens=None):
            system = messages[0].content if messages else ""
            if "TeamTurnDecision" in system:
                self.turn_decision_calls += 1
                return ChatResponse(text=json.dumps({
                    "turn_kind": "status_query",
                    "execution_mode": "direct",
                    "reason": "用户询问已有团队运行事实",
                    "status_query": {
                        "question": "刚刚用了多久，谁做了什么",
                        "scope": "latest_turn",
                        "needs": ["duration", "members", "nodes", "planning"],
                    },
                }, ensure_ascii=False))
            if "TeamStatusSnapshot" in system:
                self.status_summary_calls += 1
                payload = json.loads(messages[-1].content)
                snapshot = payload["TeamStatusSnapshot"]
                nodes = snapshot.get("nodes") or []
                assignees = sorted({node.get("assignee") for node in nodes if node.get("assignee")})
                return ChatResponse(text=f"刚刚这轮已有运行记录，成员包括：{', '.join(assignees)}；节点数 {len(nodes)}。")
            return await super().chat(messages, tools)

    class FailingPlanner:
        async def plan_async(self, *args, **kwargs):
            raise AssertionError("status query should not call PlanningDecision or create a new DAG")

    store = SQLiteKanbanStore(tmp_path / "team-status-query.db")
    provider = StatusQueryProvider()
    tm, _ = _team(provider, kanban_store=store)

    first_chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("组队算1+1", session_id="status-query-team", mode="team"))
    ]
    assert any(chunk.kind == "team_internal" for chunk in first_chunks)
    owner_store = store.for_owner("local")
    workflows_before = [
        workflow
        for workflow in owner_store.list_workflows_by_session_prefix("status-query-team")
        if (workflow.context or {}).get("source") == "team"
    ]
    assert workflows_before

    tm.graph_planner = FailingPlanner()
    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("完成这个项目花了多长时间？", session_id="status-query-team", mode="team"))
    ]

    workflows_after = [
        workflow
        for workflow in owner_store.list_workflows_by_session_prefix("status-query-team")
        if (workflow.context or {}).get("source") == "team"
    ]
    final_text = next(chunk.body["text"] for chunk in chunks if chunk.kind == "final")
    assert provider.turn_decision_calls == 0
    assert provider.status_summary_calls == 1
    assert len(workflows_after) == len(workflows_before)
    assert tm.read_plan("status-query-team", owner_account_id="local")["plan"] is not None
    assert "节点数" in final_text
    assert any(
        chunk.kind == "team_internal"
        and chunk.body.get("event_type") == "team_summary"
        and "节点数" in str(chunk.body.get("text") or "")
        for chunk in chunks
    )


async def test_direct_leader_continue_gets_team_context_summary():
    seen_messages: list[list[Message]] = []

    class ContinueProvider(RoleProvider):
        async def chat(self, messages, tools=None):
            seen_messages.append(messages)
            last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
            if "继续" in last_user and any("Team 历史上下文" in m.content for m in messages):
                return ChatResponse(text="我会继续之前的贪吃蛇测试任务。")
            return ChatResponse(text="没有上下文。")

    tm, tasks = _team(ContinueProvider())
    parent_task = tasks.create("team-continue", "你能帮我测试一下我的贪吃蛇游戏么")
    tasks.update_status(parent_task["id"], "cancelled", "已停止当前回复")
    team_task = tasks.create(
        "team-continue",
        "测试设计：你能帮我测试一下我的贪吃蛇游戏么",
        assignee="kk",
    )
    tasks.update_status(team_task["id"], "failed", "外部智能体调用失败")

    chunks = [
        chunk
        async for chunk in tm.interact(Envelope.of("继续", session_id="team-continue", mode="team"))
    ]

    assert any("继续之前的贪吃蛇测试任务" in str(chunk.body.get("text") or "") for chunk in chunks if chunk.kind == "final")
    assert any("Team 历史上下文" in message.content for messages in seen_messages for message in messages)


def test_task_manager_lifecycle():
    tasks = InMemoryTaskManager()
    t = tasks.create("s1", "标题", assignee="coder")
    assert t["status"] == "in_progress"
    tasks.update_status(t["id"], "done", "结果")
    assert tasks.get(t["id"])["status"] == "done"
    assert tasks.list("s1")[0]["result"] == "结果"


def test_legacy_task_manager_adapter_create_accepts_owner_account_id(tmp_path):
    runtime = TaskRuntime(str(tmp_path / "tasks.db"))
    tasks = LegacyTaskManagerAdapter(runtime)

    task = tasks.create(
        "same-session",
        "实现：你好",
        detail="执行团队任务",
        assignee="codex",
        owner_account_id="A:uid-a",
    )

    assert task["status"] == "in_progress"
    assert runtime.get(task["task_id"], owner_account_id="A:uid-a")["title"] == "实现：你好"
    with pytest.raises(KeyError):
        runtime.get(task["task_id"], owner_account_id="B:uid-b")


def test_legacy_task_manager_adapter_preserves_cancelled_status(tmp_path):
    runtime = TaskRuntime(str(tmp_path / "tasks.db"))
    tasks = LegacyTaskManagerAdapter(runtime)
    task = tasks.create("cancel-session", "可取消任务", assignee="worker")

    cancelled = tasks.update_status(task["id"], "cancelled", "stop")

    assert cancelled["status"] == "cancelled"
    assert runtime.get(task["task_id"])["status"] == "cancelled"


def test_team_parent_session_history_aggregates_child_sessions(auth_headers):
    class Store:
        def session_belongs_to(self, session_id: str, owner_account_id: str):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return True

        def load(self, session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return [Message(role="user", content="后台任务已完成，请根据结果继续原任务。", is_meta=True, timestamp=3)]

        def load_child_sessions(self, session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return [
                ("team_parent::turn::r1::leader", [
                    Message(role="user", content="帮我做一个方案", timestamp=1),
                    Message(role="assistant", content="我来拆解并派活。", timestamp=2),
                ]),
                ("team_parent::turn::r1::kk", [
                    Message(role="user", content="请输出架构设计", timestamp=1.5),
                    Message(
                        role="assistant",
                        content="架构设计已完成。",
                        timestamp=2.5,
                        communication_kind="user_mention_answer",
                        communication_status="answered",
                        request_id="mention_req",
                        reply_to="bus_msg",
                    ),
                ]),
                ("team_parent::turn::r1::tool_member", [
                    Message(
                        role="assistant",
                        content="",
                        tool_calls=[ToolCall("tc1", "external_tool", {"query": "内部检索"})],
                        timestamp=2.6,
                    ),
                ]),
            ]

        def get_agent_config(self, session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return {"executor": "team"}

    class Crew:
        session_store = Store()
        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return []

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_sessions_router(Crew(), dispatcher=None))
    response = TestClient(app).get("/api/session/team_parent", headers=auth_headers)
    assert response.status_code == 200
    assert [item["content"] for item in response.json()] == [
        "我来拆解并派活。",
        "架构设计已完成。",
    ]
    assert [item["source_session_id"] for item in response.json()] == [
        "team_parent::turn::r1::leader",
        "team_parent::turn::r1::kk",
    ]
    assert [item["role"] for item in response.json()] == ["team_internal", "team_internal"]
    assert response.json()[1]["communication_kind"] == "user_mention_answer"
    assert response.json()[1]["communication_status"] == "answered"
    assert response.json()[1]["request_id"] == "mention_req"
    assert response.json()[1]["reply_to"] == "bus_msg"


def test_team_recovery_gateway_routes_node_action_to_team_manager(auth_headers):
    calls: list[dict] = []

    class Store:
        def session_belongs_to(self, session_id: str, owner_account_id: str):
            return session_id == "team_recovery" and owner_account_id == "A:uid-a"

        def get_agent_config(self, session_id: str, owner_account_id: str = ""):
            return {"executor": "team"}

    class Team:
        def recover_plan_node(self, session_id: str, **kwargs):
            calls.append({"session_id": session_id, **kwargs})
            return {"ok": True, "node": {"node_id": kwargs["node_id"]}}

    class Crew:
        session_store = Store()
        team = Team()

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_sessions_router(Crew(), dispatcher=None))
    response = TestClient(app).post(
        "/api/session/team_recovery/team/recover",
        headers=auth_headers,
        json={
            "node_id": "verify",
            "action": "reassign",
            "replacement_assignee": "hh",
        },
    )

    assert response.status_code == 200
    assert calls == [{
        "session_id": "team_recovery",
        "node_id": "verify",
        "action": "reassign",
        "replacement_assignee": "hh",
        "owner_account_id": "A:uid-a",
    }]


def test_team_parent_session_history_prefers_kanban_events_over_child_sessions(auth_headers):
    class Store:
        def session_belongs_to(self, session_id: str, owner_account_id: str):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return True

        def load(self, session_id: str, owner_account_id: str = ""):
            return []

        def load_child_sessions(self, session_id: str, owner_account_id: str = ""):
            return [
                ("team_parent::turn::r1::crew", [
                    Message(role="assistant", content="Crew 子会话最终总结，不应展示。", timestamp=3),
                ]),
            ]

        def get_agent_config(self, session_id: str, owner_account_id: str = ""):
            return {"executor": "team"}

    class Team:
        @staticmethod
        def event_history_for_session(session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return [{
                "role": "team_internal",
                "content": "Leader 已完成最终总结。",
                "agent_id": "leader",
                "agent_name": "hh",
                "agent_role": "leader",
                "is_leader": True,
                "source_session_id": "team_parent::turn::r1::leader",
                "timestamp": 4,
            }]

    class Crew:
        session_store = Store()
        team = Team()
        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return []

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_sessions_router(Crew(), dispatcher=None))
    response = TestClient(app).get("/api/session/team_parent", headers=auth_headers)
    assert response.status_code == 200
    assert [item["content"] for item in response.json()] == ["Leader 已完成最终总结。"]


def test_team_history_restores_direct_mention_sender_identity_for_old_events():
    class ExternalAgents:
        @staticmethod
        def get_team(team_id: str, *, owner_account_id: str = ""):
            assert team_id == "team_1"
            assert owner_account_id == "local"
            return {
                "leader_agent_id": CREW_BUILTIN_AGENT_ID,
                "members": [
                    {"agent_id": CREW_BUILTIN_AGENT_ID, "agent_name": "Crew", "role": "leader"},
                    {"agent_id": "agent_kk", "agent_name": "kk", "role": "负责全栈开发"},
                ],
            }

    class Team:
        @staticmethod
        def event_history_for_session(session_id: str, owner_account_id: str = ""):
            return [{
                "role": "team_internal",
                "content": "我是 kk，当前使用的模型是 kimi-code/k3。",
                "agent_id": CREW_BUILTIN_AGENT_ID,
                "agent_name": "Crew",
                "communication_kind": "user_mention_answer",
                "communication_status": "answered",
                "mention_from": "kk",
                "mention_to": ["user"],
                "request_id": "mention_1",
                "timestamp": 1,
            }]

    class Crew:
        external_agents = ExternalAgents()
        team = Team()

    items = team_internal_history_items(
        Crew(),
        "team_parent",
        [],
        owner_account_id="local",
        config={"team": {"external_team_id": "team_1"}},
    )

    assert len(items) == 1
    assert items[0]["agent_id"] == "agent_kk"
    assert items[0]["agent_name"] == "kk"
    assert items[0]["agent_id"] != CREW_BUILTIN_AGENT_ID


def test_team_history_maps_legacy_crew_child_session_to_builtin_identity():
    class Crew:
        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return []

    items = team_internal_history_items(
        Crew(),
        "team_parent",
        [
            (
                "team_parent::turn::req_1::crew",
                [Message(role="assistant", content="Crew 已完成 QA。", timestamp=1)],
            )
        ],
    )

    assert len(items) == 1
    assert items[0]["agent_id"] == CREW_BUILTIN_AGENT_ID
    assert items[0]["agent_name"] == "Crew"
    assert items[0]["source_session_id"] == "team_parent::turn::req_1::crew"


def test_team_history_keeps_distinct_communication_replies_with_same_text():
    class Crew:
        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return []

    items = team_internal_history_items(
        Crew(),
        "team_parent",
        [
            (
                "team_parent::turn::req_1::coder",
                [Message(
                    role="assistant",
                    content="当前使用 K3 模型。",
                    timestamp=1,
                    communication_kind="user_mention_answer",
                    communication_status="answered",
                    request_id="req_1",
                    reply_to="bus_1",
                )],
            ),
            (
                "team_parent::turn::req_2::coder",
                [Message(
                    role="assistant",
                    content="当前使用 K3 模型。",
                    timestamp=2,
                    communication_kind="user_mention_answer",
                    communication_status="answered",
                    request_id="req_2",
                    reply_to="bus_2",
                )],
            ),
        ],
    )

    assert len(items) == 2
    assert [item["request_id"] for item in items] == ["req_1", "req_2"]
    assert items[0]["communication_kind"] == "user_mention_answer"
    assert items[0]["communication_status"] == "answered"
    assert items[0]["reply_to"] == "bus_1"


def test_team_history_preserves_terminal_user_mention_state_for_retry():
    class Crew:
        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return []

    items = team_internal_history_items(
        Crew(),
        "team_parent",
        [
            (
                "team_parent::turn::req_expired::coder",
                [Message(
                    role="assistant",
                    content="coder 的回答已超时。",
                    timestamp=3,
                    communication_kind="user_mention_answer",
                    communication_status="expired",
                    request_id="req_expired",
                    reply_to="bus_expired",
                    communication_request_text="@coder 你现在用什么模型？",
                )],
            )
        ],
    )

    assert items == [{
        "role": "team_internal",
        "content": "coder 的回答已超时。",
        "agent_id": "coder",
        "agent_name": "coder",
        "agent_role": "",
        "is_leader": False,
        "agent_tone": 0,
        "source_session_id": "team_parent::turn::req_expired::coder",
        "timestamp": 3,
        "communication_kind": "user_mention_answer",
        "communication_status": "expired",
        "request_id": "req_expired",
        "reply_to": "bus_expired",
        "communication_request_text": "@coder 你现在用什么模型？",
    }]


def test_team_parent_direct_reply_history_uses_leader_identity(auth_headers):
    class Store:
        def session_belongs_to(self, session_id: str, owner_account_id: str):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return True

        def load(self, session_id: str, owner_account_id: str = ""):
            return []

        def load_child_sessions(self, session_id: str, owner_account_id: str = ""):
            return []

        def get_agent_config(self, session_id: str, owner_account_id: str = ""):
            return {"executor": "team", "team": {"external_team_id": "team_1"}}

    class ExternalAgents:
        @staticmethod
        def get_team(team_id: str, *, owner_account_id: str = ""):
            assert team_id == "team_1"
            assert owner_account_id == "A:uid-a"
            return {
                "leader_agent_id": "agent_hh",
                "members": [
                    {
                        "agent_id": "agent_hh",
                        "agent_name": "hh",
                        "role": "leader",
                        "role_label": "leader",
                    }
                ],
            }

    class Crew:
        session_store = Store()
        external_agents = ExternalAgents()

        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return [{
                    "kind": "agent_turn",
                    "session_id": "team_parent",
                    "detail": "你好",
                    "result": "你好！我是 hh，很高兴在 Crew 团队里与你交流。",
                    "created_at": 1,
                    "updated_at": 2,
                }]

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_sessions_router(Crew(), dispatcher=None))
    response = TestClient(app).get("/api/session/team_parent", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == [
        {
            "role": "user",
            "content": "你好",
            "source_session_id": "team_parent",
            "timestamp": 1.0,
        },
        {
            "role": "team_internal",
            "content": "你好！我是 hh，很高兴在 Crew 团队里与你交流。",
            "source_session_id": "team_parent",
            "timestamp": 2.0,
            "agent_id": "agent_hh",
            "agent_name": "hh",
            "agent_role": "leader",
            "is_leader": True,
            "agent_tone": 0,
        },
    ]


def test_team_parent_session_history_dedupes_parent_final_against_leader_summary(auth_headers):
    class Store:
        def session_belongs_to(self, session_id: str, owner_account_id: str):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return True

        def load(self, session_id: str, owner_account_id: str = ""):
            return []

        def load_child_sessions(self, session_id: str, owner_account_id: str = ""):
            return []

        def get_agent_config(self, session_id: str, owner_account_id: str = ""):
            return {"executor": "team"}

    class Team:
        @staticmethod
        def event_history_for_session(session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return [{
                "role": "team_internal",
                "content": "本次团队任务完成。测试结论：可以验收。",
                "event_type": "team_summary",
                "agent_id": "leader",
                "agent_name": "hh",
                "agent_role": "leader",
                "is_leader": True,
                "source_session_id": "team_parent::turn::r1::leader",
                "timestamp": 4,
            }]

    class Crew:
        session_store = Store()
        team = Team()

        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return [{
                    "kind": "agent_turn",
                    "session_id": "team_parent",
                    "detail": "测试一下之前开发的贪吃蛇是否可验收",
                    "result": "本次团队任务完成。测试结论：可以验收。",
                    "created_at": 1,
                    "updated_at": 5,
                }]

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_sessions_router(Crew(), dispatcher=None))
    response = TestClient(app).get("/api/session/team_parent", headers=auth_headers)
    assert response.status_code == 200
    assert [(item["role"], item["content"]) for item in response.json()] == [
        ("user", "测试一下之前开发的贪吃蛇是否可验收"),
        ("team_internal", "本次团队任务完成。测试结论：可以验收。"),
    ]


def test_team_parent_session_history_suppresses_child_fallback_when_team_workflow_exists(auth_headers):
    class Store:
        def session_belongs_to(self, session_id: str, owner_account_id: str):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return True

        def load(self, session_id: str, owner_account_id: str = ""):
            return []

        def load_child_sessions(self, session_id: str, owner_account_id: str = ""):
            return [
                ("team_parent::turn::r1::leader", [
                    Message(role="assistant", content="Leader 最终总结。", timestamp=2),
                ]),
                ("team_parent::turn::r1::crew", [
                    Message(role="assistant", content="Crew 子会话最终总结，不应展示。", timestamp=3),
                ]),
            ]

        def get_agent_config(self, session_id: str, owner_account_id: str = ""):
            return {"executor": "team"}

    class Team:
        @staticmethod
        def event_history_for_session(session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return []

        @staticmethod
        def has_team_workflow_for_session(session_id: str, owner_account_id: str = ""):
            assert session_id == "team_parent"
            assert owner_account_id == "A:uid-a"
            return True

    class Crew:
        session_store = Store()
        team = Team()
        class tasks:
            @staticmethod
            def list_tasks(*args, **kwargs):
                return []

    app = FastAPI()

    @app.middleware("http")
    async def attach_test_account(request, call_next):
        request.state.account = AccountContext(owner_account_id="A:uid-a")
        return await call_next(request)

    app.include_router(create_sessions_router(Crew(), dispatcher=None))
    response = TestClient(app).get("/api/session/team_parent", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.asyncio
async def test_cancel_owner_stops_owner_teams_and_delegates_without_cross_owner_effect():
    """cancel_owner 清掉指定 owner 的 team 缓存 / 活跃子 agent / detached 后台委派，
    且不影响其他 owner（owner 隔离）。覆盖 logout 经 LogoutCoordinator 调用本方法的路径。"""
    tm, _tasks = _team()
    owner_a = "A:uid-a"
    owner_b = "B:uid-b"

    # 两个 owner 各有一个 team + 活跃子 agent；leader/agent 的 interrupt 用 lambda 兜底，
    # 让 interrupt() 全程 graceful。_plans 不塞——_cancel_plan 对缺失 plan 返回 False。
    tm._teams[(owner_a, "sess-a")] = SimpleNamespace(leader=SimpleNamespace(interrupt=lambda _m: None))
    tm._teams[(owner_b, "sess-b")] = SimpleNamespace(leader=SimpleNamespace(interrupt=lambda _m: None))
    tm._active_children[(owner_a, "sess-a")] = {"c1": {"agent": SimpleNamespace(interrupt=lambda _m: None)}}
    tm._active_children[(owner_b, "sess-b")] = {"c2": {"agent": SimpleNamespace(interrupt=lambda _m: None)}}

    # detached 后台委派：长 sleep task 模拟正在跑的 fire-and-forget 委派
    async def _long_delegate():
        await asyncio.sleep(100)

    task_a = asyncio.create_task(_long_delegate())
    task_b = asyncio.create_task(_long_delegate())
    tm._track_delegate_task("sess-a", owner_a, task_a)
    tm._track_delegate_task("sess-b", owner_b, task_b)

    try:
        cancelled = await tm.cancel_owner(owner_a, reason="账号退出登录")
        # owner_a 的 team 缓存、活跃子 agent、delegate 桶全部清空，其 delegate 被取消
        assert (owner_a, "sess-a") not in tm._teams
        assert (owner_a, "sess-a") not in tm._active_children
        assert (owner_a, "sess-a") not in tm._delegate_tasks
        assert task_a.cancelled()
        # owner_b 完全不受影响（owner 隔离）
        assert (owner_b, "sess-b") in tm._teams
        assert (owner_b, "sess-b") in tm._active_children
        assert (owner_b, "sess-b") in tm._delegate_tasks
        assert not task_b.cancelled()
        assert cancelled >= 1
    finally:
        # 断言在 try 内完成；finally 只兜底回收两个 task，避免 asyncio 未等待 task 告警
        for _t in (task_a, task_b):
            if not _t.done():
                _t.cancel()
        await asyncio.gather(task_a, task_b, return_exceptions=True)
