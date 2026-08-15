"""Subagent 能力：定义解析 / 两层注册表 / 工具过滤(禁嵌套) / schema / FakeProvider 冒烟。

端到端测试不在此（交他人）；这里只覆盖单元 + 链路冒烟。
"""

from __future__ import annotations

import json

from crew.agent.prompt_builder import build_prompt_parts
from crew.agent.subagent import (
    ActiveSubagents,
    SubagentRegistry,
    build_run_agent_schema,
    build_delegate_task_schema,
)
from crew.agent.subagent import registry as sub_registry_mod
from crew.agent.subagent.definition import parse_definition
from crew.app import build_app
from crew.core.runctx import current_authorized_tool_names
from crew.core.types import ToolCall
from crew.state.access_control import AccessControlConfig
from crew.state.config import Config


# ── 1. 定义解析 ────────────────────────────────────────────────────────────

def test_parse_definition(tmp_path):
    f = tmp_path / "myagent.md"
    f.write_text(
        "---\n"
        "name: my-agent\n"
        "description: 测试用\n"
        "toolsets: [file, terminal]\n"
        "tools: file_read\n"
        "skills: [crew-wiki-curator]\n"
        "model: deepseek\n"
        "max_iterations: 7\n"
        "---\n"
        "你是测试子智能体。\n",
        encoding="utf-8",
    )
    d = parse_definition(f, source="user")
    assert d is not None
    assert d.name == "my-agent"
    assert d.description == "测试用"
    assert d.toolsets == ["file", "terminal"]
    assert d.tools == ["file_read"]          # 字符串归一化为单元素列表
    assert d.skills == ["crew-wiki-curator"]
    assert d.model == "deepseek"
    assert d.max_iterations == 7
    assert d.system_prompt == "你是测试子智能体。"
    assert d.source == "user"


def test_parse_definition_defaults(tmp_path):
    f = tmp_path / "bare.md"
    f.write_text("---\nname: bare\n---\n正文第一行\n", encoding="utf-8")
    d = parse_definition(f)
    assert d.toolsets is None and d.tools is None
    assert d.model == "inherit"
    assert d.max_iterations is None
    assert d.description == "正文第一行"  # 无 description 用正文首行兜底


# ── 2. 注册表两层覆盖 ──────────────────────────────────────────────────────

def test_registry_builtin_presets():
    reg = SubagentRegistry()
    # 内置预设只保留通用能力，不包含特定产品的状态栏或指南。
    assert set(reg.names()) >= {"general-purpose", "Explore", "Plan", "Wiki", "verification"}
    assert reg.get("Explore").source == "builtin"
    wiki = reg.get("Wiki")
    assert wiki is not None
    # Wiki 权限由统一策略计算：父主 Agent 最终权限 + Wiki 专属 Toolset。
    assert wiki.toolsets is None
    assert wiki.tools is None
    assert wiki.skills == ["crew-wiki-curator"]


