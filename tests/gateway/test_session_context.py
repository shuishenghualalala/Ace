"""测试会话上下文构建。"""


from crew.gateway.session_context import (
    SessionContext,
    SessionSource,
    build_session_context_prompt,
    build_session_key,
)


def test_session_source_description():
    """测试 SessionSource 人类可读描述。"""
    # DM
    source = SessionSource(platform="telegram", chat_id="123", chat_type="dm", user_name="Alice")
    assert "Alice" in source.description
    assert "私聊" in source.description

    # 群组
    source = SessionSource(
        platform="discord", chat_id="456", chat_name="General", chat_type="group"
    )
    assert "General" in source.description
    assert "群组" in source.description

    # 本地
    source = SessionSource(platform="local", chat_id="cli", chat_type="dm")
    assert "CLI 终端" in source.description


def test_session_source_to_dict_and_from_dict():
    """测试 SessionSource 序列化与反序列化。"""
    original = SessionSource(
        platform="web",
        chat_id="web-session-1",
        chat_name="Web Chat",
        chat_type="dm",
        user_id="user-123",
        user_name="Bob",
        thread_id="thread-1",
        guild_id="guild-1",
        message_id="msg-1",
    )

    data = original.to_dict()
    restored = SessionSource.from_dict(data)

    assert restored.platform == original.platform
    assert restored.chat_id == original.chat_id
    assert restored.chat_name == original.chat_name
    assert restored.user_name == original.user_name
    assert restored.thread_id == original.thread_id
    assert restored.guild_id == original.guild_id


def test_build_session_key_dm():
    """测试 DM 会话 key 构建。"""
    source = SessionSource(platform="telegram", chat_id="12345", chat_type="dm")
    key = build_session_key(source)
    assert key == "agent:main:telegram:dm:12345"

    # DM 带 thread
    source = SessionSource(
        platform="telegram", chat_id="12345", chat_type="dm", thread_id="thread-1"
    )
    key = build_session_key(source)
    assert key == "agent:main:telegram:dm:12345:thread-1"

    # DM 无 chat_id 但有 user_id
    source = SessionSource(platform="web", chat_id="", chat_type="dm", user_id="user-123")
    key = build_session_key(source)
    assert key == "agent:main:web:dm:user-123"


def test_build_session_key_group():
    """测试群组会话 key 构建。"""
    # 群组，按用户隔离（默认）
    source = SessionSource(
        platform="discord", chat_id="guild-123", chat_type="group", user_id="user-456"
    )
    key = build_session_key(source, per_user_in_group=True)
    assert key == "agent:main:discord:group:guild-123:user-456"

    # 群组，不按用户隔离（共享会话）
    key = build_session_key(source, per_user_in_group=False)
    assert key == "agent:main:discord:group:guild-123"


def test_build_session_key_thread():
    """测试线程会话 key 构建。"""
    # 线程，默认共享（不按用户隔离）
    source = SessionSource(
        platform="telegram",
        chat_id="forum-123",
        chat_type="group",
        thread_id="topic-456",
        user_id="user-789",
    )
    key = build_session_key(source, per_user_in_group=True, per_user_in_thread=False)
    # 线程共享：不追加 user_id
    assert key == "agent:main:telegram:group:forum-123:topic-456"

    # 线程，按用户隔离
    key = build_session_key(source, per_user_in_group=True, per_user_in_thread=True)
    assert key == "agent:main:telegram:group:forum-123:topic-456:user-789"


def test_build_session_context_prompt():
    """测试动态系统提示构建。"""
    source = SessionSource(
        platform="web",
        chat_id="web-1",
        chat_type="dm",
        user_name="Charlie",
    )
    context = SessionContext(
        source=source,
        connected_platforms=["local", "web"],
        shared_multi_user=False,
        session_id="session-123",
    )

    prompt = build_session_context_prompt(context)

    assert "当前会话上下文" in prompt
    assert "Web" in prompt
    assert "Charlie" in prompt
    assert "已连接平台" in prompt
    assert "定时任务投递选项" in prompt


def test_build_session_context_prompt_multi_user():
    """测试多用户会话的系统提示。"""
    source = SessionSource(
        platform="discord",
        chat_id="guild-123",
        chat_name="General",
        chat_type="group",
    )
    context = SessionContext(
        source=source,
        connected_platforms=["local", "discord"],
        shared_multi_user=True,  # 多用户共享
        session_id="session-456",
    )

    prompt = build_session_context_prompt(context)

    assert "多用户会话" in prompt
    assert "消息前缀有发送者名称" in prompt


def test_session_context_to_dict():
    """测试 SessionContext 序列化。"""
    source = SessionSource(platform="web", chat_id="web-1", chat_type="dm")
    context = SessionContext(
        source=source,
        connected_platforms=["local", "web"],
        session_id="session-123",
        workspace_id="workspace-1",
    )

    data = context.to_dict()

    assert data["session_id"] == "session-123"
    assert data["workspace_id"] == "workspace-1"
    assert "web" in data["connected_platforms"]
    assert data["source"]["platform"] == "web"
