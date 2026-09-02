"""Wiki Agent 会话状态管理器。

持久化专用 Wiki Agent 的活跃知识库设置，并维护工具确认、卡片和
变更事件等对话级状态。不提供普通会话的 Wiki 模式进入/退出状态机。
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from crew.core.runctx import current_owner_account_id
from crew.state.home import get_crew_home, safe_path_segment
from crew.state.logging import get_logger

log = get_logger("wiki.manager")
SessionKey = tuple[str, str]


class WikiSessionManager:
    """Wiki Agent 会话状态；内存态按 ``(owner_account_id, session_id)`` 隔离。"""

    def __init__(self, store: Any = None) -> None:
        self._pending_cards: dict[SessionKey, list[dict[str, Any]]] = {}
        self._pending_changes: dict[SessionKey, list[dict[str, Any]]] = {}
        self._confirmations: dict[tuple[str, str, str], dict[str, Any]] = {}
        # (owner, session_id, action, kb_id) 会话级批量授权（内存态，进程生命周期）
        self._action_grants: set[tuple[str, str, str, str]] = set()
        self._loaded: set[SessionKey] = set()
        # 当前会话选中的知识库（默认 "default"）
        self._kb_ids: dict[SessionKey, str] = {}
        # WikiStore 引用（由 build_app 通过构造函数注入），用于 Wiki Agent 上下文构建等
        self.store: Any = store

    @staticmethod
    def _key(session_id: str, owner_account_id: str | None = None) -> SessionKey:
        owner = current_owner_account_id.get() if owner_account_id is None else owner_account_id
        return owner or "", session_id or "default"

    def _state_dir(self, key: SessionKey) -> Path:
        owner, sid = key
        return get_crew_home() / "wiki_sessions" / safe_path_segment(owner, "legacy") / safe_path_segment(sid, "default")

    def _state_path(self, key: SessionKey) -> Path:
        return self._state_dir(key) / "state.json"

    def _persist(self, key: SessionKey) -> None:
        try:
            path = self._state_path(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "kb_id": self._kb_ids.get(key, "default"),
                "updated_at": time_iso(),
            }
            path.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.warning("wiki 状态持久化失败 %s: %s", key, exc)

    def _restore(self, key: SessionKey) -> None:
        if key in self._loaded:
            return
        self._loaded.add(key)
        try:
            path = self._state_path(key)
            if not path.is_file():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            kb_id = str(data.get("kb_id") or "default").strip()
            self._kb_ids[key] = kb_id if kb_id else "default"
        except Exception as exc:  # noqa: BLE001
            log.warning("wiki 状态恢复失败 %s: %s", key, exc)

    # ---- 会话知识库设置 ----

    def set_kb_id(self, session_id: str, kb_id: str, owner_account_id: str | None = None) -> None:
        """设置当前会话的默认知识库。"""
        key = self._key(session_id, owner_account_id)
        normalized = str(kb_id or "default").strip()
        self._kb_ids[key] = normalized if normalized else "default"
        self._persist(key)

    def get_kb_id(self, session_id: str, owner_account_id: str | None = None) -> str:
        """获取当前会话的默认知识库，未设置时返回 'default'。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        return self._kb_ids.get(key, "default") or "default"

    # ---- 待推送 Wiki 卡片 ----

    def add_pending_cards(self, session_id: str, cards: list[dict[str, Any]], owner_account_id: str | None = None) -> None:
        """登记本轮待推送给前端的 Wiki 卡片。"""
        key = self._key(session_id, owner_account_id)
        self._pending_cards.setdefault(key, []).extend(cards)

    def take_pending_cards(self, session_id: str, owner_account_id: str | None = None) -> list[dict[str, Any]]:
        """一次性消费并返回待推送卡片。"""
        return self._pending_cards.pop(self._key(session_id, owner_account_id), [])

    # ---- Wiki 变更事件 ----

    def add_pending_change(
        self,
        session_id: str,
        change: dict[str, Any],
        owner_account_id: str | None = None,
    ) -> None:
        self._pending_changes.setdefault(self._key(session_id, owner_account_id), []).append(dict(change))

    def take_pending_changes(
        self,
        session_id: str,
        owner_account_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._pending_changes.pop(self._key(session_id, owner_account_id), [])

    # ---- 一次性危险操作确认 ----

    def issue_confirmation(
        self,
        session_id: str,
        *,
        action: str,
        kb_id: str,
        payload: dict[str, Any],
        summary: str,
        impact: dict[str, Any],
        owner_account_id: str | None = None,
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        owner, sid = self._key(session_id, owner_account_id)
        now = time.time()
        self._prune_confirmations(now)
        confirmation_id = f"wcf_{uuid.uuid4().hex}"
        expires_at = now + max(60, int(ttl_seconds))
        self._confirmations[(owner, sid, confirmation_id)] = {
            "action": action,
            "kb_id": kb_id,
            "payload": dict(payload),
            "summary": summary,
            "impact": dict(impact),
            "expires_at": expires_at,
        }
        return {
            "requires_confirmation": True,
            "confirmation_id": confirmation_id,
            "action": action,
            "kb_id": kb_id,
            "summary": summary,
            "impact": impact,
            "expires_at": expires_at,
        }

    def consume_confirmation(
        self,
        session_id: str,
        confirmation_id: str,
        *,
        action: str,
        kb_id: str,
        owner_account_id: str | None = None,
    ) -> dict[str, Any] | None:
        owner, sid = self._key(session_id, owner_account_id)
        self._prune_confirmations()
        key = (owner, sid, str(confirmation_id or "").strip())
        item = self._confirmations.get(key)
        if item is None or item.get("action") != action or item.get("kb_id") != kb_id:
            return None
        self._confirmations.pop(key, None)
        return dict(item.get("payload") or {})

    def cancel_confirmation(
        self,
        session_id: str,
        confirmation_id: str,
        owner_account_id: str | None = None,
    ) -> bool:
        owner, sid = self._key(session_id, owner_account_id)
        self._prune_confirmations()
        return self._confirmations.pop((owner, sid, str(confirmation_id or "").strip()), None) is not None

    def _prune_confirmations(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        expired = [key for key, value in self._confirmations.items() if float(value.get("expires_at", 0)) <= current]
        for key in expired:
            self._confirmations.pop(key, None)

    # ---- 会话级批量授权（「本批次全部允许」） ----

    def grant_action(
        self,
        session_id: str,
        *,
        action: str,
        kb_id: str,
        owner_account_id: str | None = None,
    ) -> None:
        """记录会话级授权：本进程内同 (action, kb_id) 的后续危险操作不再询问。"""
        owner, sid = self._key(session_id, owner_account_id)
        self._action_grants.add((owner, sid, str(action), str(kb_id)))

    def has_action_grant(
        self,
        session_id: str,
        *,
        action: str,
        kb_id: str,
        owner_account_id: str | None = None,
    ) -> bool:
        owner, sid = self._key(session_id, owner_account_id)
        return (owner, sid, str(action), str(kb_id)) in self._action_grants


def time_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
