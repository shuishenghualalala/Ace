"""One explicitly requested Goal step on the foreground conversation dispatcher."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, TypeVar

from crew.core.envelope import Envelope, TurnOutcome
from crew.features.outbound import format_outbound_payload
from crew.gateway.dispatcher import ForegroundAdmission
from crew.state.logging import get_logger
from crew.state.session_goals import GoalSnapshot, GoalStepConflict, ReservedGoalStep, SessionGoalStore, StepTerminal

if TYPE_CHECKING:
    from crew.app import CrewApp
    from crew.gateway.connections import ConnectionManager

log = get_logger("gateway.goal_steps")
STEP_LIMIT_SECONDS = 120.0
STEP_QUERY = "推进已保存的会话目标。"
_T = TypeVar("_T")


class GoalSteps:
    def __init__(self, crew: CrewApp, connections: ConnectionManager, goals: SessionGoalStore):
        self._crew = crew
        self._dispatcher = crew.dispatcher
        self._connections = connections
        self._goals = goals
        self._closing = False
        self._tasks: set[asyncio.Task] = set()

    def _start(self, operation: Coroutine[Any, Any, _T], *, name: str) -> asyncio.Task[_T]:
        task = asyncio.create_task(operation, name=name)
        self._tasks.add(task)

        def finished(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()  # Ingress may have disconnected after durable acceptance.

        task.add_done_callback(finished)
        return task

    async def recover(self) -> None:
        for request_id in await asyncio.to_thread(self._goals.recover_steps):
            log.info("goal_step_interrupted entry_point=http_goal request_id=%s code=gateway_restarted", request_id)

    async def advance(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        dispatcher = self._dispatcher
        epoch = dispatcher.owner_epoch(owner)
        blocker = dispatcher.foreground_blocker(session_id, owner)
        if self._closing or blocker:
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict(blocker or "goal_gateway_draining", "当前会话或网关仍有任务，暂不推进目标")
        # The receipt and scheduling handoff outlive a cancelled HTTP response.
        return await asyncio.shield(self._start(
            self._reserve(owner, session_id, version, epoch), name="goal-reservation",
        ))

    async def _reserve(self, owner: str, session_id: str, version: str, epoch: int) -> GoalSnapshot | None:
        if await self._crew.tasks.active_stop_targets(session_id, owner):
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict("goal_session_busy", "会话还有独立任务在运行，暂不推进目标")
        step = await asyncio.to_thread(
            self._goals.reserve_step, owner, session_id, version, self._crew.config.agent_executor,
        )
        if step is None:
            return None
        if self._closing or self._dispatcher.closed:
            await asyncio.to_thread(self._goals.finish_step, step, "interrupted", "gateway_shutdown")
            return await asyncio.to_thread(self._goals.read, owner, session_id, step.incarnation)
        log.info("goal_step_accepted entry_point=http_goal request_id=%s", step.request_id)
        self._start(self._run(step, epoch), name=step.request_id)
        return step.snapshot

    async def pause(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        # Keep the durable pause and exact interruption in the same owned handoff.
        return await asyncio.shield(self._start(self._pause(owner, session_id, version), name="goal-pause"))

    async def _pause(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        snapshot = await asyncio.to_thread(self._goals.apply, owner, session_id, "pause", version)
        if snapshot is not None and snapshot.run is not None and snapshot.run.state == "stopping":
            await self._dispatcher.stop_request(session_id, snapshot.run.request_id, owner_account_id=owner)
        return snapshot

    async def _run(self, step: ReservedGoalStep, epoch: int) -> None:
        sid, rid, owner = step.session_id, step.request_id, step.owner
        started_at = time.monotonic()
        claimed = False

        async def claim() -> str | None:
            nonlocal claimed
            manager = self._crew.plan_manager
            if manager is not None and manager.is_active(sid, owner_account_id=owner):
                return "goal_plan_active"
            if await self._crew.tasks.active_stop_targets(sid, owner):
                return "goal_session_busy"
            try:
                await asyncio.to_thread(self._goals.claim_step, step, self._crew.config.agent_executor)
            except GoalStepConflict as exc:
                return exc.code
            claimed = True
            log.info("goal_step_claimed entry_point=http_goal request_id=%s", rid)
            return None

        envelope = Envelope.of(
            STEP_QUERY, session_id=sid, request_id=rid, channel="web", user_id=owner,
            workspace_id=step.workspace_id, mode="agent", params={"_goal_step": True, "_allow_mcp_install": False},
        )
        deadline = asyncio.get_running_loop().time() + STEP_LIMIT_SECONDS
        admission = ForegroundAdmission(step.incarnation, epoch, claim, deadline)
        terminal: StepTerminal = "interrupted"
        code = "goal_no_terminal"
        bound = asyncio.timeout_at(deadline)
        try:
            async with bound, aclosing(self._crew.dispatch(envelope, foreground=admission)) as stream:
                async for chunk in stream:
                    if chunk.is_final or chunk.kind in {"error", "final"}:
                        outcome = TurnOutcome.from_body(chunk.body.get("outcome"))
                        status = outcome.status if outcome is not None else None
                        error = str(chunk.body.get("code") or "")
                        if status == "completed":
                            terminal, code = "finished", ""
                        elif status == "stopped":
                            terminal, code = "stopped", "goal_step_stopped"
                        else:
                            terminal, code = "failed", error if error.startswith("goal_") else "goal_step_failed"
                        if error == "timeout" or (outcome and outcome.reason in {"timed_out", "deadline_exceeded"}):
                            terminal, code = "failed", "goal_deadline"
                        if error == "session_replaced" or (outcome and outcome.reason == "gateway_shutdown"):
                            terminal, code = "interrupted", "goal_identity_lost"
                    if claimed and self._dispatcher.owner_epoch(owner) == epoch:
                        payload = format_outbound_payload(chunk, session_id=sid, context={"session_id": sid})
                        if payload is not None:
                            # Never clear another request's replay buffer or mistake push I/O for execution.
                            try:
                                await self._connections.push_payload(sid, payload, owner_account_id=owner)
                            except Exception:
                                log.exception("goal_step_push_failed entry_point=http_goal request_id=%s", rid)
        except TimeoutError:
            if terminal != "finished":
                terminal, code = "failed", "goal_deadline"
        except asyncio.CancelledError:
            terminal, code = "interrupted", "goal_worker_cancelled"
            raise
        except Exception:
            terminal, code = "failed", "goal_dispatch_failed"
            log.exception("goal_step_dispatch_failed entry_point=http_goal request_id=%s", rid)
        finally:
            if bound.expired() and terminal != "finished":
                terminal, code = "failed", "goal_deadline"
            try:
                settled = await asyncio.to_thread(self._goals.finish_step, step, terminal, code)
            except Exception:
                log.exception("goal_step_settlement_unconfirmed entry_point=http_goal request_id=%s", rid)
            else:
                if settled:
                    log.info("goal_step_settled entry_point=http_goal request_id=%s state=%s code=%s elapsed_ms=%.1f",
                             rid, terminal, code, (time.monotonic() - started_at) * 1000)

    async def close(self) -> None:
        self._closing = True
        if not self._tasks:
            return
        # The dispatcher owns interruption and protects already-settling writes.
        # Do not re-cancel those writes from the Goal lifecycle.
        await self._dispatcher.shutdown()
        if self._tasks:
            _, pending = await asyncio.wait(tuple(self._tasks), timeout=2.0)
            if pending:
                log.warning("goal_step_shutdown_incomplete entry_point=http_goal count=%d", len(pending))
