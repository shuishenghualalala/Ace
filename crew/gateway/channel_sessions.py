"""渠道会话：session_id 识别、绑定者 owner 映射、列表与桌面续聊投递。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from crew.gateway.session_context import SessionContext, SessionSource, session_context_from_envelope
from crew.state.logging import get_logger

log = get_logger("gateway.channel_sessions")

_CHANNEL_PREFIX = "agent:main:"
_RESET_COMMANDS = {"/new", "/reset"}


def is_channel_session_id(session_id: str) -> bool:
    """是否为平台渠道会话 key（build_session_key 生成）。"""
    return str(session_id or "").startswith(_CHANNEL_PREFIX)


def channel_platform_from_session_id(session_id: str) -> str | None:
    """从 session_id 解析平台名，如 agent:main:feishu:dm:uid → feishu。"""
    sid = str(session_id or "")
    if not sid.startswith(_CHANNEL_PREFIX):
        return None
    parts = sid.split(":")
    return parts[2] if len(parts) >= 3 else None


def _channel_session_prefix(platform: str) -> str:
    plat = str(platform or "").strip().lower()
    return f"{_CHANNEL_PREFIX}{plat}:"


def _new_channel_session_id(session_key: str) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return f"{session_key}:session:{stamp}_{uuid.uuid4().hex[:8]}"


def _public_agent_config(config: dict[str, Any] | None) -> dict[str, Any]:
    value = dict(config or {})
    value.pop("_created_at", None)
    value.pop("_updated_at", None)
    return value


def resolve_channel_session(
    store: Any,
    session_key: str,
    owner_account_id: str,
) -> str:
    """Resolve the active conversation for a stable platform/chat key.

    Legacy channel sessions use the stable key itself as the session ID. Keeping
    that as the initial target preserves existing history without migration.
    """
    getter = getattr(store, "get_channel_session", None)
    setter = getattr(store, "set_channel_session", None)
    active = getter(session_key, owner_account_id=owner_account_id) if callable(getter) else None
    if active:
        return str(active)
    if callable(setter):
        setter(session_key, session_key, owner_account_id=owner_account_id)
    return session_key


def rotate_channel_session(
    store: Any,
    session_key: str,
    owner_account_id: str,
    *,
    workspace_id: str = "default",
) -> str:
    """Create and activate a fresh conversation while retaining channel routing."""
    current = resolve_channel_session(store, session_key, owner_account_id)
    new_session_id = _new_channel_session_id(session_key)
    inherited = _public_agent_config(
        store.get_agent_config(current, owner_account_id=owner_account_id)
    )
    inherited["channel_session_key"] = session_key
    store.ensure_session(
        new_session_id,
        workspace_id=store.get_workspace_id(current, owner_account_id=owner_account_id)
        or workspace_id
        or "default",
        owner_account_id=owner_account_id,
    )
    if inherited:
        store.set_agent_config(
            new_session_id,
            inherited,
            owner_account_id=owner_account_id,
        )
    store.set_channel_session(
        session_key,
        new_session_id,
        owner_account_id=owner_account_id,
    )
    return new_session_id


def bind_channel_platform_for_owner(crew: Any, platform: str, owner_account_id: str) -> dict[str, Any] | None:
    """把某平台绑定到当前桌面账号；绑定之后的新渠道消息归属该账号。"""
    bindings = getattr(crew, "channel_bindings", None)
    if bindings is None:
        return None
    plat = str(platform or "").strip().lower()
    owner = str(owner_account_id or "").strip()
    if not plat or not owner:
        return None
    return bindings.bind_on_connect(plat, owner)


def ensure_channel_source_config(store: Any, envelope: Any) -> None:
    """首次渠道消息时把 SessionSource 写入 session_agent_config，供桌面续聊回传。"""
    getter = getattr(store, "get_agent_config", None)
    setter = getattr(store, "set_agent_config", None)
    if not callable(getter) or not callable(setter):
        return
    owner = str(getattr(envelope, "user_id", "") or "")
    sid = str(getattr(envelope, "session_id", "") or "")
    if not owner or not sid:
        return
    existing = getter(sid, owner_account_id=owner) or {}
    session_key = str((getattr(envelope, "params", {}) or {}).get("channel_session_key") or "")
    if existing.get("channel_source") and (
        not session_key or existing.get("channel_session_key") == session_key
    ):
        return
    params = getattr(envelope, "params", {}) or {}
    ctx_raw = params.get("session_context")
    source: SessionSource | None = None
    if isinstance(ctx_raw, SessionContext):
        source = ctx_raw.source
    elif isinstance(ctx_raw, dict):
        src = ctx_raw.get("source")
        if isinstance(src, dict):
            source = SessionSource.from_dict(src)
    if source is None:
        source = SessionSource(
            platform=str(getattr(envelope, "channel", "") or "web"),
            chat_id=str(params.get("platform_chat_id") or sid),
            chat_type=str(params.get("platform_chat_type") or "dm"),
            user_id=params.get("platform_uid"),
            user_name=params.get("platform_user_name"),
            thread_id=params.get("platform_thread_id"),
            message_id=params.get("platform_message_id"),
        )
    setter(
        sid,
        {
            **existing,
            "channel": str(getattr(envelope, "channel", "") or ""),
            "channel_source": source.to_dict(),
            "channel_session_key": session_key or sid,
        },
        owner_account_id=owner,
    )


def prepare_inbound_channel_envelope(crew: Any, envelope: Any) -> None:
    """入站渠道消息：若有绑定者，将会话 owner 切为 gateway 账号并写入 channel_source。"""
    platform = str(getattr(envelope, "channel", "") or "").strip().lower()
    if platform in ("", "web", "local"):
        return
    bindings = getattr(crew, "channel_bindings", None)
    if bindings is None:
        return
    bindings_store = bindings
    binder = bindings_store.get_binding(platform)
    if not binder:
        return
    params = getattr(envelope, "params", None) or {}
    platform_uid = str(params.get("platform_uid") or getattr(envelope, "user_id", "") or "")
    if platform_uid:
        params.setdefault("platform_uid", platform_uid)
    envelope.params = params
    envelope.user_id = binder
    session_key = str(envelope.session_id or "")
    envelope.params["channel_session_key"] = session_key
    active_session_id = resolve_channel_session(crew.session_store, session_key, binder)
    command = str(getattr(envelope, "query", "") or "").strip().lower()
    if command in _RESET_COMMANDS:
        active_session_id = rotate_channel_session(
            crew.session_store,
            session_key,
            binder,
            workspace_id=str(getattr(envelope, "workspace_id", "default") or "default"),
        )
        envelope.params["channel_session_command"] = command[1:]
    envelope.session_id = active_session_id
    envelope.params["session_context"] = session_context_from_envelope(envelope, [platform, "local"])
    ensure_channel_source_config(crew.session_store, envelope)


def register_channel_session_tools(registry: Any, store: Any) -> None:
    """Register the model-callable equivalent of channel /new and /reset."""
    from crew.core.runctx import (
        current_owner_account_id,
        current_session_id,
        current_session_source,
        current_workspace_id,
    )
    from crew.tools.registry import tool_error, tool_result

    async def handle_new_conversation(args: dict[str, Any]) -> str:
        source = current_session_source.get() or {}
        platform = str(source.get("platform") or "").strip().lower()
        if platform in {"", "web", "local"}:
            return tool_error("当前不是微信或飞书等渠道会话，不能切换渠道对话")
        owner = current_owner_account_id.get()
        current_sid = current_session_id.get()
        key_getter = getattr(store, "get_channel_session_key", None)
        session_key = (
            key_getter(current_sid, owner_account_id=owner)
            if callable(key_getter)
            else None
        )
        if not session_key:
            config = store.get_agent_config(current_sid, owner_account_id=owner) or {}
            session_key = str(config.get("channel_session_key") or "")
        if not session_key:
            return tool_error("当前渠道会话缺少路由信息，无法新建对话")
        new_session_id = rotate_channel_session(
            store,
            session_key,
            owner,
            workspace_id=current_workspace_id.get(),
        )
        return tool_result({
            "success": True,
            "action": str(args.get("action") or "new"),
            "new_session_id": new_session_id,
            "note": "新对话已创建。当前回复结束后，用户的下一条消息会进入新对话。",
        })

    registry.register(
        name="new_conversation",
        toolset="interaction",
        schema={
            "name": "new_conversation",
            "description": (
                "在当前微信、飞书等渠道中开启全新对话。"
                "当用户说‘新建对话’、‘重新开始’、‘清空上下文’或要求执行 /new、/reset 时调用；"
                "不要只回复命令文本。新对话不会继承当前消息历史。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["new", "reset"],
                        "description": "对应 /new 或 /reset；两者都会开启无历史的新对话",
                    }
                },
                "required": ["action"],
            },
        },
        handler=handle_new_conversation,
        is_async=True,
        display_name="新建渠道对话",
        ui_label_template="新建渠道对话",
        always_load=True,
        search_hint="new reset conversation session 微信 飞书 新建对话 清空上下文",
    )


def build_outbound_channel_envelope(crew: Any, envelope: Any, *, owner: str) -> bool:
    """桌面 WS 发往渠道会话：补全 channel / session_context。返回是否渠道会话。"""
    sid = str(getattr(envelope, "session_id", "") or "")
    platform = channel_platform_from_session_id(sid)
    if not platform:
        return False
    getter = getattr(crew.session_store, "get_agent_config", None)
    if not callable(getter):
        return False
    cfg = getter(sid, owner_account_id=owner) or {}
    source_raw = cfg.get("channel_source")
    if not isinstance(source_raw, dict):
        return False
    source = SessionSource.from_dict(source_raw)
    envelope.channel = platform
    envelope.user_id = owner
    envelope.params["session_context"] = SessionContext(
        source=source,
        connected_platforms=[platform, "local"],
        shared_multi_user=source.chat_type in {"group", "channel"},
        session_id=sid,
        workspace_id=getattr(envelope, "workspace_id", "default"),
    )
    return True


async def deliver_channel_session_reply(crew: Any, session_id: str, owner: str, text: str) -> bool:
    """把桌面侧生成的回复投递回原渠道。"""
    platform = channel_platform_from_session_id(session_id)
    if not platform:
        return False
    body = str(text or "").strip()
    if not body:
        return False
    getter = getattr(crew.session_store, "get_agent_config", None)
    if not callable(getter):
        return False
    cfg = getter(session_id, owner_account_id=owner) or {}
    source_raw = cfg.get("channel_source")
    if not isinstance(source_raw, dict):
        return False
    source = SessionSource.from_dict(source_raw)
    router = getattr(crew, "delivery_router", None)
    if router is None:
        return False
    chat_id = source.chat_id or str(source.user_id or "")
    if not chat_id:
        return False
    result = await router.deliver(f"{platform}:{chat_id}", body, origin=source)
    return bool(result.get("ok"))


def list_channel_session_groups(
    store: Any,
    bindings: Any,
    owner_account_id: str,
    platform_labels: dict[str, str],
) -> list[dict[str, Any]]:
    """列出绑定者可见的渠道会话分组（仅绑定后且有消息的会话）。"""
    lister = getattr(store, "list_sessions", None)
    if not callable(lister):
        return []
    bound = bindings.list_for_owner(owner_account_id)
    if not bound:
        return []
    all_rows = lister(
        owner_account_id=owner_account_id,
        include_archived=False,
        exclude_channel_sessions=False,
    )
    groups: list[dict[str, Any]] = []
    for row in bound:
        platform = str(row.get("platform") or "")
        bound_at = float(row.get("bound_at") or 0)
        label = platform_labels.get(platform) or platform
        sessions = []
        prefix = _channel_session_prefix(platform)
        for s in all_rows:
            sid = str(s.get("session_id") or "")
            if not sid.startswith(prefix):
                continue
            if float(s.get("updated_at") or s.get("created_at") or 0) < bound_at:
                continue
            if int(s.get("message_count") or 0) <= 0:
                continue
            sessions.append({**s, "platform": platform})
        if sessions:
            groups.append({"platform": platform, "label": label, "sessions": sessions})
    return groups
