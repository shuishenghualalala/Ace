"""Feature Event → 旧 kind 帧的出口兼容适配器（Legacy Adapter，有删除期限）。

生产侧（Team 编排、Wiki 推送点）已统一产出命名空间 `feature_event`；
当前桌面/Web 端的 reducer 仍只认识旧 kind（`team_internal`、`wiki_cards`、
`wiki_changed`、`wiki_ingest_progress`），因此在 WS 出口把已登记的
feature_event 翻译回旧帧，线上协议对现有客户端逐字节不变。

deprecated_after：前端 reducer 全部迁移到 `ace.feature-event.v1`（阶段 5）
之后删除本模块与三个旧 ChunkKind 枚举；删除前 CLI/Desktop/Web 需都已切到
feature_event。登记新映射时必须同时登记删除跟踪。
"""

from __future__ import annotations

from typing import Any

from crew.state.logging import get_logger

log = get_logger("gateway.event_compat")

# (feature, event, version) → 旧帧 kind；payload 原样透传为旧帧 body。
_LEGACY_KIND_BY_EVENT: dict[tuple[str, str, int], str] = {
    ("team", "internal_message", 1): "team_internal",
    ("wiki", "cards", 1): "wiki_cards",
    ("wiki", "changed", 1): "wiki_changed",
    ("wiki", "ingest_progress", 1): "wiki_ingest_progress",
    ("kanban", "started", 1): "kanban",
    ("kanban", "board_changed", 1): "kanban",
    ("kanban", "call_completed", 1): "kanban",
    ("kanban", "workflow_progress", 1): "workflow_progress",
}

# 透传到旧帧的帧级字段（kind/body 由映射决定，gateway_sequence 由出口分配）。
_PASSTHROUGH_FRAME_FIELDS = ("is_final", "sequence", "request_id", "session_id")


def expand_outgoing_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """把出口帧展开为对当前客户端可发的帧序列。

    - 非 feature_event：原样返回（单元素）。
    - 已登记的 feature_event：转换为对应旧 kind 帧，帧级字段透传。
    - 未登记的 feature_event：旧客户端没有等价物，记录并丢弃，不把未知
      事件泄漏给尚未支持它的前端。
    """
    if payload.get("kind") != "feature_event":
        return [payload]
    body = payload.get("body") or {}
    key = (
        str(body.get("feature") or ""),
        str(body.get("event") or ""),
        int(body.get("version") or 0),
    )
    legacy_kind = _LEGACY_KIND_BY_EVENT.get(key)
    if legacy_kind is None:
        log.warning(
            "丢弃未登记的 feature_event feature=%s event=%s version=%s",
            key[0],
            key[1],
            key[2],
        )
        return []
    frame: dict[str, Any] = {
        "kind": legacy_kind,
        "body": body.get("payload") or {},
    }
    # 旧 kanban 帧 body 约定以 event 字段开头，feature_event payload 不重复携带
    # event 名，因此在这里重建，保证出口旧帧与迁移前逐字节一致。
    if legacy_kind == "kanban":
        frame["body"] = {"event": key[1], **dict(frame["body"])}
    for field in _PASSTHROUGH_FRAME_FIELDS:
        if field in payload:
            frame[field] = payload[field]
    return [frame]
