from crew.agent.runtime import SingleAgent
from crew.core.envelope import Envelope
from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.plugins.manager import PluginManager
from crew.state.home import task_workspace_path
from crew.tools.registry import Registry, register_builtin_tools
from crew.tools.policy import ToolDisclosureMode
from crew.wiki.manager import WikiSessionManager


def _agent(provider, **kw):
    reg = Registry()
    register_builtin_tools(reg)
    return SingleAgent(
        provider=provider,
        registry=reg,
        session_store=kw.pop("session_store", InMemorySessionStore()),
        memory=NullMemory(),
        plugins=PluginManager(),
        max_iterations=5,
        wiki_manager=kw.pop("wiki_manager", None),
        **kw,
    )


def test_effective_tool_filter_uses_preassembled_agent_scope():
    agent = _agent(FakeProvider())
    tools = agent._effective_tool_filter("s1")
    assert "wiki_init" not in tools
    assert "enter_plan_mode" not in tools


def test_effective_tool_filter_respects_wiki_preset_scope():
    wiki = WikiSessionManager()
    agent = _agent(
        FakeProvider(),
        wiki_manager=wiki,
        tool_filter=["wiki_orient", "wiki_search"],
        tool_disclosure_mode=ToolDisclosureMode.DIRECT,
    )
    tools = agent._effective_tool_filter("s1")
    assert tools == ["wiki_orient", "wiki_search"]


def test_wiki_manager_does_not_expand_tool_scope_dynamically():
    wiki = WikiSessionManager()
    agent = _agent(
        FakeProvider(),
        wiki_manager=wiki,
        tool_filter=["wiki_search", "wiki_apply_ingest"],
        tool_disclosure_mode=ToolDisclosureMode.DIRECT,
    )
    assert agent._effective_tool_filter("s1") == ["wiki_search", "wiki_apply_ingest"]


def test_resolve_agent_workdir_uses_workspace_for_wiki_agent():
    wiki = WikiSessionManager()
    wiki.set_kb_id("s1", "work_kb", owner_account_id="owner1")
    agent = _agent(FakeProvider(), wiki_manager=wiki)
    env = Envelope.of("hi", session_id="s1", user_id="owner1", workspace_id="default")
    env.params["wiki_kb_id"] = "work_kb"
    cwd = agent._resolve_agent_workdir(env)
    assert cwd == str(task_workspace_path("default", owner_account_id="owner1"))


def test_resolve_agent_workdir_falls_back_to_workspace_path():
    agent = _agent(FakeProvider())
    env = Envelope.of("hi", session_id="s1", user_id="owner1", workspace_id="default")
    cwd = agent._resolve_agent_workdir(env)
    assert cwd == str(task_workspace_path("default", owner_account_id="owner1"))


def test_resolve_agent_workdir_explicit_cwd_overrides_wiki_agent(tmp_path):
    wiki = WikiSessionManager()
    agent = _agent(FakeProvider(), wiki_manager=wiki)
    explicit = tmp_path / "explicit_dir"
    explicit.mkdir()
    env = Envelope.of("hi", session_id="s1", user_id="owner1", workspace_id="default")
    env.params["cwd"] = str(explicit)
    env.params["wiki_kb_id"] = "work_kb"
    cwd = agent._resolve_agent_workdir(env)
    assert cwd == str(explicit.resolve())


def test_wiki_prompts_capture_message_attachments_first():
    """消息里已带的附件必须先 capture 入库，禁止引导用户重新上传（防误导措辞回潮）。"""
    from crew.wiki.prompts import (
        WIKI_AGENT_CONTEXT_REMINDER,
        WIKI_AGENT_SYSTEM_PROMPT,
        WIKI_LIST_SOURCES_PROMPT,
    )

    # 空库时先检查本轮附件并 capture，仅本轮无附件才请用户上传
    assert "wiki_capture_attachment" in WIKI_LIST_SOURCES_PROMPT
    assert "仅当本轮消息确实没有附件时，才请用户通过 Wiki Composer 附件区上传" in WIKI_LIST_SOURCES_PROMPT
    # 每轮提醒 / 预设正文均明确：消息附件直接 capture，不让用户重复上传
    assert "不要让用户重新上传" in WIKI_AGENT_CONTEXT_REMINDER
    assert "绝不要求用户重新上传消息里已有的附件" in WIKI_AGENT_SYSTEM_PROMPT