def test_registry_user_overrides_builtin(tmp_path, monkeypatch):
    user_dir = tmp_path / "agents"
    user_dir.mkdir()
    (user_dir / "explore.md").write_text(
        "---\nname: Explore\ndescription: 用户覆盖版\n---\n用户自定义 explore\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sub_registry_mod, "get_user_agents_dir", lambda: user_dir)

    reg = SubagentRegistry()
    explore = reg.get("Explore")
    assert explore.source == "user"
    assert explore.description == "用户覆盖版"
    assert "Plan" in reg.names()  # 内置仍在


# ── 3. 工具过滤：禁嵌套 + 白名单交集 ────────────────────────────────────────

def test_subagent_tool_filter_blacklist():
    """🔴 工具黑名单：禁嵌套（delegate_task/run_agent/delegate_to_external_agent）
    + 写副作用工具（memory/cron/feishu/wiki_*），正常只读工具保留。"""
    app = build_app(config=Config(max_iterations=5))
    # 继承全量（toolsets=None）也不能拿到嵌套委派工具
    inherited = app._subagent_tool_filter(None, None)
    assert "delegate_task" not in inherited
    assert "run_agent" not in inherited
    assert "delegate_to_external_agent" not in inherited
    # 写副作用工具同样被过滤
    assert "memory" not in inherited
    assert "cron_create" not in inherited
    assert not any(n.startswith("feishu") for n in inherited)
    assert not any(name.startswith("wiki_") for name in inherited)
    assert "file_read" in inherited  # 正常只读工具仍在


def test_subagent_tool_filter_whitelist():
    app = build_app(config=Config(max_iterations=5))
    names = app._subagent_tool_filter(["file"], ["file_read"])
    assert names == ["file_read"]


# ── 4. schema ──────────────────────────────────────────────────────────────

def test_run_agent_schema_enum_matches_registry():
    reg = SubagentRegistry()
    schema = build_run_agent_schema(reg.list())
    assert schema["name"] == "run_agent"
    assert set(schema["parameters"]["properties"]["agent_type"]["enum"]) == set(reg.names())
    assert schema["parameters"]["required"] == ["agent_type", "goal"]
    # 描述里应包含每个 agent 的 description（whenToUse 语义）
    for agent in reg.list():
        assert agent.description in schema["description"]


def test_delegate_task_schema():
    schema = build_delegate_task_schema()
    props = schema["parameters"]["properties"]
    assert "goal" in props and "tasks" in props and "toolsets" in props
    # goal / tasks 二选一：顶层不强制 goal，纯 tasks 批量模式才能过校验
    assert schema["parameters"]["required"] == []
    # skills 与 toolsets 并列，单任务/批量子任务都暴露
    assert "skills" in props
    assert "skills" in props["tasks"]["items"]["properties"]


# ── 4b. delegate_task 继承主 agent 技能 ─────────────────────────────────────

def test_subagent_inherits_parent_skills():
    """delegate_task 子 agent 继承主 agent 的 skills：不传=继承全部，
    指定=取交集，传[]=不继承；固定预设可绑定自己的 Skill。"""
    from crew.app import _parent_allowed_skill_slugs
    from crew.core.runctx import current_skill_scope

    app = build_app(config=Config(max_iterations=5))
    parent_slugs = _parent_allowed_skill_slugs(None, None)
    assert parent_slugs, "测试环境应至少有一个可用 skill"
    sample = next(iter(parent_slugs))

    base_spec = {
        "system_prompt": "x",
        "toolsets": None,
        "tools": None,
        "model": "inherit",
        "max_iterations": None,
    }
    current_skill_scope.set((None, None))
    try:
        # A: 不传 skills → 继承父全部（enabled/disabled 均为 None）
        a = app._make_subagent({**base_spec, "inherit_skills": True, "skills": None})
        assert a.inject_skills is True
        assert a.enabled_skills is None and a.disabled_skills is None

        # B: 指定列表 → 与父允许集取交集
        b = app._make_subagent({**base_spec, "inherit_skills": True, "skills": [sample, "no-such-skill"]})
        assert b.inject_skills is True
        assert b.enabled_skills == [sample]

        # C: 传空列表 → 不继承
        c = app._make_subagent({**base_spec, "inherit_skills": True, "skills": []})
        assert c.inject_skills is True
        assert c.disabled_skills == ["*"]

        # D: run_agent 路径（无 inherit_skills）→ 不注入
        d = app._make_subagent({**base_spec})
        assert d.inject_skills is False

        preset = app._make_subagent({**base_spec, "preset_skills": [sample], "skills": [sample]})
        assert preset.inject_skills is True
        assert preset.enabled_skills == [sample]

        # E: 父范围受限，子请求父没有的 skill → 越权裁剪到空
        current_skill_scope.set(([sample], None))
        e = app._make_subagent({**base_spec, "inherit_skills": True, "skills": ["some-other-skill"]})
        assert e.inject_skills is True
        assert e.enabled_skills == [] and e.disabled_skills == ["*"]
        current_skill_scope.set((None, None))
    finally:
        current_skill_scope.set((None, None))


def test_lightweight_subagent_prompt_injects_skills_when_inherited():
    """delegate_task 子 agent（lightweight+inject_skills）prompt 含 skills 索引；
    run_agent 路径（inject_skills=False）不含。"""
    from crew.agent.skills import list_skills
    if not list_skills():
        return  # 无 skill 环境 skip

    inj = build_prompt_parts(lightweight=True, inject_skills=True)
    assert "可用 Skills" in inj["user_reminder"]

    no_inj = build_prompt_parts(lightweight=True, inject_skills=False)
    assert "可用 Skills" not in no_inj["user_reminder"]


# ── 5. FakeProvider 冒烟：工具跑通 + 子 agent 无 delegate_task ──────────────

async def test_run_agent_tool_smoke():
    app = build_app(config=Config(max_iterations=5))  # 无 key → FakeProvider
    assert "run_agent" in app.registry.names()
    result = await app.registry.execute(
        ToolCall("c1", "run_agent", {"agent_type": "Explore", "goal": "说你好"})
    )
    assert not result.is_error
    assert result.content_trust == "untrusted"
    assert result.content_source == "subagent"
    # 结构化结果（🟡）：status / summary / duration_seconds / tool_calls
    payload = json.loads(result.content)
    one = payload["results"][0]
    assert one["agent"] == "Explore"
    assert one["status"] == "completed"
    assert one["summary"].strip()
    assert "duration_seconds" in one and "tool_calls" in one


async def test_delegate_task_tool_smoke():
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall("c2", "delegate_task", {"goal": "算 1+1", "toolsets": ["terminal"]})
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["results"][0]["status"] == "completed"


async def test_delegate_task_batch_parallel():
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall("c3", "delegate_task", {
            "goal": "ignored",
            "tasks": [{"goal": "任务A"}, {"goal": "任务B"}],
        })
    )
    assert not result.is_error
    labels = {r["agent"] for r in json.loads(result.content)["results"]}
    assert labels == {"task#0", "task#1"}


