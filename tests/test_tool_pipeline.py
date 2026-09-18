"""工具执行流水线（Crew 8 阶段）补齐后的单测：

覆盖 Crew 新增的三个阶段 + alias + onProgress：
  - Stage 2 输入验证：JSON Schema 校验失败回灌 <tool_use_error>
  - Stage 4 权限检查：allow/deny/ask 规则匹配 + 交互确认（mock followup）
  - Stage 6 大结果落盘：超阈值落盘 + 路径回灌
  - Stage 1 别名 + 废弃警告
  - Stage 5 onProgress：emit_tool_progress 在无 sink 时 no-op，有 sink 时捕获
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from crew.core.runctx import (
    current_tool_progress_fn,
    emit_tool_progress,
)
from crew.core.types import ToolCall
from crew.tools import pipeline
from crew.tools.pipeline import (
    PermissionConfig,
    PermissionRule,
    check_permission,
    extract_match_key,
    grant_session_allow,
    load_permission_config,
    truncate_or_persist,
    validate_arguments,
)
from crew.tools.registry import Registry


# --------------------------------------------------------------------------- #
# Stage 2：输入验证
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "schema,args,expected_err_parts",
    [
        # 合法入参 → 通过（None）
        (
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            {"path": "a"},
            None,
        ),
        # 缺必填字段
        (
            {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            {},
            ["required", "t"],
        ),
        # 类型错误
        (
            {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            {"n": "not-a-number"},
            ["integer"],
        ),
        # args 非对象
        ({"type": "object"}, "not-a-dict", ["对象"]),
        # 工具未声明 parameters 时不做结构校验，交给业务层
        ({}, {"anything": 1}, None),
    ],
    ids=["pass", "missing_required", "wrong_type", "non_dict_args", "no_schema_skips"],
)
def test_validate_arguments(schema, args, expected_err_parts):
    err = validate_arguments("t", schema, args)
    if expected_err_parts is None:
        assert err is None
    else:
        assert err is not None
        for part in expected_err_parts:
            assert part in err


# --------------------------------------------------------------------------- #
# Stage 6：大结果落盘
# --------------------------------------------------------------------------- #
def test_truncate_small_result_unchanged():
    assert truncate_or_persist("id", "t", "short", max_chars=50) == "short"


def test_truncate_large_result_persists_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline, "get_crew_home", lambda: tmp_path)
    big = "A" * 60000
    out = truncate_or_persist("tc-persist", "file_read", big, max_chars=5000)
    assert "truncated" in out
    assert "60000" in out
    # 完整内容落盘
    persisted = (tmp_path / "tool-results" / "tc-persist.txt").read_text(encoding="utf-8")
    assert persisted == big
    # 返回里含路径
    assert "tc-persist.txt" in out


def test_truncate_fallback_inline_when_persist_fails(monkeypatch):
    # 让落盘抛异常 → 降级为就地截断（保留首尾），不丢信息
    def _boom(_id, _content):
        raise OSError("disk on fire")
    monkeypatch.setattr(pipeline, "_persist_tool_result", _boom)
    out = truncate_or_persist("id", "t", "B" * 600, max_chars=100)
    assert "truncated" in out
    assert out.startswith("B")


# --------------------------------------------------------------------------- #
# Stage 4：权限规则
# --------------------------------------------------------------------------- #
def test_load_permission_config_parses_rules():
    raw = [
        {"tool": "terminal", "match": "git push:*", "behavior": "ask"},
        {"tool": "terminal", "match": "rm -rf:*", "behavior": "deny"},
        {"tool": "file_write", "match": "*", "behavior": "allow"},
    ]
    cfg = load_permission_config(raw)
    assert len(cfg.rules) == 3
    assert cfg.rules[0].behavior == "ask"


def test_load_permission_config_drops_invalid():
    raw = [
        {"tool": "", "match": "*", "behavior": "ask"},        # 缺 tool 跳过
        {"tool": "terminal", "match": "*", "behavior": "weird"},  # behavior 归一 ask
        "not-a-dict",
    ]
    cfg = load_permission_config(raw)
    assert len(cfg.rules) == 1
    assert cfg.rules[0].behavior == "ask"


@pytest.mark.parametrize(
    "rule,checks",
    [
        # 精确匹配：不匹配前缀
        (
            {"tool": "terminal", "match": "ls", "behavior": "deny"},
            [("terminal", "ls", "deny"), ("terminal", "ls -la", "allow")],
        ),
        # 前缀匹配（`:*` 后缀）
        (
            {"tool": "terminal", "match": "git push:*", "behavior": "ask"},
            [("terminal", "git push origin main", "ask"), ("terminal", "git pull", "allow")],
        ),
        # 通配后缀（` *`）
        (
            {"tool": "terminal", "match": "git *", "behavior": "ask"},
            [("terminal", "git commit", "ask"), ("terminal", "npm install", "allow")],
        ),
        # 全量匹配
        (
            {"tool": "file_write", "match": "*", "behavior": "deny"},
            [("file_write", "/any/path", "deny")],
        ),
    ],
    ids=["exact_match", "prefix_match", "wildcard_suffix", "blanket_match"],
)
def test_permission_rule_matching(rule, checks):
    cfg = load_permission_config([rule])
    for tool, key, expected in checks:
        assert cfg.check(tool, key)[0] == expected


def test_permission_deny_priority_over_ask():
    cfg = load_permission_config([
        {"tool": "terminal", "match": "*", "behavior": "ask"},
        {"tool": "terminal", "match": "rm -rf:*", "behavior": "deny"},
    ])
    assert cfg.check("terminal", "rm -rf /tmp")[0] == "deny"


def test_permission_session_allow_overrides():
    cfg = load_permission_config([{"tool": "terminal", "match": "git push:*", "behavior": "ask"}])
    assert cfg.check("terminal", "git push", session_id="s1")[0] == "ask"
    cfg.add_session_allow("s1", PermissionRule("terminal", "git push:*", "allow"))
    assert cfg.check("terminal", "git push", session_id="s1")[0] == "allow"


def test_check_permission_default_behavior_is_forwarded_and_invalid_is_denied():
    cfg = PermissionConfig()
    assert check_permission("terminal", {"command": "echo ok"}, config=cfg, default_behavior="ask")[0] == "ask"
    assert check_permission("terminal", {"command": "echo ok"}, config=cfg, default_behavior="deny")[0] == "deny"
    assert check_permission("terminal", {"command": "echo ok"}, config=cfg, default_behavior="unexpected")[0] == "deny"
    assert check_permission("terminal", {"command": "echo ok"}, config=cfg, default_behavior=[])[0] == "deny"


def test_extract_match_key_by_tool():
    assert extract_match_key("terminal", {"command": "ls"}) == "ls"
    assert extract_match_key("file_write", {"path": "/x"}) == "/x"
    # 其它工具用整段 args json
    assert extract_match_key("memory", {"q": "a"}) == json.dumps(
        {"q": "a"}, ensure_ascii=False, sort_keys=True
    )


def test_grant_session_allow_persists_across_calls(monkeypatch):
    # 用单一实例的 config，保证 grant 写入与 check 读取是同一对象
    shared_cfg = load_permission_config(
        [{"tool": "terminal", "match": "git push:*", "behavior": "ask"}]
    )
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: shared_cfg)
    grant_session_allow("s9", "terminal", "git push origin")
    assert check_permission("terminal", {"command": "git push origin"}, session_id="s9")[0] == "allow"
    assert check_permission("terminal", {"command": "git push origin main"}, session_id="s9")[0] == "ask"


def test_ui_session_allow_is_exact_and_rejects_prefix_scope(monkeypatch):
    shared_cfg = load_permission_config(
        [{"tool": "terminal", "match": "*", "behavior": "ask"}]
    )
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: shared_cfg)

    grant_session_allow("s-exact", "terminal", "git push origin main")
    grant_session_allow("s-exact", "terminal", "rm:*")

    assert check_permission(
        "terminal", {"command": "git push origin main"}, session_id="s-exact"
    )[0] == "allow"
    assert check_permission(
        "terminal", {"command": "git push origin main --force"}, session_id="s-exact"
    )[0] == "ask"
    assert check_permission(
        "terminal", {"command": "rm /tmp/x"}, session_id="s-exact"
    )[0] == "ask"


def _unused_config() -> PermissionConfig:
    return PermissionConfig()


# --------------------------------------------------------------------------- #
# 集成：registry.execute 串起 Stage2 + Stage6 + alias
# --------------------------------------------------------------------------- #
async def test_registry_execute_schema_error_wrapped():
    reg = Registry()
    reg.register(
        name="demo", toolset="t",
        schema={"name": "demo", "parameters": {
            "type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"],
        }},
        handler=lambda a: json.dumps(a),
    )
    r = await reg.execute(ToolCall("1", "demo", {}))
    assert r.is_error
    assert "tool_use_error" in r.content
    assert r.name == "demo"


async def test_registry_execute_alias_deprecation_note():
    reg = Registry()
    reg.register(
        name="echo", toolset="t",
        schema={"name": "echo", "parameters": {"type": "object", "properties": {"msg": {"type": "string"}}}},
        handler=lambda a: json.dumps({"got": a.get("msg")}),
        aliases=["say"],
    )
    r = await reg.execute(ToolCall("1", "say", {"msg": "hi"}))
    assert not r.is_error
    assert "别名" in r.content
    assert "echo" in r.content
    assert "hi" in r.content


async def test_registry_execute_large_result_truncated():
    reg = Registry()
    reg.register(
        name="big", toolset="t",
        schema={"name": "big", "parameters": {"type": "object"}},
        handler=lambda a: "Z" * 60000,
        max_result_size_chars=5000,
    )
    r = await reg.execute(ToolCall("1", "big", {}))
    assert not r.is_error
    assert "truncated" in r.content
    assert len(r.content) < 60000


# --------------------------------------------------------------------------- #
# Stage 5：onProgress
# --------------------------------------------------------------------------- #
async def test_emit_progress_noop_without_sink():
    # 无 sink 时不抛异常
    await emit_tool_progress("hello")


async def test_emit_progress_captures_with_sink():
    received: list[str] = []

    async def sink(text: str) -> None:
        received.append(text)

    token = current_tool_progress_fn.set(sink)
    try:
        await emit_tool_progress("chunk-1")
        await emit_tool_progress("chunk-2")
    finally:
        current_tool_progress_fn.reset(token)
    assert received == ["chunk-1", "chunk-2"]


async def test_emit_progress_sink_exception_swallowed():
    async def sink(_text: str) -> None:
        raise RuntimeError("boom")

    token = current_tool_progress_fn.set(sink)
    try:
        # sink 抛异常时 emit_tool_progress 必须吞掉，不得冒泡
        await emit_tool_progress("x")
    finally:
        current_tool_progress_fn.reset(token)


# --------------------------------------------------------------------------- #
# 集成：ToolRunner Stage4 ask 路径（mock followup）
# --------------------------------------------------------------------------- #
async def test_tool_runner_permission_deny(monkeypatch):
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.plugins.manager import PluginManager

    # 注入一条 deny 规则的 PermissionConfig
    cfg = load_permission_config([{"tool": "terminal", "match": "rm -rf:*", "behavior": "deny"}])
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: cfg)

    runner = ToolRunner(
        registry=Registry(),
        plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(),
        session_id="s1",
    )
    # 直接调 _check_permission，绕过执行
    block = await runner._check_permission(
        ToolCall("1", "terminal", {"command": "rm -rf /tmp/x"})
    )
    assert block is not None
    assert "权限拒绝" in block


async def test_tool_runner_permission_ask_allows_on_choice(monkeypatch):
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.plugins.manager import PluginManager

    cfg = load_permission_config([{"tool": "terminal", "match": "git push:*", "behavior": "ask"}])
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: cfg)

    # mock followup：用户选「允许一次」
    captured = {}

    async def fake_send(questions, title="", **kw):
        captured.update({"questions": questions, "title": title, **kw})
        return "s1", "qid"

    async def fake_wait(sid, qid, **kw):
        return [{"id": "perm", "answers": ["allow_once"]}]

    monkeypatch.setattr("crew.agent.loop.tool_runner.send_followup_question", fake_send)
    monkeypatch.setattr("crew.agent.loop.tool_runner.wait_for_answer", fake_wait)

    runner = ToolRunner(
        registry=Registry(), plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(), session_id="s1",
    )
    block = await runner._check_permission(
        ToolCall("1", "terminal", {"command": "git push origin main"})
    )
    assert block is None  # 放行
    assert captured["record_history"] is False
    assert captured["questions"][0]["allowFreeText"] is False
    assert "```" not in captured["questions"][0]["question"]
    assert "`terminal`" not in captured["questions"][0]["question"]


async def test_tool_runner_permission_ask_denies_on_reject(monkeypatch):
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.plugins.manager import PluginManager

    cfg = load_permission_config([{"tool": "terminal", "match": "git push:*", "behavior": "ask"}])
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: cfg)

    async def fake_send(questions, title="", **kw):
        return "s1", "qid"

    async def fake_wait(sid, qid, **kw):
        return [{"id": "perm", "answers": ["deny"]}]

    monkeypatch.setattr("crew.agent.loop.tool_runner.send_followup_question", fake_send)
    monkeypatch.setattr("crew.agent.loop.tool_runner.wait_for_answer", fake_wait)

    runner = ToolRunner(
        registry=Registry(), plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(), session_id="s1",
    )
    block = await runner._check_permission(
        ToolCall("1", "terminal", {"command": "git push origin main"})
    )
    assert block is not None
    assert "用户拒绝" in block


async def test_tool_runner_permission_wait_interrupted_denies_and_cleans_up(monkeypatch):
    """权限等待期间 interrupt：权限按拒绝处理（安全默认），竞争失败的等待 task
    被显式取消回收，不残留永久挂起的 task。"""
    from crew.agent.loop.control import TurnControl
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.plugins.manager import PluginManager

    cfg = load_permission_config([{"tool": "terminal", "match": "git push:*", "behavior": "ask"}])
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: cfg)

    async def fake_send(questions, title="", **kw):
        return "s1", "qid"

    cancelled: list = []

    async def fake_wait(sid, qid, **kw):
        try:
            await asyncio.Event().wait()  # 模拟用户一直未作答
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr("crew.agent.loop.tool_runner.send_followup_question", fake_send)
    monkeypatch.setattr("crew.agent.loop.tool_runner.wait_for_answer", fake_wait)

    control = TurnControl()
    runner = ToolRunner(
        registry=Registry(), plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(), session_id="s1", control=control,
    )
    task = asyncio.create_task(
        runner._check_permission(
            ToolCall("1", "terminal", {"command": "git push origin main"})
        )
    )
    await asyncio.sleep(0.05)  # 进入权限等待段
    control.interrupt()
    block = await asyncio.wait_for(task, timeout=5)

    assert block is not None
    assert "未得到明确许可" in block  # 安全默认：interrupt 按拒绝处理
    assert cancelled, "interrupt 先到时，竞争失败的等待 task 必须被显式取消"
    await asyncio.sleep(0)  # 让取消回收落定
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftover == [], f"不得残留挂起 task: {leftover}"


# --------------------------------------------------------------------------- #
# 执行段看门狗：per-tool 超时 + interrupt 可取消在途工具
# --------------------------------------------------------------------------- #
def _watchdog_registry(hang_cancelled: list | None = None) -> Registry:
    """注册三个测试工具：永挂、快返回、慢但会完成（全部只读语义，无权限拦截）。"""
    reg = Registry()

    async def _hang(_args):
        try:
            await asyncio.sleep(3600)
            return "unreachable"
        except asyncio.CancelledError:
            if hang_cancelled is not None:
                hang_cancelled.append(True)
            raise

    async def _quick(_args):
        return "quick-ok"

    async def _slow(_args):
        await asyncio.sleep(0.3)
        return "slow-finished"

    for name, handler in (
        ("hang_tool", _hang),
        ("quick_tool", _quick),
        ("slow_ok_tool", _slow),
        # 并发安全白名单名：让并行段测试走 _run_parallel_segment 真实路径。
        ("web_search", _hang),
        ("browser_snapshot", _quick),
    ):
        reg.register(
            name=name,
            toolset="test",
            schema={"name": name, "parameters": {"type": "object", "properties": {}}},
            handler=handler,
            is_async=True,
        )
    return reg


def _watchdog_runner(reg: Registry, *, tool_timeout: float = 0.1, control=None):
    from crew.agent.loop.control import TurnControl
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController, ToolCallGuardrailConfig
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.plugins.manager import PluginManager

    return ToolRunner(
        registry=reg,
        plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(ToolCallGuardrailConfig()),
        session_id="s1",
        control=control if control is not None else TurnControl(),
        tool_execution_timeout_seconds=tool_timeout,
    )


def _seq_counter():
    n = 0

    def nxt() -> int:
        nonlocal n
        n += 1
        return n

    return nxt


async def _drive_batch(runner, calls, messages):
    return [
        c
        async for c in runner.run_batch(calls, messages, "rid", _seq_counter())
    ]


async def test_tool_execution_watchdog_timeout_advances_turn():
    """永挂工具在小超时下被看门狗取消：回合正常推进，tool output 为 timed out。"""
    runner = _watchdog_runner(_watchdog_registry(), tool_timeout=0.1)
    messages: list = []
    calls = [ToolCall("h1", "hang_tool", {})]

    started = time.monotonic()
    await _drive_batch(runner, calls, messages)
    elapsed = time.monotonic() - started

    assert elapsed < 5  # 没有被挂死工具冻结整回合
    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "h1"  # tool_call/tool output 配对完整
    assert "timed out after 0.1s" in tool_msgs[0].content


async def test_tool_interrupt_aborts_inflight_tool_with_grace_window():
    """工具执行期间 interrupt：100ms 优雅窗口后工具被取消，输出 aborted by user。"""
    from crew.agent.loop.control import TurnControl

    hang_cancelled: list = []
    control = TurnControl()
    runner = _watchdog_runner(
        _watchdog_registry(hang_cancelled), tool_timeout=0, control=control
    )
    messages: list = []
    task = asyncio.create_task(
        _drive_batch(runner, [ToolCall("h1", "hang_tool", {})], messages)
    )
    await asyncio.sleep(0.05)  # 等工具进入执行段
    control.interrupt()
    await asyncio.wait_for(task, timeout=5)

    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "aborted by user after" in tool_msgs[0].content
    assert hang_cancelled, "优雅窗口到期后 hang 工具应收到 cancel"


async def test_tool_interrupt_parallel_batch_keeps_completed_results():
    """并行段被 interrupt：在途工具走 aborted 语义，已完成工具结果原样保留。"""
    from crew.agent.loop.control import TurnControl

    control = TurnControl()
    runner = _watchdog_runner(
        _watchdog_registry(), tool_timeout=0, control=control
    )
    messages: list = []
    calls = [
        ToolCall("w1", "web_search", {}),        # 永挂（并发安全名 → 并行段）
        ToolCall("b1", "browser_snapshot", {}),  # 立即完成
    ]
    task = asyncio.create_task(_drive_batch(runner, calls, messages))
    await asyncio.sleep(0.05)
    control.interrupt()
    await asyncio.wait_for(task, timeout=5)

    by_id = {m.tool_call_id: m.content for m in messages if m.role == "tool"}
    assert set(by_id) == {"w1", "b1"}  # 整批结果配对完整，无孤儿 tool_call
    assert by_id["b1"] == "quick-ok"   # 已完成工具不受 interrupt 影响
    assert "aborted by user after" in by_id["w1"]


async def test_tool_completed_during_grace_window_keeps_result():
    """interrupt 后在优雅窗口内自行收尾的工具：正常收取结果，不被取消改写。"""
    from crew.agent.loop.control import TurnControl

    release = asyncio.Event()

    async def _gated(_args):
        await release.wait()
        return "slow-finished"

    reg = Registry()
    reg.register(
        name="gated_tool",
        toolset="test",
        schema={"name": "gated_tool", "parameters": {"type": "object", "properties": {}}},
        handler=_gated,
        is_async=True,
    )
    control = TurnControl()
    runner = _watchdog_runner(reg, tool_timeout=0, control=control)
    messages: list = []
    task = asyncio.create_task(
        _drive_batch(runner, [ToolCall("s1", "gated_tool", {})], messages)
    )
    await asyncio.sleep(0.05)
    control.interrupt()
    await asyncio.sleep(0.02)  # 100ms 优雅窗口内放行工具自行收尾
    release.set()
    await asyncio.wait_for(task, timeout=5)

    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "slow-finished"


async def test_tool_execution_watchdog_disabled_when_zero():
    """超时=0：看门狗关闭，短任务行为不变（正常收取结果）。"""
    runner = _watchdog_runner(_watchdog_registry(), tool_timeout=0)
    messages: list = []

    await asyncio.wait_for(
        _drive_batch(runner, [ToolCall("q1", "quick_tool", {})], messages),
        timeout=5,
    )

    tool_msgs = [m for m in messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].content == "quick-ok"
