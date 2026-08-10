"""端到端测试共享 helper。"""

from __future__ import annotations

import asyncio

from crew.app import CrewApp
from crew.core.envelope import Envelope, ResponseChunk


async def collect_chunks(app: CrewApp, envelope: Envelope, timeout: float | None = None) -> list[ResponseChunk]:
    """收集 handle 返回的所有 ResponseChunk，可选总超时保护。"""
    chunks: list[ResponseChunk] = []

    async def _consume() -> None:
        async for chunk in app.handle(envelope):
            chunks.append(chunk)

    if timeout is None:
        await _consume()
        return chunks
    try:
        await asyncio.wait_for(_consume(), timeout=timeout)
    except asyncio.TimeoutError:
        # 总超时仍保留已收集的 chunk，方便断言给出诊断信息
        chunks.append(ResponseChunk.error(envelope.request_id, "总超时"))
    return chunks
