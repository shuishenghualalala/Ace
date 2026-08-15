"""智能组队建议（suggest_external_team）的截断 + schema 校验测试（X8）。

覆盖两条防御线：
1. 用户输入字段被截断，megabyte 级 payload 不会原样灌进 LLM prompt；
2. LLM 返回畸形 JSON 时回退到 fallback_team_suggestion，不 500。
"""

from __future__ import annotations

import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from crew.app import build_app
from crew.core.mocks import FakeProvider
from crew.core.types import ChatResponse
from crew.gateway.helpers import build_team_draft, fallback_team_suggestion, fast_team_suggestion
from crew.gateway.routers.runtimes import (
    _draft_cache_key,
    _runtime_availability,
    SUGGEST_FIELD_CHAR_CAP,
    SUGGEST_PROMPT_CHAR_CAP,
    _truncate_user_payload,
)
from crew.gateway.server import create_app
from crew.state.config import ModelProfile
from crew.team.formation import (
    FORMATION_AI_MAX_AGENT_CANDIDATES,
    FORMATION_AI_MAX_EVIDENCE_PER_AGENT,
    build_agent_profile,
    formation_auto_decision,
    formation_ai_context,
)

OWNER_A = "A:uid-a"


class ClosableFakeProvider(FakeProvider):
    def __init__(self, script: list[ChatResponse] | None = None) -> None:
        super().__init__(script=script)
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_owner_provider_does_not_swallow_request_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    borrowed = FakeProvider()
    crew.provider = borrowed
    monkeypatch.setattr(
        crew.config,
        "owner_active_model_profile",
        lambda owner_account_id: ModelProfile(id="missing-key", api_key=""),
    )

    with pytest.raises(RuntimeError, match="route failed"):
        async with crew.owner_provider(OWNER_A) as provider:
            assert provider is borrowed
            raise RuntimeError("route failed")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    return create_app(crew)


# --------------------------------------------------------------------------- #
# 纯函数单测：截断与 schema 校验助手
# --------------------------------------------------------------------------- #

class TestTruncateUserPayload:
    def test_long_string_field_is_capped(self):
        huge = "X" * (SUGGEST_FIELD_CHAR_CAP * 3)
        out = _truncate_user_payload({"description": huge, "name": "ok"})
        assert len(out["description"]) == SUGGEST_FIELD_CHAR_CAP
        assert out["name"] == "ok"

    def test_non_string_values_preserved(self):
        payload = {"attachments": [{"uri": "a"}], "count": 5, "flag": True, "name": "t"}
        out = _truncate_user_payload(payload)
        assert out["attachments"] == [{"uri": "a"}]
        assert out["count"] == 5
        assert out["flag"] is True

    def test_short_string_unchanged(self):
        out = _truncate_user_payload({"name": "团队A"})
        assert out["name"] == "团队A"


def test_team_draft_cache_key_normalizes_whitespace_and_isolates_accounts():
    first = _draft_cache_key(
        "description",
        {"name": "质量  保障团队"},
        owner_account_id="A:uid-a",
    )
    equivalent = _draft_cache_key(
        "description",
        {"name": " 质量 保障团队 "},
        owner_account_id="A:uid-a",
    )
    other_account = _draft_cache_key(
        "description",
        {"name": "质量 保障团队"},
        owner_account_id="B:uid-b",
    )

    assert first == equivalent
    assert first != other_account


class TestRuntimeAvailability:
    def test_marks_unprobed_executable_runtime_degraded(self, tmp_path):
        executable = tmp_path / "agent"
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)

        runtime = _runtime_availability({
            "protocol": "acp",
            "executable_path": str(executable),
        })

        assert runtime["available"] is False
        assert runtime["availability_status"] == "degraded"

    def test_marks_missing_runtime_unavailable(self, tmp_path):
        runtime = _runtime_availability({
            "protocol": "cli",
            "executable_path": str(tmp_path / "missing"),
        })

        assert runtime["available"] is False
        assert runtime["availability_status"] == "unavailable"


def test_formation_ai_context_keeps_baseline_members_and_caps_optional_evidence():
    agents = [
        {
            "id": f"agent_{index:02d}",
            "name": f"Agent {index:02d}",
            "provider": "generic",
            "capabilities": {
                "implementation": 0.9 - index * 0.01,
                "frontend": 0.85,
                "backend": 0.82,
                "testing": 0.8,
                "verification": 0.78,
                "documentation": 0.76,
                "analysis": 0.74,
                "research": 0.72,
            },
        }
        for index in range(FORMATION_AI_MAX_AGENT_CANDIDATES + 8)
    ]
    payload = {
        "name": "研发团队",
        "description": "开发前后端功能，完成测试验证并整理文档",
        "workflow": "这段旧工作流不应重复发送给 Formation AI",
    }
    baseline = fast_team_suggestion(payload, agents)

    context = formation_ai_context(payload, agents, baseline, [])

    baseline_ids = {member["agent_id"] for member in baseline["members"]}
    selected_ids = {agent["agent_id"] for agent in context["available_agents"]}
    assert baseline_ids <= selected_ids
    assert len(context["available_agents"]) <= max(
        FORMATION_AI_MAX_AGENT_CANDIDATES,
        len(baseline_ids),
    )
    assert all(
        len(agent["capability_evidence"]) <= FORMATION_AI_MAX_EVIDENCE_PER_AGENT
        for agent in context["available_agents"]
    )
    assert "workflow_hint" not in context["team_input"]


# --------------------------------------------------------------------------- #
# 集成测试：经 HTTP 端点验证截断 + 回退
# --------------------------------------------------------------------------- #

def _seed_agents(crew) -> list[dict]:
    """注册一个 runtime + 两个 agent，返回 agent 列表。"""
    store = crew.external_agents
    store.upsert_runtime({"id": "rt-1", "type": "claude", "provider": "anthropic"})
    a1 = store.create_agent(
        owner_account_id=OWNER_A,
        name="A1",
        runtime_id="rt-1",
        model="claude-4",
    )
    a2 = store.create_agent(
        owner_account_id=OWNER_A,
        name="A2",
        runtime_id="rt-1",
        model="claude-4",
    )
    return [a1, a2]


def _draft_stream_events(response) -> list[dict]:
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_fast_team_suggestion_returns_team_spec_and_testing_roles():
    result = fast_team_suggestion(
        {"description": "帮我测试一下之前开发的贪吃蛇，不需要开发新功能"},
        [
            {"id": "agent_kimi", "name": "Kimi Writer", "provider": "kimi", "model": "moonshot"},
            {"id": "agent_codex", "name": "Codex Coder", "provider": "codex", "model": "code"},
        ],
    )

    assert result["team_spec"]["execution_profile"]["intent"] == "testing"
    assert result["team_spec"]["execution_profile"]["needs_build"] is False
    role_keys = [member["role_key"] for member in result["members"]]
    assert "qa_engineer" in role_keys
    assert "fullstack_developer" not in role_keys