async def test_delegate_task_batch_without_goal_passes_schema():
    """🔴 回归：纯 tasks 批量模式（不传 goal）必须能过 schema 校验。
    顶层 required=['goal'] 的旧 schema 会以 'goal' is a required property 拒绝调用，
    正是用户实际踩坑的场景（goal 与 tasks 应二选一）。
    """
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall("c3b", "delegate_task", {
            "tasks": [{"goal": "任务A"}, {"goal": "任务B"}],
        })
    )
    assert not result.is_error, f"纯 tasks 模式被拒：{result.content}"
    payload = json.loads(result.content)
    # 同步路径返回 results 数组（非 launched 单对象）
    assert isinstance(payload.get("results"), list)
    assert len(payload["results"]) == 2
    labels = {r["agent"] for r in payload["results"]}
    assert labels == {"task#0", "task#1"}
    assert all(r["status"] == "completed" for r in payload["results"])


async def test_delegate_task_empty_input_rejected_at_handler():
    """🔴 goal 与 tasks 二选一放宽后，两者都不传仍要被运行时兜底拒绝
    （handler 把空输入构造成单任务 -> goal 空 -> 返回 tool_error 串）。
    注意：tool_error 走成功路径返回 JSON 串（is_error=False），错误在 content 里。
    """
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(ToolCall("c3c", "delegate_task", {}))
    assert "Each task must provide a non-empty goal" in result.content


async def test_delegate_task_caps_task_count():
    """🔴 批量任务数上限：超过 max_tasks 直接拒绝，防失控 spawn。"""
    app = build_app(config=Config(max_iterations=5, subagent_max_tasks=2))
    result = await app.registry.execute(
        ToolCall("c4", "delegate_task", {
            "goal": "x",
            "tasks": [{"goal": "a"}, {"goal": "b"}, {"goal": "c"}],
        })
    )
    assert result.is_error and ("最多委派" in result.content or "Too many tasks" in result.content)


def test_subagent_inherits_parent_final_authorization_snapshot():
    app = build_app(config=Config(max_iterations=5))
    token = current_authorized_tool_names.set(frozenset({"file_read"}))
    try:
        inherited = app._subagent_tool_filter(["file", "terminal"], None)
    finally:
        current_authorized_tool_names.reset(token)
    assert inherited == ["file_read"]


def test_lightweight_prompt_skips_global_context():
    """🔴 上下文隔离：lightweight 子 agent 不注入全局 workspace/记忆。"""
    app = build_app(config=Config(max_iterations=5))
    child = app._make_subagent(
        {"system_prompt": "x", "toolsets": None, "tools": None,
         "model": "inherit", "max_iterations": 5}
    )
    assert child.lightweight is True
    light = build_prompt_parts(workspace_instructions="组织规则X", lightweight=True)
    full = build_prompt_parts(workspace_instructions="组织规则X", lightweight=False)
    assert "组织规则X" not in light["user_reminder"]
    assert "组织规则X" in full["user_reminder"]


