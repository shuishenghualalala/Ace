"""工具结果生命周期集成测试（C1–C10）。

与 tests/test_compact.py 的差异：那里用手写 resolver 验证压缩规则本身；
这里用**真实注册表**（register_builtin_tools / register_blueprint_tools /
register_wiki_tools / register_subagent_tools / 团队与外援工具）+ 真实
``registry.result_policy`` 驱动真实 ``ContextCompactor``，模拟 Site 场景下
连续调用 Skill、Widget/Canvas、文件、终端、Wiki、子 Agent 工具的会话序列，
验证：

- Skill 指令不被临时结果挤掉、重复读取只留最新版（G1）；
- 用户选择、团队消息、外援与子 Agent 结论不被日常压缩清除（G2）；
- file_read 旧分片由 L1 清成信息摘要，Wiki 页面只保留最近版本（G3）；
- 完整压缩后受保护结果恢复且跨多次压缩存活，最近文件从磁盘重读（G4）；
- 未声明工具与已卸载插件工具按重要结果保护（G5）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.agent.compact.microcompact import (
    INSTRUCTION_REPLACED_STUB,
    RESOURCE_REPLACED_STUB,
    TOOL_SUMMARY_PREFIX,
    micro_compact,
)
from crew.agent.compact.pipeline import ContextCompactor
from crew.agent.compact.post_compact import (
    POST_COMPACT_RESULTS_MARKER,
    build_post_compact_attachments,
)
from crew.agent.compact.summary import SUMMARY_MARKER
from crew.agent.external.tools import register_external_agent_tools
from crew.agent.loop.tool_guardrails import (
    ToolCallGuardrailConfig,
    ToolCallGuardrailController,
)
from crew.agent.subagent.tools import SubagentRegistry, register_subagent_tools
from crew.core.interfaces import ToolResultRetention
from crew.core.types import ChatResponse, Message, ToolCall
from crew.team.bus import TeamBus, register_team_bus_tools
from crew.tools.blueprint_tools import register_blueprint_tools
from crew.tools.builtin import register_builtin_tools
from crew.tools.registry import Registry
from crew.wiki.tools import register_wiki_tools


# --------------------------------------------------------------------------- #
# 真实注册表：按 Site 会话的工具面组装，只注册不执行
# --------------------------------------------------------------------------- #
def _site_registry() -> Registry:
    registry = Registry()
    register_builtin_tools(registry)
    register_blueprint_tools(
        registry, SimpleNamespace(blueprint=SimpleNamespace(store=None))
    )
    register_wiki_tools(registry, None, None, None, None)  # type: ignore[arg-type]
    register_subagent_tools(
        registry, SubagentRegistry(), build_child=lambda _args: None  # type: ignore[arg-type,return-value]
    )
    register_team_bus_tools(
        registry, TeamBus(), team_session_id="team", member_id="m1", member_ids=["m1"]
    )
    register_external_agent_tools(registry, None)  # type: ignore[arg-type]
    return registry


@pytest.fixture()
def registry() -> Registry:
    return _site_registry()


# --------------------------------------------------------------------------- #
# 消息构造助手
# --------------------------------------------------------------------------- #
def _exchange(
    messages: list[Message],
    call_id: str,
    tool_name: str,
    arguments: dict,
    content: str,
) -> None:
    """追加一轮 assistant(tool_calls) + tool 结果，保持配对完整。"""
    messages.append(
        Message.assistant(
            f"调用 {tool_name}",
            tool_calls=[ToolCall(id=call_id, name=tool_name, arguments=arguments)],
        )
    )
    messages.append(Message.tool(call_id, content, name=tool_name))


def _temporary_exchange(messages: list[Message], index: int, content_size: int = 50) -> None:
    _exchange(
        messages,
        f"terminal_{index}",
        "terminal",
        {"command": f"cmd {index}"},
        f"临时输出{index}" * content_size,
    )


def _tool_contents(out: list[Message], name: str) -> list[str]:
    return [m.content for m in out if m.role == "tool" and m.name == name]


class _FakeProvider:
    async def chat(self, messages, tools=None):  # noqa: ANN001
        return ChatResponse(text="历史摘要")

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        yield  # pragma: no cover


# --------------------------------------------------------------------------- #
# C10：Site 会话工具面的生命周期声明审计（其余用例的前置）
# --------------------------------------------------------------------------- #
def test_site_session_tool_retention_audit(registry: Registry):
    expectations = {
        "terminal": ToolResultRetention.TEMPORARY,
        "grep": ToolResultRetention.TEMPORARY,
        # 文件内容按临时结果处理：旧分片由 L1 清成一行摘要（可随时重读），
        # 压缩后恢复按 path 从磁盘重读最新内容，不做分片级 RESOURCE 去重。
        "file_read": ToolResultRetention.TEMPORARY,
        "skill_view": ToolResultRetention.INSTRUCTION,
        "wiki_read": ToolResultRetention.RESOURCE,
        "ask_followup_question": ToolResultRetention.IMPORTANT,
        "delegate_task": ToolResultRetention.IMPORTANT,
        "run_agent": ToolResultRetention.IMPORTANT,
        "collect_subagent": ToolResultRetention.IMPORTANT,
        "team_read_messages": ToolResultRetention.IMPORTANT,
        "delegate_to_external_agent": ToolResultRetention.IMPORTANT,
        "publish_site": ToolResultRetention.IMPORTANT,
    }
    for tool_name, retention in expectations.items():
        args = {"name": "webapp-building"} if tool_name == "skill_view" else {}
        policy = registry.result_policy(tool_name, args)
        assert policy.retention is retention, f"{tool_name} 应为 {retention}"

    # Widget / Canvas：读类动作是临时过程，写类动作按重要结果保护
    for tool_name in ("Widget", "Canvas"):
        read_policy = registry.result_policy(tool_name, {"action": "list"})
        write_policy = registry.result_policy(tool_name, {"action": "create"})
        assert read_policy.retention is ToolResultRetention.TEMPORARY
        assert write_policy.retention is ToolResultRetention.IMPORTANT

    # 所有已注册工具的策略解析都不抛异常、返回合法策略
    for tool_name in registry.names():
        policy = registry.result_policy(tool_name, {})
        assert policy.retention in set(ToolResultRetention)


# --------------------------------------------------------------------------- #
# C1：连续 Widget/Canvas/文件/终端调用后，Skill 指令不被挤掉
# --------------------------------------------------------------------------- #
def test_skill_instruction_survives_site_tool_flood(registry: Registry):
    msgs: list[Message] = [Message.user("创建一个灵感 App")]
    _exchange(
        msgs, "skill_1", "skill_view", {"name": "webapp-building"}, "建站指令" * 100
    )
    for i in range(4):
        _exchange(msgs, f"widget_{i}", "Widget", {"action": "list"}, f"组件列表{i}" * 40)
        _exchange(msgs, f"canvas_{i}", "Canvas", {"action": "read"}, f"画布内容{i}" * 40)
        _temporary_exchange(msgs, i)

    out = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=registry.result_policy
    )

    skill = next(m for m in out if m.tool_call_id == "skill_1")
    assert skill.content == "建站指令" * 100  # 指令原文完整保留
    # 临时过程只留最近 3 条，更早的变成信息摘要
    temporary = [
        m
        for m in out
        if m.role == "tool" and m.name in {"terminal", "Widget", "Canvas"}
    ]
    summarized = [m for m in temporary if m.content.startswith(TOOL_SUMMARY_PREFIX)]
    assert len(temporary) - len(summarized) == 3


# --------------------------------------------------------------------------- #
# C2：重复读取同一 Skill 只保留最新版本；大小写与路径分隔符归一
# --------------------------------------------------------------------------- #
def test_duplicate_skill_view_keeps_only_latest(registry: Registry):
    msgs: list[Message] = [Message.user("建站")]
    _exchange(msgs, "s1", "skill_view", {"name": "WebApp-Building"}, "旧版指令")
    _temporary_exchange(msgs, 0)
    _exchange(msgs, "s2", "skill_view", {"name": "webapp-building"}, "新版指令")
    # 同一资源文件：Windows 反斜杠与正斜杠应识别为同一资源
    _exchange(
        msgs, "s3", "skill_view",
        {"name": "webapp-building", "file_path": "references\\runtime.md"}, "旧运行时说明",
    )
    _exchange(
        msgs, "s4", "skill_view",
        {"name": "webapp-building", "file_path": "references/runtime.md"}, "新运行时说明",
    )

    out = micro_compact(
        msgs, keep_recent_tools=6, result_policy_resolver=registry.result_policy
    )
    by_id = {m.tool_call_id: m.content for m in out if m.role == "tool"}

    assert by_id["s1"] == INSTRUCTION_REPLACED_STUB.format(identity="skill=webapp-building")
    assert by_id["s2"] == "新版指令"
    assert by_id["s3"] == RESOURCE_REPLACED_STUB.format(
        identity="skill=webapp-building|file=references/runtime.md"
    )
    assert by_id["s4"] == "新运行时说明"


# --------------------------------------------------------------------------- #
# C3：file_read 旧分片由 L1 清成信息摘要（含 path/offset，模型可随时重读）
# --------------------------------------------------------------------------- #
def test_file_read_old_shards_are_summarized_by_l1(registry: Registry):
    msgs: list[Message] = [Message.user("看代码")]
    _exchange(msgs, "f1", "file_read", {"path": "a.py"}, "第一版" * 100)
    _exchange(msgs, "f2", "file_read", {"path": "a.py", "offset": 100}, "第二页" * 100)
    _exchange(msgs, "f3", "file_read", {"path": "b.py"}, "另一个文件" * 100)
    _exchange(msgs, "f4", "file_read", {"path": "c.py"}, "最新读取" * 100)

    out = micro_compact(
        msgs, keep_recent_tools=2, result_policy_resolver=registry.result_policy
    )
    by_id = {m.tool_call_id: m.content for m in out if m.role == "tool"}

    # 旧分片不积累原文：清成一行信息摘要，保留 path/offset 便于重读
    assert by_id["f1"].startswith(TOOL_SUMMARY_PREFIX)
    assert "[file_read] read a.py" in by_id["f1"]
    assert by_id["f2"].startswith(TOOL_SUMMARY_PREFIX)
    assert "from line 100" in by_id["f2"]
    # 最近 2 条临时结果保留原文
    assert by_id["f3"] == "另一个文件" * 100
    assert by_id["f4"] == "最新读取" * 100


# --------------------------------------------------------------------------- #
# C4：wiki_read 同页面只留最近版本，不同页面互不干扰
# --------------------------------------------------------------------------- #
def test_wiki_read_keeps_latest_per_page(registry: Registry):
    msgs: list[Message] = [Message.user("查 Wiki")]
    _exchange(
        msgs, "w1", "wiki_read", {"kb_id": "kb", "page_id": "p1"}, "p1 旧版"
    )
    _temporary_exchange(msgs, 0)
    _exchange(msgs, "w2", "wiki_read", {"kb_id": "kb", "page_id": "p2"}, "p2 内容")
    _exchange(msgs, "w3", "wiki_read", {"kb_id": "kb", "page_id": "p1"}, "p1 新版")

    out = micro_compact(
        msgs, keep_recent_tools=6, result_policy_resolver=registry.result_policy
    )
    by_id = {m.tool_call_id: m.content for m in out if m.role == "tool"}

    assert by_id["w1"].startswith(RESOURCE_REPLACED_STUB.split("{", 1)[0])
    assert by_id["w2"] == "p2 内容"
    assert by_id["w3"] == "p1 新版"


# --------------------------------------------------------------------------- #
# C5：用户选择、团队消息、外援与子 Agent 结论不被日常压缩清除
# --------------------------------------------------------------------------- #
def test_important_results_survive_daily_compaction(registry: Registry):
    msgs: list[Message] = [Message.user("开始")]
    _exchange(
        msgs, "answer", "ask_followup_question",
        {"questions": [{"id": "q1"}]}, "用户选择：方案 A",
    )
    _exchange(
        msgs, "team", "team_read_messages", {}, "队友结论：采用画布布局",
    )
    _exchange(
        msgs, "ext", "delegate_to_external_agent",
        {"agent_id": "cli"}, "外援结论：依赖已安装",
    )
    _exchange(
        msgs, "sub", "delegate_task", {"title": "调研"}, "子 Agent 结论：用 Widget X",
    )
    for i in range(10):
        _temporary_exchange(msgs, i)

    out = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=registry.result_policy
    )
    by_id = {m.tool_call_id: m.content for m in out if m.role == "tool"}

    assert by_id["answer"] == "用户选择：方案 A"
    assert by_id["team"] == "队友结论：采用画布布局"
    assert by_id["ext"] == "外援结论：依赖已安装"
    assert by_id["sub"] == "子 Agent 结论：用 Widget X"


# --------------------------------------------------------------------------- #
# C6：完整压缩后 Skill 指令、用户选择、子 Agent 结论恢复，最近文件从磁盘重读
# --------------------------------------------------------------------------- #
async def test_full_compaction_recovers_protected_results(registry: Registry, tmp_path):
    app_file = tmp_path / "app.py"
    app_file.write_text("磁盘上的 app.py 最新内容" * 5, encoding="utf-8")

    msgs: list[Message] = [Message.user("创建一个灵感 App")]
    _exchange(msgs, "skill", "skill_view", {"name": "webapp-building"}, "建站指令" * 80)
    _exchange(msgs, "answer", "ask_followup_question", {"questions": []}, "用户选择：方案 A")
    _exchange(msgs, "sub", "delegate_task", {"title": "调研"}, "子 Agent 结论：用 Widget X")
    _exchange(msgs, "file", "file_read", {"path": str(app_file)}, "压缩时的旧快照" * 20)
    for i in range(8):
        if i % 2 == 0:
            msgs.append(Message.user(f"继续第 {i} 步"))  # 多轮会话提供 user 边界
        _temporary_exchange(msgs, i, content_size=80)

    compactor = ContextCompactor(
        _FakeProvider(),
        enabled=True,
        token_budget=200,  # 小预算强制触发完整压缩
        keep_recent=4,
        keep_recent_tools=3,
        post_compact_files=3,
        result_policy_resolver=registry.result_policy,
    )
    out = await compactor.maybe_compact(msgs, session_id="site-session")

    assert any(SUMMARY_MARKER in m.content for m in out if m.role == "system")
    attachments = [
        m for m in out if m.role == "system" and POST_COMPACT_RESULTS_MARKER in m.content
    ]
    payload = "\n".join(m.content for m in attachments)
    assert "建站指令" in payload  # INSTRUCTION 恢复
    assert "用户选择：方案 A" in payload  # 用户选择恢复
    assert "子 Agent 结论：用 Widget X" in payload  # 子 Agent 结论恢复

    # 最近文件按 path 从磁盘重读最新内容，而不是回放压缩时的旧快照
    from crew.agent.compact.post_compact import POST_COMPACT_FILES_MARKER

    file_attachments = [
        m for m in out if m.role == "system" and POST_COMPACT_FILES_MARKER in m.content
    ]
    assert len(file_attachments) == 1
    assert "磁盘上的 app.py 最新内容" in file_attachments[0].content
    assert "压缩时的旧快照" not in file_attachments[0].content


# --------------------------------------------------------------------------- #
# C6 补充：重要结果超过恢复预算（8 条）时按最近优先保留
# --------------------------------------------------------------------------- #
def test_important_attachment_budget_keeps_most_recent(registry: Registry):
    msgs: list[Message] = [Message.user("连续决策")]
    for i in range(10):
        _exchange(
            msgs, f"a{i}", "ask_followup_question",
            {"questions": [{"id": f"q{i}"}]}, f"用户选择 {i}",
        )

    attachments = build_post_compact_attachments(
        msgs, result_policy_resolver=registry.result_policy
    )
    important = [
        m for m in attachments if '"retention": "important"' in m.content
    ]
    payload = "\n".join(m.content for m in important)

    assert len(important) == 8
    assert "用户选择 0" not in payload  # 最旧的两条被预算丢弃
    assert "用户选择 1" not in payload
    for i in range(2, 10):
        assert f"用户选择 {i}" in payload


# --------------------------------------------------------------------------- #
# C7：受保护附件跨多次完整压缩存活
# --------------------------------------------------------------------------- #
async def test_protected_attachments_survive_repeated_compactions(registry: Registry):
    msgs: list[Message] = [Message.user("创建一个灵感 App")]
    _exchange(msgs, "skill", "skill_view", {"name": "webapp-building"}, "建站指令" * 80)
    _exchange(msgs, "answer", "ask_followup_question", {"questions": []}, "用户选择：方案 A")
    for i in range(8):
        if i % 2 == 0:
            msgs.append(Message.user(f"继续第 {i} 步"))
        _temporary_exchange(msgs, i, content_size=80)

    compactor = ContextCompactor(
        _FakeProvider(),
        enabled=True,
        token_budget=200,
        keep_recent=4,
        keep_recent_tools=3,
        l2_incremental=False,  # 每轮都走 L3 全量，模拟长会话反复触顶
        post_compact_files=3,
        result_policy_resolver=registry.result_policy,
    )
    first = await compactor.maybe_compact(msgs, session_id="site-session")

    # 第二轮：在压缩后的上下文上继续积累临时调用，再次触顶
    second_round = list(first)
    for i in range(8, 16):
        if i % 2 == 0:
            second_round.append(Message.user(f"继续第 {i} 步"))
        _temporary_exchange(second_round, i, content_size=80)
    second = await compactor.maybe_compact(second_round, session_id="site-session")

    attachments = [
        m for m in second if m.role == "system" and POST_COMPACT_RESULTS_MARKER in m.content
    ]
    payload = "\n".join(m.content for m in attachments)
    assert "建站指令" in payload
    assert "用户选择：方案 A" in payload


# --------------------------------------------------------------------------- #
# C8：guardrail 防循环——同参重复读取被拦，换参/内容变化不拦
# --------------------------------------------------------------------------- #
def test_guardrail_blocks_repeated_skill_read_only(registry: Registry):
    cfg = ToolCallGuardrailConfig(hard_stop_enabled=True, no_progress_block_after=2)
    guard = ToolCallGuardrailController(cfg)
    main_args = {"name": "webapp-building"}

    # 前两次同参同结果放行，第三次拦截：Agent 不会陷入重复读取循环
    guard.after_call("skill_view", main_args, "建站指令")
    assert not guard.before_call("skill_view", main_args).should_halt
    guard.after_call("skill_view", main_args, "建站指令")
    decision = guard.before_call("skill_view", main_args)
    assert decision.should_halt and decision.code == "idempotent_no_progress_block"

    # 换文件读取不受同名拦截影响（签名按工具名 + 参数）
    other_args = {"name": "webapp-building", "file_path": "references/runtime.md"}
    assert not guard.before_call("skill_view", other_args).should_halt

    # 内容发生变化的重读不算无进展，不拦截
    guard2 = ToolCallGuardrailController(cfg)
    guard2.after_call("skill_view", main_args, "旧版指令")
    guard2.after_call("skill_view", main_args, "新版指令")
    assert not guard2.before_call("skill_view", main_args).should_halt


# --------------------------------------------------------------------------- #
# C9：未声明生命周期的工具与已卸载插件的历史结果按重要结果保护
# --------------------------------------------------------------------------- #
def test_undeclared_and_uninstalled_tools_are_protected(registry: Registry):
    registry.register(
        name="plugin_chat_export",
        toolset="plugin",
        schema={"name": "plugin_chat_export", "parameters": {}},
        handler=lambda _args: "ok",
    )
    msgs: list[Message] = [Message.user("开始")]
    _exchange(msgs, "p1", "plugin_chat_export", {}, "插件导出的聊天记录")
    _exchange(msgs, "gone", "uninstalled_plugin_tool", {}, "已卸载插件的历史结果")
    for i in range(8):
        _temporary_exchange(msgs, i)

    out = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=registry.result_policy
    )
    by_id = {m.tool_call_id: m.content for m in out if m.role == "tool"}

    # 插件未声明生命周期 → 默认 IMPORTANT，原样保留
    assert registry.result_policy("plugin_chat_export", {}).retention is (
        ToolResultRetention.IMPORTANT
    )
    assert by_id["p1"] == "插件导出的聊天记录"
    # 历史里残留已卸载插件的工具结果 → resolver 找不到工具，同样安全保留
    assert by_id["gone"] == "已卸载插件的历史结果"


def test_plugin_declared_temporary_result_is_cleaned(registry: Registry):
    registry.register(
        name="plugin_progress_log",
        toolset="plugin",
        schema={"name": "plugin_progress_log", "parameters": {}},
        handler=lambda _args: "ok",
        result_retention="temporary",
    )
    msgs: list[Message] = [Message.user("开始")]
    for i in range(6):
        _exchange(msgs, f"log_{i}", "plugin_progress_log", {}, f"进度{i}" * 50)

    out = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=registry.result_policy
    )
    logs = _tool_contents(out, "plugin_progress_log")
    assert sum(c.startswith(TOOL_SUMMARY_PREFIX) for c in logs) == 3
    assert logs[3:] == [f"进度{i}" * 50 for i in range(3, 6)]


# --------------------------------------------------------------------------- #
# C7 补充：附件 meta 可被下一轮解析（build_post_compact_attachments 直接复验）
# --------------------------------------------------------------------------- #
def test_attachment_meta_roundtrip_with_real_registry(registry: Registry, tmp_path):
    app_file = tmp_path / "app.py"
    app_file.write_text("app.py 磁盘内容", encoding="utf-8")

    msgs: list[Message] = [Message.user("建站")]
    _exchange(msgs, "skill", "skill_view", {"name": "webapp-building"}, "建站指令")
    _exchange(msgs, "file", "file_read", {"path": str(app_file)}, "app.py 旧快照")

    first = build_post_compact_attachments(
        msgs, result_policy_resolver=registry.result_policy
    )
    # file_read 已是 TEMPORARY，不走 RESOURCE 附件：只剩 Skill 指令一条
    assert len(first) == 1

    # 附件再次进入压缩流程时按 meta 存活，不需要原 tool 消息配对
    second = build_post_compact_attachments(
        [*first, Message.user("继续")], result_policy_resolver=registry.result_policy
    )
    assert [m.content for m in second] == [m.content for m in first]

    # 文件恢复走磁盘重读通道，且跨多轮压缩存活
    from crew.agent.compact.post_compact import build_post_compact_file_attachments

    first_files = build_post_compact_file_attachments(msgs)
    assert len(first_files) == 1
    assert "app.py 磁盘内容" in first_files[0].content
    second_files = build_post_compact_file_attachments([Message.user("继续"), *first_files])
    assert len(second_files) == 1
    assert "app.py 磁盘内容" in second_files[0].content
