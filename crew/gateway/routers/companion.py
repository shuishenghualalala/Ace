"""Companion HTTP API.

The Gateway owns domain state and canonical conversation history.  Desktop link
adapters only discover peers and move opaque events; they do not own chat state.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from crew.core.types import Message
from crew.gateway.auth import account_from_request
from crew.gateway.context import save_upload
from crew.state.home import get_owner_runtime_home


MAX_COMPANION_FILE_BYTES = 4 * 1024 * 1024


def _prepare_attachment(owner: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Read one already-uploaded attachment without allowing path escape."""
    uploads_root = (get_owner_runtime_home(owner) / "uploads").expanduser().resolve()
    path = Path(str(raw.get("path") or "")).expanduser().resolve()
    try:
        path.relative_to(uploads_root)
    except ValueError as exc:
        raise ValueError("附件不属于当前账号的上传目录") from exc
    if not path.is_file():
        raise ValueError("附件不存在或不是普通文件")
    size = path.stat().st_size
    if size > MAX_COMPANION_FILE_BYTES:
        raise ValueError("同伴附件不能超过 4 MiB")
    data = path.read_bytes()
    name = Path(str(raw.get("name") or path.name)).name
    if not name or name in {".", ".."}:
        raise ValueError("附件名无效")
    mime_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return {
        "file_id": str(raw.get("id") or f"file_{hashlib.sha256(data).hexdigest()[:24]}"),
        "name": name,
        "path": str(path),
        "type": "image" if mime_type.startswith("image/") else "file",
        "mime_type": mime_type,
        "size": size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "data_base64": base64.b64encode(data).decode("ascii"),
    }


def _store_received_attachment(owner: str, raw: dict[str, Any]) -> dict[str, Any]:
    name = Path(str(raw.get("name") or "")).name
    encoded = str(raw.get("data_base64") or "")
    if not name or name in {".", ".."} or len(encoded) > MAX_COMPANION_FILE_BYTES * 2:
        raise ValueError("收到的同伴附件无效")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("收到的同伴附件不是有效的 Base64") from exc
    if len(data) > MAX_COMPANION_FILE_BYTES:
        raise ValueError("收到的同伴附件超过 4 MiB")
    expected_size = raw.get("size")
    expected_sha256 = str(raw.get("sha256") or "")
    actual_sha256 = hashlib.sha256(data).hexdigest()
    if expected_size != len(data) or not expected_sha256 or actual_sha256 != expected_sha256.lower():
        raise ValueError("收到的同伴附件校验失败")
    return save_upload(name, data, owner_account_id=owner)


