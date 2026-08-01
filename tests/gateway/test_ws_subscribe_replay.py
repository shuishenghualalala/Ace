"""WebSocket subscribe/resume 断线 replay 契约测试。

ws.py 在 subscribe/resume 携带 last_gateway_sequences 时调用
ConnectionManager.replay(after_gateway_sequence=...)；此处用 ConnectionManager
直接验证该契约（与 test_connections.py 互补）。
"""

from __future__ import annotations

import pytest

from crew.gateway.connections import ConnectionManager


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


@pytest.mark.asyncio
async def test_subscribe_replay_contract_after_last_sequence():
    """模拟 desktop/web subscribe：last_gateway_sequences[sid]=1 只回放 seq>1 的帧。"""
    conn = ConnectionManager(min_interval=0)
    owner = "A:uid-a"
    sid = "s1"

    await conn.push_payload(
        sid,
        {"kind": "delta", "body": {"text": "a"}, "session_id": sid},
        owner_account_id=owner,
    )
    await conn.push_payload(
        sid,
        {"kind": "delta", "body": {"text": "b"}, "session_id": sid},
        owner_account_id=owner,
    )

    ws = _FakeWS()
    conn.register(sid, ws, owner_account_id=owner)
    last_gateway_sequences = {sid: 1}
    after = last_gateway_sequences.get(sid, 0)
    if after > 0:
        await conn.replay(sid, ws, after_gateway_sequence=after, owner_account_id=owner)

    assert len(ws.sent) == 1
    assert ws.sent[0]["gateway_sequence"] == 2
    assert ws.sent[0]["body"]["text"] == "b"
