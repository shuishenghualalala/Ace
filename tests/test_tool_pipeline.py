"""工具执行流水线（Crew 8 阶段）补齐后的单测：

覆盖 Crew 新增的三个阶段 + alias + onProgress：
  - Stage 2 输入验证：JSON Schema 校验失败回灌 <tool_use_error>
  - Stage 4 权限检查：allow/deny/ask 规则匹配 + 交互确认（mock followup）
  - Stage 6 大结果落盘：超阈值落盘 + 路径回灌
  - Stage 1 别名 + 废弃警告
  - Stage 5 onProgress：emit_tool_progress 在无 sink 时 no-op，有 sink 时捕获
"""

from __future__ import annotations

import json

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
    validate_input_hook,
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


def test_validate_input_hook_does_not_echo_host_exception():
    class Tool:
        def validate_input(self, _args):
            raise ValueError(r"C:\private\key.pem ACCESS_TOKEN=must-not-leak")

    assert validate_input_hook(Tool(), {}) == "业务校验异常"


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


def test_permission_persistent_deny_overrides_existing_session_allow():
    cfg = load_permission_config(
        [{"tool": "remote__write", "match": "*", "behavior": "deny"}]
    )
    cfg.add_session_allow(
        "s1",
        PermissionRule(
            tool="remote__write",
            match="*",
            behavior="allow",
            reason="older approval",
        ),
    )

    behavior, reason, _suggested = cfg.check(
        "remote__write",
        '{"target": "blocked"}',
        session_id="s1",
        default_behavior="ask",
    )

    assert behavior == "deny"
    assert reason


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


async def test_strict_mode_mcp_tools_require_per_call_authorization(monkeypatch):
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.plugins.manager import PluginManager

    registry = Registry()
    registry.register(
        name="remote__dangerous",
        toolset="mcp:remote",
        schema={
            "name": "remote__dangerous",
            "parameters": {"type": "object", "properties": {"target": {"type": "string"}}},
        },
        handler=lambda _args: "should not run",
        is_mcp=True,
    )
    monkeypatch.setattr("crew.security.settings.strict_security_enabled", lambda: True)
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: load_permission_config([]))

    runner = ToolRunner(
        registry=registry,
        plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(),
        session_id="s1",
    )
    block = await runner._check_permission(
        ToolCall("1", "remote__dangerous", {"target": "outside"})
    )

    assert block is not None
    assert json.loads(block)["error"]


async def test_strict_mode_mcp_tool_uses_exact_explicit_allow_rule(monkeypatch):
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.plugins.manager import PluginManager

    registry = Registry()
    registry.register(
        name="remote__read",
        toolset="mcp:remote",
        schema={
            "name": "remote__read",
            "parameters": {"type": "object", "properties": {"target": {"type": "string"}}},
        },
        handler=lambda _args: "ok",
        is_mcp=True,
    )
    cfg = load_permission_config(
        [
            {
                "tool": "remote__read",
                "match": '{"target": "allowed"}',
                "behavior": "allow",
            }
        ]
    )
    monkeypatch.setattr("crew.security.settings.strict_security_enabled", lambda: True)
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: cfg)

    runner = ToolRunner(
        registry=registry,
        plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(),
        session_id="s1",
    )

    assert (
        await runner._check_permission(
            ToolCall("1", "remote__read", {"target": "allowed"})
        )
        is None
    )
    assert (
        await runner._check_permission(
            ToolCall("2", "remote__read", {"target": "different"})
        )
        is not None
    )


async def test_mcp_always_allow_is_scoped_to_canonical_arguments(monkeypatch):
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.plugins.manager import PluginManager

    registry = Registry()
    registry.register(
        name="remote__write",
        toolset="mcp:remote",
        schema={
            "name": "remote__write",
            "parameters": {"type": "object", "properties": {"target": {"type": "string"}}},
        },
        handler=lambda _args: "ok",
        is_mcp=True,
    )
    cfg = load_permission_config([])
    prompts: list[str] = []

    async def fake_send(questions, title="", **_kwargs):
        prompts.append(questions[0]["question"])
        return "s1", f"qid-{len(prompts)}"

    async def fake_wait(_sid, _qid, **_kwargs):
        return [{"id": "perm", "answers": ["always"]}]

    monkeypatch.setattr("crew.security.settings.strict_security_enabled", lambda: True)
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: cfg)
    monkeypatch.setattr("crew.agent.loop.tool_runner.send_followup_question", fake_send)
    monkeypatch.setattr("crew.agent.loop.tool_runner.wait_for_answer", fake_wait)
    runner = ToolRunner(
        registry=registry,
        plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(),
        session_id="s1",
    )

    assert (
        await runner._check_permission(
            ToolCall("1", "remote__write", {"target": "first"})
        )
        is None
    )
    assert (
        await runner._check_permission(
            ToolCall("2", "remote__write", {"target": "second"})
        )
        is None
    )
    assert len(prompts) == 2
    assert all(
        "first" not in rule.match and rule.match.startswith("sha256:")
        for rule in cfg._session_allows["s1"]
    )


async def test_mcp_permission_card_redacts_secrets_and_bounds_arguments(monkeypatch):
    from crew.agent.loop.tool_guardrails import ToolCallGuardrailController
    from crew.agent.loop.tool_runner import ToolRunner
    from crew.plugins.manager import PluginManager

    registry = Registry()
    registry.register(
        name="remote__send",
        toolset="mcp:remote",
        schema={"name": "remote__send", "parameters": {"type": "object"}},
        handler=lambda _args: "ok",
        is_mcp=True,
    )
    captured: list[str] = []
    secret = "sk-test-super-secret-token-123456789"

    async def fake_send(questions, title="", **_kwargs):
        captured.append(questions[0]["question"])
        return "s1", "qid"

    async def fake_wait(_sid, _qid, **_kwargs):
        return [{"id": "perm", "answers": ["deny"]}]

    monkeypatch.setattr("crew.security.settings.strict_security_enabled", lambda: True)
    monkeypatch.setattr(pipeline, "get_permission_config", lambda: load_permission_config([]))
    monkeypatch.setattr("crew.agent.loop.tool_runner.send_followup_question", fake_send)
    monkeypatch.setattr("crew.agent.loop.tool_runner.wait_for_answer", fake_wait)
    runner = ToolRunner(
        registry=registry,
        plugins=PluginManager([]),
        guardrails=ToolCallGuardrailController(),
        session_id="s1",
    )

    assert (
        await runner._check_permission(
            ToolCall(
                "1",
                "remote__send",
                {"api_key": secret, "payload": "x" * 20_000},
            )
        )
        is not None
    )
    assert secret not in captured[0]
    assert len(captured[0]) <= 4_500
