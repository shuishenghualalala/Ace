# ruff: noqa: E402 -- e2e test configures environment/path before application imports
"""端到端：真实 LLM 驱动下走完整 Plan 模式闭环。

默认用 default profile，需要环境变量 CREW_MODEL_API_KEY。
运行：
    CREW_MODEL_API_KEY=... python tests/e2e_plan_mode.py

安全：脚本在一个**临时项目目录**里运行（os.chdir + 临时 CREW_HOME），并在其中放一个
玩具 cli.py 作为待改对象——agent 的 terminal/file_write 只作用于临时目录，绝不触碰真实仓库。

流程（模拟 CLI /plan 闭环）：
    1. plan_manager.enter(sid)                进入只读 plan 模式
    2. 发需求 → 模型只读探索玩具项目、把计划写入 plans/<owner>/<sid>/plan_<timestamp>.md、调 exit_plan_mode
    3. 断言：规划轮只用只读工具 + file_write 仅写计划文件 + 触发 awaiting_approval
    4. approve → 发执行 kickoff → 观察模型解锁写工具、在临时目录里改 cli.py
脚本打印逐帧转录，便于人工核验。
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("CREW_MODEL_PROFILE", "default")

# 真实 LLM：默认不跑；`pytest -m e2e tests/e2e_plan_mode.py` 或直接 python 本文件。
import pytest

pytestmark = pytest.mark.e2e

TASK = (
    "当前目录有一个玩具 CLI 项目 cli.py，里面已有 /help、/quit 两个 slash 命令。"
    "请给它增加一个 /version 命令，打印常量 VERSION。先探索 cli.py 现有写法，再给出实现计划。"
    "注意：只在当前临时目录内操作，不要执行任何 git 命令。"
)

TOY_CLI = '''"""玩具 CLI（仅供 e2e 测试用）。"""

VERSION = "9.9.9"


def repl():
    while True:
        cmd = input("> ").strip()
        if cmd == "/help":
            print("/help 显示帮助; /quit 退出")
        elif cmd == "/quit":
            break
        else:
            print(f"未知命令: {cmd}")


if __name__ == "__main__":
    repl()
'''


def _banner(title: str) -> None:
    print("\n" + "=" * 70 + f"\n{title}\n" + "=" * 70)


async def _run_turn(app, envelope, label: str) -> list[tuple[str, str]]:
    _banner(label)
    tool_events: list[tuple[str, str]] = []
    async for chunk in app.handle(envelope):
        if chunk.kind == "tool":
            name = chunk.body.get("name", "")
            phase = chunk.body.get("phase", "")
            tool_events.append((name, phase))
            if phase == "start":
                print(f"  → tool {name}({chunk.body.get('args', '')[:160]})")
            else:
                print(f"  ← {name}: {chunk.body.get('detail', '')[:140]}")
        elif chunk.kind == "final":
            print(f"\n[最终回复]\n{chunk.body.get('text', '')[:800]}\n")
        elif chunk.kind == "error":
            print(f"[错误] {chunk.body.get('message')}")
    return tool_events


async def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="crew_plan_e2e_"))
    (work / "cli.py").write_text(TOY_CLI, encoding="utf-8")
    os.environ["CREW_HOME"] = str(work / ".crew")
    os.chdir(work)  # agent 的 terminal/file 工具只作用于此临时目录

    from crew.app import build_app
    from crew.core.envelope import Envelope
    from crew.agent.plan import plan_path, read_plan

    app = build_app(enable_team=False)
    if not app.config.has_llm_key:
        print(
            f"[跳过] profile={app.config.active_model_id} 未配置 API Key。"
            f"请 export CREW_MODEL_API_KEY=... 后重跑。"
        )
        return 2

    print(f"使用 profile={app.config.active_model_id} model={app.config.model}")
    print(f"临时工作目录: {work}")
    await app.startup()
    sid = "e2e_plan"
    ok = True
    try:
        # Envelope.user_id 默认 "local"，plan 状态按 (owner, sid) 隔离——enter 必须用同一 owner，
        # 否则 dispatch 时 is_active 查不到 plan 态，plan 工作流提醒不会注入，模型就退化为普通 Q&A。
        app.plan_manager.enter(sid, owner_account_id="local")
        print(f"[进入 plan 模式] 计划文件: {plan_path(sid, owner_account_id='local')}")

        env1 = Envelope.of(TASK, session_id=sid, channel="cli", mode="agent")
        events = await _run_turn(app, env1, "规划轮（只读探索 + 写计划 + exit_plan_mode）")

        used = {n for n, p in events if p == "start"}
        print(f"\n规划轮用到的工具: {sorted(used)}")
        # 规划轮唯一允许的写工具是写计划文件的 file_write，其余写类工具不应出现
        leaked = used & {"patch", "memory", "cron_create", "delegate_task"}
        if leaked:
            print(f"[FAIL] 规划轮泄漏写类工具: {leaked}")
            ok = False
        # cli.py 在规划轮不应被改动
        if (work / "cli.py").read_text(encoding="utf-8") != TOY_CLI:
            print("[FAIL] 规划轮竟改动了 cli.py（只读约束被破坏）")
            ok = False
        else:
            print("[OK] 规划轮 cli.py 未被改动（只读约束生效）")

        plan_text = read_plan(sid, owner_account_id="local")
        if plan_text:
            print(f"[OK] 计划已写入（{len(plan_text)} 字符）：\n{plan_text[:500]}\n...")
        else:
            print("[FAIL] 计划文件未写入")
            ok = False
        if app.plan_manager.is_awaiting_approval(sid, owner_account_id="local"):
            print("[OK] exit_plan_mode 已触发，进入待审批")
        else:
            print("[FAIL] 未触发 exit_plan_mode")
            ok = False

        if app.plan_manager.is_awaiting_approval(sid, owner_account_id="local"):
            app.plan_manager.approve(sid, owner_account_id="local")
            print("\n[用户批准计划]")
            env2 = Envelope.of(
                "计划已批准，请按计划开始执行（只在当前目录操作，勿用 git）。",
                session_id=sid, channel="cli", mode="agent",
            )
            exec_events = await _run_turn(app, env2, "执行轮（已解锁写工具）")
            after = (work / "cli.py").read_text(encoding="utf-8")
            if "/version" in after and "VERSION" in after:
                print("[OK] 执行轮已在 cli.py 中实现 /version 命令")
            else:
                print("[WARN] 执行轮未见 cli.py 出现 /version（模型可能换了实现路径）")

            # 回归：执行轮收尾后不应再次进入待审批。
            # 若模型在收尾误调 exit_plan_mode，awaiting 会变 True → plan_review 二次弹卡。
            exec_used = {n for n, p in exec_events if p == "start"}
            print(f"\n执行轮用到的工具: {sorted(exec_used)}")
            if "exit_plan_mode" in exec_used:
                print("[FAIL] 执行轮不该调用 exit_plan_mode（plan 已退出）")
                ok = False
            if app.plan_manager.is_awaiting_approval(sid, owner_account_id="local"):
                print("[FAIL] 执行轮后再次进入 awaiting_approval（plan_review 会二次弹卡）")
                ok = False
            elif app.plan_manager.is_active(sid, owner_account_id="local"):
                print("[FAIL] 执行轮后 plan 模式仍处于 active")
                ok = False
            else:
                print("[OK] 执行轮收尾后 plan 状态干净，无二次审批")
    finally:
        await app.shutdown()

    print("\n" + "=" * 70 + f"\n结果: {'通过' if ok else '存在问题，见上'}\n" + "=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
