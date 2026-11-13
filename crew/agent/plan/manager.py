"""Plan 模式状态机 + 计划文件读写。

- 计划文件落盘到 Layer 1 系统层 ``.crew/plans/``（蓝图，不是任务产物）。
  每个会话独占一个子目录
  ``plans/<owner>/<session_id>/``，其下放 ``plan_<timestamp>.md``（计划正文，可多份留历史）
  与 ``state.json``（phase + 当前 plan_id 权威指针）。隔离靠目录归属，不靠文件名前缀匹配，
  因此 session_id 长度变化不影响隔离，也不存在前缀截断碰撞导致的跨会话泄漏。
  会话仍存活时旧 plan 文件可留作历史；删会话 / ``PlanModeManager.reset`` 时整目录回收，
  避免 ``.crew/plans/`` 长期堆积。
- ``PlanModeManager`` 的内存状态按 ``(owner_account_id, session_id)`` 隔离。
- Plan phase 使用以下阶段：inactive / active / review / approved /
  revising / rejected / cancelled。计划正文仍只保存在 Markdown 文件里。

状态流转：
    inactive --enter--> active(只读)
    active --submit_review--> review（计划已写盘，等用户确认）
    review --approve--> approved + just_approved(一次性)   # 解锁写工具，开始执行
    review --revise/reject--> revising（继续完善同一份计划）
    active/review/revising --cancel/exit--> cancelled
    review --reject_and_exit--> rejected
"""

from __future__ import annotations

import json
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from crew.core.runctx import current_owner_account_id
from crew.state.home import safe_path_segment
from crew.state.logging import get_logger

from .todo import TodoStore

log = get_logger("agent.plan")
SessionKey = tuple[str, str]
PlanPhase = Literal["inactive", "active", "review", "approved", "revising", "rejected", "cancelled"]
ACTIVE_PHASES: set[str] = {"active", "review", "revising"}
AWAITING_PHASES: set[str] = {"review"}
VALID_PHASES: set[str] = ACTIVE_PHASES | {"inactive", "approved", "rejected", "cancelled"}


# --------------------------------------------------------------------------- #
# 计划文件（镜像 Crew plans.ts）
# --------------------------------------------------------------------------- #
_CURRENT_PLAN_ID_BY_SESSION: dict[SessionKey, str] = {}


def _plan_key(session_id: str, owner_account_id: str | None = None) -> SessionKey:
    owner = current_owner_account_id.get() if owner_account_id is None else owner_account_id
    return owner or "", session_id or "default"


def _plan_dir(session_id: str, owner_account_id: str | None = None) -> Path:
    """某会话的计划文件子目录：plans/<owner>/<session_id>/。

    owner / session_id 整段作为目录名（``safe_path_segment`` 已截到 96 字符并做字符安全化），
    不再做 ``[:16]`` 截断——隔离靠目录归属，不靠文件名前缀匹配，session_id 长度变化无影响。
    """
    owner, sid = _plan_key(session_id, owner_account_id)
    return plans_dir() / safe_path_segment(owner, "legacy") / safe_path_segment(sid, "default")


def _new_plan_id(session_id: str | None = None, owner_account_id: str | None = None) -> str:
    """生成计划文件 id：仅时间戳 + 短随机后缀。

    owner / session_id 已体现在子目录上（见 ``_plan_dir``），文件名不再承载它们，避免前缀
    截断碰撞导致的跨会话泄漏。同一 session 多次创建计划（enter→exit→enter）会产生不同
    文件；会话仍存活时旧文件留在子目录作历史，删会话 / ``reset`` 时整目录回收。
    """
    return f"plan_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{secrets.token_hex(2)}"


def plans_dir() -> Path:
    """计划文件目录（Layer 1：.crew/plans/，蓝图属于 Crew 系统层）。"""
    from crew.state.home import get_crew_home
    return get_crew_home() / "plans"