def test_active_subagents_interrupt_cascade():
    """🔴 中断级联：CrewApp.interrupt 经 ActiveSubagents 下发到运行中的子 agent。"""
    class _FakeChild:
        def __init__(self):
            self.interrupted = False

        def interrupt(self, message=None):
            self.interrupted = True

    active = ActiveSubagents()
    child = _FakeChild()
    active.register("s1", "c1", {"child_id": "c1", "label": "x", "agent": child})
    assert active.snapshot("s1")[0]["label"] == "x"      # 快照不含 agent 句柄
    assert "agent" not in active.snapshot("s1")[0]
    assert active.interrupt("s1") is True
    assert child.interrupted is True
    active.unregister("s1", "c1")
    assert active.snapshot("s1") == []


def test_explore_preset_is_read_only():
    """Explore 预设与 Crew 对齐：只读，拿不到 file_write / patch。"""
    reg = SubagentRegistry()
    explore = reg.get("Explore")
    assert explore.tools == ["file_read", "glob", "grep", "terminal"]

    app = build_app(config=Config(max_iterations=5))
    names = app._subagent_tool_filter(explore.toolsets, explore.tools)
    assert "file_read" in names
    assert "file_write" not in names and "patch" not in names


def _fake_child(chunks_fn):
    """构造一个假 child：run() 产出 chunks_fn() 生成的 ResponseChunk 序列。"""
    import asyncio as _asyncio  # noqa: F401

    class _Fake:
        def __init__(self):
            self.interrupted = False
            self.closed = False

        async def run(self, env):
            async for c in chunks_fn():
                yield c

        def interrupt(self, message=None):
            self.interrupted = True

        async def aclose(self):
            self.closed = True

    return _Fake()


async def test_subagent_closes_child_owned_resources_after_run():
    from crew.core.envelope import ResponseChunk

    async def chunks():
        yield ResponseChunk.final("r", "done")

    child = _fake_child(chunks)
    result = await _one_child(lambda _spec: child, idle=1, mx=1)

    assert result["status"] == "completed"
    assert child.closed is True


async def test_subagent_output_is_untrusted_and_errors_are_stable():
    """子 agent 的输出不参与授权，异常也不能把宿主细节回传给主 agent。"""
    from crew.core.envelope import ResponseChunk

    async def chunks():
        yield ResponseChunk.final(
            "r", "完成 sk-proj-abcdefghijklmnopqrstuvwxyz1234567890"
        )

    result = await _one_child(lambda _spec: _fake_child(chunks), idle=1, mx=1)

    assert result["content_trust"] == "untrusted"
    assert result["content_source"] == "subagent"
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz1234567890" not in result["summary"]

    async def errors():
        yield ResponseChunk.error("r", r"C:\private\config.yaml ACCESS_TOKEN=secret")

    failed = await _one_child(lambda _spec: _fake_child(errors), idle=1, mx=1)
    assert failed["status"] == "error"
    assert failed["summary"] == "子智能体执行失败"
    assert "config.yaml" not in failed["summary"]


async def _one_child(build, *, idle, mx):
    from crew.agent.subagent.tools import _run_one_child
    return await _run_one_child(
        label="x", spec={}, goal_text="g", build_child=build,
        parent_session_id="s", active=None, idle_timeout=idle, max_runtime=mx,
    )


async def test_subagent_idle_timeout_kills_hung_child():
    """🔴 空闲超时：发了工具+部分文本后卡死 → 中止，返回诊断+部分输出+last_tool。"""
    import asyncio as _asyncio
    from crew.core.envelope import ResponseChunk

    async def chunks():
        yield ResponseChunk.tool_event("r", "grep", "start", tool_call_id="t1")
        yield ResponseChunk.delta("r", "正在分析 foo.py")
        await _asyncio.sleep(10)  # 卡死

    res = await _one_child(lambda spec: _fake_child(chunks), idle=0.3, mx=0)
    assert res["status"] == "timeout"
    assert res["tool_calls"] == 1
    assert res["last_tool"] == "grep"
    assert "grep" in res["summary"]      # 诊断含最后工具
    assert "正在分析 foo.py" in res["summary"]    # 部分输出


