"""浏览器动作治理层测试：四档分类、一次性审批令牌与双入口汇流。

治理判定集中在 ``BrowserManager.permission_for`` / ``confirm_approval``；
deferred ``browser_*`` 工具与单工具 ``browser_use`` 都汇到这两个方法。
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from crew.browser.manager import BrowserManager
from crew.browser.tools import register_browser_tools
from crew.browser.types import BrowserConfig
from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_user_type,
)
from crew.core.types import ToolCall
from crew.state.config import Config
from crew.tools.registry import Registry
from plugins.browser.tool import BrowserUseTool
from tests.test_browser_use import FakeBrowserDriver

OWNER = "A:uid-governance"
SESSION = "session-governance"

# FakeBrowserDriver 的本代快照固定含两个元素：
# p1:e17 = 宿主标注 [action=submit] 的提交按钮；p1:e18 = 普通文本框。
SUBMIT_REF = "p1:e17"
PLAIN_REF = "p1:e18"


@pytest.fixture
async def browser(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    driver = FakeBrowserDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    await manager.startup()
    try:
        yield manager, driver
    finally:
        await manager.aclose()


@pytest.fixture
def ctx_vars():
    """设置 browser_use 执行期所需的 runctx；每个用例后还原。"""
    tokens = [
        (current_owner_account_id, current_owner_account_id.set(OWNER)),
        (current_session_id, current_session_id.set(SESSION)),
        (current_agent_workdir, current_agent_workdir.set("")),
        (current_user_type, current_user_type.set("internal")),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _assert_ask(decision, *, allow_always: bool = True):
    assert decision is not None and decision.behavior == "ask"
    assert decision.approval_token
    assert decision.reason
    assert decision.allow_always is allow_always
    return decision


# ---- 分类：confirm_sensitive（默认档） ----


async def test_confirm_sensitive_allows_reads_and_plain_writes(browser):
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")

    allowed = (
        ("browser_navigate", {"url": "https://example.com/other"}),
        ("browser_snapshot", {}),
        ("browser_screenshot", {}),
        ("browser_find", {"text": "search"}),
        ("browser_console", {}),
        ("browser_network_requests", {}),
        ("browser_wait", {"time_seconds": 0.1}),
        ("browser_hover", {"ref": PLAIN_REF}),
        ("browser_scroll", {"direction": "down"}),
        ("browser_back", {}),
        ("browser_tabs", {"action": "new", "url": "https://example.com/2"}),
        ("browser_takeover", {"action": "takeover"}),
        ("browser_takeover", {"action": "pause"}),
        # 普通写交互：非提交点击、不提交的输入、非回车按键、永不提交的批量表单
        ("browser_click", {"ref": PLAIN_REF}),
        ("browser_type", {"ref": PLAIN_REF, "text": "x"}),
        ("browser_type", {"ref": PLAIN_REF, "text": "x", "submit": False}),
        ("browser_press", {"key": "Delete"}),
        ("browser_keyup", {"key": "Enter"}),
        ("browser_keydown", {"key": "Shift"}),
        ("browser_select", {"ref": PLAIN_REF, "values": ["one"]}),
        ("browser_check", {"ref": PLAIN_REF, "checked": True}),
        ("browser_drag", {"start_ref": SUBMIT_REF, "end_ref": PLAIN_REF}),
        ("browser_drop", {"ref": PLAIN_REF, "data": {"text/plain": "hi"}}),
        ("browser_dialog", {"action": "dismiss"}),
        (
            "browser_fill_form",
            {"fields": [{"type": "textbox", "ref": PLAIN_REF, "value": "x"}]},
        ),
    )
    for tool_name, args in allowed:
        assert manager.permission_for(tool_name, args, OWNER, SESSION) is None, tool_name


async def test_confirm_sensitive_asks_for_sensitive_actions(browser):
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")

    sensitive = (
        ("browser_type", {"ref": PLAIN_REF, "text": "x", "submit": True}),
        ("browser_press", {"key": "Enter"}),
        ("browser_press", {"key": "Enter", "ref": PLAIN_REF}),
        ("browser_keydown", {"key": "Enter"}),
        # 宿主标注的提交按钮
        ("browser_click", {"ref": SUBMIT_REF}),
        ("browser_upload", {"ref": PLAIN_REF, "paths": ["a.txt"]}),
        ("browser_drop", {"ref": PLAIN_REF, "paths": ["a.txt"]}),
        ("browser_download", {"ref": PLAIN_REF, "filename": "f.bin"}),
        ("browser_dialog", {"action": "accept"}),
    )
    for tool_name, args in sensitive:
        decision = manager.permission_for(tool_name, args, OWNER, SESSION)
        _assert_ask(decision)
        # 中文原因要能说明动作语义
        assert any("\u4e00" <= ch <= "\u9fff" for ch in decision.reason), tool_name

    # 页面内执行代码：禁止「本次对话允许」复用
    for tool_name, args in (
        ("browser_evaluate", {"expression": "() => 1"}),
        ("browser_run_code_unsafe", {"code": "pass"}),
    ):
        decision = manager.permission_for(tool_name, args, OWNER, SESSION)
        _assert_ask(decision, allow_always=False)


async def test_submit_click_detection_follows_host_ref_actions(browser):
    """提交判定只信宿主下发的 ref_actions；查不到标记按普通点击放行。"""
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")
    session = manager._owners[OWNER].sessions[SESSION]
    assert session.ref_actions == {"@e17": "submit"}

    # 普通按钮（无提交标记）放行
    assert (
        manager.permission_for("browser_click", {"ref": PLAIN_REF}, OWNER, SESSION)
        is None
    )
    # 标记丢失（例如宿主未下发）时不误伤
    session.ref_actions.clear()
    assert (
        manager.permission_for("browser_click", {"ref": SUBMIT_REF}, OWNER, SESSION)
        is None
    )


async def test_batch_takes_the_highest_risk_step(browser):
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")

    safe_steps = [
        {"action": "find", "text": "search"},
        {"action": "wait", "time_seconds": 0.1},
        {"action": "scroll", "direction": "down"},
        {"action": "click", "ref": PLAIN_REF},
    ]
    assert (
        manager.permission_for("browser_batch", {"steps": safe_steps}, OWNER, SESSION)
        is None
    )

    for risky_step in (
        {"action": "type", "ref": PLAIN_REF, "text": "x", "submit": True},
        {"action": "press", "key": "Enter"},
        {"action": "click", "ref": SUBMIT_REF},
    ):
        steps = [{"action": "find", "text": "search"}, risky_step]
        decision = manager.permission_for(
            "browser_batch", {"steps": steps}, OWNER, SESSION
        )
        _assert_ask(decision)
        assert "敏感步骤" in decision.reason


# ---- 分类：其余三档 ----


async def test_confirm_writes_also_gates_plain_writes(browser):
    manager, _driver = browser
    manager.config.governance_mode = "confirm_writes"
    await manager.navigate(OWNER, SESSION, "https://example.com")

    writes = (
        ("browser_click", {"ref": PLAIN_REF}),
        ("browser_type", {"ref": PLAIN_REF, "text": "x"}),
        ("browser_press", {"key": "Delete"}),
        ("browser_select", {"ref": PLAIN_REF, "values": ["one"]}),
        ("browser_check", {"ref": PLAIN_REF, "checked": True}),
        ("browser_drag", {"start_ref": SUBMIT_REF, "end_ref": PLAIN_REF}),
        ("browser_drop", {"ref": PLAIN_REF, "data": {"text/plain": "hi"}}),
        (
            "browser_fill_form",
            {"fields": [{"type": "textbox", "ref": PLAIN_REF, "value": "x"}]},
        ),
    )
    for tool_name, args in writes:
        _assert_ask(manager.permission_for(tool_name, args, OWNER, SESSION))

    # 只读与低层原语仍不打扰
    for tool_name, args in (
        ("browser_navigate", {"url": "https://example.com/2"}),
        ("browser_snapshot", {}),
        ("browser_hover", {"ref": PLAIN_REF}),
        ("browser_keydown", {"key": "Shift"}),
    ):
        assert manager.permission_for(tool_name, args, OWNER, SESSION) is None


async def test_read_only_denies_writes_and_sensitive_actions(browser):
    manager, _driver = browser
    manager.config.governance_mode = "read_only"
    await manager.navigate(OWNER, SESSION, "https://example.com")

    denied = (
        ("browser_click", {"ref": PLAIN_REF}),
        ("browser_type", {"ref": PLAIN_REF, "text": "x"}),
        ("browser_press", {"key": "Enter"}),
        ("browser_upload", {"ref": PLAIN_REF, "paths": ["a.txt"]}),
        ("browser_evaluate", {"expression": "() => 1"}),
        ("browser_batch", {"steps": [{"action": "click", "ref": PLAIN_REF}]}),
    )
    for tool_name, args in denied:
        decision = manager.permission_for(tool_name, args, OWNER, SESSION)
        assert decision is not None and decision.behavior == "deny", tool_name
        assert "read_only" in decision.reason

    for tool_name, args in (
        ("browser_navigate", {"url": "https://example.com/2"}),
        ("browser_snapshot", {}),
        ("browser_hover", {"ref": PLAIN_REF}),
        ("browser_batch", {"steps": [{"action": "find", "text": "search"}]}),
    ):
        assert manager.permission_for(tool_name, args, OWNER, SESSION) is None


async def test_governance_off_allows_everything(browser):
    manager, _driver = browser
    manager.config.governance_mode = "off"
    await manager.navigate(OWNER, SESSION, "https://example.com")

    for tool_name, args in (
        ("browser_click", {"ref": SUBMIT_REF}),
        ("browser_type", {"ref": PLAIN_REF, "text": "x", "submit": True}),
        ("browser_press", {"key": "Enter"}),
        ("browser_upload", {"ref": PLAIN_REF, "paths": ["a.txt"]}),
        ("browser_download", {"ref": PLAIN_REF, "filename": "f.bin"}),
        ("browser_dialog", {"action": "accept"}),
        ("browser_evaluate", {"expression": "() => 1"}),
        ("browser_run_code_unsafe", {"code": "pass"}),
    ):
        assert manager.permission_for(tool_name, args, OWNER, SESSION) is None


def test_governance_mode_config_parsing():
    assert BrowserConfig().governance_mode == "confirm_sensitive"
    for mode in ("off", "confirm_sensitive", "confirm_writes", "read_only"):
        assert BrowserConfig.from_raw({"governance_mode": mode}).governance_mode == mode
    # 非法值回落默认档，不产生第四个行为分支
    assert (
        BrowserConfig.from_raw({"governance_mode": "bogus"}).governance_mode
        == "confirm_sensitive"
    )
    assert BrowserConfig.from_raw(None).governance_mode == "confirm_sensitive"


# ---- 一次性审批令牌 ----


async def test_approval_token_is_one_shot(browser):
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")

    args = {"ref": PLAIN_REF, "paths": ["a.txt"]}
    decision = manager.permission_for("browser_upload", args, OWNER, SESSION)
    token = _assert_ask(decision).approval_token

    assert manager.confirm_approval(token, "browser_upload", args, OWNER, SESSION)
    # 已弹出：同一 token 不可重放
    assert not manager.confirm_approval(
        token, "browser_upload", args, OWNER, SESSION
    )
    # 从未签发的 token 一律拒绝
    assert not manager.confirm_approval(
        "never-issued", "browser_upload", args, OWNER, SESSION
    )


async def test_approval_token_rejects_tampered_args_and_expiry(browser):
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")

    args = {"ref": PLAIN_REF, "paths": ["a.txt"]}
    token = _assert_ask(
        manager.permission_for("browser_upload", args, OWNER, SESSION)
    ).approval_token
    # 审批后参数被改动（换了要上传的文件）→ digest 不匹配
    tampered = {"ref": PLAIN_REF, "paths": ["b.txt"]}
    assert not manager.confirm_approval(
        token, "browser_upload", tampered, OWNER, SESSION
    )

    token = _assert_ask(
        manager.permission_for("browser_upload", args, OWNER, SESSION)
    ).approval_token
    grant = manager._approval_tokens[token]
    manager._approval_tokens[token] = replace(
        grant, expires_at=time.monotonic() - 1
    )
    assert not manager.confirm_approval(
        token, "browser_upload", args, OWNER, SESSION
    )


async def test_approval_token_dies_with_page_generation_and_ref(browser):
    manager, _driver = browser
    await manager.navigate(OWNER, SESSION, "https://example.com")
    session = manager._owners[OWNER].sessions[SESSION]

    # 审批后页面换代（重新观察）→ 令牌作废
    upload_args = {"ref": PLAIN_REF, "paths": ["a.txt"]}
    token = _assert_ask(
        manager.permission_for("browser_upload", upload_args, OWNER, SESSION)
    ).approval_token
    await manager.snapshot(OWNER, SESSION)
    assert not manager.confirm_approval(
        token, "browser_upload", upload_args, OWNER, SESSION
    )

    # 审批后目标 ref 失效（页面未换代但元素表被清）→ 令牌作废
    click_args = {"ref": "p2:e17"}  # snapshot 换代后的提交按钮
    token = _assert_ask(
        manager.permission_for("browser_click", click_args, OWNER, SESSION)
    ).approval_token
    session.refs.pop("p2:e17")
    assert not manager.confirm_approval(
        token, "browser_click", click_args, OWNER, SESSION
    )


# ---- 双入口汇流：browser_use 单工具与 deferred browser_* 工具 ----


async def test_browser_use_single_tool_entry_uses_the_same_governance(
    browser, ctx_vars, tmp_path
):
    manager, _driver = browser
    config = Config(db_path=str(tmp_path / "crew.db"), cron_enabled=False)
    tool = BrowserUseTool(manager, config, None)
    await manager.navigate(OWNER, SESSION, "https://example.com")

    # action 映射成逻辑名后走同一 permission_for
    args = {"action": "upload", "paths": ["a.txt"]}
    decision = tool.permission_resolver(args)
    _assert_ask(decision)
    assert tool.permission_approver(decision.approval_token, args)
    # 一次性：再次确认失败
    assert not tool.permission_approver(decision.approval_token, args)

    # 提交型点击经 ref_actions 判定
    submit_click = {"action": "click", "ref": SUBMIT_REF}
    _assert_ask(tool.permission_resolver(submit_click))
    # 子 action 展开：dialog_accept ask / dialog_dismiss 放行 / snapshot 放行
    _assert_ask(tool.permission_resolver({"action": "dialog_accept"}))
    assert tool.permission_resolver({"action": "dialog_dismiss"}) is None
    assert tool.permission_resolver({"action": "snapshot"}) is None


async def test_deferred_browser_tools_entry_uses_the_same_governance(browser, ctx_vars):
    manager, _driver = browser
    registry = Registry()
    register_browser_tools(registry, manager)
    await manager.navigate(OWNER, SESSION, "https://example.com")

    call = ToolCall("tc-upload", "browser_upload", {"paths": ["a.txt"]})
    decision = await registry.resolve_permission(call)
    _assert_ask(decision)
    assert await registry.confirm_permission(call, decision)
    # 一次性：同一决定的 token 不可重放
    assert not await registry.confirm_permission(call, decision)

    # 读动作直接放行
    assert (
        await registry.resolve_permission(
            ToolCall("tc-snapshot", "browser_snapshot", {})
        )
        is None
    )
    assert (
        await registry.resolve_permission(
            ToolCall(
                "tc-navigate", "browser_navigate", {"url": "https://example.com/2"}
            )
        )
        is None
    )