def test_team_draft_keeps_leader_separate_from_suggested_role_slots():
    agents = [
        {"id": "agent_a", "name": "A", "provider": "codex", "model": "code"},
        {"id": "agent_b", "name": "B", "provider": "claude", "model": "code"},
    ]
    draft = build_team_draft(
        {"name": "像素游戏开发", "description": "开发并测试小游戏", "leader_agent_id": "agent_b"},
        agents,
    )

    assert draft["slots"][0]["agent_id"] == "agent_b"
    assert draft["slots"][0]["is_leader"] is True
    assert draft["slots"][0]["role_key"] == "project_manager"
    assert "Leader：B" in draft["workflow"]


def test_team_draft_description_uses_four_point_goal_outline():
    draft = build_team_draft(
        {"name": "像素游戏开发", "leader_agent_id": "agent_b"},
        [
            {"id": "agent_a", "name": "A", "provider": "codex", "model": "code"},
            {"id": "agent_b", "name": "B", "provider": "claude", "model": "code"},
        ],
    )

    lines = draft["description"].splitlines()
    assert len(lines) == 4
    assert lines[0] == "1. 负责范围：围绕像素游戏开发承接并拆解用户目标。"
    assert lines[1].startswith("2. 所需能力：需要")
    assert "执行实现" in lines[1]
    assert "测试验证" in lines[1]
    assert lines[2].startswith("3. 交付结果：")
    assert lines[3].startswith("4. 验收标准：")


def test_team_draft_accepts_lightweight_llm_role_slots_as_source():
    agents = [
        {"id": "agent_a", "name": "A", "provider": "codex", "model": "code"},
        {"id": "agent_b", "name": "B", "provider": "claude", "model": "code"},
        {"id": "agent_c", "name": "C", "provider": "kimi", "model": "moonshot"},
    ]
    draft = build_team_draft(
        {
            "name": "像素开发",
            "description": "开发一个像素小游戏并完成验证",
            "leader_agent_id": "agent_b",
            "draft_slots": [
                {"role_key": "frontend_developer", "agent_id": "agent_a", "required": True},
                {"role_key": "qa_engineer", "agent_id": "agent_c", "required": True},
            ],
        },
        agents,
    )

    non_leader_slots = [slot for slot in draft["slots"] if not slot["is_leader"]]
    assert [slot["role_key"] for slot in non_leader_slots] == ["frontend_developer", "qa_engineer"]
    assert [slot["agent_id"] for slot in non_leader_slots] == ["agent_a", "agent_c"]
    assert "前端开发：建议【A】担任" in draft["workflow"]
    assert "测试工程师：建议【C】担任" in draft["workflow"]


@pytest.mark.asyncio
async def test_team_draft_endpoint_uses_one_llm_call_for_description(tmp_path, monkeypatch, auth_headers):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    generated = (
        "1. 负责范围：拆解并推进质量保障目标。\n"
        "2. 所需能力：需要测试设计、异常分析与协作复核能力。\n"
        "3. 交付结果：形成测试结论和问题清单。\n"
        "4. 验收标准：关键路径完成验证，结论可追踪且经 Leader 审阅。"
    )
    fake = FakeProvider(script=[ChatResponse(text=json.dumps({"description": generated, "slots": []}, ensure_ascii=False))])
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/external-teams/draft/description", json={"name": "质量保障团队"})

    assert resp.status_code == 200
    events = _draft_stream_events(resp)
    draft_events = [event for event in events if event["type"] == "draft"]
    description_events = [event for event in events if event["type"] == "description_delta"]
    assert [event["phase"] for event in draft_events] == ["initial", "optimized"]
    assert draft_events[0]["draft"]["description"] != generated
    assert draft_events[1]["draft"]["description"] == generated
    assert description_events
    assert description_events[-1]["text"] == generated
    assert isinstance(draft_events[-1]["llm_elapsed_ms"], int)
    assert draft_events[-1]["cache_hit"] is False
    assert len(fake.stream_calls) == 1
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_role_suggest_endpoint_exact_rejects_unknown_fields(tmp_path, monkeypatch, auth_headers):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        rejected = await client.post(
            "/api/external-teams/roles/suggest",
            json={"role_key": "qa_engineer", "agent_name": "测试", "scope": "global", "action": "admin"},
        )
        accepted = await client.post(
            "/api/external-teams/roles/suggest",
            json={"role_key": "qa_engineer", "agent_name": "测试", "workflow": "验证质量"},
        )

    assert rejected.status_code == 400
    assert rejected.json()["ok"] is False
    assert set(rejected.json()["fields"]) == {"scope", "action"}
    assert accepted.status_code == 200
    assert accepted.json()["role"]


@pytest.mark.asyncio
async def test_team_draft_endpoint_preserves_manual_description_while_optimizing_slots(
    tmp_path,
    monkeypatch,
    auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    agents = _seed_agents(crew)
    manual_description = "用户手动维护的团队目标与交付口径。"
    fake = FakeProvider(script=[ChatResponse(text=json.dumps({
        "description": "不应覆盖用户描述",
        "slots": [{"role_key": "qa_engineer", "agent_id": agents[1]["id"], "required": True}],
    }, ensure_ascii=False))])
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/external-teams/draft/formation", json={
            "name": "质量保障团队",
            "description": manual_description,
            "leader_agent_id": agents[0]["id"],
        })
        cached_resp = await client.post("/api/external-teams/draft/formation", json={
            "name": " 质量保障团队 ",
            "description": f" {manual_description} ",
            "leader_agent_id": agents[0]["id"],
        })

    assert resp.status_code == 200
    events = _draft_stream_events(resp)
    draft_events = [event for event in events if event["type"] == "draft"]
    assert [event["phase"] for event in draft_events] == ["initial", "optimized"]
    data = draft_events[-1]["draft"]
    assert data["description"] == manual_description
    non_leader_slots = [slot for slot in data["slots"] if not slot["is_leader"]]
    assert [(slot["role_key"], slot["agent_id"]) for slot in non_leader_slots] == [
        ("qa_engineer", agents[1]["id"]),
    ]
    assert len(fake.calls) == 1
    assert len(fake.stream_calls) == 1
    cached_events = _draft_stream_events(cached_resp)
    cached_final = [event for event in cached_events if event["type"] == "draft"][-1]
    assert cached_final["cache_hit"] is True


