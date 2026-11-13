"""事件循环阻塞回归测试：重活必须离开 gateway 的单线程事件循环。

gateway 由 uvicorn 单进程驱动（crew/gateway/app.py），任何同步阻塞调用都会
冻结健康探测 / WebSocket / 流式推送。这里用"心跳间隔"量化阻塞：心跳协程每
20ms 醒一次，若被测路径在循环上同步执行，心跳会被拖出大间隔。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from crew.agent.external import detector
from crew.memory.simple import SQLiteMemory
from crew.core.types import Message


async def _max_heartbeat_gap(during, *, interval: float = 0.02, beats: int = 10) -> float:
    """运行 during 协程的同时跑心跳，返回心跳实际间隔超出 interval 的最大值。"""
    max_gap = 0.0

    async def heartbeat() -> None:
        nonlocal max_gap
        last = time.perf_counter()
        for _ in range(beats):
            await asyncio.sleep(interval)
            now = time.perf_counter()
            max_gap = max(max_gap, now - last - interval)
            last = now

    await asyncio.gather(during, heartbeat())
    return max_gap


@pytest.mark.asyncio
async def test_discover_local_runtimes_does_not_block_loop(monkeypatch):
    def slow_scan(_descriptors):
        time.sleep(0.3)  # 模拟 Windows 上 login shell / --version 探测
        return []

    monkeypatch.setattr(detector, "_scan_descriptors", slow_scan)

    gap = await _max_heartbeat_gap(detector.discover_local_runtimes())
    assert gap < 0.2, f"运行时探测阻塞了事件循环: 心跳被拖延 {gap:.3f}s"


@pytest.mark.asyncio
async def test_memory_write_does_not_block_loop(tmp_path, monkeypatch):
    memory = SQLiteMemory(db_path=str(tmp_path / "memory.db"))
    real_execute = memory._writer.execute

    def slow_execute(fn):
        time.sleep(0.3)  # 模拟 SQLiteWriteHelper 锁冲突时的 sleep 重试
        return real_execute(fn)

    monkeypatch.setattr(memory._writer, "execute", slow_execute)

    gap = await _max_heartbeat_gap(
        memory.write("s1", [Message(role="user", content="你好")])
    )
    memory.close()
    assert gap < 0.2, f"记忆写入阻塞了事件循环: 心跳被拖延 {gap:.3f}s"


@pytest.mark.asyncio
async def test_prompt_build_skills_cold_scan_does_not_block_loop(monkeypatch):
    """skills 冷扫描（cache miss）必须在线程池里，不能卡住 gateway 事件循环。"""
    from crew.agent.prompt_builder import build_prompt_parts
    from crew.agent.skills import _invalidate_cache
    from crew.agent.skills import scanner

    real_scan_all = scanner.scan_all

    def slow_scan_all():
        time.sleep(0.3)  # 模拟 Windows + 杀软下的全目录 stat/解析
        return real_scan_all()

    monkeypatch.setattr(scanner, "scan_all", slow_scan_all)
    _invalidate_cache()

    gap = await _max_heartbeat_gap(
        build_prompt_parts(lightweight=True, inject_skills=True)
    )
    assert gap < 0.2, f"skills 冷扫描阻塞了事件循环: 心跳被拖延 {gap:.3f}s"
