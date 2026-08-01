"""对话事件广播：把一次 dispatch 的 ResponseChunk 流实时推给该 (owner, session) 的所有 WS 观察者。

这是「桌面端实时渲染对话」的唯一实现，WS 入口与各平台 channel 共用。

**owner 的正确来源**：广播落在「消费 ``crew.dispatch`` 之后」，而 ``crew.dispatch`` 第一步
``prepare_inbound_channel_envelope`` 会把渠道会话的 ``envelope.user_id`` 从平台原始 uid 改写成
**绑定的桌面账号（binder）**——即会话真实 owner，也是桌面端订阅所用的 owner。因此本模块统一
取「dispatch 改写后的 ``envelope.user_id``」作为广播 owner，永远与订阅端对齐。WS 入口（web 会话，
dispatch 不改写 user_id）可显式传入鉴权账号，语义等价。

**桌面渲染规则**：广播的唯一受众是桌面 WS 观察者，因此一律用「桌面渲染」过滤——保留 ``<thinking>``
供前端 ``<details>`` 卡片展示（密钥脱敏等安全过滤不依赖 channel，仍全程生效）。IM 内联 thinking 剥离
只用于各 adapter 回包外部平台时自行 ``apply_text_filters``，与广播互不影响。

下沉到「渠道 handler 包装」而非每个 adapter 各自广播：一处接入，所有渠道零改自动获得实时渲染。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Callable

from crew.core.envelope import Envelope, ResponseChunk
from crew.gateway.outbound import format_outbound_payload


async def _broadcast_stream(
    crew: Any,
    connections: Any,
    envelope: Envelope,
    *,
    owner_account_id: str | None = None,
) -> AsyncIterator[ResponseChunk]:
    """消费 ``crew.dispatch(envelope)``，逐帧广播给会话真实 owner 的 WS 观察者，并原样 yield 每帧。

    - owner：``owner_account_id`` 非 None 时用它（WS 入口传鉴权账号）；否则取 **dispatch 改写后**的
      ``envelope.user_id``（渠道会话 = 绑定桌面账号 binder），保证与桌面订阅端同桶。
    - clear：owner 已知（WS）→ dispatch 前清回放缓存，保持原 WS 语义（断线 replay 依赖此时机）；
      owner 待定（渠道）→ 首帧拿到真实 owner 再清同一桶。
    - 过滤：广播用「桌面渲染」context（只带 session_id，不含渠道 channel）→ 保留 <thinking>；
      密钥脱敏等安全过滤不依赖 channel，仍生效。
    - 不吞异常——dispatch 抛出的异常透传给调用方。
    """
    # 桌面渲染 context：不含 IM channel，故 strip_thinking 不生效、<thinking> 得以保留供前端卡片渲染。
    render_context = {"session_id": envelope.session_id}
    # owner 一次确定后固定复用（不每帧重读 envelope.user_id，避免 dispatch 内后续误改导致同轮帧分到不同桶）：
    # WS 显式传 → 全程用它、dispatch 前清缓存；渠道待定 → 首帧取 dispatch 改写后的真实 owner（binder）再清同桶。
    owner = owner_account_id
    if owner is not None:
        connections.clear_buffer(envelope.session_id, owner_account_id=owner)
    async for chunk in crew.dispatch(envelope):
        if owner is None:
            owner = str(envelope.user_id or "")
            connections.clear_buffer(envelope.session_id, owner_account_id=owner)
        payload = format_outbound_payload(chunk, session_id=envelope.session_id, context=render_context)
        if payload is not None:
            await connections.push_payload(envelope.session_id, payload, owner_account_id=owner)
        yield chunk


async def stream_and_broadcast(
    crew: Any,
    connections: Any,
    envelope: Envelope,
    owner_account_id: str | None = None,
) -> tuple[str, str]:
    """WS/消费版：消费 dispatch 流并广播，返回 ``(final_text, error_text)`` 供调用方决定出站。

    owner 语义见 ``_broadcast_stream``；WS 入口显式传鉴权账号，其余（渠道回退路径）取会话真实 owner。
    """
    final_text = ""
    error_text = ""
    async for chunk in _broadcast_stream(crew, connections, envelope, owner_account_id=owner_account_id):
        if chunk.kind == "final":
            final_text = str(chunk.body.get("text") or "")
        elif chunk.kind == "error":
            error_text = str(chunk.body.get("message") or "")
    return final_text, error_text


def make_broadcasting_handler(crew: Any, connections: Any) -> Callable[[Envelope], AsyncIterator[ResponseChunk]]:
    """把 ``crew.dispatch`` 包一层「边消费边广播」的 handler，供所有平台 channel 装配时统一使用。

    渠道 adapter 照常 ``async for chunk in handler(envelope)`` 消费取 final 回包外部平台，
    中间帧已在此透明广播给桌面端该会话的 WS 观察者（owner 自动取会话真实 owner = 绑定桌面账号）。
    新增渠道无需任何改动即获得实时渲染。
    """

    def _handler(envelope: Envelope) -> AsyncIterator[ResponseChunk]:
        # 渠道 owner 待定（取 dispatch 改写后的 envelope.user_id=binder），故 _broadcast_stream 在首帧确定 owner
        # 后才 clear_buffer。当前 current_push_fn 的旁路推送（会话标题等）都在对话结束后、
        # 不早于首帧。若增加首帧前的旁路事件，必须先解析 channel binding 确定 owner，
        # 并在 dispatch 之前清理对应缓冲区。
        return _broadcast_stream(crew, connections, envelope)

    return _handler