async def test_subagent_idle_timeout_not_triggered_when_progressing():
    """健康 agent 持续吐 chunk（间隔 < idle）→ 正常完成，不被误杀。"""
    import asyncio as _asyncio
    from crew.core.envelope import ResponseChunk

    async def chunks():
        for i in range(20):           # 总时长 ~1s，远超 idle=0.3
            await _asyncio.sleep(0.05)
            yield ResponseChunk.delta("r", f"step{i} ")
        yield ResponseChunk.final("r", "完成")

    res = await _one_child(lambda spec: _fake_child(chunks), idle=0.3, mx=0)
    assert res["status"] == "completed"
    assert res["summary"] == "完成"


async def test_subagent_absolute_runtime_backstop():
    """🔴 绝对上限兜底：即使持续吐 chunk，超过 max_runtime 也中止。"""
    import asyncio as _asyncio
    from crew.core.envelope import ResponseChunk

    async def chunks():
        while True:                    # 永不停，但持续有活动
            await _asyncio.sleep(0.05)
            yield ResponseChunk.delta("r", "x")

    res = await _one_child(lambda spec: _fake_child(chunks), idle=5.0, mx=0.5)
    assert res["status"] == "timeout"
    assert "运行上限" in res["summary"]


def test_subagent_capped_by_parent_user_type():
    """🔴 防越权：外部受限用户的子 agent 不能拿到父本身拿不到的工具。"""
    cfg = Config(max_iterations=5)
    # 外部用户只允许 file 工具集
    cfg.access_control = AccessControlConfig(
        user_type="internal",
        external={"enabled_toolsets": ["file"]},
        internal={},
    )
    app = build_app(config=cfg)

    # 内部父：toolsets=None 继承全量 → 含 terminal
    internal_names = app._subagent_tool_filter(None, None, user_type="internal")
    assert "terminal" in internal_names

    # 外部父：即便子请求 terminal 工具集，也被父 access_control 封顶剔除
    external_names = app._subagent_tool_filter(["terminal"], None, user_type="external")
    assert "terminal" not in external_names
    assert external_names == [] or all(
        app.registry.toolset_for(n) == "file" for n in external_names
    )


async def test_subagent_session_not_persisted():
    """会话堆积：lightweight 子 agent 不把一次性会话写进 session_store。"""
    app = build_app(config=Config(max_iterations=5))
    before = len(app.session_store.list_sessions())
    await app.registry.execute(
        ToolCall("p1", "run_agent", {"agent_type": "Explore", "goal": "hi"})
    )
    after = len(app.session_store.list_sessions())
    assert after == before  # 子会话未落库


def test_subagent_tool_filter_coerces_string_toolsets():
    """🔴 健壮性：模型把 toolsets/tools 传成字符串时不能静默拿到空工具集。"""
    app = build_app(config=Config(max_iterations=5))
    as_list = app._subagent_tool_filter(["file"], None)
    as_str = app._subagent_tool_filter("file", None)
    assert as_str == as_list and as_str  # 字符串与列表等价，且非空
    # tools 白名单同理
    assert app._subagent_tool_filter("file", "file_read") == ["file_read"]


def test_run_agent_not_registered_without_presets(monkeypatch):
    """🔴 健壮性：无预设时不注册 run_agent（避免空 enum），delegate_task 仍在。"""
    empty_dir = sub_registry_mod._PRESETS_DIR.parent / "presets_none_xyz"
    # 指向一个不存在的目录 → 注册表为空
    monkeypatch.setattr(sub_registry_mod, "_PRESETS_DIR", empty_dir)
    monkeypatch.setattr(sub_registry_mod, "get_user_agents_dir", lambda: empty_dir)

    app = build_app(config=Config(max_iterations=5))
    assert app.subagent_registry.names() == []
    assert "run_agent" not in app.registry.names()
    assert "delegate_task" in app.registry.names()  # 自定义委派不受影响


def test_run_agent_schema_has_model_override():
    """run_agent 支持 per-call model 覆盖。"""
    schema = build_run_agent_schema(["Explore"])
    assert "model" in schema["parameters"]["properties"]
    assert "model" not in schema["parameters"]["required"]  # 可选


