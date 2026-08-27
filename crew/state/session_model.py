"""会话级模型绑定：读写 session_agent_config 中的 model 字段。

桌面端 Composer 切换模型走会话 API，不再调用全局 crew.use_model。
pending_model_profile_id 在 session 完全 idle 后由 agent:end 钩子提升为正式绑定。
"""

from __future__ import annotations

from typing import Any

from crew.state.config import Config, ModelProfile


def _public_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if not config:
        return {}
    return {k: v for k, v in config.items() if not str(k).startswith("_")}


def _catalog(cfg: Config, profiles: dict[str, ModelProfile] | None = None) -> dict[str, ModelProfile]:
    return profiles if profiles is not None else cfg.model_profiles


def default_model_id(
    cfg: Config,
    profiles: dict[str, ModelProfile] | None = None,
    *,
    preferred_model_id: str | None = None,
) -> str:
    """新建会话与孤儿绑定的回退模型 id。"""
    catalog = _catalog(cfg, profiles)
    preferred = str(preferred_model_id or cfg.default_model_id or cfg.active_model_id or "")
    if preferred in catalog and catalog[preferred].loaded:
        return preferred
    active = str(cfg.active_model_id or "")
    if active in catalog and catalog[active].loaded:
        return active
    if catalog:
        return sorted(catalog)[0]
    return active


def resolve_profile_id(
    cfg: Config,
    model_id: str | None,
    profiles: dict[str, ModelProfile] | None = None,
    *,
    fallback_model_id: str | None = None,
) -> str:
    """校验 profile 存在；无效则回退当前可用模型。"""
    catalog = _catalog(cfg, profiles)
    mid = str(model_id or "").strip()
    if not mid or mid == "inherit":
        return default_model_id(cfg, catalog, preferred_model_id=fallback_model_id)
    profile = catalog.get(mid)
    if profile is None or not profile.loaded:
        return default_model_id(cfg, catalog, preferred_model_id=fallback_model_id)
    return mid


def read_binding(
    agent_config: dict[str, Any] | None,
    cfg: Config,
    profiles: dict[str, ModelProfile] | None = None,
    *,
    fallback_model_id: str | None = None,
) -> dict[str, Any]:
    """解析会话模型绑定（含 pending 与展示用 label）。"""
    raw = _public_config(agent_config)
    catalog = _catalog(cfg, profiles)
    active = resolve_profile_id(
        cfg,
        raw.get("model_profile_id"),
        catalog,
        fallback_model_id=fallback_model_id,
    )
    pending_raw = str(raw.get("pending_model_profile_id") or "").strip()
    pending = (
        resolve_profile_id(cfg, pending_raw, catalog, fallback_model_id=fallback_model_id)
        if pending_raw
        else None
    )
    if pending == active:
        pending = None

    def _label(mid: str) -> str:
        p = catalog.get(mid)
        return (p.name if p else None) or (p.model if p else None) or mid

    return {
        "model_profile_id": active,
        "pending_model_profile_id": pending,
        "model_label": _label(active),
        "pending_label": _label(pending) if pending else None,
        "has_pending": pending is not None,
    }


