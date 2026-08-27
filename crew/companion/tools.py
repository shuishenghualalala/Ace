"""Model-callable tools for the pluggable Companion Agent."""

from __future__ import annotations

from typing import Any

from crew.core.runctx import current_owner_account_id
from crew.tools.registry import Registry, tool_error, tool_result

from .service import CompanionService


def register_companion_tools(registry: Registry, service: CompanionService) -> None:
    def handle_list(_args: dict[str, Any]) -> str:
        owner = current_owner_account_id.get()
        return tool_result(
            {
                "peers": service.store.list_peers(owner),
                "rooms": service.store.list_rooms(owner),
            }
        )

    def handle_send_room(args: dict[str, Any]) -> str:
        room_id = str(args.get("room_id") or "").strip()
        text = str(args.get("text") or "").strip()
        if not room_id:
            return tool_error("room_id 不能为空")
        try:
            receipt = service.enqueue_agent_room_message(
                current_owner_account_id.get(),
                room_id=room_id,
                text=text,
            )
        except (KeyError, ValueError) as exc:
            return tool_error(str(exc))
        return tool_result({"ok": True, **receipt})

    registry.register(
        name="companion_list",
        toolset="companion",
        schema={
            "name": "companion_list",
            "description": "列出当前用户的同伴和群聊。Agent 没有私聊入口。",
            "parameters": {"type": "object", "properties": {}},
        },
        handler=handle_list,
        display_name="查看同伴",
        ui_label_template="查看同伴与群聊",
        search_hint="companion nearby peers rooms 同伴 群聊",
    )
    registry.register(
        name="companion_send_room_message",
        toolset="companion",
        schema={
            "name": "companion_send_room_message",
            "description": (
                "向主人仍在其中的同伴群聊发送一条 Agent 消息。"
                "只能在用户明确要求发送或继续群协作时调用，不能发送任何私聊。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "room_id": {"type": "string", "description": "目标同伴群 ID"},
                    "text": {"type": "string", "description": "要发送的最终消息"},
                },
                "required": ["room_id", "text"],
            },
        },
        handler=handle_send_room,
        display_name="发送同伴群消息",
        ui_label_template="发送同伴群消息 {room_id}",
        should_defer=True,
        search_hint="send companion nearby room group message 同伴 群聊 发送",
    )