def test_verification_preset_is_background():
    """verification 预设默认后台，其余预设默认前台。"""
    reg = SubagentRegistry()
    assert reg.get("verification").background is True
    assert reg.get("Explore").background is False


def test_verification_preset_can_actually_receive_browser_use():
    """tools 白名单与 toolsets 取交集；必须显式包含 browser toolset。"""
    app = build_app(config=Config(max_iterations=5))
    verification = SubagentRegistry().get("verification")

    names = app._subagent_tool_filter(verification.toolsets, verification.tools)

    assert "browser_use" in names


async def test_run_agent_background_launch_and_collect():
    """后台异步：run_in_background 立即返回 task_id，collect(wait) 取结构化结果。"""
    app = build_app(config=Config(max_iterations=5))
    assert "collect_subagent" in app.registry.names()

    launched = await app.registry.execute(
        ToolCall("b1", "run_agent",
                 {"agent_type": "Explore", "goal": "hi", "run_in_background": True})
    )
    payload = json.loads(launched.content)
    assert payload["status"] == "launched"
    task_id = payload["task_id"]

    collected = await app.registry.execute(
        ToolCall("b2", "collect_subagent", {"task_id": task_id, "wait": True})
    )
    res = json.loads(collected.content)
    assert res["agent"] == "Explore"
    assert res["status"] == "completed"
    assert res["summary"].strip()
    # 看板状态也更新为 done
    assert app.subagent_tasks.get(task_id)["status"] == "done"
    # 完成后 wait=False 轮询同样取到结构化结果
    polled = await app.registry.execute(
        ToolCall("b2p", "collect_subagent", {"task_id": task_id, "wait": False})
    )
    assert json.loads(polled.content)["status"] == "completed"


async def test_background_completion_auto_injected_next_turn():
    """后台完成结果自动注入下一轮主 agent 上下文（无需模型主动 collect）。"""
    from crew.core.envelope import Envelope
    from crew.core.runctx import current_session_id
    from crew.agent.runtime import _format_subagent_notifications

    app = build_app(config=Config(max_iterations=5))
    tok = current_session_id.set("s1")
    try:
        await app.registry.execute(
            ToolCall("n1", "run_agent",
                     {"agent_type": "Explore", "goal": "hi", "run_in_background": True})
        )
        # 等后台跑完（不 collect）→ 完成回调入队
        for t in list(app._subagent_bg_tasks):
            await t
    finally:
        current_session_id.reset(tok)

    assert ("", "s1") in app._subagent_pending  # 未 collect → 已入队待自动注入

    # 下一轮：handle 的 drain 把待通知放进 envelope.params
    env = Envelope.of("继续", session_id="s1", user_id="")
    app._drain_subagent_notifications(env)
    notifs = env.params["subagent_notifications"]
    assert notifs and notifs[0]["agent"] == "Explore"
    assert ("", "s1") not in app._subagent_pending  # 排空后不重复注入

    block = _format_subagent_notifications(notifs)
    assert "后台子任务完成通知" in block and "Explore" in block


async def test_collect_dedupes_auto_injection():
    """主动 collect 取走结果后，不再于下一轮重复自动注入。"""
    from crew.core.runctx import current_session_id

    app = build_app(config=Config(max_iterations=5))
    tok = current_session_id.set("s2")
    try:
        launched = await app.registry.execute(
            ToolCall("d1", "run_agent",
                     {"agent_type": "Explore", "goal": "hi", "run_in_background": True})
        )
        tid = json.loads(launched.content)["task_id"]
        await app.registry.execute(
            ToolCall("d2", "collect_subagent", {"task_id": tid, "wait": True})
        )
    finally:
        current_session_id.reset(tok)

    # collect 已消费 → 不应再留在待注入队列
    assert ("", "s2") not in app._subagent_pending


def test_subagent_pending_collect_is_owner_scoped():
    """同名 session 的后台通知按 owner 清理，collect 不会误删其它账号队列。"""
    from crew.core.envelope import Envelope

    app = build_app(config=Config(max_iterations=5))
    app._subagent_pending[("A:uid-a", "same")] = [{"task_id": "a", "agent": "Explore"}]
    app._subagent_pending[("B:uid-b", "same")] = [{"task_id": "b", "agent": "Explore"}]

    app._on_subagent_collected("same", "a", owner_account_id="A:uid-a")

    assert ("A:uid-a", "same") not in app._subagent_pending
    assert app._subagent_pending[("B:uid-b", "same")][0]["task_id"] == "b"

    env_b = Envelope.of("继续", session_id="same", user_id="B:uid-b")
    app._drain_subagent_notifications(env_b)
    assert env_b.params["subagent_notifications"][0]["task_id"] == "b"


