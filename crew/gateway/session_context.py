"""会话上下文：消息来源跟踪与动态系统提示注入。

用于 gateway/session.py 的 SessionSource + SessionContext + build_session_context_prompt，
但只保留核心结构，不迁移 Crew 的重型 SessionStore（已有 crew.state.SessionStore）、
PII 脱敏、WhatsApp 特定逻辑、复杂会话重置策略。

SessionSource 描述"消息从哪来"（平台、渠道、用户），用于：
1. 路由响应回正确位置
2. 注入上下文到系统提示（告诉 Agent 当前在哪个平台/渠道）
3. 追踪来源用于 cron 投递

SessionContext 包装 SessionSource + 全局配置，生成动态系统提示段落。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SessionSource:
    """消息来源描述。

    用于路由响应、注入上下文、追踪来源。
    """

    platform: str  # "web" / "telegram" / "discord" / "local" 等
    chat_id: str  # 聊天 ID（群组/私聊标识）
    chat_name: str | None = None  # 聊天显示名
    chat_type: str = "dm"  # "dm" / "group" / "channel" / "thread"
    user_id: str | None = None  # 发送者 ID
    user_name: str | None = None  # 发送者名称
    thread_id: str | None = None  # 论坛主题/线程 ID（Telegram forum、Discord thread）
    guild_id: str | None = None  # Discord guild / Slack workspace 范围
    message_id: str | None = None  # 触发消息 ID（用于回复/引用）

    @property
    def description(self) -> str:
        """人类可读的来源描述。"""
        if self.platform == "local":
            return "CLI 终端"

        parts = []
        if self.chat_type == "dm":
            parts.append(f"与 {self.user_name or self.user_id or 'user'} 的私聊")
        elif self.chat_type == "group":
            parts.append(f"群组: {self.chat_name or self.chat_id}")
        elif self.chat_type == "channel":
            parts.append(f"频道: {self.chat_name or self.chat_id}")
        else:
            parts.append(self.chat_name or self.chat_id)

        if self.thread_id:
            parts.append(f"主题: {self.thread_id}")

        return ", ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "chat_id": self.chat_id,
            "chat_name": self.chat_name,
            "chat_type": self.chat_type,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "thread_id": self.thread_id,
            "guild_id": self.guild_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionSource:
        return cls(
            platform=str(data["platform"]),
            chat_id=str(data["chat_id"]),
            chat_name=data.get("chat_name"),
            chat_type=data.get("chat_type", "dm"),
            user_id=data.get("user_id"),
            user_name=data.get("user_name"),
            thread_id=data.get("thread_id"),
            guild_id=data.get("guild_id"),
            message_id=data.get("message_id"),
        )


@dataclass
class SessionContext:
    """会话完整上下文，用于动态系统提示注入。

    Agent 收到这些信息以理解：
    - 消息来自哪里
    - 可用的平台有哪些
    - 定时任务输出可投递到哪里
    """

    source: SessionSource
    connected_platforms: list[str]  # ["local", "web", "telegram"]
    shared_multi_user: bool = False  # 多用户共享会话（群组/线程）
    session_id: str = ""  # 会话 ID
    workspace_id: str = "default"  # 工作空间 ID

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source.to_dict(),
            "connected_platforms": self.connected_platforms,
            "shared_multi_user": self.shared_multi_user,
            "session_id": self.session_id,
            "workspace_id": self.workspace_id,
        }


def build_session_context_prompt(
    context: SessionContext,
    *,
    workspace_path: str | None = None,
) -> str:
    """构建动态系统提示段落，告诉 Agent 当前会话上下文。

    注入到系统提示，让 Agent 知道：
    - 消息来自哪个平台/渠道
    - 哪些平台已连接
    - 定时任务输出可投递到哪里
    - 当前项目空间路径（文件操作根目录）
    """
    lines = [
        "## 当前会话上下文",
        "",
    ]

    # 来源信息
    platform_name = context.source.platform.title()
    if context.source.platform == "local":
        lines.append(f"**来源:** {platform_name}（运行此 Agent 的本机）")
    else:
        desc = context.source.description
        lines.append(f"**来源:** {platform_name}（{desc}）")

    # 用户身份
    # 多用户共享会话（群组/线程）时，不固定单个用户名（会破坏 prompt cache），
    # 而是注明这是多用户会话，每条消息前缀会带发送者名称。
    if context.shared_multi_user:
        session_label = "多用户线程" if context.source.thread_id else "多用户会话"
        lines.append(
            f"**会话类型:** {session_label} — 消息前缀有发送者名称，可能有多位用户参与。"
        )
    elif context.source.user_name:
        lines.append(f"**用户:** {context.source.user_name}")
    elif context.source.user_id:
        lines.append(f"**用户 ID:** {context.source.user_id}")

    # 项目空间路径
    if workspace_path:
        lines.append(f"**项目空间:** {workspace_path}")

    # 已连接平台
    platforms_list = []
    for p in context.connected_platforms:
        if p == "local":
            platforms_list.append("local（本机文件）")
        else:
            platforms_list.append(f"{p}: 已连接 ✓")

    lines.append(f"**已连接平台:** {', '.join(platforms_list)}")

    # 定时任务投递选项
    lines.append("")
    lines.append("**定时任务投递选项:**")

    # origin 投递
    if context.source.platform == "local":
        lines.append("- `\"origin\"` → 本地输出（保存到文件）")
    else:
        origin_label = context.source.chat_name or context.source.chat_id
        lines.append(f"- `\"origin\"` → 回到此聊天（{origin_label}）")

    # local 总是可用
    lines.append("- `\"local\"` → 仅保存到本地文件")

    # 明确目标投递
    lines.append("")
    lines.append("*若要明确指定目标，使用 `\"platform:chat_id\"` 格式（如用户提供了具体聊天 ID）。*")

    return "\n".join(lines)


def build_session_key(
    source: SessionSource,
    *,
    per_user_in_group: bool = True,
    per_user_in_thread: bool = False,
) -> str:
    """从 SessionSource 构建确定性的会话 key。

    这是会话 key 构建的唯一来源。

    DM 规则：
      - 包含 chat_id（若存在），隔离每个私聊会话。
      - thread_id 进一步区分 DM 内的线程。
      - 无 chat_id 时，user_id 作为最佳备用（避免无标识时所有 DM 合并）。

    群组/频道规则：
      - chat_id 标识父群组/频道。
      - user_id 在启用 per_user_in_group 时隔离参与者。
      - thread_id 区分该父聊天内的线程。当 per_user_in_thread=False（默认）时，
        线程*共享*所有参与者 — 不追加 user_id，每个用户在同一线程里共享单一会话。
        这是线程对话的预期 UX（Telegram 论坛主题、Discord 线程、Slack 线程）。
      - 无参与者标识或禁用隔离时，回退到每聊天一个共享会话。
      - 无标识时，回退到每平台/chat_type 一个会话。
    """
    platform = source.platform
    if source.chat_type == "dm":
        dm_chat_id = source.chat_id

        if dm_chat_id:
            if source.thread_id:
                return f"agent:main:{platform}:dm:{dm_chat_id}:{source.thread_id}"
            return f"agent:main:{platform}:dm:{dm_chat_id}"

        # 无 chat_id — 回退到发送者标识，避免所有 DM 合并到一个会话
        dm_participant_id = source.user_id
        if dm_participant_id:
            if source.thread_id:
                return f"agent:main:{platform}:dm:{dm_participant_id}:{source.thread_id}"
            return f"agent:main:{platform}:dm:{dm_participant_id}"
        if source.thread_id:
            return f"agent:main:{platform}:dm:{source.thread_id}"
        return f"agent:main:{platform}:dm"

    participant_id = source.user_id
    key_parts = ["agent:main", platform, source.chat_type]

    if source.chat_id:
        key_parts.append(source.chat_id)
    if source.thread_id:
        key_parts.append(source.thread_id)

    # 线程中，默认共享会话（所有参与者看到同一对话）。
    # 只有显式启用 per_user_in_thread 时才按用户隔离，或无线程（常规群组）时。
    isolate_user = per_user_in_group
    if source.thread_id and not per_user_in_thread:
        isolate_user = False

    if isolate_user and participant_id:
        key_parts.append(str(participant_id))

    return ":".join(key_parts)


def session_context_from_envelope(
    envelope: Any,
    connected_platforms: list[str],
) -> SessionContext:
    """从 Envelope 构建 SessionContext，供 runtime 注入 system prompt。"""
    platform = str(getattr(envelope, "channel", None) or "web")
    params = getattr(envelope, "params", {}) or {}
    chat_id = str(params.get("platform_chat_id") or envelope.session_id)
    chat_type = str(params.get("platform_chat_type") or "dm")
    source = SessionSource(
        platform=platform,
        chat_id=chat_id,
        chat_name=params.get("platform_chat_name"),
        chat_type=chat_type,
        user_id=params.get("platform_uid") or getattr(envelope, "user_id", None),
        user_name=params.get("platform_user_name"),
        thread_id=params.get("platform_thread_id"),
        message_id=params.get("platform_message_id"),
    )
    shared = chat_type in {"group", "channel"} or bool(params.get("shared_multi_user"))
    return SessionContext(
        source=source,
        connected_platforms=connected_platforms,
        shared_multi_user=shared,
        session_id=envelope.session_id,
        workspace_id=getattr(envelope, "workspace_id", "default"),
    )
