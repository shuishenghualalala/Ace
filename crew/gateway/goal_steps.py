"""One explicitly requested Goal step on the foreground conversation dispatcher."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Coroutine
from contextlib import aclosing
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING, Any, TypeVar

from crew.core.envelope import Envelope, TurnOutcome
from crew.features.outbound import format_outbound_payload
from crew.gateway.dispatcher import ForegroundAdmission
from crew.gateway.goal_verification import VerificationVerdict, verify_goal
from crew.state.logging import get_logger
from crew.state.session_goals import (
    ContinuationTerminal,
    GoalSnapshot,
    GoalStepConflict,
    GoalVersionConflict,
    ReservedGoalContinuation,
    ReservedGoalStep,
    ReservedGoalVerification,
    SessionGoalStore,
    StepTerminal,
)

if TYPE_CHECKING:
    from crew.app import CrewApp
    from crew.gateway.connections import ConnectionManager

log = get_logger("gateway.goal_steps")
STEP_LIMIT_SECONDS = 120.0
STEP_QUERY = "推进已保存的会话目标。"
_T = TypeVar("_T")


@dataclass
class _Continuation:
    request_id: str
    cancelled: Event
    task: asyncio.Task | None
    step_request_id: str = ""
    verification_request_id: str = ""


class GoalSteps:
    def __init__(self, crew: CrewApp, connections: ConnectionManager, goals: SessionGoalStore):
        self._crew = crew
        self._dispatcher = crew.dispatcher
        self._connections = connections
        self._goals = goals
        self._closing = False
        self._tasks: set[asyncio.Task] = set()
        self._verification_worker: tuple[str, Event, asyncio.Task[GoalSnapshot | None]] | None = None
        self._verification_call: tuple[ReservedGoalVerification, asyncio.Task[VerificationVerdict]] | None = None
        self._continues: dict[tuple[str, str], _Continuation] = {}

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
        for request_id in await asyncio.to_thread(self._goals.recover_continuations):
            log.info("goal_continuation_interrupted entry_point=http_goal_continue request_id=%s code=gateway_restarted",
                     request_id)
        for request_id in await asyncio.to_thread(self._goals.recover_steps):
            log.info("goal_step_interrupted entry_point=http_goal request_id=%s code=gateway_restarted", request_id)
        for request_id in await asyncio.to_thread(self._goals.recover_verifications):
            log.info("goal_verification_interrupted entry_point=http_goal_verify request_id=%s code=gateway_restarted", request_id)

    async def verify(
        self, owner: str, session_id: str, version: str, *, respond_async: bool = False,
    ) -> GoalSnapshot | None:
        blocker = self._dispatcher.foreground_blocker(session_id, owner)
        previous = self._verification_worker
        if previous is not None and not previous[2].done():
            blocker = "goal_verifier_busy"
        if self._closing or blocker:
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict(blocker or "goal_gateway_draining", "会话或验收服务正在使用，暂不能验收目标")
        cancelled = Event()
        receipt: asyncio.Future[GoalSnapshot] | None = asyncio.get_running_loop().create_future() if respond_async else None
        worker = self._start(self._verify(owner, session_id, version, self._dispatcher.owner_epoch(owner), cancelled, receipt),
                             name="goal-verification")
        self._verification_worker = (owner, cancelled, worker)
        if receipt is None:
            return await asyncio.shield(worker)
        # A disconnected HTTP waiter cancels neither reservation nor model work.
        # If admission fails, the worker returns/raises before a receipt exists.
        done, _ = await asyncio.wait({worker, receipt}, return_when=asyncio.FIRST_COMPLETED)
        return receipt.result() if receipt in done else worker.result()

    async def stop_verification_for_owner(self, owner: str) -> None:
        active = self._verification_worker
        if active is None or active[0] != owner or active[2].done():
            return
        _, cancelled, worker = active
        cancelled.set()
        if self._verification_call is not None and self._verification_call[0].owner == owner:
            self._verification_call[1].cancel()
        # Pending SQLite writes settle before logout releases the lease, but an
        # admission rejected before creating an operation has nothing to drain.
        try:
            await asyncio.shield(worker)
        except (GoalStepConflict, GoalVersionConflict):
            pass

    async def _verify(
        self, owner: str, session_id: str, version: str, epoch: int, cancelled: Event,
        receipt: asyncio.Future[GoalSnapshot] | None,
    ) -> GoalSnapshot | None:
        if await self._crew.tasks.active_stop_targets(session_id, owner):
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict("goal_session_busy", "会话还有任务未结束，暂不能验收目标")
        executor = self._crew.config.agent_executor
        reserved = await asyncio.to_thread(self._goals.reserve_verification, owner, session_id, version, executor)
        if reserved is None:
            return None
        check, snapshot = reserved
        rid = check.check.request_id
        start = time.monotonic()
        log.info("goal_verification_started entry_point=http_goal_verify request_id=%s step_request_id=%s",
                 rid, check.check.step_request_id)
        if receipt is not None:
            receipt.set_result(snapshot)

        def scope_lost() -> bool:
            return (cancelled.is_set() or self._closing or self._dispatcher.owner_epoch(owner) != epoch
                    or self._dispatcher.owner_is_blocked(owner))

        async def check_plan() -> str:
            manager = self._crew.plan_manager
            try:
                return "goal_plan_active" if manager is not None and await asyncio.to_thread(
                    manager.is_active, session_id, owner_account_id=owner,
                ) else ""
            except Exception:
                return "goal_plan_unconfirmed"

        async def call_verifier() -> VerificationVerdict:
            current = await asyncio.to_thread(self._goals.read, owner, session_id, check.incarnation)
            if scope_lost() or current is None or current.version != check.version:
                return VerificationVerdict("interrupted", "goal_verification_scope_lost")
            return await verify_goal(self._crew, check)

        plan_blocker = await check_plan()
        if scope_lost():
            verdict = VerificationVerdict("interrupted", "goal_verification_scope_lost")
        elif plan_blocker:
            verdict = VerificationVerdict("interrupted", plan_blocker)
        elif self._dispatcher.foreground_blocker(session_id, owner):
            verdict = VerificationVerdict("interrupted", "goal_session_busy")
        else:
            call = asyncio.create_task(call_verifier(), name=rid)
            self._verification_call = (check, call)
            try:
                verdict = await call
            except asyncio.CancelledError:
                verdict = VerificationVerdict("interrupted", "goal_verification_cancelled")
            except Exception:
                verdict = VerificationVerdict("unconfirmed", "goal_verification_failed")
            finally:
                self._verification_call = None
        if verdict.state == "passed" and (plan_blocker := await check_plan()):
            verdict = VerificationVerdict("interrupted", plan_blocker)
        if scope_lost():
            verdict = VerificationVerdict("interrupted", "goal_verification_scope_lost")
        elif self._dispatcher.foreground_blocker(session_id, owner) not in {"", "goal_gateway_busy"}:
            verdict = VerificationVerdict("interrupted", "goal_evidence_changed")
        try:
            result = await asyncio.to_thread(
                self._goals.finish_verification, check, verdict.state, verdict.code, verdict.citations, executor, cancelled.is_set,
            )
        except Exception as exc:
            log.error("goal_verification_settlement_unconfirmed entry_point=http_goal_verify request_id=%s error_type=%s",
                      rid, type(exc).__name__)
            raise
        settled = result.verification if result is not None else None
        own_result = settled is not None and settled.request_id == rid
        log.info("goal_verification_settled entry_point=http_goal_verify request_id=%s state=%s code=%s elapsed_ms=%.1f",
                 rid, settled.state if own_result else "discarded", settled.code if own_result else "goal_changed",
                 (time.monotonic() - start) * 1000)
        return result

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

    async def _reserve(
        self, owner: str, session_id: str, version: str, epoch: int, *,
        wait: bool = False, worker: _Continuation | None = None,
    ) -> GoalSnapshot | None:
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
        if wait:
            if worker is not None:
                worker.step_request_id = step.request_id
            try:
                await self._run(step, epoch)
            finally:
                if worker is not None:
                    worker.step_request_id = ""
            return await asyncio.to_thread(self._goals.read, owner, session_id, step.incarnation)
        self._start(self._run(step, epoch), name=step.request_id)
        return step.snapshot

    async def pause(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        # Keep the durable pause and exact interruption in the same owned handoff.
        return await asyncio.shield(self._start(self._pause(owner, session_id, version), name="goal-pause"))

    async def _pause(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        snapshot = await asyncio.to_thread(self._goals.apply, owner, session_id, "pause", version)
        if snapshot is not None:
            worker = self._continues.get((owner, session_id))
            if worker is not None:
                worker.cancelled.set()
        if snapshot is not None and snapshot.run is not None and snapshot.run.state == "stopping":
            await self._dispatcher.stop_request(session_id, snapshot.run.request_id, owner_account_id=owner)
        if snapshot is not None and snapshot.verification is not None and self._verification_call is not None:
            check, call = self._verification_call
            if (owner, session_id, snapshot.verification.request_id) == (check.owner, check.session_id, check.check.request_id):
                if snapshot.verification.state == "interrupted":
                    call.cancel()
        return snapshot

    async def start_continuation(
        self, owner: str, session_id: str, version: str, budget: int,
    ) -> GoalSnapshot | None:
        if self._closing:
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict("goal_gateway_draining", "网关闭中，暂不能自动推进目标")
        blocker = self._dispatcher.foreground_blocker(session_id, owner)
        if blocker:
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict(blocker, "当前会话或网关仍有任务，暂不自动推进目标")
        if await self._crew.tasks.active_stop_targets(session_id, owner):
            if await asyncio.to_thread(self._goals.read, owner, session_id) is None:
                return None
            raise GoalStepConflict("goal_session_busy", "会话还有独立任务在运行，暂不自动推进目标")
        reserved = await asyncio.to_thread(
            self._goals.start_continuation, owner, session_id, version, budget, self._crew.config.agent_executor,
        )
        if reserved is None:
            return None
        loop, snapshot = reserved
        log.info("goal_continuation_accepted entry_point=http_goal_continue request_id=%s budget=%d",
                 loop.request_id, loop.budget)
        self._start(self._continue_loop(loop), name="goal-continuation")
        return snapshot

    async def stop_continuation(self, owner: str, session_id: str, version: str) -> GoalSnapshot | None:
        async def stop() -> GoalSnapshot | None:
            snapshot = await asyncio.to_thread(self._goals.stop_continuation, owner, session_id, version)
            if snapshot is None:
                return None
            worker = self._continues.get((owner, session_id))
            if worker is not None:
                worker.cancelled.set()
            if snapshot.run is not None and snapshot.run.state == "stopping":
                await self._dispatcher.stop_request(session_id, snapshot.run.request_id, owner_account_id=owner)
            if snapshot.verification is not None and self._verification_call is not None:
                check, call = self._verification_call
                if (owner, session_id, snapshot.verification.request_id) == (
                    check.owner, check.session_id, check.check.request_id,
                ):
                    if snapshot.verification.state == "interrupted":
                        call.cancel()
            return snapshot

        return await asyncio.shield(self._start(stop(), name="goal-continue-stop"))

    async def stop_goal_work_for_owner(self, owner: str) -> None:
        loops = [(key, worker) for key, worker in list(self._continues.items()) if key[0] == owner]
        for (_, session_id), worker in loops:
            worker.cancelled.set()
            if worker.step_request_id:
                try:
                    await self._dispatcher.stop_request(session_id, worker.step_request_id, owner_account_id=owner)
                except Exception:
                    log.exception("goal_continuation_stop_failed owner=%s session=%s", owner, session_id)
        await self.stop_verification_for_owner(owner)
        for (_, session_id), worker in loops:
            if worker.task is None:
                continue
            try:
                await asyncio.shield(worker.task)
            except (GoalStepConflict, GoalVersionConflict):
                pass
            except Exception:
                log.exception("goal_continuation_cleanup_failed owner=%s session=%s", owner, session_id)

    @staticmethod
    def _continuation_conflict(code: str) -> tuple[ContinuationTerminal, str]:
        if code in {"goal_session_busy", "goal_step_active", "goal_verification_active", "goal_continue_active"}:
            return "yielded", "goal_continue_yielded"
        if code == "goal_plan_active":
            return "stopped", code
        if code == "goal_not_active":
            return "stopped", code
        return "failed", code or "goal_continue_failed"

    @staticmethod
    def _continuation_step_code(run: Any) -> tuple[ContinuationTerminal, str]:
        code = str(getattr(run, "code", "") or "")
        if code in {"goal_session_busy", "goal_step_active", "goal_verification_active", "goal_gateway_busy"}:
            return "yielded", "goal_continue_yielded"
        if code == "goal_plan_active":
            return "stopped", code
        if code.startswith("goal_") and code != "goal_no_terminal":
            terminal = {"stopped": "stopped", "failed": "failed", "interrupted": "interrupted"}.get(
                getattr(run, "state", ""), "failed",
            )
            return terminal, code
        return "failed", "goal_continue_step_failed"

    async def _continue_loop(self, loop: ReservedGoalContinuation) -> None:
        owner, sid, loop_id = loop.owner, loop.session_id, loop.request_id
        epoch = self._dispatcher.owner_epoch(owner)
        worker = _Continuation(loop_id, Event(), asyncio.current_task())
        self._continues[(owner, sid)] = worker
        iterations = 0
        last_step = ""
        last_check = ""
        terminal: ContinuationTerminal = "failed"
        code = "goal_continue_failed"
        started_at = time.monotonic()
        try:
            while True:
                if worker.cancelled.is_set():
                    terminal, code = "stopped", "goal_continue_cancelled"
                    break
                if self._closing:
                    terminal, code = "interrupted", "gateway_shutdown"
                    break
                if self._dispatcher.owner_epoch(owner) != epoch or self._dispatcher.owner_is_blocked(owner):
                    terminal, code = "interrupted", "goal_continue_scope_lost"
                    break
                current = await asyncio.to_thread(self._goals.read, owner, sid, loop.incarnation)
                if current is None:
                    terminal, code = "failed", "goal_continue_session_lost"
                    break
                active = current.continuation
                if active is None or active.request_id != loop_id or active.state != "running":
                    terminal, code = "stopped", "goal_continue_cancelled"
                    break
                if current.goal is None or current.goal.status != "active":
                    terminal, code = "stopped", "goal_not_active"
                    break
                if iterations >= loop.budget:
                    terminal, code = "exhausted", "goal_continue_budget"
                    break
                if (self._dispatcher.foreground_blocker(sid, owner)
                        or await self._crew.tasks.active_stop_targets(sid, owner)):
                    terminal, code = "yielded", "goal_continue_yielded"
                    break
                try:
                    stepped = await self._reserve(owner, sid, current.version, epoch, wait=True, worker=worker)
                except GoalVersionConflict:
                    terminal, code = "stopped", "goal_continue_changed"
                    break
                except GoalStepConflict as exc:
                    terminal, code = self._continuation_conflict(exc.code)
                    break
                if stepped is None:
                    terminal, code = "failed", "goal_continue_session_lost"
                    break
                if stepped.run is None or stepped.run.state != "finished":
                    terminal, code = self._continuation_step_code(stepped.run)
                    break
                iterations += 1
                last_step = stepped.run.request_id
                await asyncio.to_thread(
                    self._goals.record_continuation, loop, iterations, last_step, last_check,
                )
                if worker.cancelled.is_set():
                    terminal, code = "stopped", "goal_continue_cancelled"
                    break
                try:
                    checked = await self.verify(owner, sid, stepped.version)
                except GoalVersionConflict:
                    terminal, code = "stopped", "goal_continue_changed"
                    break
                except GoalStepConflict as exc:
                    terminal, code = self._continuation_conflict(exc.code)
                    break
                if checked is None:
                    terminal, code = "failed", "goal_continue_session_lost"
                    break
                check = checked.verification
                if check is None:
                    terminal, code = "failed", "goal_continue_verification_missing"
                    break
                last_check = check.request_id
                await asyncio.to_thread(
                    self._goals.record_continuation, loop, iterations, last_step, last_check,
                )
                if check.state == "passed":
                    terminal, code = "passed", "goal_continue_passed"
                    break
                if check.state == "interrupted":
                    if check.code in {"goal_evidence_changed", "goal_session_busy"}:
                        terminal, code = "yielded", "goal_continue_yielded"
                    else:
                        terminal, code = "interrupted", check.code or "goal_continue_interrupted"
                    break
                if check.state != "incomplete":
                    # The verifier could not judge, so more model work would be guesswork.
                    terminal, code = "stopped", "goal_continue_uncertain"
                    break
                if worker.cancelled.is_set():
                    terminal, code = "stopped", "goal_continue_cancelled"
                    break
        except asyncio.CancelledError:
            terminal, code = "interrupted", "goal_continue_cancelled"
            raise
        except Exception:
            terminal, code = "failed", "goal_continue_failed"
            log.exception("goal_continuation_failed entry_point=http_goal_continue request_id=%s", loop_id)
        finally:
            if self._continues.get((owner, sid)) is worker:
                self._continues.pop((owner, sid), None)
            if self._closing and terminal == "failed":
                terminal, code = "interrupted", "gateway_shutdown"
            try:
                settled = await asyncio.to_thread(
                    self._goals.finish_continuation, loop, terminal, code, iterations, last_step, last_check,
                )
            except Exception:
                log.exception("goal_continuation_settlement_unconfirmed entry_point=http_goal_continue request_id=%s",
                              loop_id)
            else:
                log.info("goal_continuation_settled entry_point=http_goal_continue request_id=%s state=%s code=%s "
                         "iterations=%d elapsed_ms=%.1f", loop_id, terminal, code, iterations,
                         (time.monotonic() - started_at) * 1000)
                if settled is None:
                    log.warning("goal_continuation_snapshot_missing entry_point=http_goal_continue request_id=%s", loop_id)

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
        for worker in self._continues.values():
            worker.cancelled.set()
        if self._verification_worker is not None:
            self._verification_worker[1].set()
        if self._verification_call is not None:
            self._verification_call[1].cancel()
        if not self._tasks:
            return
        # The dispatcher owns interruption and protects already-settling writes.
        # Do not re-cancel those writes from the Goal lifecycle.
        await self._dispatcher.shutdown()
        if self._tasks:
            _, pending = await asyncio.wait(tuple(self._tasks), timeout=2.0)
            if pending:
                log.warning("goal_step_shutdown_incomplete entry_point=http_goal count=%d", len(pending))
