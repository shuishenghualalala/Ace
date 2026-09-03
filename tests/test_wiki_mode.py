from crew.agent.executor.external import ExternalExecutor
from crew.agent.runtime import SingleAgent
from crew.core.envelope import Envelope
from crew.core.mocks import FakeProvider, InMemorySessionStore, NullMemory
from crew.features import (
    ContextContributor,
    ContextContributorRegistry,
    ContextPhase,
    FeatureGeneration,
    FeatureScope,
)
from crew.plugins.manager import PluginManager
from crew.state.home import external_session_workspace_path, task_workspace_path
from crew.tools.policy import ToolDisclosureMode
from crew.tools.registry import Registry, register_builtin_tools
from crew.wiki import (
    FileSystemWikiStore,
    WikiSessionManager,
    build_wiki_agent_context_contributor,
)


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
        **kw,
    )


def test_effective_tool_filter_uses_preassembled_agent_scope():
    agent = _agent(FakeProvider())
    tools = agent._effective_tool_filter("s1")
    assert "wiki_init" not in tools
    assert "enter_plan_mode" not in tools


def test_effective_tool_filter_respects_wiki_preset_scope():
    agent = _agent(
        FakeProvider(),
        tool_filter=["wiki_orient", "wiki_search"],
        tool_disclosure_mode=ToolDisclosureMode.DIRECT,
    )
    tools = agent._effective_tool_filter("s1")
    assert tools == ["wiki_orient", "wiki_search"]


def test_wiki_context_tag_does_not_expand_tool_scope_dynamically():
    agent = _agent(
        FakeProvider(),
        context_tags=("wiki",),
        tool_filter=["wiki_search", "wiki_apply_ingest"],
        tool_disclosure_mode=ToolDisclosureMode.DIRECT,
    )
    assert agent._effective_tool_filter("s1") == ["wiki_search", "wiki_apply_ingest"]


def test_resolve_agent_workdir_uses_workspace_for_wiki_agent():
    agent = _agent(FakeProvider(), context_tags=("wiki",))
    env = Envelope.of("hi", session_id="s1", user_id="owner1", workspace_id="default")
    env.params["wiki_kb_id"] = "work_kb"
    cwd = agent._resolve_agent_workdir(env)
    assert cwd == str(task_workspace_path("default", owner_account_id="owner1"))


def test_resolve_agent_workdir_falls_back_to_workspace_path():
    agent = _agent(FakeProvider())
    env = Envelope.of("hi", session_id="s1", user_id="owner1", workspace_id="default")
    cwd = agent._resolve_agent_workdir(env)
    assert cwd == str(task_workspace_path("default", owner_account_id="owner1"))


def test_external_agent_uses_isolated_session_workspace_by_default():
    agent = _agent(
        FakeProvider(),
        executor=ExternalExecutor({"external_agent_id": "external-1"}),
    )
    env = Envelope.of("hi", session_id="side::turn::req", user_id="owner1", workspace_id="default")
    cwd = agent._resolve_agent_workdir(env, task_session_id="stable-session")
    assert cwd == str(external_session_workspace_path(
        "default",
        "stable-session",
        "external-1",
        owner_account_id="owner1",
    ))


def test_external_agent_keeps_existing_runtime_binding_workspace(tmp_path):
    class LegacyBindingStore:
        def latest_runtime_session_binding_for_agent(self, **_kwargs):
            return {"cwd": str(tmp_path)}

    agent = _agent(
        FakeProvider(),
        executor=ExternalExecutor({
            "external_agent_id": "external-1",
            "external_store": LegacyBindingStore(),
        }),
    )
    env = Envelope.of("hi", session_id="side::turn::req", user_id="owner1", workspace_id="default")

    assert agent._resolve_agent_workdir(env, task_session_id="stable-session") == str(tmp_path.resolve())


def test_resolve_agent_workdir_explicit_cwd_overrides_wiki_agent(tmp_path):
    agent = _agent(FakeProvider(), context_tags=("wiki",))
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


async def test_wiki_context_contributor_persists_hidden_attachment(tmp_path):
    session_store = InMemorySessionStore()
    manager = WikiSessionManager(
        store=FileSystemWikiStore(storage_root=tmp_path / "wiki")
    )
    context_registry = ContextContributorRegistry()
    scope = FeatureScope(FeatureGeneration("wiki-context-test", 1))
    base_contributor = build_wiki_agent_context_contributor(manager, session_store)
    contribution_calls: list[str] = []

    async def tracked_contributor(envelope: Envelope):
        contribution_calls.append(envelope.session_id)
        return await base_contributor(envelope)

    context_registry.register(
        scope,
        ContextContributor(
            "wiki.agent.context",
            tracked_contributor,
            phase=ContextPhase.PROMPT,
            persistent=True,
        ),
    )
    scope.activate()
    agent = _agent(
        FakeProvider(),
        session_store=session_store,
        context_contributors=context_registry,
        context_tags=("wiki",),
        tool_filter=["wiki_orient", "wiki_search"],
        tool_disclosure_mode=ToolDisclosureMode.DIRECT,
    )
    envelope = Envelope.of(
        "查看知识库",
        session_id="wiki-session",
        user_id="owner1",
        params={"wiki_kb_id": "work_kb"},
    )

    _chunks = [chunk async for chunk in agent.run(envelope)]
    history = session_store.load("wiki-session", owner_account_id="owner1")
    attachments = [
        message
        for message in history
        if message.attachment_type == "wiki_agent_context"
    ]

    assert len(attachments) == 1
    assert "work_kb" in attachments[0].content
    assert manager.get_kb_id("wiki-session", owner_account_id="owner1") == "work_kb"
    assert contribution_calls == ["wiki-session"]

    preview = await agent.preview_context(
        "wiki-session",
        owner_account_id="owner1",
    )
    persisted_after_preview = session_store.load(
        "wiki-session",
        owner_account_id="owner1",
    )
    assert preview is not None
    assert contribution_calls == ["wiki-session", "wiki-session"]
    assert sum(
        message.attachment_type == "wiki_agent_context"
        for message in persisted_after_preview
    ) == 1
    assert manager.get_kb_id("wiki-session", owner_account_id="owner1") == "work_kb"
    await scope.dispose()
