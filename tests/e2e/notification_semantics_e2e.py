"""通知语义全链路 E2E 验证（真实 build_app + SessionDispatcher + 任务运行时）。

与 tests/e2e/_run_case.py 同风格：所有状态（CREW_HOME / db / 工作区 / 日志）都落在
独立 case 目录下，互不污染；全程强制 FakeProvider，绝不触真实模型 Key。

为什么不走 scenarios.yaml：scenarios.yaml 的 runner 经 app.handle 驱动，绕开了
SessionDispatcher，而 agent_turn 运行时任务（backgrounded 标记的载体）恰恰只在
dispatcher 里创建。本脚本改用 app.dispatch —— 即 gateway/cron 真实入口。

验证的两个场景（对应 _should_notify_task / _on_task_completion 语义）：
A. 前台回合：普通对话 turn（agent_turn, backgrounded=False）完成 → 恰好 1 条
   tasks 通知。后端总是如实发布；用户正看着时由前端按可见性静默已读——这正是
   「人在别的对话/别的页签也能收到完成提醒」的来源。
B. 后台任务：backgrounded shell 任务完成（中间任务不单独通知）；恢复回合的
   agent_turn（internal_task_resume, backgrounded=True）完成 → 本会话恰好 1 条
   通知，source="tasks"，kind="task_completed"，body 为恢复回合的最终回复文本。

用法：
    .venv/bin/python tests/e2e/notification_semantics_e2e.py [--case-dir DIR]
退出码：0 全部通过；1 有断言失败。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from crew.app import CrewApp, build_app  # noqa: E402
from crew.core.envelope import Envelope, ResponseChunk  # noqa: E402
from crew.state.config import load_config  # noqa: E402

OWNER = "local"

# 恢复回合是 asyncio.create_task 异步执行的，轮询通知库而非固定 sleep
RESUME_WAIT_DEADLINE_SECONDS = 30.0
# 通知经完成回调 call_soon_threadsafe 异步投递的到达窗口
TURN_NOTIFY_ARRIVAL_SECONDS = 15.0
# 去重确认的静默观察窗口
DEDUP_GRACE_SECONDS = 3.0


def _setup_config(case_dir: Path) -> Any:
    """隔离配置：对齐 _run_case._setup_config，并强制清空一切 LLM Key。"""
    os.environ["CREW_MODEL_PROFILE"] = "default"
    os.environ["CREW_HOME"] = str(case_dir / ".crew")

    cfg = load_config()

    # 开发态 load_config 会读仓库 config/.env（用户真实 Key），必须彻底清掉，
    # 保证 build_provider 回退 FakeProvider、全程不触真实模型。
    key_env_names = {"CREW_API_KEY", "CREW_MODEL_API_KEY"}
    for profile in cfg.model_profiles.values():
        key_env_names.add(str(getattr(profile, "api_key_env", "") or ""))
        profile.api_key = ""
    for name in key_env_names:
        if name:
            os.environ.pop(name, None)
    cfg.api_key = ""
    if not cfg.has_llm_key:
        pass
    else:  # pragma: no cover - 防御：清 Key 失败宁可直接失败也不碰真实模型
        raise RuntimeError("未能清空 LLM API Key，中止（避免误用真实模型）")

    cfg.db_path = str(case_dir / "crew.db")
    cfg.memory_db_path = str(case_dir / "memory.db")
    cfg.crew_home = str(case_dir / ".crew")
    cfg.task_workspace_root = str(case_dir / "task_workspaces")
    cfg.log_file = str(case_dir / "crew.log")
    cfg.log_level = "INFO"
    cfg.llm_trace = True
    cfg.title_auto = False
    cfg.cron_enabled = False
    cfg.plugins_enabled = []
    cfg.mcp_servers = {}
    cfg.security_enabled = False
    cfg.sqlite_wal = True
    cfg.tasks_auto_background_after_seconds = 0.0
    cfg.external_agents_enabled = False
    cfg.external_security_enabled = False
    return cfg


async def _collect_dispatch(
    app: CrewApp,
    envelope: Envelope,
    timeout: float,
) -> list[ResponseChunk]:
    """经 app.dispatch（真实 gateway 路径，过 SessionDispatcher）收集全部帧。"""
    chunks: list[ResponseChunk] = []

    async def _consume() -> None:
        async for chunk in app.dispatch(envelope):
            chunks.append(chunk)

    await asyncio.wait_for(_consume(), timeout=timeout)
    return chunks


async def _wait_until(predicate: Any, deadline: float, interval: float = 0.2) -> bool:
    """在 deadline 秒内轮询 predicate，命中即返回 True。"""
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


def _notifications(app: CrewApp) -> list[Any]:
    return app.notifications.list(OWNER, limit=100)


def _session_notifications(app: CrewApp, sid: str) -> list[Any]:
    return [n for n in _notifications(app) if (n.payload or {}).get("session_id") == sid]


def _dump_notifications(app: CrewApp) -> str:
    lines = []
    for n in _notifications(app):
        lines.append(
            f"    - source={n.source} kind={n.kind} title={n.title!r} "
            f"body={n.body[:80]!r} payload={n.payload}"
        )
    return "\n".join(lines) if lines else "    （空）"


class _Failures:
    """收集断言失败，最后统一输出，避免中途异常丢失现场。"""

    def __init__(self) -> None:
        self.items: list[str] = []

    def check(self, condition: bool, message: str) -> None:
        status = "OK  " if condition else "FAIL"
        print(f"    [{status}] {message}")
        if not condition:
            self.items.append(message)


async def _scenario_a(app: CrewApp, failures: _Failures) -> None:
    """场景 A：前台回合完成 → 恰好 1 条通知。"""
    print("[Scenario A] 前台 agent_turn（backgrounded=False）完成，期望恰好 1 条通知")
    sid = "e2e_notif_foreground"
    app.session_store.ensure_session(sid, workspace_id="default", owner_account_id=OWNER)

    envelope = Envelope.of(
        "请用一句话介绍你自己",
        session_id=sid,
        user_id=OWNER,
        workspace_id="default",
        mode="agent",
    )
    chunks = await _collect_dispatch(app, envelope, timeout=120.0)

    errors = [str(c.body.get("message") or "") for c in chunks if c.kind == "error"]
    failures.check(not errors, f"前台回合无 error 帧（实际: {errors[:3]}）")
    final_text = "".join(str(c.body.get("text") or "") for c in chunks if c.kind == "final")
    failures.check(bool(final_text.strip()), f"前台回合有 final 回复（前 60 字符: {final_text[:60]!r}）")

    #  sanity：确认走的是 dispatcher 的 agent_turn 记账，且 backgrounded=False
    turns = [
        t
        for t in app.tasks.list_tasks(session_id=sid, owner_account_id=OWNER)
        if t["kind"] == "agent_turn"
    ]
    failures.check(len(turns) == 1, f"dispatcher 为前台回合记账了 1 个 agent_turn（实际 {len(turns)}）")
    if turns:
        failures.check(
            turns[0]["backgrounded"] is False,
            f"前台 agent_turn backgrounded=False（实际 {turns[0]['backgrounded']}）",
        )
        failures.check(
            turns[0]["status"] == "completed",
            f"前台 agent_turn 状态 completed（实际 {turns[0]['status']}）",
        )

    # 回合终态 → 恰好 1 条通知：后端如实发布；用户正看着时才由前端按可见性静默已读
    arrived = await _wait_until(
        lambda: len(_session_notifications(app, sid)) >= 1,
        deadline=TURN_NOTIFY_ARRIVAL_SECONDS,
    )
    failures.check(arrived, f"回合结束后 {TURN_NOTIFY_ARRIVAL_SECONDS:.0f}s 内产生了通知")
    items = _session_notifications(app, sid)
    failures.check(len(items) == 1, f"本会话通知恰好 1 条（实际 {len(items)}）")
    if items:
        n = items[0]
        failures.check(n.source == "tasks", f"source == 'tasks'（实际 {n.source!r}）")
        failures.check(n.kind == "task_completed", f"kind == 'task_completed'（实际 {n.kind!r}）")
        failures.check(n.title == "任务已完成", f"title == '任务已完成'（实际 {n.title!r}）")
        failures.check(
            n.body == final_text[:200],
            f"body == 回合 final 回复前 200 字（实际 {n.body[:60]!r}）",
        )
        failures.check(
            (n.payload or {}).get("task_kind") == "agent_turn",
            "通知来自 agent_turn",
        )
    failures.check(
        app.notifications.unread_count(OWNER) >= 1,
        "通知为未读（后端不做已读假设，由前端可见性决定消费方式）",
    )
    print("  通知库内容:")
    print(_dump_notifications(app))


async def _scenario_b(app: CrewApp, failures: _Failures) -> None:
    """场景 B：后台 shell 完成（抑制）→ 恢复回合完成 → 恰好 1 条通知。"""
    print("[Scenario B] 后台 shell 完成 → 恢复回合 agent_turn（backgrounded=True）完成，期望恰好 1 条通知")
    sid = "e2e_notif_background"
    app.session_store.ensure_session(sid, workspace_id="default", owner_account_id=OWNER)

    # 直接驱动任务运行时：backgrounded shell 任务完成，触发 _on_task_completion
    # → will_resume=True（抑制通知）→ 经真实 dispatcher 派发 internal_task_resume 恢复回合
    shell_task = app.tasks.create_runtime(
        kind="shell",
        session_id=sid,
        title="E2E 后台 shell 任务",
        detail="echo hello",
        backgrounded=True,
        owner_account_id=OWNER,
    )
    shell_task_id = shell_task["task_id"]
    app.tasks.mark_running(shell_task_id)
    app.tasks.finish(
        shell_task_id,
        owner_account_id=OWNER,
        status="completed",
        result="E2E 后台命令输出",
    )

    # 恢复回合是异步的：轮询本会话通知直到出现 1 条（或超时）
    arrived = await _wait_until(
        lambda: len(_session_notifications(app, sid)) >= 1,
        deadline=RESUME_WAIT_DEADLINE_SECONDS,
    )
    failures.check(arrived, f"恢复回合在 {RESUME_WAIT_DEADLINE_SECONDS:.0f}s 内产生了通知")

    shell_after = app.tasks.get(shell_task_id, owner_account_id=OWNER)
    failures.check(
        shell_after.get("notified_at") is not None,
        "shell 任务已被 mark_notified（完成回调确实跑过）",
    )
    failures.check(
        shell_after.get("resume_enqueued_at") is not None,
        "shell 任务已入队恢复回合（mark_resume_enqueued）",
    )

    # 恢复回合的 agent_turn：backgrounded=True 且最终 completed
    def _resume_turn() -> dict[str, Any] | None:
        for t in app.tasks.list_tasks(session_id=sid, owner_account_id=OWNER):
            if t["kind"] == "agent_turn" and t["backgrounded"]:
                return t
        return None

    await _wait_until(
        lambda: (t := _resume_turn()) is not None and t["status"] == "completed",
        deadline=RESUME_WAIT_DEADLINE_SECONDS,
    )
    resume_turn = _resume_turn()
    failures.check(resume_turn is not None, "存在 backgrounded=True 的恢复回合 agent_turn")
    if resume_turn is not None:
        failures.check(
            resume_turn["status"] == "completed",
            f"恢复回合 agent_turn 状态 completed（实际 {resume_turn['status']}）",
        )

    # 通知本体断言：本会话恰好 1 条，source/kind/title 正确，body 为恢复回合最终回复
    items = _session_notifications(app, sid)
    failures.check(len(items) == 1, f"本会话通知总数恰好 1 条（实际 {len(items)}）")
    if items:
        n = items[0]
        payload = n.payload or {}
        failures.check(n.source == "tasks", f"source == 'tasks'（实际 {n.source!r}）")
        failures.check(
            n.kind == "task_completed",
            f"kind == 'task_completed'（实际 {n.kind!r}）",
        )
        failures.check(n.title == "任务已完成", f"title 正确（实际 {n.title!r}）")
        failures.check(
            payload.get("task_kind") == "agent_turn",
            f"通知来自恢复回合 agent_turn（实际 task_kind={payload.get('task_kind')!r}）",
        )
        failures.check(
            payload.get("session_id") == sid,
            f"通知归属会话 {sid}（实际 {payload.get('session_id')!r}）",
        )
        if resume_turn is not None:
            expected_body = str(resume_turn.get("result") or "")[:200]
            failures.check(bool(expected_body.strip()), "恢复回合 final 回复非空")
            failures.check(
                n.body == expected_body,
                f"body == 恢复回合最终回复（通知 body={n.body[:60]!r}，回合 result={expected_body[:60]!r}）",
            )
    failures.check(
        app.notifications.unread_count(OWNER) >= len(items),
        "通知均为未读（无人消费）",
    )

    # 去重确认：再等一个宽限窗口，本会话总数必须仍为 1（shell 任务本身不得再发）
    await asyncio.sleep(DEDUP_GRACE_SECONDS)
    total = len(_session_notifications(app, sid))
    failures.check(total == 1, f"宽限 {DEDUP_GRACE_SECONDS:.0f}s 后本会话通知总数仍为 1（实际 {total}）")
    print("  通知库内容:")
    print(_dump_notifications(app))


async def _run(case_dir: Path) -> int:
    failures = _Failures()
    app: CrewApp | None = None
    try:
        cfg = _setup_config(case_dir)

        import crew.state.logging as logging_mod

        logging_mod._CONFIGURED = False
        logging_mod._LLM_TRACE_ENABLED = False

        app = build_app(config=cfg, enable_team=True)
        await app.startup(start_cron=False)

        from crew.core.mocks import FakeProvider

        failures.check(
            isinstance(app.provider, FakeProvider),
            f"Provider 为 FakeProvider（实际 {type(app.provider).__name__}）",
        )
        if not isinstance(app.provider, FakeProvider):
            raise RuntimeError("Provider 不是 FakeProvider，中止（避免调用真实模型）")

        await _scenario_a(app, failures)
        await _scenario_b(app, failures)
    except Exception:
        traceback.print_exc()
        failures.items.append(f"未捕获异常: {traceback.format_exc(limit=3)}")
    finally:
        if app is not None:
            await app.shutdown(timeout=5.0)

    print()
    if failures.items:
        print(f"[FAILED] {len(failures.items)} 条断言未通过:")
        for item in failures.items:
            print(f"  - {item.splitlines()[0]}")
        return 1
    print("[PASSED] 通知语义全链路 E2E：场景 A（前台回合恰好 1 通知）+ 场景 B（后台恢复回合恰好 1 通知）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="通知语义全链路 E2E 验证")
    parser.add_argument(
        "--case-dir",
        default="",
        help="case 产物目录（默认系统临时目录下新建，跑完保留供排查）",
    )
    args = parser.parse_args()

    if args.case_dir:
        case_dir = Path(args.case_dir).resolve()
        case_dir.mkdir(parents=True, exist_ok=True)
    else:
        case_dir = Path(tempfile.mkdtemp(prefix="ace_notif_e2e_"))
    print(f"case 目录: {case_dir}")

    return asyncio.run(_run(case_dir))


if __name__ == "__main__":
    raise SystemExit(main())