def test_format_subagent_notifications_empty():
    from crew.agent.runtime import _format_subagent_notifications
    assert _format_subagent_notifications(None) == ""
    assert _format_subagent_notifications([]) == ""


async def test_background_concurrency_capped():
    """🔴 防失控：后台子任务达并发上限时拒绝新的 run_in_background。"""
    import asyncio as _asyncio

    app = build_app(config=Config(max_iterations=5, subagent_max_concurrent=1))
    # 占满后台容量（一个未完成的占位任务）
    filler = _asyncio.ensure_future(_asyncio.sleep(0.2))
    app._subagent_bg_tasks.add(filler)
    try:
        result = await app.registry.execute(
            ToolCall("cap", "run_agent",
                     {"agent_type": "Explore", "goal": "x", "run_in_background": True})
        )
        payload = json.loads(result.content)
        assert payload["status"] == "rejected"
    finally:
        filler.cancel()
        app._subagent_bg_tasks.discard(filler)


async def test_collect_unknown_task_errors():
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall("b3", "collect_subagent", {"task_id": "nope"})
    )
    assert "error" in result.content and ("任务不存在" in result.content or "Task not found" in result.content)


def test_child_agent_has_no_subagent_tools():
    app = build_app(config=Config(max_iterations=5))
    child = app._make_subagent(
        {"system_prompt": "x", "toolsets": None, "tools": None,
         "model": "inherit", "max_iterations": 5}
    )
    assert "delegate_task" not in child.tool_filter
    assert "run_agent" not in child.tool_filter


# ── 6. delegate_task 后台模式（run_in_background） ──────────────────────────

def test_delegate_task_schema_has_run_in_background():
    """delegate_task schema 暴露 run_in_background，且为可选（不强制）。"""
    schema = build_delegate_task_schema()
    props = schema["parameters"]["properties"]
    assert "run_in_background" in props
    assert props["run_in_background"]["type"] == "boolean"
    assert "run_in_background" not in schema["parameters"]["required"]


async def test_delegate_task_background_launch_and_collect():
    """单任务 + run_in_background 立即返回 task_id；collect(wait) 取结构化结果。
    复用 run_agent 同一条后台管道（_launch_one_bg / _run_background / tasks 看板）。"""
    app = build_app(config=Config(max_iterations=5))
    assert "collect_subagent" in app.registry.names()

    launched = await app.registry.execute(
        ToolCall("dbg1", "delegate_task",
                 {"goal": "查一下", "run_in_background": True})
    )
    payload = json.loads(launched.content)
    assert payload["status"] == "launched"
    task_id = payload["task_id"]
    # 后台 agent 标签用 goal 摘要，比默认的 task#0 可读
    assert payload["agent"] == "查一下"

    collected = await app.registry.execute(
        ToolCall("dbg2", "collect_subagent", {"task_id": task_id, "wait": True})
    )
    res = json.loads(collected.content)
    assert res["status"] == "completed"
    assert res["summary"].strip()
    assert app.subagent_tasks.get(task_id)["status"] == "done"


async def test_delegate_task_background_rejects_batch():
    """后台仅单任务：tasks 数组 + run_in_background 被拒（ToolError）。"""
    app = build_app(config=Config(max_iterations=5))
    result = await app.registry.execute(
        ToolCall("dbg3", "delegate_task", {
            "run_in_background": True,
            "tasks": [{"goal": "任务A"}, {"goal": "任务B"}],
        })
    )
    assert result.is_error
    assert "后台模式不支持批量任务" in result.content


