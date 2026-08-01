"""渠道会话：session_id 识别、绑定者 owner 映射、列表与桌面续聊投递。"""

from __future__ import annotations

from typing import Any

from crew.gateway.session_context import SessionContext, SessionSource, session_context_from_envelope
from crew.state.logging import get_logger

log = get_logger("gateway.channel_sessions")

_CHANNEL_PREFIX = "agent:main:"


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
    if existing.get("channel_source"):
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
    envelope.params["session_context"] = session_context_from_envelope(envelope, [platform, "local"])
    ensure_channel_source_config(crew.session_store, envelope)


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