@pytest.mark.asyncio
async def test_team_ai_routes_use_current_owner_model_and_close_request_providers(
    tmp_path,
    monkeypatch,
    auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    agents = _seed_agents(crew)
    global_provider = FakeProvider()
    crew.provider = global_provider

    owner_profile = ModelProfile(
        id="owner-model",
        name="Owner Model",
        api_key="test-owner-api-key",
        api_key_env="OWNER_MODEL_API_KEY",
        base_url="https://api.example.com/v1",
        model="owner-model-name",
    )
    monkeypatch.setattr(
        crew.config,
        "owner_active_model_profile",
        lambda owner_account_id: owner_profile if owner_account_id == OWNER_A else None,
    )

    audit = {
        "requirement_audit": {"required_roles": []},
        "member_changes": {"remove_agent_ids": [], "upsert_members": []},
        "staffing_plan": {"required": False, "members": []},
        "separation_constraints": [],
    }
    generated_description = (
        "1. 负责范围：负责当前 owner 的质量保障工作。\n"
        "2. 所需能力：具备测试设计、问题分析和协作复核能力。\n"
        "3. 交付结果：形成可追踪的测试结论与问题清单。\n"
        "4. 验收标准：关键路径验证完成并由 Leader 确认。"
    )
    owner_providers = [
        ClosableFakeProvider(script=[ChatResponse(text=json.dumps(audit, ensure_ascii=False))]),
        ClosableFakeProvider(script=[ChatResponse(text=json.dumps(
            {"description": generated_description},
            ensure_ascii=False,
        ))]),
        ClosableFakeProvider(script=[ChatResponse(text=json.dumps({"slots": []}, ensure_ascii=False))]),
    ]
    built_profiles: list[ModelProfile] = []

    def build_owner_provider(profile: ModelProfile, stream_read_timeout=None):
        built_profiles.append(profile)
        return owner_providers[len(built_profiles) - 1]

    monkeypatch.setattr("crew.app.build_provider_for_profile", build_owner_provider)
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        suggest_response = await client.post(
            "/api/external-teams/suggest",
            json={
                "name": "Owner 专属组队审核",
                "description": "开发接口并完成独立测试",
                "formation_mode": "ai",
            },
        )
        description_response = await client.post(
            "/api/external-teams/draft/description",
            json={"name": "Owner 专属团队描述"},
        )
        formation_response = await client.post(
            "/api/external-teams/draft/formation",
            json={
                "name": "Owner 专属槽位草案",
                "description": "完成实现与验证",
                "leader_agent_id": agents[0]["id"],
            },
        )

    assert suggest_response.status_code == 200
    assert description_response.status_code == 200
    assert formation_response.status_code == 200
    assert built_profiles == [owner_profile, owner_profile, owner_profile]
    assert len(owner_providers[0].calls) == 1
    assert len(owner_providers[1].stream_calls) == 1
    assert len(owner_providers[2].stream_calls) == 1
    assert [provider.close_calls for provider in owner_providers] == [1, 1, 1]
    assert global_provider.calls == []
    assert global_provider.stream_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("raw_text", [
    "not valid json",
    json.dumps({"description": "这是一段没有按照四个目标要点返回的团队描述，因此应当使用稳定的本地参考。"}, ensure_ascii=False),
])
async def test_team_draft_stream_marks_malformed_llm_output_as_fallback(
    tmp_path,
    monkeypatch,
    auth_headers,
    raw_text,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    fake = FakeProvider(script=[ChatResponse(text=raw_text)])
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/external-teams/draft/description", json={"name": "质量保障团队"})

    assert resp.status_code == 200
    events = _draft_stream_events(resp)
    draft_events = [event for event in events if event["type"] == "draft"]
    assert [event["phase"] for event in draft_events] == ["initial", "fallback"]
    assert draft_events[0]["draft"] == draft_events[1]["draft"]


@pytest.mark.asyncio
async def test_team_description_draft_cache_normalizes_equivalent_names(tmp_path, monkeypatch, auth_headers):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    generated = (
        "1. 负责范围：承接并拆解质量保障目标。\n"
        "2. 所需能力：需要验证、复核和风险分析能力。\n"
        "3. 交付结果：形成边界清楚的质量结论。\n"
        "4. 验收标准：过程可追踪，结果可直接验收。"
    )
    fake = FakeProvider(script=[ChatResponse(text=json.dumps({"description": generated}, ensure_ascii=False))])
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        first = await client.post(
            "/api/external-teams/draft/description",
            json={"name": "质量  保障团队"},
        )
        second = await client.post(
            "/api/external-teams/draft/description",
            json={"name": " 质量 保障团队 "},
        )

    first_events = _draft_stream_events(first)
    second_events = _draft_stream_events(second)
    first_final = [event for event in first_events if event["type"] == "draft"][-1]
    second_final = [event for event in second_events if event["type"] == "draft"][-1]
    assert first_final["cache_hit"] is False
    assert second_final["cache_hit"] is True
    assert second_final["draft"]["description"] == generated
    assert len(fake.stream_calls) == 1


def test_confirmed_slots_are_the_complete_team_and_do_not_auto_fill():
    agents = [
        {"id": "agent_a", "name": "A", "provider": "codex", "model": "code"},
        {"id": "agent_b", "name": "B", "provider": "claude", "model": "code"},
        {"id": "agent_c", "name": "C", "provider": "kimi", "model": "moonshot"},
    ]
    result = fast_team_suggestion(
        {
            "name": "研发团队",
            "description": "开发、测试并编写文档",
            "leader_agent_id": "agent_a",
            "slots": [
                {
                    "slot_id": "leader",
                    "role_key": "product_manager",
                    "agent_id": "agent_a",
                    "is_leader": True,
                },
                {
                    "slot_id": "build",
                    "role_key": "fullstack_developer",
                    "agent_id": "agent_b",
                    "is_leader": False,
                },
            ],
        },
        agents,
    )

    assert result["leader_agent_id"] == "agent_a"
    assert [member["agent_id"] for member in result["members"]] == ["agent_a", "agent_b"]
    assert "formation" not in result["team_spec"]
    assert "agent_c" not in [item["agent_id"] for item in result["formation_plan"]["members"]]
    assert "B（全栈开发）" in result["workflow"]
    assert "Agent C" not in result["workflow"]


def test_fast_confirmed_plan_roles_compile_distinct_responsibilities():
    agents = [
        {"id": "cc", "name": "cc", "provider": "codex", "model": "code"},
        {"id": "kimi", "name": "kimi", "provider": "kimi", "model": "moonshot"},
    ]
    result = fast_team_suggestion(
        {
            "name": "像素游戏团队",
            "description": "负责设计和交付轻量小游戏",
            "slots": [
                {
                    "slot_id": "leader",
                    "role_key": "project_manager",
                    "agent_id": "crew::builtin",
                    "is_leader": True,
                },
                {
                    "slot_id": "requirements",
                    "role_key": "product_manager",
                    "agent_id": "cc",
                    "is_leader": False,
                },
                {
                    "slot_id": "research",
                    "role_key": "research_analyst",
                    "agent_id": "kimi",
                    "is_leader": False,
                },
            ],
        },
        agents,
    )

    by_id = {member["agent_id"]: member for member in result["formation_plan"]["members"]}
    cc = by_id["cc"]["responsibility"]
    kimi = by_id["kimi"]["responsibility"]
    assert cc["mission"] != kimi["mission"]
    assert cc["deliverables"] == ["需求范围", "业务或玩法规则", "验收清单"]
    assert kimi["deliverables"] == ["调研结论", "参考依据", "风险与建议"]
    assert "cc（产品经理）" in result["workflow"]
    assert "kimi（研究分析）" in result["workflow"]
    assert "验收清单" in result["workflow"]
    assert "调研结论" in result["workflow"]


def test_fast_keeps_user_locked_duplicate_roles_but_warns_about_overlap():
    agents = [
        {"id": "pm_a", "name": "PM A", "provider": "codex", "model": "code"},
        {"id": "pm_b", "name": "PM B", "provider": "kimi", "model": "moonshot"},
    ]
    result = fast_team_suggestion(
        {
            "name": "产品团队",
            "description": "负责产品需求和验收",
            "slots": [
                {
                    "slot_id": "leader",
                    "role_key": "project_manager",
                    "agent_id": "crew::builtin",
                    "is_leader": True,
                },
                {
                    "slot_id": "pm_a",
                    "role_key": "product_manager",
                    "agent_id": "pm_a",
                    "is_leader": False,
                },
                {
                    "slot_id": "pm_b",
                    "role_key": "product_manager",
                    "agent_id": "pm_b",
                    "is_leader": False,
                },
            ],
        },
        agents,
    )

    assert {"pm_a", "pm_b"} <= {member["agent_id"] for member in result["members"]}
    assert any("常驻职责范围相同" in warning for warning in result["formation_plan"]["warnings"])
    covered = result["formation_plan"]["coverage"]["covered"]
    assert len(covered) == len(set(covered))


def test_empty_confirmed_slots_keep_only_leader_and_ignore_stale_workflow_assignments():
    agents = [
        {"id": "agent_a", "name": "A", "provider": "codex", "model": "code"},
        {"id": "agent_b", "name": "B", "provider": "claude", "model": "code"},
        {"id": "agent_c", "name": "C", "provider": "kimi", "model": "moonshot"},
    ]
    result = fast_team_suggestion(
        {
            "name": "研发团队",
            "description": "开发、测试并编写文档",
            "workflow": "开发建议【B】担任，测试建议【C】担任。",
            "leader_agent_id": "agent_a",
            "slots": [],
        },
        agents,
    )

    assert result["leader_agent_id"] == "agent_a"
    assert [member["agent_id"] for member in result["members"]] == ["agent_a"]
    assert "formation" not in result["team_spec"]
    assert result["formation_plan"]["confidence"]["coverage"] == 0.0
    assert "A 独立完成任务并直接交付结果" in result["workflow"]
    assert not any("B" in reason or "C" in reason for reason in result["reasons"])


def test_text_constraints_lock_leader_assignment_and_exclusion():
    agents = [
        {"id": "agent_a", "name": "A", "provider": "codex", "model": "code"},
        {"id": "agent_b", "name": "B", "provider": "claude", "model": "code"},
        {"id": "agent_c", "name": "C", "provider": "kimi", "model": "moonshot"},
    ]
    result = fast_team_suggestion(
        {"description": "A 是 Leader，B 负责开发，不让 C 加入"},
        agents,
    )

    assert result["leader_agent_id"] == "agent_a"
    assert "agent_c" not in [member["agent_id"] for member in result["members"]]
    assert any(member["agent_id"] == "agent_b" for member in result["members"])


def test_external_team_persists_team_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    agents = _seed_agents(crew)
    spec = {
        "version": 2,
        "execution_profile": {"intent": "implementation"},
        "team_requirements": {"roles": ["frontend_developer", "qa_engineer"]},
    }

    team = crew.external_agents.create_team(
        owner_account_id=OWNER_A,
        name="Spec Team",
        description="开发并测试",
        leader_agent_id=agents[0]["id"],
        instructions="按 TeamSpec 执行",
        team_spec=spec,
        members=[
            {"agent_id": agents[0]["id"], "role": "Leader", "role_key": "tech_lead"},
            {"agent_id": agents[1]["id"], "role": "测试", "role_key": "qa_engineer"},
        ],
    )

    assert team["team_spec"] == spec
    assert crew.external_agents.get_team(
        team["id"],
        owner_account_id=OWNER_A,
    )["team_spec"] == spec


@pytest.mark.asyncio
async def test_create_team_normalizes_manual_roster_into_formation_plan(tmp_path, monkeypatch, auth_headers):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    crew.config.gateway_admin_accounts = ["A:uid-a"]
    agents = _seed_agents(crew)
    api = create_app(crew)
    leader = agents[0]

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/external-teams", json={
            "name": "手动团队",
            "description": "手动选择成员",
            "leader_agent_id": leader["id"],
            "team_spec": {"version": 3, "goal": "手动选择成员"},
            "members": [{
                "agent_id": leader["id"],
                "role": "负责统筹和验收",
                "role_key": "project_manager",
                "assigned_capabilities": ["planning"],
            }],
        })

    assert resp.status_code == 200
    team = resp.json()
    assert team["formation_plan"]["leader_agent_id"] == leader["id"]
    assert team["formation_plan"]["members"][0]["responsibility_markdown"] == "负责统筹和验收"
    assert team["members"][0]["assigned_capabilities"] == ["planning"]