async def test_delegate_task_background_auto_injected_next_turn():
    """后台完成结果自动注入下一轮主 agent 上下文（与 run_agent 同路径，
    共用 _on_subagent_background_done -> _drain_subagent_notifications）。"""
    from crew.core.envelope import Envelope
    from crew.core.runctx import current_session_id
    from crew.agent.runtime import _format_subagent_notifications

    app = build_app(config=Config(max_iterations=5))
    tok = current_session_id.set("s-dbg")
    try:
        await app.registry.execute(
            ToolCall("dbg4", "delegate_task",
                     {"goal": "hi", "run_in_background": True})
        )
        for t in list(app._subagent_bg_tasks):
            await t
    finally:
        current_session_id.reset(tok)

    assert ("", "s-dbg") in app._subagent_pending  # 未 collect -> 已入队待自动注入

    env = Envelope.of("继续", session_id="s-dbg", user_id="")
    app._drain_subagent_notifications(env)
    notifs = env.params["subagent_notifications"]
    assert notifs and notifs[0]["status"] == "completed"
    block = _format_subagent_notifications(notifs)
    assert "后台子任务完成通知" in block
    assert "delegate_task" in block  # 文案泛化后含 delegate_task
    assert ("", "s-dbg") not in app._subagent_pending  # 排空后不重复注入


# ── 7. 多智能体 member 后台 delegate_task 通知回到发起 member ────────────────

async def test_team_member_bg_delegate_notifies_to_member_session():
    """🔴 适配后：team member 后台 delegate_task 通知按 member 子会话隔离，
    入队到 child_session_id（team-s1::coder）而非 team_session（team-s1）。
    member 执行时 current_session_id=team_session(task_sid)，但 current_subagent_notify_session
    =member_session_id 优先作为入队 key。"""
    from crew.core.runctx import current_session_id, current_subagent_notify_session

    app = build_app(config=Config(max_iterations=5))
    tok1 = current_session_id.set("team-s1")               # member 的 task_sid
    tok2 = current_subagent_notify_session.set("team-s1::coder")  # member 子会话
    try:
        await app.registry.execute(
            ToolCall("tmbg1", "delegate_task", {"goal": "hi", "run_in_background": True})
        )
        for t in list(app._subagent_bg_tasks):
            await t
    finally:
        current_session_id.reset(tok1)
        current_subagent_notify_session.reset(tok2)

    # 入队到 member 子会话，而非 team_session
    assert ("", "team-s1::coder") in app._subagent_pending
    assert ("", "team-s1") not in app._subagent_pending


async def test_team_member_run_drains_bg_notifications():
    """🔴 适配后：member 下一轮被派活（SingleAgent.run）时，开头 drain 自己的后台
    完成通知注入本轮上下文（team 模式下 member.run 不经 app.handle 的 drain）。"""
    from crew.core.envelope import Envelope
    from crew.core.runctx import current_session_id, current_subagent_notify_session
    from crew.agent.runtime import SingleAgent, _format_subagent_notifications

    app = build_app(config=Config(max_iterations=5))
    # 上轮：member 后台 delegate_task -> 通知入队 child_session_id
    tok1 = current_session_id.set("team-s1")
    tok2 = current_subagent_notify_session.set("team-s1::coder")
    try:
        await app.registry.execute(
            ToolCall("tmbg2", "delegate_task", {"goal": "hi", "run_in_background": True})
        )
        for t in list(app._subagent_bg_tasks):
            await t
    finally:
        current_session_id.reset(tok1)
        current_subagent_notify_session.reset(tok2)
    assert ("", "team-s1::coder") in app._subagent_pending

    # member 下一轮：构造带 subagent_drain_fn 的 member，run member sub_env
    member = SingleAgent(
        provider=app.provider,
        registry=app.registry,
        session_store=app.session_store,
        memory=app.memory,
        plugins=app.plugins,
        system_prompt="你是 member",
        max_iterations=2,
        subagent_drain_fn=app.pop_subagent_notifications,
    )
    sub_env = Envelope.of(
        "继续",
        session_id="team-s1::coder",
        user_id="",
        params={"member_session_id": "team-s1::coder", "task_session_id": "team-s1"},
    )
    async for _chunk in member.run(sub_env):
        pass  # 跑完即可，drain 在 run 开头发生

    # drain 已把通知注入 sub_env.params + 从队列移除
    assert ("", "team-s1::coder") not in app._subagent_pending
    notifs = sub_env.params.get("subagent_notifications")
    assert notifs and notifs[0]["status"] == "completed"
    assert "后台子任务完成通知" in _format_subagent_notifications(notifs)
