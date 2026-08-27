"""Companion application service and domain invariants."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from crew.core.types import Message

from .store import CompanionStore


DEFAULT_WORKSPACE_ID = "companion"
BUILTIN_CREW_SOURCE_ID = "crew"
SOURCE_REF_RE = re.compile(r"^(builtin|external):([A-Za-z0-9_.:-]{1,160})$")
TARGET_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,160}$")


class CompanionService:
    def __init__(
        self,
        store: CompanionStore,
        *,
        session_store: Any,
        workspace_store: Any,
        external_agents: Any | None = None,
    ) -> None:
        self.store = store
        self.session_store = session_store
        self.workspace_store = workspace_store
        self.external_agents = external_agents

    def ensure_defaults(self, owner_account_id: str) -> None:
        self.store.ensure_profile(owner_account_id)
        self.workspace_store.get(DEFAULT_WORKSPACE_ID, owner_account_id=owner_account_id)
        publications = self.store.list_publications(owner_account_id)
        if not any(
            item["source_kind"] == "builtin" and item["source_id"] == BUILTIN_CREW_SOURCE_ID
            for item in publications
        ):
            self.store.upsert_publication(
                owner_account_id,
                source_kind="builtin",
                source_id=BUILTIN_CREW_SOURCE_ID,
                display_name="Crew",
                description="本机 Crew Agent，可在主人所在且已授权的群聊中参与协作。",
                capabilities=["chat", "workspace", "tools_with_approval"],
                enabled=True,
            )

    def publication_candidates(self, owner_account_id: str) -> list[dict[str, Any]]:
        self.ensure_defaults(owner_account_id)
        existing = {
            (row["source_kind"], row["source_id"]): row
            for row in self.store.list_publications(owner_account_id)
        }
        candidates = [
            {
                "source_ref": "builtin:crew",
                "source_kind": "builtin",
                "source_id": "crew",
                "display_name": "Crew",
                "description": "本机 Crew Agent",
                "provider": "crew",
                "available": True,
                "published": bool(existing.get(("builtin", "crew"), {}).get("enabled")),
                "public_agent_id": existing.get(("builtin", "crew"), {}).get("public_agent_id", ""),
            }
        ]
        if self.external_agents is None:
            return candidates
        try:
            agents = self.external_agents.list_agents(owner_account_id=owner_account_id)
        except Exception:
            agents = []
        for agent in agents:
            agent_id = str(agent.get("id") or "").strip()
            if not agent_id:
                continue
            try:
                _, runtime = self.external_agents.agent_with_runtime(
                    agent_id,
                    owner_account_id=owner_account_id,
                )
            except KeyError:
                continue
            provider = str(agent.get("provider") or runtime.get("provider") or "external").strip()
            current = existing.get(("external", agent_id), {})
            candidates.append(
                {
                    "source_ref": f"external:{agent_id}",
                    "source_kind": "external",
                    "source_id": agent_id,
                    "display_name": str(agent.get("name") or provider or "外援 Agent"),
                    "description": str(agent.get("description") or "本机外援 Agent"),
                    "provider": provider,
                    "available": str(runtime.get("availability_status") or "ready") != "unavailable",
                    "published": bool(current.get("enabled")),
                    "public_agent_id": current.get("public_agent_id", ""),
                }
            )
        return candidates

    def update_publications(self, owner_account_id: str, source_refs: list[str]) -> list[dict[str, Any]]:
        self.ensure_defaults(owner_account_id)
        requested: set[tuple[str, str]] = set()
        for raw in source_refs:
            match = SOURCE_REF_RE.fullmatch(str(raw or "").strip())
            if not match:
                raise ValueError(f"无效的 Agent 来源: {raw}")
            requested.add((match.group(1), match.group(2)))
        candidates = {
            (item["source_kind"], item["source_id"]): item
            for item in self.publication_candidates(owner_account_id)
        }
        missing = requested - set(candidates)
        if missing:
            raise ValueError(f"Agent 不存在或不可读取: {sorted(missing)}")
        for source in requested:
            item = candidates[source]
            self.store.upsert_publication(
                owner_account_id,
                source_kind=source[0],
                source_id=source[1],
                display_name=item["display_name"],
                description=item["description"],
                capabilities=["chat", "workspace", "tools_with_approval"],
                enabled=True,
            )
        self.store.set_publications_enabled(owner_account_id, requested)
        return self.store.list_publications(owner_account_id, enabled_only=True)

    def public_profile(self, owner_account_id: str) -> dict[str, Any]:
        self.ensure_defaults(owner_account_id)
        profile = self.store.ensure_profile(owner_account_id)
        agents = []
        for row in self.store.list_publications(owner_account_id, enabled_only=True):
            agents.append(
                {
                    "public_agent_id": row["public_agent_id"],
                    "display_name": row["display_name"],
                    "description": row["description"],
                    "kind": "crew" if row["source_kind"] == "builtin" else "external",
                    "capabilities": row["capabilities"],
                    "revision": row["revision"],
                }
            )
        return {
            "display_name": profile["display_name"],
            "avatar": profile["avatar"],
            "discoverable": profile["discoverable"],
            "revision": profile["revision"],
            "agents": agents,
        }

    @staticmethod
    def _session_id(owner_account_id: str, kind: str, target_id: str) -> str:
        digest = hashlib.sha256(f"{owner_account_id}\0{kind}\0{target_id}".encode()).hexdigest()[:32]
        segment = "dm" if kind == "nearby_dm" else "room"
        return f"agent:main:nearby:{segment}:{digest}"

    def open_conversation(
        self,
        owner_account_id: str,
        *,
        kind: str,
        target_id: str,
        workspace_id: str | None = None,
        title: str = "",
    ) -> dict[str, Any]:
        if kind not in {"nearby_dm", "nearby_room"}:
            raise ValueError("同伴会话类型必须是 nearby_dm 或 nearby_room")
        target = str(target_id or "").strip()
        if not TARGET_RE.fullmatch(target):
            raise ValueError("无效的同伴会话目标")
        self.ensure_defaults(owner_account_id)
        existing = None
        try:
            existing = self.store.get_binding(owner_account_id, kind=kind, target_id=target)
        except KeyError:
            pass
        workspace = str(
            workspace_id
            or (existing or {}).get("workspace_id")
            or DEFAULT_WORKSPACE_ID
        ).strip() or DEFAULT_WORKSPACE_ID
        self.workspace_store.get(workspace, owner_account_id=owner_account_id)
        session_id = self._session_id(owner_account_id, kind, target)
        session_title = str(title or ("同伴私聊" if kind == "nearby_dm" else "同伴群聊")).strip()
        self.session_store.ensure_session(
            session_id,
            workspace_id=workspace,
            title=session_title,
            owner_account_id=owner_account_id,
        )
        binding = self.store.bind_conversation(
            owner_account_id,
            kind=kind,
            target_id=target,
            session_id=session_id,
            workspace_id=workspace,
            title=session_title,
        )
        return {**binding, "capabilities": self.capabilities(kind)}

    @staticmethod
    def capabilities(kind: str) -> dict[str, bool]:
        room = kind == "nearby_room"
        return {
            "can_send_text": True,
            "can_attach": True,
            "can_mention_people": room,
            "can_mention_agents": room,
            "show_model_picker": False,
            "show_skills": False,
            "show_plan_mode": False,
        }

    def require_online(
        self,
        owner_account_id: str,
        *,
        kind: str,
        target_id: str,
    ) -> None:
        connected_peer_ids = {
            peer["peer_id"]
            for peer in self.store.list_peers(owner_account_id)
            if peer["connection_state"] == "connected"
        }
        if kind == "nearby_dm":
            if target_id not in connected_peer_ids:
                raise ValueError("同伴暂时离线，重新连接后才能发消息")
            return
        if kind != "nearby_room":
            raise ValueError("不支持的同伴会话类型")
        room = next(
            (
                item
                for item in self.store.list_rooms(owner_account_id)
                if item["room_id"] == target_id
            ),
            None,
        )
        if room is None:
            raise KeyError(target_id)
        remote_member_ids = {
            member["id"]
            for member in room["members"]
            if member["kind"] == "human" and member["state"] == "active"
        }
        if not connected_peer_ids.intersection(remote_member_ids):
            raise ValueError("群内暂无其他在线同伴，暂时不能发送消息")

    def require_session_online(self, owner_account_id: str, session_id: str) -> None:
        binding = self.store.binding_for_session(owner_account_id, session_id)
        if binding is None:
            raise KeyError(session_id)
        self.require_online(
            owner_account_id,
            kind=binding["kind"],
            target_id=binding["target_id"],
        )

    def enqueue_human_message(
        self,
        owner_account_id: str,
        *,
        session_id: str,
        text: str,
        mentions: list[str] | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        binding = self.store.binding_for_session(owner_account_id, session_id)
        if binding is None:
            raise KeyError(session_id)
        self.require_online(
            owner_account_id,
            kind=binding["kind"],
            target_id=binding["target_id"],
        )
        body = str(text or "").strip()
        files = attachments or []
        if (not body and not files) or len(body) > 8_000:
            raise ValueError("消息和附件不能同时为空，文字不能超过 8000 个字符")
        normalized_mentions = [
            str(item).strip()
            for item in mentions or []
            if isinstance(item, str) and str(item).strip()
        ]
        if binding["kind"] == "nearby_dm":
            normalized_mentions = []
        receipt = self.store.enqueue(
            owner_account_id,
            kind=binding["kind"],
            target_id=binding["target_id"],
            payload={
                "type": "chat.message",
                "session_id": session_id,
                "text": body,
                "mentions": list(dict.fromkeys(normalized_mentions)),
                "files": [
                    {
                        "file_id": item["file_id"],
                        "name": item["name"],
                        "mime_type": item["mime_type"],
                        "size": item["size"],
                        "sha256": item["sha256"],
                    }
                    for item in files
                ],
            },
        )
        markers = [f'附件「{item["name"]}」位于: {item["path"]}' for item in files]
        history_text = "\n".join(markers)
        if body:
            history_text = f"{history_text}\n\n{body}" if history_text else body
        self.session_store.append(
            session_id,
            [Message.user(history_text)],
            owner_account_id=owner_account_id,
        )
        return receipt

    def enqueue_agent_room_message(
        self,
        owner_account_id: str,
        *,
        room_id: str,
        text: str,
    ) -> dict[str, Any]:
        self.require_online(owner_account_id, kind="nearby_room", target_id=room_id)
        body = str(text or "").strip()
        if not body or len(body) > 8_000:
            raise ValueError("群消息不能为空且不能超过 8000 个字符")
        return self.store.enqueue(
            owner_account_id,
            kind="nearby_room",
            target_id=room_id,
            payload={"type": "agent.result", "text": body},
        )
