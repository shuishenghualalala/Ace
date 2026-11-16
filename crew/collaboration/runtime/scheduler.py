"""Dependency scheduler with bounded concurrency and an explicit executor port."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from crew.collaboration.runtime.store import ExecutionStore


class RejectedResult(ValueError):
    """Keep real but invalid output for audit; it must never count as delivery."""
    def __init__(self, message: str, result: dict) -> None:
        super().__init__(message)
        self.result = result


class Scheduler:
    def __init__(self, store: ExecutionStore, executor: Callable[[dict], Awaitable[dict]], *, concurrency: int = 2,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self.store, self.executor = store, executor
        self.concurrency, self.on_change = concurrency, on_change

    async def run(self, owner: str, task_id: str) -> dict:
        active: set[asyncio.Task] = set()

        def publish() -> None:
            if self.on_change:
                self.on_change(self.store.get(owner, task_id))

        async def execute(package: dict) -> None:
            try:
                result = await self.executor(package)
                if not isinstance(result, dict) or not str(result.get('text') or '').strip() or not result.get('executor'):
                    raise ValueError('执行器未返回可审阅成果或执行者身份')
            except asyncio.CancelledError:
                self.store.finish(owner, task_id, package['attempt_id'], state='unknown', error='调用已中断；外部结果待核实')
                raise
            except RejectedResult as exc:
                self.store.finish(owner, task_id, package['attempt_id'], state='failed',
                                  result=exc.result, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - executor boundary; preserve failure in journal
                self.store.finish(owner, task_id, package['attempt_id'], state='failed', error=str(exc) or type(exc).__name__)
            else:
                # Persistence errors must escape, not be rewritten as model failures.
                self.store.finish(owner, task_id, package['attempt_id'], state='completed', result=result)
            publish()

        try:
            while True:
                self.store.settle(owner, task_id)
                while len(active) < self.concurrency:
                    package = self.store.claim(owner, task_id, concurrency=self.concurrency)
                    if package is None:
                        break
                    active.add(asyncio.create_task(execute(package)))
                    publish()
                if not active:
                    break
                done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for job in done:
                    job.result()
            publish()
            return self.store.get(owner, task_id)
        finally:
            for job in active:
                job.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
