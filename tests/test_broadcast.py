"""对话广播核心测试：stream_and_broadcast（WS/consume）+ make_broadcasting_handler（渠道）+ 共享核心。

重点覆盖修复核心：广播 owner 取 **dispatch 改写后**的 envelope.user_id（渠道会话真实 owner=绑定桌面账号 binder），
而非平台原始 uid；WS 入口可显式传鉴权账号覆盖。用真实 ResponseChunk 驱动，覆盖 format/skip 真实过滤。
"""

from __future__ import annotations

import asyncio

from crew.core.envelope import Envelope, ResponseChunk
from crew.gateway.broadcast import make_broadcasting_handler, stream_and_broadcast


class _FakeConnections:
    def __init__(self) -> None:
        self.cleared: list = []
        self.pushed: list = []

    def clear_buffer(self, session_id, owner_account_id=""):
        self.cleared.append((session_id, owner_account_id))

    async def push_payload(self, session_id, payload, owner_account_id=""):
        self.pushed.append((session_id, payload, owner_account_id))


def _crew(chunks, *, rewrite_user_id=None):
    """模拟 crew.dispatch：可选在产帧前改写 envelope.user_id（模拟 prepare_inbound_channel_envelope）。"""
    class _Crew:
        def dispatch(self, envelope):
            async def _gen():
                if rewrite_user_id is not None:
                    envelope.user_id = rewrite_user_id
                for c in chunks:
                    yield c
            return _gen()
    return _Crew()


def _env(user_id="u1"):
    return Envelope.of("hi", session_id="agent:main:feishu:dm:u1", channel="feishu", user_id=user_id)


def _frames():
    return [
        ResponseChunk("r1", kind="delta", body={"text": "hel"}, sequence=1),
        ResponseChunk("r1", kind="tool", body={"name": "search"}),
        ResponseChunk("r1", kind="final", body={"text": "hello"}, is_final=True),
    ]


# --------------------------------------------------------------------------- #
# stream_and_broadcast（WS / consume 版）
# --------------------------------------------------------------------------- #
def test_broadcasts_each_frame_and_returns_final():
    conn = _FakeConnections()
    final, error = asyncio.run(stream_and_broadcast(_crew(_frames()), conn, _env(), "u1"))
    assert final == "hello" and error == ""
    assert conn.cleared == [("agent:main:feishu:dm:u1", "u1")]
    assert [p[1]["kind"] for p in conn.pushed] == ["delta", "tool", "final"]
    assert all(sid == "agent:main:feishu:dm:u1" and owner == "u1" for sid, _, owner in conn.pushed)


def test_explicit_owner_overrides_envelope_user_id():
    """WS 入口传鉴权账号 → 恒用它，不受 dispatch 对 envelope.user_id 的改写影响。"""
    conn = _FakeConnections()
    asyncio.run(stream_and_broadcast(_crew(_frames(), rewrite_user_id="ignored"), conn, _env(), "desktop-acct"))
    assert all(owner == "desktop-acct" for _, _, owner in conn.pushed)


def test_owner_defaults_to_dispatch_rewritten_user_id():
    """owner 不传时使用 dispatch 改写后的 envelope.user_id（渠道会话真实 owner=binder）。

    envelope 进来 user_id=u1（渠道 uid）；dispatch 改写成 binder-desktop（绑定桌面账号）→ 广播落桌面订阅的正确桶。
    """
    conn = _FakeConnections()
    asyncio.run(stream_and_broadcast(_crew(_frames(), rewrite_user_id="binder-desktop"), conn, _env("u1")))
    assert all(owner == "binder-desktop" for _, _, owner in conn.pushed)
    assert conn.cleared == [("agent:main:feishu:dm:u1", "binder-desktop")]  # clear 也用改写后 owner，同桶


def test_captures_error_text():
    conn = _FakeConnections()
    final, error = asyncio.run(stream_and_broadcast(
        _crew([ResponseChunk("r1", kind="error", body={"message": "boom"}, is_final=True)]), conn, _env(), "u1"))
    assert final == "" and error == "boom"
    assert [p[1]["kind"] for p in conn.pushed] == ["error"]


def test_silent_final_skipped():
    conn = _FakeConnections()
    final, _ = asyncio.run(stream_and_broadcast(
        _crew([ResponseChunk("r1", kind="final", body={"text": ""}, is_final=True)]), conn, _env(), "u1"))
    assert final == "" and conn.pushed == []


def test_thinking_preserved_in_broadcast():
    """广播给桌面用桌面渲染规则：即使是 feishu 渠道会话（envelope.channel=feishu），<thinking> 也保留供前端卡片渲染，
    不被 IM 内联剥离（IM 剥离只用于 adapter 回包外部平台）。"""
    chunks = [ResponseChunk("r1", kind="final", body={"text": "<thinking>推理过程</thinking>答案"}, is_final=True)]
    conn = _FakeConnections()
    asyncio.run(stream_and_broadcast(_crew(chunks), conn, _env(), "u1"))  # _env channel=feishu
    assert conn.pushed, "final 帧应被广播"
    assert "<thinking>推理过程</thinking>" in conn.pushed[-1][1]["body"]["text"]  # thinking 保留


def test_propagates_dispatch_exception():
    class _Boom:
        def dispatch(self, envelope):
            async def _gen():
                if False:
                    yield
                raise RuntimeError("dispatch failed")
            return _gen()
    conn = _FakeConnections()
    try:
        asyncio.run(stream_and_broadcast(_Boom(), conn, _env(), "u1"))
        assert False, "应透传 dispatch 异常"
    except RuntimeError as exc:
        assert "dispatch failed" in str(exc)


# --------------------------------------------------------------------------- #
# make_broadcasting_handler（渠道版：所有 channel 装配统一使用）
# --------------------------------------------------------------------------- #
def test_handler_broadcasts_and_yields_with_rewritten_owner():
    """渠道 handler：消费即广播（owner=dispatch 改写后的 binder）+ 原样 yield 给 adapter 取 final。"""
    conn = _FakeConnections()
    handler = make_broadcasting_handler(_crew(_frames(), rewrite_user_id="binder-desktop"), conn)
    yielded: list = []

    async def _consume():
        async for chunk in handler(_env("u1")):
            yielded.append(chunk.kind)

    asyncio.run(_consume())
    assert yielded == ["delta", "tool", "final"]  # adapter 侧照常拿到每帧
    assert [p[1]["kind"] for p in conn.pushed] == ["delta", "tool", "final"]  # 桌面观察者收到中间帧
    assert all(owner == "binder-desktop" for _, _, owner in conn.pushed)  # owner=改写后 binder


# --------------------------------------------------------------------------- #
# 装配级回归：所有渠道入口统一用 crew.channel_handler
# --------------------------------------------------------------------------- #
def test_channel_handler_wired_by_create_app():
    """create_app 后 crew.channel_handler 注入为广播包装 handler，供所有渠道入口
    （start_all / connect / reconnect / webhook fallback）统一消费 —— 防某入口漏接、回退到裸 crew.dispatch。"""
    from crew.app import build_app
    from crew.gateway.app import create_app

    crew = build_app()
    create_app(crew)  # 装配（不启 lifespan）
    assert callable(crew.channel_handler)
    assert crew.connections is not None