def clear_plan_dir(session_id: str, owner_account_id: str | None = None) -> bool:
    """删除某会话的整个计划目录 ``plans/<owner>/<session_id>/``（含历史 plan_*.md 与 state.json）。

    仅允许删除 ``plans_dir()`` 下的会话子目录，防止路径逃逸。目录不存在视为已清理。
    返回是否实际删除了目录。
    """
    directory = _plan_dir(session_id, owner_account_id).resolve()
    root = plans_dir().resolve()
    try:
        directory.relative_to(root)
    except ValueError:
        log.warning("拒绝清理非 plans 目录: %s", directory)
        return False
    if directory == root:
        log.warning("拒绝清理 plans 根目录")
        return False
    if not directory.exists():
        return False
    try:
        shutil.rmtree(directory)
    except Exception as exc:  # noqa: BLE001
        log.warning("计划目录删除失败 %s: %s", directory, exc)
        return False
    # 若 owner 子目录已空，顺手收掉空壳，避免长期堆积空目录
    owner_dir = directory.parent
    try:
        if owner_dir.is_dir() and owner_dir != root and not any(owner_dir.iterdir()):
            owner_dir.rmdir()
    except OSError:
        pass
    return True


def _existing_plan_id(session_id: str, owner_account_id: str | None = None) -> str | None:
    """从磁盘找回本 session 当前 plan id（仅用于进程重启后恢复）。

    权威来源是子目录下 ``state.json`` 的 ``plan_id`` 字段；state 缺失或无 plan_id 时，
    回退到本 session 子目录内 glob 最新 ``plan_*.md``（目录已隔离，前缀碰撞只影响自己）。
    """
    directory = _plan_dir(session_id, owner_account_id)
    state_file = directory / "state.json"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            plan_id = str(data.get("plan_id") or "").strip()
            if plan_id:
                return plan_id
        except Exception as exc:  # noqa: BLE001
            log.warning("plan state 读取失败 %s: %s", state_file, exc)
    if not directory.is_dir():
        return None
    files = sorted(f for f in directory.glob("plan_*.md") if f.is_file())
    return files[-1].stem if files else None


def current_plan_id(
    session_id: str,
    *,
    create: bool = True,
    owner_account_id: str | None = None,
) -> str | None:
    """返回当前计划 id；无内存态时从磁盘恢复，必要时创建新 id。"""
    key = _plan_key(session_id, owner_account_id)
    plan_id = _CURRENT_PLAN_ID_BY_SESSION.get(key)
    if plan_id:
        return plan_id
    plan_id = _existing_plan_id(key[1], owner_account_id=key[0])
    if plan_id:
        _CURRENT_PLAN_ID_BY_SESSION[key] = plan_id
        return plan_id
    if not create:
        return None
    plan_id = _new_plan_id(key[1], owner_account_id=key[0])
    _CURRENT_PLAN_ID_BY_SESSION[key] = plan_id
    return plan_id


def start_new_plan(session_id: str, owner_account_id: str | None = None) -> str:
    """为本 session 开启一个新的计划文件 id（/plan 命令或 enter_plan_mode 时调用）。"""
    key = _plan_key(session_id, owner_account_id)
    plan_id = _new_plan_id(key[1], owner_account_id=key[0])
    _CURRENT_PLAN_ID_BY_SESSION[key] = plan_id
    return plan_id


def plan_path(session_id: str, owner_account_id: str | None = None) -> Path:
    """某会话的计划文件绝对路径（模型用此路径写入）。"""
    plan_id = current_plan_id(session_id, create=True, owner_account_id=owner_account_id) or _new_plan_id(
        session_id,
        owner_account_id=owner_account_id,
    )
    return _plan_dir(session_id, owner_account_id) / f"{plan_id}.md"


def plan_display_path(session_id: str, owner_account_id: str | None = None) -> str:
    """返回给模型的计划文件路径（绝对路径，确保模型 file_write 时能正确定位）。"""
    return str(plan_path(session_id, owner_account_id=owner_account_id))


def read_plan(session_id: str, owner_account_id: str | None = None) -> str | None:
    """读取计划内容；不存在或为空返回 None。"""
    plan_id = current_plan_id(session_id, create=False, owner_account_id=owner_account_id)
    if not plan_id:
        return None
    p = _plan_dir(session_id, owner_account_id) / f"{plan_id}.md"
    if not p.is_file():
        return None
    try:
        text = p.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001
        log.warning("读取计划文件失败 %s: %s", p, exc)
        return None
    return text or None