def merge_agent_config_patch(
    store: Any,
    session_id: str,
    owner_account_id: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    """合并写入 session_agent_config（保留 executor 等其它字段）。"""
    getter = getattr(store, "get_agent_config", None)
    setter = getattr(store, "set_agent_config", None)
    if not callable(getter) or not callable(setter):
        raise RuntimeError("session_store 不支持 agent_config")
    existing = _public_config(getter(session_id, owner_account_id=owner_account_id))
    existing.update(patch)
    # 清空 pending 时显式删除键
    if patch.get("pending_model_profile_id") is None and "pending_model_profile_id" in patch:
        existing.pop("pending_model_profile_id", None)
    return setter(session_id, existing, owner_account_id=owner_account_id)


def set_session_model(
    store: Any,
    cfg: Config,
    profiles: dict[str, ModelProfile] | None,
    session_id: str,
    model_profile_id: str,
    owner_account_id: str = "",
    *,
    busy: bool,
    fallback_model_id: str | None = None,
) -> dict[str, Any]:
    """设置会话模型：busy 时只写 pending，否则立即激活。"""
    catalog = _catalog(cfg, profiles)
    mid = resolve_profile_id(cfg, model_profile_id, catalog, fallback_model_id=fallback_model_id)
    profile = catalog.get(mid)
    if profile is None or not profile.loaded:
        raise KeyError(f"未知或未加载的模型: {model_profile_id}")
    if not profile.has_key:
        raise ValueError(f"模型未配置 API Key: {mid}")

    if busy:
        stored = merge_agent_config_patch(
            store,
            session_id,
            owner_account_id,
            {"pending_model_profile_id": mid},
        )
    else:
        stored = merge_agent_config_patch(
            store,
            session_id,
            owner_account_id,
            {"model_profile_id": mid, "pending_model_profile_id": None},
        )
    return read_binding(stored, cfg, catalog, fallback_model_id=fallback_model_id)


def promote_pending_session_model(
    store: Any,
    cfg: Config,
    profiles: dict[str, ModelProfile] | None,
    session_id: str,
    owner_account_id: str = "",
    *,
    fallback_model_id: str | None = None,
) -> bool:
    """idle 后将 pending 提升为正式绑定。有 pending 时返回 True。"""
    getter = getattr(store, "get_agent_config", None)
    if not callable(getter):
        return False
    raw = _public_config(getter(session_id, owner_account_id=owner_account_id))
    pending = str(raw.get("pending_model_profile_id") or "").strip()
    if not pending:
        return False
    catalog = _catalog(cfg, profiles)
    mid = resolve_profile_id(cfg, pending, catalog, fallback_model_id=fallback_model_id)
    merge_agent_config_patch(
        store,
        session_id,
        owner_account_id,
        {"model_profile_id": mid, "pending_model_profile_id": None},
    )
    return True


def sessions_using_model(
    store: Any,
    model_id: str,
    owner_account_id: str = "",
) -> list[dict[str, str]]:
    """列出某账号下绑定 model_id 的会话。"""
    lister = getattr(store, "list_sessions", None)
    getter = getattr(store, "get_agent_config", None)
    if not callable(lister) or not callable(getter):
        return []
    hits: list[dict[str, str]] = []
    for row in lister(owner_account_id=owner_account_id, include_archived=True):
        sid = str(row.get("session_id") or "")
        if not sid:
            continue
        raw = _public_config(getter(sid, owner_account_id=owner_account_id))
        if str(raw.get("model_profile_id") or "") == model_id or str(
            raw.get("pending_model_profile_id") or ""
        ) == model_id:
            hits.append({"session_id": sid, "owner_account_id": owner_account_id})
    return hits


def rebind_sessions_from_model(
    store: Any,
    cfg: Config,
    profiles: dict[str, ModelProfile] | None,
    from_model_id: str,
    owner_account_id: str = "",
    *,
    to_model_id: str | None = None,
    fallback_model_id: str | None = None,
) -> list[str]:
    """把某账号下绑定 from_model_id 的 session 改绑到 to_model_id。"""
    catalog = _catalog(cfg, profiles)
    target = resolve_profile_id(
        cfg,
        to_model_id or default_model_id(cfg, catalog, preferred_model_id=fallback_model_id),
        catalog,
        fallback_model_id=fallback_model_id,
    )
    getter = getattr(store, "get_agent_config", None)
    if not callable(getter):
        return []
    affected: list[str] = []
    for hit in sessions_using_model(store, from_model_id, owner_account_id):
        sid = hit["session_id"]
        merge_agent_config_patch(
            store,
            sid,
            owner_account_id,
            {"model_profile_id": target, "pending_model_profile_id": None},
        )
        affected.append(sid)
    return affected