def create_companion_router(crew) -> APIRouter:
    router = APIRouter(prefix="/api/companion", tags=["companion"])

    def _owner(request: Request) -> str:
        return account_from_request(request).owner_account_id

    def _service():
        service = getattr(crew, "companion", None)
        if service is None:
            raise RuntimeError("同伴功能未启用")
        return service

    @router.get("/profile")
    async def profile(request: Request) -> JSONResponse:
        service = _service()
        owner = _owner(request)
        service.ensure_defaults(owner)
        return JSONResponse(
            {
                "profile": service.store.ensure_profile(owner),
                "public_profile": service.public_profile(owner),
                "agent_candidates": service.publication_candidates(owner),
            }
        )

    @router.put("/profile")
    async def update_profile(request: Request, payload: dict[str, Any]) -> JSONResponse:
        service = _service()
        owner = _owner(request)
        try:
            fields: dict[str, Any] = {}
            if "display_name" in payload:
                display_name = " ".join(str(payload.get("display_name") or "").split())[:120]
                fields["display_name"] = display_name
            if "avatar" in payload:
                avatar = str(payload.get("avatar") or "").strip()
                if len(avatar) > 2_048:
                    raise ValueError("头像地址过长")
                fields["avatar"] = avatar
            if "discoverable" in payload:
                if not isinstance(payload.get("discoverable"), bool):
                    raise ValueError("discoverable 必须是布尔值")
                fields["discoverable"] = payload["discoverable"]
            if fields:
                service.store.update_profile(owner, **fields)
            if "published_agent_refs" in payload:
                refs = payload.get("published_agent_refs")
                if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
                    raise ValueError("published_agent_refs 必须是字符串数组")
                service.update_publications(owner, refs)
            return await profile(request)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/conversations")
    async def conversations(request: Request) -> JSONResponse:
        service = _service()
        owner = _owner(request)
        return JSONResponse(
            {
                "conversations": service.store.list_bindings(owner),
                "peers": service.store.list_peers(owner),
                "rooms": service.store.list_rooms(owner),
            }
        )

    @router.post("/conversations/open")
    async def open_conversation(request: Request, payload: dict[str, Any]) -> JSONResponse:
        try:
            service = _service()
            owner = _owner(request)
            kind = str(payload.get("kind") or "")
            target_id = str(payload.get("target_id") or "")
            service.require_online(owner, kind=kind, target_id=target_id)
            binding = service.open_conversation(
                owner,
                kind=kind,
                target_id=target_id,
                workspace_id=(
                    str(payload.get("workspace_id") or "").strip() or None
                ),
                title=str(payload.get("title") or ""),
            )
            return JSONResponse({"ok": True, **binding})
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.post("/conversations/{session_id}/messages")
    async def send_message(
        request: Request,
        session_id: str,
        payload: dict[str, Any],
    ) -> JSONResponse:
        service = _service()
        owner = _owner(request)
        try:
            service.require_session_online(owner, session_id)
            raw_attachments = payload.get("attachments")
            if raw_attachments is not None and (
                not isinstance(raw_attachments, list)
                or not all(isinstance(item, dict) for item in raw_attachments)
            ):
                raise ValueError("attachments 必须是对象数组")
            attachments = [
                _prepare_attachment(owner, item)
                for item in (raw_attachments or [])[:8]
            ]
            receipt = service.enqueue_human_message(
                owner,
                session_id=session_id,
                text=str(payload.get("text") or ""),
                mentions=payload.get("mentions") if isinstance(payload.get("mentions"), list) else None,
                attachments=attachments,
            )
            return JSONResponse({"ok": True, **receipt})
        except KeyError:
            return JSONResponse({"ok": False, "error": "同伴会话不存在"}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.post("/files/prepare")
    async def prepare_file(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """Prepare a main-conversation upload for the LinkAdapter."""
        try:
            return JSONResponse({"ok": True, "file": _prepare_attachment(_owner(request), payload)})
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    @router.get("/outbox")
    async def claim_outbox(request: Request, limit: int = 50) -> JSONResponse:
        events = _service().store.claim_outbox(_owner(request), limit=limit)
        return JSONResponse({"events": events})

    @router.post("/outbox/{event_id}/settle")
    async def settle_outbox(
        request: Request,
        event_id: str,
        payload: dict[str, Any],
    ) -> JSONResponse:
        delivered = payload.get("delivered")
        if not isinstance(delivered, bool):
            return JSONResponse(
                {"ok": False, "error": "delivered 必须是布尔值"}, status_code=400
            )
        _service().store.settle_outbox(_owner(request), event_id, delivered=delivered)
        return JSONResponse({"ok": True})

    @router.post("/link-state")
    async def link_state(request: Request, payload: dict[str, Any]) -> JSONResponse:
        """Project normalized LinkAdapter events into the Companion domain."""
        service = _service()
        owner = _owner(request)
        event_type = str(payload.get("type") or "").strip()
        try:
            if event_type == "peer":
                peer_id = str(payload.get("peer_id") or "").strip()
                if not peer_id:
                    raise ValueError("peer_id 不能为空")
                profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
                service.store.upsert_peer(
                    owner,
                    peer_id,
                    profile=profile,
                    relationship=str(payload.get("relationship") or "nearby"),
                    connection_state=str(payload.get("connection_state") or "unavailable"),
                )
            elif event_type == "room":
                room_id = str(payload.get("room_id") or "").strip()
                if not room_id:
                    raise ValueError("room_id 不能为空")
                members = payload.get("human_member_ids")
                service.store.upsert_room(
                    owner,
                    room_id,
                    name=" ".join(str(payload.get("name") or "同伴群聊").split())[:120],
                    owner_peer_id=str(payload.get("owner_peer_id") or ""),
                    revision=int(payload.get("revision") or 1),
                    human_member_ids=[str(item) for item in members] if isinstance(members, list) else [],
                )
            elif event_type in {"message", "file"}:
                kind = str(payload.get("kind") or "")
                target_id = str(payload.get("target_id") or "")
                try:
                    binding = service.store.get_binding(owner, kind=kind, target_id=target_id)
                except KeyError:
                    binding = service.open_conversation(
                        owner,
                        kind=kind,
                        target_id=target_id,
                        title=str(payload.get("conversation_title") or ""),
                    )
                sender_kind = str(payload.get("sender_kind") or "human")
                sender_name = " ".join(str(payload.get("sender_name") or "同伴").split())[:120]
                if event_type == "file":
                    if sender_kind == "agent" and kind != "nearby_room":
                        raise ValueError("Agent 只能在群聊中发送附件")
                    raw_file = payload.get("file")
                    if not isinstance(raw_file, dict):
                        raise ValueError("缺少同伴附件")
                    saved = _store_received_attachment(owner, raw_file)
                    message = Message.user(f'附件「{saved["name"]}」位于: {saved["path"]}')
                    message.name = sender_name
                    crew.session_store.append(
                        binding["session_id"], [message], owner_account_id=owner
                    )
                    return JSONResponse({"ok": True, "attachment": saved})
                text = str(payload.get("text") or "").strip()
                if not text or len(text) > 8_000:
                    raise ValueError("消息不能为空且不能超过 8000 个字符")
                if sender_kind == "agent":
                    if kind != "nearby_room":
                        raise ValueError("Agent 只能在群聊中发言")
                    message = Message.assistant(text)
                    message.name = sender_name
                else:
                    message = Message.user(text)
                    message.name = sender_name
                crew.session_store.append(
                    binding["session_id"], [message], owner_account_id=owner
                )
            else:
                raise ValueError("不支持的 LinkAdapter 事件")
        except KeyError:
            return JSONResponse({"ok": False, "error": "同伴会话不存在"}, status_code=404)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @router.get("/runs")
    async def runs(request: Request, room_id: str | None = None) -> JSONResponse:
        return JSONResponse(
            {"runs": _service().store.list_runs(_owner(request), room_id=room_id)}
        )

    return router