def write_plan(session_id: str, text: str, owner_account_id: str | None = None) -> Path:
    """写入计划内容（覆盖），返回路径。"""
    p = plan_path(session_id, owner_account_id=owner_account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
class PlanModeManager:
    """对话级 plan 模式状态机；内存态按 ``(owner_account_id, session_id)`` 隔离。"""

    def __init__(self, session_store=None) -> None:
        self._phase: dict[SessionKey, PlanPhase] = {}
        self._just_approved: set[SessionKey] = set()
        self._todos: dict[SessionKey, TodoStore] = {}
        self._todo_reminder_pending: set[SessionKey] = set()
        self._plan_exit_attachment_pending: set[SessionKey] = set()
        self._plan_reentry_attachment_pending: set[SessionKey] = set()
        self._file_changes: dict[SessionKey, list] = {}
        # 本轮（当前 request）文件改动摘要缓冲；persist 时 drain 到 assistant.turn_file_changes。
        # 与 _file_changes（会话累计、含 diff）分离，避免把大 diff 写入历史 JSON。
        self._turn_file_changes: dict[SessionKey, list] = {}
        # exit_plan_mode 登记的「待推送 review」信号：
        # {key: {"plan": str|None, "empty": bool, "phase": str, "status": str}}
        # 瞬时一次性，由 ws.py 在本轮 dispatch 结束后 take_pending_review 消费；不持久化。
        self._pending_review: dict[SessionKey, dict] = {}
        # 已从磁盘恢复过的 session key，避免每次 is_active 都读盘。
        self._loaded: set[SessionKey] = set()
        # 可选：传入 session_store 后，todo_store 首次创建会从历史 hydrate（见下）。
        self._session_store = session_store

    @staticmethod
    def _key(session_id: str, owner_account_id: str | None = None) -> SessionKey:
        owner = current_owner_account_id.get() if owner_account_id is None else owner_account_id
        return owner or "", session_id

    # ---- 状态持久化（phase 落盘，重启后恢复）----
    def _state_path(self, key: SessionKey) -> Path:
        """某会话的 plan 状态文件路径：plans/<owner>/<sid>/state.json。"""
        return _plan_dir(key[1], key[0]) / "state.json"

    def _persist(self, key: SessionKey) -> None:
        """把 phase 落盘。just_approved / todo_reminder 是瞬时态，不持久化。"""
        try:
            p = self._state_path(key)
            p.parent.mkdir(parents=True, exist_ok=True)
            plan_id = current_plan_id(key[1], create=False, owner_account_id=key[0])
            phase = self._phase_of_key(key)
            data = {
                "phase": phase,
                # 兼容旧前端/测试/人工排查字段；权威字段是 phase。
                "active": phase in ACTIVE_PHASES,
                "awaiting": phase in AWAITING_PHASES,
                "plan_id": plan_id,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            p.write_text(json.dumps(data), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — 持久化失败不得阻断主流程
            log.warning("plan 状态持久化失败 %s: %s", key, exc)

    def _restore(self, key: SessionKey) -> None:
        """惰性恢复：首次查询某会话状态时从磁盘读回 phase，兼容旧 active / awaiting。"""
        if key in self._loaded:
            return
        self._loaded.add(key)
        try:
            p = self._state_path(key)
            if not p.is_file():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
            phase = str(data.get("phase") or "").strip().lower()
            if phase not in VALID_PHASES:
                if data.get("awaiting"):
                    phase = "review"
                elif data.get("active"):
                    phase = "active"
                else:
                    phase = "inactive"
            self._phase[key] = phase  # type: ignore[assignment]
            plan_id = str(data.get("plan_id") or "").strip()
            if plan_id:
                _CURRENT_PLAN_ID_BY_SESSION[key] = plan_id
            # 旧 state.json 可能含 options 字段，直接忽略即可。
        except Exception as exc:  # noqa: BLE001 — 恢复失败回退到默认非 plan 态
            log.warning("plan 状态恢复失败 %s: %s", key, exc)

    def _phase_of_key(self, key: SessionKey) -> PlanPhase:
        return self._phase.get(key, "inactive")

    def phase(self, session_id: str, owner_account_id: str | None = None) -> PlanPhase:
        """返回当前 Plan 阶段，供 API/UI 用明确状态恢复卡片。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        return self._phase_of_key(key)

    def _set_phase(self, key: SessionKey, phase: PlanPhase) -> None:
        if phase == "inactive":
            self._phase.pop(key, None)
        else:
            self._phase[key] = phase

    # ---- 进入 / 查询 ----
    def enter(self, session_id: str, owner_account_id: str | None = None) -> None:
        """进入 plan 模式（CLI /plan 或模型调用 enter_plan_mode）。

        幂等语义：若会话已处于 Plan（active/review/revising），保持现有计划文件，
        避免「继续修改」时丢失正文。

        若上一轮已批准 / 拒绝 / 取消（或 inactive），再次 enter 视为**新计划周期**：
        新建 plan_*.md，不把已落地的旧计划重新拉回可编辑/待审批态。
        """
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        # 清上轮可能滞留的待推送 review 信号（防 dispatch 异常未走到 ws 推送段时残留）
        self._pending_review.pop(key, None)
        prev = self._phase_of_key(key)
        if prev in ACTIVE_PHASES:
            self._set_phase(key, "active")
            self._persist(key)
            log.info("已在 plan 模式，保持现有计划 session=%s", session_id)
            return
        # approved / rejected / cancelled / inactive：开启新计划文件，旧文件留作历史。
        if prev in {"approved", "rejected", "cancelled"} or not read_plan(
            session_id, owner_account_id=key[0]
        ):
            start_new_plan(session_id, owner_account_id=key[0])
        else:
            # inactive 但磁盘上仍有未闭环计划（进程中断等）：复用并提示 reentry。
            self._plan_reentry_attachment_pending.add(key)
        self._set_phase(key, "active")
        self._persist(key)
        log.info("进入 plan 模式 session=%s prev=%s", session_id, prev)

    def is_active(self, session_id: str, owner_account_id: str | None = None) -> bool:
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        return self._phase_of_key(key) in ACTIVE_PHASES

    def is_awaiting_approval(self, session_id: str, owner_account_id: str | None = None) -> bool:
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        return self._phase_of_key(key) in AWAITING_PHASES

    # ---- 退出 / 审批 ----
    def request_approval(self, session_id: str, owner_account_id: str | None = None) -> None:
        """模型调用 exit_plan_mode：计划已写盘，等待用户在 CLI 确认。仍保持只读。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        self._set_phase(key, "review")
        self._persist(key)
        log.info("plan 等待审批 session=%s", session_id)

    def submit_review(
        self,
        session_id: str,
        owner_account_id: str | None = None,
    ) -> None:
        """exit_plan_mode 的统一入口：根据 plan 文件是否为空登记「待推送 review」。

        - plan 非空 → 兜底落盘（write_plan 幂等）+ awaiting + {"plan": text, "empty": False}，
          前端弹正常审批卡。
        - plan 为空 → 不 awaiting + {"plan": None, "empty": True}，前端弹「计划为空」提示卡
          （无审批按钮），倒逼模型先 file_write 落盘再调 exit_plan_mode。

        取代 ``is_awaiting_approval`` 作为 ws.py 推送 plan_review 帧的唯一触发条件，
        确保「模型调了 exit_plan_mode 就一定有卡片」，修复「看不到审批卡片」bug。
        """
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        plan = read_plan(session_id, owner_account_id=key[0])
        if plan:
            write_plan(session_id, plan, owner_account_id=key[0])  # B2 兜底落盘（幂等）
            self._set_phase(key, "review")
            self._pending_review[key] = {
                "plan": plan,
                "empty": False,
                "phase": "review",
                "status": "pending",
            }
        else:
            self._set_phase(key, "active")
            self._pending_review[key] = {
                "plan": None,
                "empty": True,
                "phase": "active",
                "status": "empty",
            }
        self._persist(key)
        log.info("plan 提交 review session=%s empty=%s", session_id, not plan)

    def take_pending_review(self, session_id: str, owner_account_id: str | None = None) -> dict | None:
        """ws.py 在本轮 dispatch 结束后消费一次（pop，幂等防重复推）。"""
        return self._pending_review.pop(self._key(session_id, owner_account_id), None)

    def update_plan(
        self,
        session_id: str,
        text: str,
        owner_account_id: str | None = None,
    ) -> dict:
        """用户在看板手改计划正文：写盘并返回供前端刷新的 review 载荷。

        仅在 plan 激活（active / review / revising）时允许。待审批时保持 review，
        并刷新 ``_pending_review``，避免后续 take 读到旧正文。
        """
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        phase = self._phase_of_key(key)
        if phase not in ACTIVE_PHASES:
            raise ValueError("plan inactive")
        plan_text = text if isinstance(text, str) else str(text or "")
        write_plan(session_id, plan_text, owner_account_id=key[0])
        empty = not plan_text.strip()
        if phase == "review":
            status = "empty" if empty else "pending"
            self._pending_review[key] = {
                "plan": None if empty else plan_text,
                "empty": empty,
                "phase": "review" if not empty else "active",
                "status": status,
            }
            # 清空正文时退回 active，避免空计划仍可被批准。
            if empty:
                self._set_phase(key, "active")
        elif phase == "revising":
            status = "revising"
        else:
            status = "editing"
        self._persist(key)
        log.info("plan 用户手改 session=%s empty=%s phase=%s", session_id, empty, self._phase_of_key(key))
        return {
            "plan": plan_text,
            "empty": empty,
            "phase": self._phase_of_key(key),
            "status": status,
        }

    def approve(self, session_id: str, owner_account_id: str | None = None) -> None:
        """用户批准：退出 plan 模式（解锁写工具），并打一次性 just_approved 标记。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        self._set_phase(key, "approved")
        self._just_approved.add(key)
        self._todo_reminder_pending.add(key)
        self._plan_exit_attachment_pending.add(key)
        self._pending_review.pop(key, None)
        self._persist(key)
        log.info("plan 已批准 session=%s", session_id)

    def revise(self, session_id: str, owner_account_id: str | None = None) -> None:
        """用户要求修订：离开 review，留在 plan 模式继续完善同一份计划。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        self._set_phase(key, "revising")
        self._plan_exit_attachment_pending.discard(key)
        self._plan_reentry_attachment_pending.discard(key)
        self._pending_review.pop(key, None)
        self._persist(key)
        log.info("plan 进入修订 session=%s", session_id)

    def reject(self, session_id: str, owner_account_id: str | None = None) -> None:
        """兼容旧入口：前端「继续修改」仍走 plan_reject，内部语义是 revise。"""
        self.revise(session_id, owner_account_id=owner_account_id)

    def reject_and_exit(self, session_id: str, owner_account_id: str | None = None) -> None:
        """用户拒绝并退出：不执行计划，保留 rejected 阶段用于审计。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        self._set_phase(key, "rejected")
        self._just_approved.discard(key)
        self._plan_exit_attachment_pending.add(key)
        self._pending_review.pop(key, None)
        self._persist(key)
        log.info("plan 被拒绝并退出 session=%s", session_id)

    def cancel(self, session_id: str, owner_account_id: str | None = None) -> None:
        """用户取消 Plan 模式：不执行计划，保留 cancelled 阶段用于审计。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        self._set_phase(key, "cancelled")
        self._just_approved.discard(key)
        self._plan_exit_attachment_pending.add(key)
        self._pending_review.pop(key, None)
        self._persist(key)
        log.info("plan 已取消 session=%s", session_id)

    def exit(self, session_id: str, owner_account_id: str | None = None) -> None:
        """兼容旧入口：用户主动退出 plan 模式等价于 cancel。"""
        self.cancel(session_id, owner_account_id=owner_account_id)

    def take_just_approved(self, session_id: str, owner_account_id: str | None = None) -> bool:
        """一次性消费 just_approved 标记（用于下一轮注入「开始执行」提示）。"""
        key = self._key(session_id, owner_account_id)
        if key in self._just_approved:
            self._just_approved.discard(key)
            return True
        return False

    def reset(self, session_id: str, owner_account_id: str | None = None) -> None:
        """彻底清除某会话的 plan 状态与磁盘目录（删会话 / CLI /new）。

        除内存态外，整目录删除 ``plans/<owner>/<sid>/``（含历史 ``plan_*.md`` 与
        ``state.json``），避免删会话后计划文件长期占盘。
        """
        key = self._key(session_id, owner_account_id)
        self._phase.pop(key, None)
        self._just_approved.discard(key)
        self._todo_reminder_pending.discard(key)
        self._plan_exit_attachment_pending.discard(key)
        self._plan_reentry_attachment_pending.discard(key)
        self._pending_review.pop(key, None)
        self._todos.pop(key, None)
        self._file_changes.pop(key, None)
        self._turn_file_changes.pop(key, None)
        _CURRENT_PLAN_ID_BY_SESSION.pop(key, None)
        self._loaded.discard(key)
        clear_plan_dir(key[1], owner_account_id=key[0])

    def take_plan_exit_attachment(self, session_id: str, owner_account_id: str | None = None) -> bool:
        """一次性消费 plan_mode_exit attachment 标记。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        if key not in self._plan_exit_attachment_pending:
            return False
        self._plan_exit_attachment_pending.discard(key)
        return True

    def take_plan_reentry_attachment(self, session_id: str, owner_account_id: str | None = None) -> bool:
        """一次性消费 plan_mode_reentry attachment 标记。"""
        key = self._key(session_id, owner_account_id)
        self._restore(key)
        if key not in self._plan_reentry_attachment_pending:
            return False
        self._plan_reentry_attachment_pending.discard(key)
        return True

    # ---- Todo ----
    def mark_todo_used(self, session_id: str, owner_account_id: str | None = None) -> None:
        """记录本会话已主动维护 todo，清除待注入的内部提醒。"""
        self._todo_reminder_pending.discard(self._key(session_id, owner_account_id))

    def take_todo_reminder(self, session_id: str, owner_account_id: str | None = None) -> str | None:
        """一次性取出内部 todo_reminder 文案；只给模型看，不发前端。"""
        key = self._key(session_id, owner_account_id)
        if key not in self._todo_reminder_pending:
            return None
        self._todo_reminder_pending.discard(key)
        from .prompts import TODO_REMINDER

        return TODO_REMINDER

    def peek_todo_reminder(self, session_id: str, owner_account_id: str | None = None) -> str | None:
        """只读查看待注入 reminder，供 request preview 使用，不消费瞬时状态。"""
        key = self._key(session_id, owner_account_id)
        if key not in self._todo_reminder_pending:
            return None
        from .prompts import TODO_REMINDER

        return TODO_REMINDER

    def todo_store(self, session_id: str, owner_account_id: str | None = None) -> TodoStore:
        """取（或惰性创建）某会话的 TodoStore。

        首次创建时若提供了 session_store，从对话历史恢复最后一次 todo 快照
        （采用 ``_hydrate_todo_store``），解决 gateway 重启 / resume 后丢进度。
        """
        key = self._key(session_id, owner_account_id)
        store = self._todos.get(key)
        if store is None:
            store = TodoStore()
            if self._session_store is not None:
                try:
                    todos = self.hydrate_from_history(self._session_store.load(session_id, owner_account_id=key[0]))
                    if todos:
                        store.write(todos)
                        log.info("todo hydrate 恢复 %d 项 session=%s", len(todos), session_id)
                except Exception as exc:  # noqa: BLE001 — hydrate 失败不得阻断主流程
                    log.warning("todo hydrate 失败 session=%s: %s", session_id, exc)
            self._todos[key] = store
        return store

    def file_change_store(self, session_id: str, owner_account_id: str | None = None) -> list:
        """取（或惰性创建）某会话的文件改动清单（file_write diff 累积于此）。"""
        key = self._key(session_id, owner_account_id)
        store = self._file_changes.get(key)
        if store is None:
            store = []
            self._file_changes[key] = store
        return store

    def record_turn_file_change(
        self,
        session_id: str,
        change: dict,
        owner_account_id: str | None = None,
    ) -> None:
        """记录本轮一次文件改动摘要（按 path 覆盖；不含 diff 正文）。"""
        key = self._key(session_id, owner_account_id)
        path = change.get("path")
        if not path:
            return
        summary = {
            "path": str(path),
            "name": str(change.get("name") or Path(str(path)).name or path),
            "added": int(change.get("added") or 0),
            "removed": int(change.get("removed") or 0),
            "status": str(change.get("status") or "modified"),
        }
        if change.get("binary"):
            summary["binary"] = True
        if change.get("created_in_session"):
            summary["created_in_session"] = True
        store = self._turn_file_changes.get(key)
        if store is None:
            store = []
            self._turn_file_changes[key] = store
        prev = next((c for c in store if c.get("path") == summary["path"]), None)
        if prev and prev.get("created_in_session"):
            summary["created_in_session"] = True
        if summary.get("status") == "added":
            summary["created_in_session"] = True
        store[:] = [c for c in store if c.get("path") != summary["path"]]
        store.append(summary)

    @staticmethod
    def _resolve_change_path(path: str) -> Path:
        """相对路径按 agent workdir 绝对化，与 tool_runner 写盘口径一致。"""
        from crew.core.runctx import current_agent_workdir

        target = Path(path).expanduser()
        if not target.is_absolute():
            cwd = current_agent_workdir.get()
            if cwd:
                target = Path(cwd).expanduser() / target
        return target

    @staticmethod
    def _reconcile_change_entry(change: dict) -> dict | None:
        """按磁盘现状对账单条改动。

        - 文件已不存在且本会话新建（status=added 或 created_in_session）→ 返回 None
          （临时脚本写了又删，不落盘、不展示）
        - 文件已不存在且原为对已有文件的 modified → status=deleted（保留展示）
        - 文件仍在 → 原样返回
        """
        path = str(change.get("path") or "").strip()
        if not path:
            return None
        try:
            exists = PlanModeManager._resolve_change_path(path).is_file()
        except Exception:  # noqa: BLE001 — 路径异常按不存在处理
            exists = False
        status = str(change.get("status") or "modified")
        created_in_session = bool(change.get("created_in_session")) or status == "added"
        if not exists:
            if created_in_session:
                return None
            out = dict(change)
            out["status"] = "deleted"
            return out
        return change

    def reconcile_file_changes(
        self,
        session_id: str,
        owner_account_id: str | None = None,
    ) -> list:
        """回合结束前按磁盘对账累计 store 与本轮摘要。

        返回对账后的累计 ``file_change_store`` 列表（含 diff），供再广播一帧
        ``file_changes``；本轮摘要同步剔除「新建又删」路径，供落库干净。
        """
        key = self._key(session_id, owner_account_id)
        store = self._file_changes.get(key)
        if store is not None:
            reconciled: list = []
            for item in store:
                kept = self._reconcile_change_entry(item if isinstance(item, dict) else {})
                if kept is not None:
                    reconciled.append(kept)
            store[:] = reconciled
        turn = self._turn_file_changes.get(key)
        if turn is not None:
            reconciled_turn: list = []
            for item in turn:
                kept = self._reconcile_change_entry(item if isinstance(item, dict) else {})
                if kept is not None:
                    reconciled_turn.append(kept)
            turn[:] = reconciled_turn
        return list(self._file_changes.get(key) or [])

    def has_file_change_records(
        self,
        session_id: str,
        owner_account_id: str | None = None,
    ) -> bool:
        """本会话是否已有累计或本轮文件改动记录（用于 final 前是否需要广播对账帧）。"""
        key = self._key(session_id, owner_account_id)
        return bool(self._file_changes.get(key) or self._turn_file_changes.get(key))

    def drain_turn_file_changes(
        self,
        session_id: str,
        owner_account_id: str | None = None,
    ) -> list:
        """取出并清空本轮文件改动摘要，供落库到 assistant.turn_file_changes。"""
        key = self._key(session_id, owner_account_id)
        store = self._turn_file_changes.pop(key, None)
        return list(store) if store else []

    @staticmethod
    def hydrate_from_history(messages) -> list | None:
        """从对话历史恢复最后一次 todo 快照。

        倒序扫 ``role == "tool" and name == "todo"`` 的最后一条，parse content JSON
        取 ``todos``。历史存于 session_store._dump（整列 JSON，含 name/tool_calls）。
        """
        for m in reversed(messages):
            if getattr(m, "role", "") == "tool" and getattr(m, "name", "") == "todo":
                try:
                    data = json.loads(getattr(m, "content", "") or "")
                    todos = data.get("todos")
                    if isinstance(todos, list):
                        return todos
                except Exception:  # noqa: BLE001 — 损坏的旧记录跳过，继续往前找
                    continue
        return None