@pytest.mark.asyncio
async def test_confirmed_temporary_member_is_owner_private_hidden_and_cleaned_up(
    tmp_path, monkeypatch, auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    crew.config.gateway_admin_accounts = ["A:uid-a"]
    crew.external_agents.upsert_runtime({
        "id": "rt-ready",
        "type": "acp",
        "provider": "generic",
        "executable_path": sys.executable,
        "metadata": {
            "availability_status": "ready",
            "models": [{"id": "model-ready", "label": "Ready Model", "default": True}],
            "default_model_id": "model-ready",
        },
    })
    api = create_app(crew)
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        failed_response = await client.post("/api/external-teams", json={
            "name": "失败后回滚",
            "leader_agent_id": "missing-agent",
            "members": [{"agent_id": "missing-agent", "role": "Leader"}],
            "temporary_members": [{
                "role_key": "qa_engineer",
                "required_capabilities": ["testing"],
                "runtime_id": "rt-ready",
                "model_id": "model-ready",
            }],
        })
        assert failed_response.status_code == 404
        assert crew.external_agents.list_agents(owner_account_id=OWNER_A) == []

        created_response = await client.post("/api/external-teams", json={
            "name": "临时补员团队",
            "description": "实现并独立验证小游戏",
            "leader_agent_id": "crew::builtin",
            "members": [{
                "agent_id": "crew::builtin",
                "role": "负责团队统筹",
                "role_key": "project_manager",
                "assigned_capabilities": ["planning"],
            }],
            "formation_plan": {
                "version": 1,
                "leader_agent_id": "crew::builtin",
                "members": [],
                "coverage": {
                    "required": ["testing", "verification"],
                    "covered": [],
                    "uncovered": ["testing", "verification"],
                },
                "confidence": {},
                "staffing_mode": "ai_reviewed",
            },
            "temporary_members": [{
                "gap_id": "formation_gap_1",
                "role_key": "qa_engineer",
                "required_capabilities": ["testing", "verification"],
                "responsibility_focus": "独立验证关键路径",
                "reason": "现有成员缺少独立测试能力。",
                "runtime_id": "rt-ready",
                "model_id": "model-ready",
            }],
        })
        assert created_response.status_code == 200
        team = created_response.json()
        temporary = next(
            member
            for member in team["formation_plan"]["members"]
            if member["selection_source"] == "ai_temporary"
        )
        temporary_id = temporary["agent_id"]
        with pytest.raises(KeyError):
            crew.external_agents.get_agent(temporary_id, owner_account_id="B:uid-b")

        agents_response = await client.get("/api/external-agents")
        assert temporary_id not in {agent["id"] for agent in agents_response.json()}

        deleted_response = await client.delete(f"/api/external-teams/{team['id']}")
        assert deleted_response.status_code == 200

    with pytest.raises(KeyError):
        crew.external_agents.get_agent(temporary_id, owner_account_id=OWNER_A)


@pytest.mark.asyncio
async def test_huge_description_is_truncated_not_forwarded(tmp_path, monkeypatch, auth_headers):
    """超长 description 不会把数 MB 文本灌进 LLM —— 断言 prompt 被裁到上限内。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    agents = _seed_agents(crew)
    valid_ids = {a["id"] for a in agents}

    fake = FakeProvider()
    crew.provider = fake
    api = create_app(crew)

    huge = "Y" * (SUGGEST_FIELD_CHAR_CAP * 10)  # 远超单字段上限
    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={"name": "团队", "description": huge, "workflow": "w", "formation_mode": "ai"},
        )

    assert resp.status_code == 200
    # FakeProvider 记录了发给 LLM 的 messages：user 消息文本必须被截断到 prompt 上限内。
    assert fake.calls, "provider.chat 应被调用一次"
    user_text = fake.calls[0][-1].content  # 最后一条是 user prompt
    assert len(user_text) <= SUGGEST_PROMPT_CHAR_CAP, (
        f"prompt 未被裁剪: {len(user_text)} > {SUGGEST_PROMPT_CHAR_CAP}"
    )
    # 单字段截断：huge 不应原样出现（4KB 上限 << huge 的 120KB）
    assert huge not in user_text
    # valid agent ids 仍出现在 prompt 里（裁剪不能把指令区也吃掉）
    for aid in valid_ids:
        assert aid in user_text
    assert '"member_changes"' in user_text
    assert '"staffing_plan"' in user_text
    assert "最小化新增成员人数" in user_text
    assert "不代表必须一角一人" in user_text
    assert '"ready_runtime_options"' in user_text
    assert '"baseline_issues"' not in user_text
    assert '"deliverables"' not in user_text
    assert '"workflow_hint"' not in user_text


@pytest.mark.asyncio
async def test_malformed_llm_response_falls_back(tmp_path, monkeypatch, auth_headers, caplog):
    """LLM 返回非 JSON / 缺字段 JSON → 回退模板，不 500。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)

    # 故意返回无法解析的文本
    crew.provider = FakeProvider(script=[ChatResponse(text="这不是 JSON {{{")])
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={"name": "团队", "description": "d", "workflow": "w", "formation_mode": "ai"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_formation_mode"] == "ai"
    assert data["selected_formation_mode"] == "fast"
    assert data["fallback_reason"] == "invalid_ai_output"
    # 回退模板：leader 默认是 Crew 内置智能体，members 非空且每个 role 是 Markdown
    assert data["leader_agent_id"] == "crew::builtin"
    assert len(data["members"]) >= 1
    for member in data["members"]:
        assert member["agent_id"]
        assert "工作原则" in member["role"]  # fallback role_markdown 的标志小标题
    assert "Formation AI metrics" in caplog.text
    assert "context_chars=" in caplog.text
    assert "prompt_tokens=" in caplog.text
    assert "reasoning_chars=" in caplog.text


@pytest.mark.asyncio
async def test_invalid_schema_llm_response_falls_back(tmp_path, monkeypatch, auth_headers):
    """LLM 返回结构合法但引用了不存在 agent 的 JSON → schema 校验失败 → 回退。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)

    bad_json = json.dumps({
        "leader_agent_id": "ghost_agent",  # 不存在
        "workflow": "w",
        "members": [{"agent_id": "ghost_agent", "role": "r", "sort_order": 0}],
    })
    crew.provider = FakeProvider(script=[ChatResponse(text=bad_json)])
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={"name": "团队", "description": "d", "formation_mode": "ai"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_formation_mode"] == "ai"
    assert data["selected_formation_mode"] == "fast"
    assert data["fallback_reason"] == "invalid_ai_output"
    # 回退：leader 是 Crew 内置智能体，不是 ghost
    assert data["leader_agent_id"] == "crew::builtin"
    assert data["leader_agent_id"] != "ghost_agent"


@pytest.mark.asyncio
async def test_valid_formation_ai_gap_is_returned_for_user_confirmation(
    tmp_path, monkeypatch, auth_headers,
):
    """Formation AI 只返回紧凑审计，缺员建议由本地校验并推荐就绪模型。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    crew.external_agents.upsert_runtime({
        "id": "rt-ready",
        "type": "acp",
        "provider": "generic",
        "executable_path": sys.executable,
        "metadata": {
            "availability_status": "ready",
            "models": [{
                "id": "model-ready",
                "label": "Ready Model",
                "default": True,
                "capabilities": ["testing", "verification"],
            }],
            "default_model_id": "model-ready",
        },
    })
    crew.external_agents.upsert_runtime({
        "id": "rt-generic",
        "type": "acp",
        "provider": "generic",
        "executable_path": sys.executable,
        "metadata": {
            "availability_status": "ready",
            "models": [{"id": "model-generic", "label": "Generic Model", "default": True}],
            "default_model_id": "model-generic",
        },
    })
    profiled_agents = crew.external_agents.list_agents(owner_account_id=OWNER_A)
    for agent in profiled_agents:
        agent["profile"]["capabilities"]["testing"] = {
            "score": 0.1,
            "confidence": 0.9,
            "evidence": [{"source": "runtime_probe", "value": "no testing evidence", "weight": 0.9}],
        }
        agent["profile"]["capabilities"]["verification"] = {
            "score": 0.1,
            "confidence": 0.9,
            "evidence": [{"source": "runtime_probe", "value": "no verification evidence", "weight": 0.9}],
        }
    monkeypatch.setattr(
        crew.external_agents,
        "list_agents",
        lambda *, owner_account_id="": profiled_agents if owner_account_id == OWNER_A else [],
    )
    formation_payload = {
        "name": "质量团队",
        "description": "独立测试和验证贪吃蛇小游戏",
        "required_capabilities": ["testing", "verification"],
    }
    fast_baseline = fast_team_suggestion(formation_payload, profiled_agents)
    unsupported_test_member_ids = [
        member["agent_id"]
        for member in fast_baseline["members"]
        if member["agent_id"] != "crew::builtin"
        and {"testing", "verification"} & set(member.get("assigned_capabilities") or [])
    ]
    good_json = json.dumps({
        "requirement_audit": {"required_roles": []},
        "member_changes": {
            "remove_agent_ids": unsupported_test_member_ids,
            "upsert_members": [],
        },
        "staffing_plan": {
            "required": True,
            "members": [
                {
                    "role_key": "qa_engineer",
                    "required_capabilities": ["testing", "verification"],
                    "responsibility_focus": "独立验证小游戏关键路径",
                    "reason": "当前成员缺少可靠的测试能力证据。",
                    "recommended_runtime_id": "missing-runtime",
                    "recommended_model_id": "missing-model",
                },
                {
                    "role_key": "qa_engineer",
                    "required_capabilities": ["verification", "testing"],
                    "responsibility_focus": "重复建议应被忽略",
                    "reason": "重复建议。",
                    "recommended_runtime_id": "missing-runtime",
                    "recommended_model_id": "missing-model",
                },
            ],
        },
    })
    crew.provider = FakeProvider(script=[ChatResponse(text=good_json)])
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={
                **formation_payload,
                "formation_mode": "ai",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_formation_mode"] == "ai"
    assert data["selected_formation_mode"] == "ai", data["fallback_reason"]
    assert data["fallback_reason"] == ""
    assert data["leader_agent_id"] == "crew::builtin"
    assert data["staffing_decision_required"] is True
    assert len(data["staffing_gaps"]) == 1
    assert data["staffing_gaps"][0]["role_key"] == "qa_engineer"
    assert data["staffing_gaps"][0]["recommended_runtime_id"] == "rt-ready"
    assert data["staffing_gaps"][0]["recommended_model_id"] == "model-ready"
    assert {member["agent_id"] for member in data["members"]} >= {
        member["agent_id"]
        for member in fast_baseline["members"]
        if member["agent_id"] not in unsupported_test_member_ids
    }
    assert "完整职责" not in json.dumps(data["staffing_gaps"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_ai_suggestion_pauses_for_required_agent_capability_decision(
    tmp_path, monkeypatch, auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    agents = _seed_agents(crew)
    profiled_agents = crew.external_agents.list_agents(owner_account_id=OWNER_A)
    profiled_target = next(agent for agent in profiled_agents if agent["id"] == agents[1]["id"])
    profiled_target["profile"]["capabilities"]["design"] = {
        "score": 0.1,
        "confidence": 0.9,
        "evidence": [{"source": "runtime_probe", "value": "no design tools", "weight": 0.9}],
    }
    monkeypatch.setattr(
        crew.external_agents,
        "list_agents",
        lambda *, owner_account_id="": profiled_agents if owner_account_id == OWNER_A else [],
    )
    fake = FakeProvider()
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={
                "name": "设计团队",
                "description": "设计产品界面",
                "formation_mode": "ai",
                "required_agent_ids": [agents[1]["id"]],
                "required_capabilities": ["design"],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["decision_required"] is True
    assert data["required_agent_conflicts"][0]["agent_id"] == agents[1]["id"]
    assert not fake.calls


@pytest.mark.asyncio
async def test_ai_suggestion_restores_forced_member_omitted_by_llm(
    tmp_path, monkeypatch, auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    agents = _seed_agents(crew)
    forced_id = agents[1]["id"]
    good_json = json.dumps({
        "requirement_audit": {"required_roles": []},
        "member_changes": {"remove_agent_ids": [], "upsert_members": []},
        "staffing_gaps": [],
    })
    crew.provider = FakeProvider(script=[ChatResponse(text=good_json)])
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={
                "name": "研发团队",
                "description": "开发接口并完成测试",
                "formation_mode": "ai",
                "required_agent_ids": [forced_id],
                "force_required_agent_ids": [forced_id],
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_formation_mode"] == "ai"
    assert data["selected_formation_mode"] == "fast"
    assert data["fallback_reason"] == "no_material_improvement"
    assert forced_id in {member["agent_id"] for member in data["members"]}
    planned = next(member for member in data["formation_plan"]["members"] if member["agent_id"] == forced_id)
    assert planned["locked"] is True


@pytest.mark.asyncio
async def test_no_external_agents_returns_crew_builtin_fast_team(tmp_path, monkeypatch, auth_headers):
    """没有外部 agent 时也可用 Crew 内置智能体快速组队（不调 LLM）。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    fake = FakeProvider()
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={"name": "x", "formation_mode": "fast"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["leader_agent_id"] == "crew::builtin"
    assert [member["agent_id"] for member in data["members"]] == ["crew::builtin"]
    assert not fake.calls  # 没调 LLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": "未声明模式"},
        {"name": "旧字段", "mode": "fast"},
    ],
)
async def test_suggest_requires_explicit_formation_mode(
    tmp_path, monkeypatch, auth_headers, payload,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    api = create_app(build_app(enable_team=False))

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post("/api/external-teams/suggest", json=payload)

    assert resp.status_code == 400
    assert resp.json()["error"] == "formation_mode 必须是 fast、ai 或 auto"


def test_auto_gate_skips_ai_only_for_complete_high_confidence_simple_plan():
    payload = {"name": "简单团队", "description": "整理一份结论"}
    baseline = {
        "decision_required": False,
        "required_agent_conflicts": [],
        "formation_plan": {
            "coverage": {
                "required": ["analysis"],
                "covered": ["analysis"],
                "uncovered": [],
            },
            "confidence": {
                "requirement": 0.9,
                "capability_evidence": 0.8,
                "coverage": 1.0,
                "overall": 0.9,
            },
            "warnings": [],
        },
        "team_spec": {
            "uncertainty": "low",
            "execution_profile": {"complexity": "focused"},
            "policy": {"risk_flags": []},
            "planning": {"missing_info": []},
        },
    }

    requires_ai, reasons = formation_auto_decision(payload, baseline)

    assert requires_ai is False
    assert reasons == []


def test_auto_gate_audits_complex_or_low_evidence_plan():
    baseline = {
        "decision_required": False,
        "required_agent_conflicts": [],
        "formation_plan": {
            "coverage": {
                "required": ["implementation", "testing"],
                "covered": ["implementation", "testing"],
                "uncovered": [],
            },
            "confidence": {
                "requirement": 0.9,
                "capability_evidence": 0.7,
                "coverage": 1.0,
                "overall": 0.86,
            },
            "warnings": [],
        },
        "team_spec": {
            "uncertainty": "low",
            "execution_profile": {"complexity": "multi_role"},
            "policy": {"risk_flags": []},
            "planning": {"missing_info": []},
        },
    }

    requires_ai, reasons = formation_auto_decision({}, baseline)

    assert requires_ai is True
    assert "capability_evidence_below_0.75" in reasons
    assert "structured_multi_role_task" in reasons


@pytest.mark.asyncio
async def test_auto_stream_emits_fast_then_final_without_ai_when_gate_passes(
    tmp_path, monkeypatch, auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    fake = FakeProvider()
    crew.provider = fake
    monkeypatch.setattr(
        "crew.gateway.routers.runtimes.formation_auto_decision",
        lambda payload, baseline: (False, []),
    )
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={
                "name": "简单团队",
                "description": "整理结论",
                "formation_mode": "auto",
            },
        )

    events = _draft_stream_events(resp)
    assert [(event["type"], event["phase"]) for event in events] == [
        ("suggestion", "fast"),
        ("suggestion", "final"),
    ]
    assert events[0]["suggestion"]["requested_formation_mode"] == "auto"
    assert events[-1]["auto_decision"] == "fast"
    assert not fake.calls


@pytest.mark.asyncio
async def test_auto_stream_emits_ai_status_and_one_final_after_fast(
    tmp_path, monkeypatch, auth_headers,
):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    fake = FakeProvider(script=[ChatResponse(text="invalid json")])
    crew.provider = fake
    monkeypatch.setattr(
        "crew.gateway.routers.runtimes.formation_auto_decision",
        lambda payload, baseline: (True, ["structured_multi_role_task"]),
    )
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={
                "name": "研发团队",
                "description": "开发并测试一个小游戏",
                "formation_mode": "auto",
            },
        )

    events = _draft_stream_events(resp)
    assert [(event["type"], event["phase"]) for event in events] == [
        ("suggestion", "fast"),
        ("status", "ai_reviewing"),
        ("suggestion", "final"),
    ]
    assert events[-1]["auto_decision"] == "ai"
    assert events[-1]["suggestion"]["requested_formation_mode"] == "auto"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_fast_suggest_uses_local_rules_without_llm(tmp_path, monkeypatch, auth_headers):
    """Fast 智能组队走本地规则，不等待 LLM。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    crew = build_app(enable_team=False)
    _seed_agents(crew)
    fake = FakeProvider()
    crew.provider = fake
    api = create_app(crew)

    transport = ASGITransport(app=api)
    async with AsyncClient(transport=transport, base_url="http://test", headers=auth_headers) as client:
        resp = await client.post(
            "/api/external-teams/suggest",
            json={
                "name": "开发团队",
                "description": "开发一个带登录和后台管理的 Web 系统",
                "formation_mode": "fast",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["requested_formation_mode"] == "fast"
    assert data["selected_formation_mode"] == "fast"
    assert data["fallback_reason"] == ""
    assert data["warnings"] == data["formation_plan"]["warnings"]
    assert set(data["timing"]) == {"fast_ms", "ai_ms", "total_ms"}
    assert data["leader_agent_id"] == "crew::builtin"
    formation = data["formation_plan"]
    assert {"frontend", "backend"}.issubset(set(formation["coverage"]["required"]))
    assert "formation" not in data["team_spec"]
    assert len(data["members"]) >= 2
    assert data["reasons"]
    assert not fake.calls


def test_fallback_team_suggestion_still_works():
    """回归：fallback 也走新的 Team formation 管线。"""
    agents = [{"id": "a1", "name": "A", "provider": "p"}, {"id": "a2", "name": "B", "provider": "p"}]
    result = fallback_team_suggestion({"name": "t", "description": "d"}, agents)
    assert result["leader_agent_id"] == "crew::builtin"
    assert len(result["members"]) >= 1
    assert all("工作原则" in m["role"] for m in result["members"])


def test_fast_team_suggestion_uses_minimal_capability_cover_not_provider_brand():
    agents = [
        {"id": "agent_a", "name": "A", "provider": "kimi", "model": "moonshot"},
        {"id": "agent_b", "name": "B", "provider": "codex", "model": "code"},
        {"id": "agent_c", "name": "C", "provider": "hermes", "model": "code"},
    ]
    result = fast_team_suggestion(
        {"name": "研发团队", "description": "开发一个前端页面和后端接口，并整理交付文档"},
        agents,
    )

    assert result["leader_agent_id"] == "crew::builtin"
    non_leaders = [member for member in result["members"] if member["agent_id"] != "crew::builtin"]
    assert len(non_leaders) == 1
    assert set(non_leaders[0]["capabilities"]) >= {"frontend", "backend", "documentation"}
    assert "capability_profiles" not in result["formation_plan"]
    assert all(member.get("responsibility_markdown") for member in result["formation_plan"]["members"])
    assert all("assigned_capabilities" in member for member in result["formation_plan"]["members"])


def test_fast_team_suggestion_keeps_build_as_primary_role_and_honors_custom_capabilities():
    result = fast_team_suggestion(
        {
            "name": "像素小游戏开发团队",
            "custom_capabilities": ["全栈开发", "前端设计"],
        },
        [{"id": "hermes_1", "name": "Hermes", "provider": "hermes"}],
    )

    developer = next(member for member in result["members"] if member["agent_id"] == "hermes_1")
    assert len(result["members"]) == 2
    required = set(result["formation_plan"]["coverage"]["required"])
    assert {"design", "frontend", "implementation"} <= required
    assert {"implementation", "testing", "verification"} <= set(developer["capabilities"])
    assert developer["role_key"] in {"frontend_developer", "fullstack_developer"}


def test_fast_team_suggestion_skips_unavailable_agent_profiles():
    degraded = {
        "id": "degraded_agent",
        "name": "Degraded",
        "provider": "acp",
        "capabilities": {"implementation": 0.99, "testing": 0.99},
    }
    ready = {
        "id": "ready_agent",
        "name": "Ready",
        "provider": "acp",
        "capabilities": {"implementation": 0.8, "testing": 0.8},
    }
    degraded["profile"] = build_agent_profile(degraded).to_dict()
    degraded["profile"]["availability"] = "degraded"
    ready["profile"] = build_agent_profile(ready).to_dict()

    result = fast_team_suggestion(
        {"name": "像素小游戏开发团队", "description": "实现并测试像素小游戏"},
        [degraded, ready],
    )

    member_ids = {member["agent_id"] for member in result["members"]}
    assert "degraded_agent" not in member_ids
    assert "ready_agent" in member_ids


def test_fast_team_suggestion_rejects_unavailable_required_agent():
    agent = {
        "id": "unavailable_agent",
        "name": "Unavailable",
        "provider": "acp",
        "capabilities": {"implementation": 0.99},
    }
    agent["profile"] = build_agent_profile(agent).to_dict()
    agent["profile"]["availability"] = "unavailable"

    result = fast_team_suggestion(
        {
            "name": "开发团队",
            "description": "实现功能",
            "required_agent_ids": [agent["id"]],
            "force_required_agent_ids": [agent["id"]],
        },
        [agent],
    )

    assert result["decision_required"] is True
    assert result["required_agent_conflicts"][0]["agent_id"] == agent["id"]
    assert agent["id"] not in {member["agent_id"] for member in result["members"]}


def test_fast_team_suggestion_replaces_unavailable_leader_with_builtin():
    agent = {
        "id": "unavailable_leader",
        "name": "Unavailable Leader",
        "provider": "acp",
    }
    agent["profile"] = build_agent_profile(agent).to_dict()
    agent["profile"]["availability"] = "degraded"

    result = fast_team_suggestion(
        {
            "name": "开发团队",
            "description": "实现功能",
            "leader_agent_id": agent["id"],
        },
        [agent],
    )

    assert result["leader_agent_id"] == "crew::builtin"
    assert agent["id"] not in {member["agent_id"] for member in result["members"]}
    assert any("Leader 已改为 Crew 内置智能体" in warning for warning in result["formation_plan"]["warnings"])


def test_fast_team_suggestion_forms_non_coding_team_from_shared_capabilities():
    agents = [
        {
            "id": "researcher",
            "name": "Research Agent",
            "provider": "kimi",
            "capabilities": {
                "information_retrieval": 0.92,
                "research": 0.9,
                "analysis": 0.86,
            },
        },
        {
            "id": "writer",
            "name": "Writer Agent",
            "provider": "hermes",
            "capabilities": {"synthesis": 0.9, "documentation": 0.92},
        },
        {
            "id": "reviewer",
            "name": "Review Agent",
            "provider": "codex",
            "capabilities": {"review": 0.9, "verification": 0.88},
        },
    ]

    result = fast_team_suggestion(
        {
            "name": "法律咨询团队",
            "description": "检索资料并分析论证，汇总结论后独立复核，输出咨询报告。",
        },
        agents,
    )

    required = set(result["formation_plan"]["coverage"]["required"])
    assert {
        "information_retrieval",
        "research",
        "analysis",
        "synthesis",
        "review",
        "verification",
        "documentation",
    } <= required
    assert result["formation_plan"]["coverage"]["uncovered"] == []
    by_id = {member["agent_id"]: member for member in result["members"]}
    assert by_id["researcher"]["role_key"] == "research_analyst"
    assert "检索和筛选信息" in by_id["researcher"]["responsibility_markdown"]
    assert "实现工作" not in by_id["researcher"]["responsibility_markdown"]
    assert by_id["writer"]["role_key"] == "technical_writer"
    assert by_id["reviewer"]["role_key"] == "independent_reviewer"
    assert "legal" not in required


def test_fast_team_suggestion_user_can_assign_kimi_to_development():
    agents = [
        {"id": "kimi_1", "name": "Kimi Writer", "provider": "kimi", "model": "moonshot"},
        {"id": "hermes_1", "name": "Hermes Frontend Coder", "provider": "hermes", "model": "code"},
        {"id": "codex_1", "name": "Codex Backend Coder", "provider": "codex", "model": "code"},
    ]
    result = fast_team_suggestion(
        {"name": "研发团队", "description": "开发一个 Web 页面和后端接口，让 Kimi 做前端开发，Hermes 写文档"},
        agents,
    )

    by_role = {member["role_key"]: member for member in result["members"]}
    assert by_role["fullstack_developer"]["agent_id"] == "kimi_1"
    assert by_role["technical_writer"]["agent_id"] == "hermes_1"
    assert "用户指定" in by_role["fullstack_developer"]["selection_reason"]
    assert "用户指定" in by_role["technical_writer"]["selection_reason"]


def test_fast_team_suggestion_excludes_agent_when_user_says_do_not_join():
    agents = [
        {"id": "kimi_1", "name": "Kimi Writer", "provider": "kimi", "model": "moonshot"},
        {"id": "codex_1", "name": "Codex Coder", "provider": "codex", "model": "code"},
        {"id": "hermes_1", "name": "Hermes Coder", "provider": "hermes", "model": "code"},
    ]
    result = fast_team_suggestion(
        {"description": "开发一个 Web 页面和后端接口，不让 Codex 加入团队"},
        agents,
    )

    member_ids = {member["agent_id"] for member in result["members"]}
    assert "codex_1" not in member_ids
    assert any("排除" in reason and "Codex" in reason for reason in result["reasons"])


def test_required_agent_with_unknown_profile_is_kept_without_warning():
    agents = [
        {"id": "hermes_1", "name": "Hermes", "provider": "hermes"},
        {"id": "kimi_1", "name": "Kimi", "provider": "kimi"},
    ]

    result = fast_team_suggestion(
        {
            "name": "研发团队",
            "description": "开发接口并完成测试",
            "required_agent_ids": ["kimi_1"],
        },
        agents,
    )

    assert result["decision_required"] is False
    assert result["required_agent_conflicts"] == []
    assert "kimi_1" in {member["agent_id"] for member in result["members"]}


def test_required_agent_with_reliable_low_assessment_requires_user_decision():
    agents = [
        {
            "id": "kimi_1",
            "name": "Kimi",
            "provider": "kimi",
            "capabilities": {"design": 0.1},
        },
    ]

    result = fast_team_suggestion(
        {
            "name": "设计团队",
            "description": "设计产品界面",
            "required_agent_ids": ["kimi_1"],
            "required_capabilities": ["design"],
        },
        agents,
    )

    assert result["decision_required"] is True
    conflict = result["required_agent_conflicts"][0]
    assert conflict["agent_id"] == "kimi_1"
    assert conflict["required_capabilities"] == ["design"]
    assert conflict["best_score"] == 0.1
    assert conflict["best_confidence"] == 0.9
    assert "kimi_1" not in {member["agent_id"] for member in result["members"]}


def test_user_can_force_required_agent_after_capability_warning():
    agents = [
        {"id": "kimi_1", "name": "Kimi", "provider": "kimi", "capabilities": {"design": 0.1}},
    ]

    result = fast_team_suggestion(
        {
            "name": "设计团队",
            "description": "设计产品界面",
            "required_agent_ids": ["kimi_1"],
            "force_required_agent_ids": ["kimi_1"],
            "required_capabilities": ["design"],
        },
        agents,
    )

    assert result["decision_required"] is False
    assert "kimi_1" in {member["agent_id"] for member in result["members"]}
    planned = next(member for member in result["formation_plan"]["members"] if member["agent_id"] == "kimi_1")
    assert planned["locked"] is True
    assert planned["selection_source"] == "user"


def test_required_agent_with_matching_profile_is_kept_without_warning():
    agents = [
        {
            "id": "kimi_1",
            "name": "Kimi",
            "provider": "kimi",
            "capabilities": {"implementation": 0.9, "testing": 0.8},
        },
    ]

    result = fast_team_suggestion(
        {
            "name": "研发团队",
            "description": "开发接口并完成测试",
            "required_agent_ids": ["kimi_1"],
        },
        agents,
    )

    assert result["decision_required"] is False
    assert "kimi_1" in {member["agent_id"] for member in result["members"]}
