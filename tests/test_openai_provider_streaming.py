"""OpenAIProvider 流式工具派发单测：验证 ready_tool_call 的增量吐出。

mock 掉 OpenAI SDK 的 streaming，构造 SDK 风格的增量 chunk，断言 provider 在
index 切换 / 流结束时正确组装并提前 yield 每个工具（近似 content_block_stop）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.providers.openai_provider import OpenAIProvider, _merge_tool_argument_fragment

pytestmark = pytest.mark.asyncio


def _tc(index, *, id=None, name=None, args=None):
    """构造一段 SDK 风格的 tool_call 增量。"""
    return SimpleNamespace(
        index=index,
        id=id,
        function=SimpleNamespace(name=name, arguments=args),
    )


def _chunk(*, content=None, tool_calls=None, finish_reason=None, reasoning_content=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    if reasoning_content is not None:
        delta.reasoning_content = reasoning_content
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _provider() -> OpenAIProvider:
    return OpenAIProvider(api_key="sk-test", model="gpt-test")


async def _fake_stream(chunks):
    for c in chunks:
        yield c


async def test_stream_emits_ready_tool_call_per_index(monkeypatch):
    p = _provider()
    # index 0 分两段拼参；index 1 出现时 → index 0 应被判定完成并提前派发
    sdk_chunks = [
        _chunk(tool_calls=[_tc(0, id="call_a", name="file_read", args='{"path":')]),
        _chunk(tool_calls=[_tc(0, args='"/tmp/a.txt"}')]),
        _chunk(tool_calls=[_tc(1, id="call_b", name="web_search", args='{"q":"x"}')]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]

    ready = [c.ready_tool_call for c in out if c.ready_tool_call is not None]
    # 两个工具都被提前派发，且顺序为 index 顺序
    assert [r.id for r in ready] == ["call_a", "call_b"]
    assert ready[0].name == "file_read" and ready[0].arguments == {"path": "/tmp/a.txt"}
    assert ready[1].name == "web_search" and ready[1].arguments == {"q": "x"}

    # index 0 的 ready 必须在 index 1 的 ready 之前出现（流式重叠的前提）
    ready_positions = [i for i, c in enumerate(out) if c.ready_tool_call is not None]
    done_positions = [i for i, c in enumerate(out) if c.done]
    assert ready_positions[0] < ready_positions[1]
    # done 帧携带完整列表，且在所有 ready 之后
    done = out[done_positions[0]]
    assert [t.id for t in done.tool_calls] == ["call_a", "call_b"]
    assert ready_positions[-1] <= done_positions[0]


async def test_stream_single_tool_emits_ready_when_json_complete(monkeypatch):
    """单工具（最后一个、无后续 index 触发）：arguments 一拼成合法 JSON 即提前 yield
    ready_tool_call，不等流结束。修复「文本+单 file_write」场景下工具卡迟迟不出现。"""
    p = _provider()
    sdk_chunks = [
        _chunk(content="我来写一篇散文并保存到桌面。"),
        # file_write 参数分两段：path 先拼完，content 逐段拼
        _chunk(tool_calls=[_tc(0, id="call_x", name="file_write", args='{"path":"/a.html","content":"')]),
        _chunk(tool_calls=[_tc(0, args='<html>…</html>')]),  # content 字符串未闭合，整段 JSON 仍不合法
        _chunk(tool_calls=[_tc(0, args='"}')]),  # content 闭合 + 对象闭合 → JSON 首次合法
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    ready = [c.ready_tool_call for c in out if c.ready_tool_call is not None]
    assert len(ready) == 1 and ready[0].id == "call_x"
    assert ready[0].arguments == {"path": "/a.html", "content": "<html>…</html>"}
    # ready 必须在 done 帧之前出现（流式期间即派发，而非等流结束）
    ready_pos = next(i for i, c in enumerate(out) if c.ready_tool_call is not None)
    done_pos = next(i for i, c in enumerate(out) if c.done)
    assert ready_pos < done_pos
    # 文本 delta 正常透出
    assert any(c.delta_text == "我来写一篇散文并保存到桌面。" for c in out)


async def test_stream_strips_leaked_parameter_tags_from_nested_arguments(monkeypatch, caplog):
    p = _provider()
    sdk_chunks = [
        _chunk(tool_calls=[_tc(
            0,
            id="call_xml",
            name="skill_view",
            args=(
                '{"name":"mail-assistant</parameter>",'
                '"nested":{"values":["keep </parameter> inside",'
                '"SKILL.md</parameter>"]}}'
            ),
        )]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    ready = next(c.ready_tool_call for c in out if c.ready_tool_call is not None)
    done = next(c for c in out if c.done)
    expected = {
        "name": "mail-assistant",
        "nested": {"values": ["keep </parameter> inside", "SKILL.md"]},
    }
    assert ready.arguments == expected
    assert done.tool_calls[0].arguments == expected
    assert "清理模型工具参数中的泄漏标签" in caplog.text


async def test_stream_repairs_real_minimax_corrupted_raw_arguments(monkeypatch, caplog):
    p = _provider()
    corrupted = (
        '{"query":"北京 2026年7月15日 天气 最高气温</parameter",'
        '"freshness":"pw</parameter","summary":true"true</parametertrue,'
        '"count":5</parameter}'
    )
    sdk_chunks = [
        _chunk(tool_calls=[_tc(
            0,
            id="call_search",
            name="search__web_search",
            args=corrupted,
        )]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)
    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    ready = next(c.ready_tool_call for c in out if c.ready_tool_call is not None)
    assert ready.arguments == {
        "query": "北京 2026年7月15日 天气 最高气温",
        "freshness": "pw",
        "summary": True,
        "count": 5,
    }
    assert "修复模型泄漏标签导致的损坏 JSON" in caplog.text


async def test_stream_accepts_cumulative_argument_snapshots(monkeypatch):
    p = _provider()
    sdk_chunks = [
        _chunk(tool_calls=[_tc(
            0,
            id="call_snapshot",
            name="skill_view",
            args='{"name":"mail',
        )]),
        _chunk(tool_calls=[_tc(0, args='{"name":"mail-assistant"}')]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)
    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    ready = next(c.ready_tool_call for c in out if c.ready_tool_call is not None)
    assert ready.arguments == {"name": "mail-assistant"}


async def test_stream_preserves_repeated_character_at_delta_boundary(monkeypatch):
    """SiliconFlow 标准 delta 的相邻同字符必须全部保留。"""
    p = _provider()
    sdk_chunks = [
        _chunk(tool_calls=[_tc(
            0,
            id="call_delta",
            name="glob",
            args='{"path":"/crew/skills/AI-P',
        )]),
        _chunk(tool_calls=[_tc(0, args='PT-0618","pattern":"**/*"}')]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)
    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]

    ready = next(c.ready_tool_call for c in out if c.ready_tool_call is not None)
    done = next(c for c in out if c.done)
    expected = {"path": "/crew/skills/AI-PPT-0618", "pattern": "**/*"}
    assert ready.arguments == expected
    assert done.tool_calls[0].arguments == expected


async def test_shorter_prefix_fragment_is_not_discarded_as_old_snapshot():
    """标准 delta 即使等于累计串前缀也不能作为所谓旧快照丢弃。"""
    assert _merge_tool_argument_fragment("acct", "ac") == ("acctac", "delta")


async def test_non_stream_strips_leaked_parameter_tag(monkeypatch):
    p = _provider()
    tool_call = SimpleNamespace(
        id="call_xml",
        function=SimpleNamespace(
            name="skill_view",
            arguments='{"name":"mail-assistant</parameter>"}',
        ),
    )
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="", tool_calls=[tool_call]),
            finish_reason="tool_calls",
        )],
        usage=None,
    )

    async def fake_create(**kwargs):
        return response

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)
    result = await p.chat([], tools=[{"type": "function"}])
    assert result.tool_calls[0].arguments == {"name": "mail-assistant"}


async def test_stream_emits_tool_call_generating_when_name_arrives(monkeypatch):
    """工具 name 一出现即 yield tool_call_generating，早于 ready_tool_call。"""
    p = _provider()
    sdk_chunks = [
        _chunk(content="我来写散文并保存到桌面。"),
        # 第一个 tool_call delta：name+id 到达，arguments 空
        _chunk(tool_calls=[_tc(0, id="call_x", name="file_write", args='')]),
        # 后续逐段拼参数（content 长）
        _chunk(tool_calls=[_tc(0, args='{"path":"/a.html","content":"')]),
        _chunk(tool_calls=[_tc(0, args='<html>…</html>')]),  # content 字符串未闭合，JSON 不合法
        _chunk(tool_calls=[_tc(0, args='"}')]),  # 参数拼完
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    generating = [c.tool_call_generating for c in out if c.tool_call_generating is not None]
    ready = [c.ready_tool_call for c in out if c.ready_tool_call is not None]
    # name 出现即发 generating；path 字段闭合后同一 id 再更新一次 UI-only 参数
    assert [g.id for g in generating] == ["call_x", "call_x"]
    assert generating[0].name == "file_write" and generating[0].arguments == {}
    assert generating[1].arguments == {"path": "/a.html"}
    # ready 在参数拼完后发
    assert len(ready) == 1 and ready[0].arguments == {"path": "/a.html", "content": "<html>…</html>"}
    # generating 必须早于 ready（name 出现在参数拼完之前）
    seen_pos = next(i for i, c in enumerate(out) if c.tool_call_generating is not None)
    ready_pos = next(i for i, c in enumerate(out) if c.ready_tool_call is not None)
    assert seen_pos < ready_pos


async def test_stream_emits_tool_call_generating_when_name_arrives_before_id(monkeypatch):
    """OpenAI-compatible provider 可能 name 先到、id 很晚才到；UI 仍应立刻显示工具卡。"""
    p = _provider()
    sdk_chunks = [
        _chunk(content="我来写散文并保存到桌面。"),
        # name 先到，id 尚未到。这里仍应先发 tool_call_generating，避免 file_write
        # 卡片要等长 content 参数全部生成完才出现。
        _chunk(tool_calls=[_tc(0, id=None, name="file_write", args='')]),
        _chunk(tool_calls=[_tc(0, args='{"path":"/a.html","content":"')]),
        _chunk(tool_calls=[_tc(0, args='<html>…</html>')]),
        _chunk(tool_calls=[_tc(0, id="call_real_late", args='"}')]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    generating = [c.tool_call_generating for c in out if c.tool_call_generating is not None]
    ready = [c.ready_tool_call for c in out if c.ready_tool_call is not None]
    done = next(c for c in out if c.done)

    assert [g.id for g in generating] == ["call_stream_0", "call_stream_0"]
    assert generating[0].name == "file_write"
    assert generating[1].arguments == {"path": "/a.html"}
    assert len(ready) == 1
    assert ready[0].id == "call_stream_0"
    assert done.tool_calls[0].id == "call_stream_0"
    assert done.tool_calls[0].arguments == {"path": "/a.html", "content": "<html>…</html>"}
    seen_pos = next(i for i, c in enumerate(out) if c.tool_call_generating is not None)
    ready_pos = next(i for i, c in enumerate(out) if c.ready_tool_call is not None)
    assert seen_pos < ready_pos


async def test_stream_single_tool_no_ready_until_json_complete(monkeypatch):
    """单工具参数 JSON 一直不完整（content 字符串未闭合）期间不提前派发；流结束兜底。"""
    p = _provider()
    sdk_chunks = [
        _chunk(tool_calls=[_tc(0, id="call_x", name="file_write", args='{"path":"/a","content":"<html>')]),
        _chunk(tool_calls=[_tc(0, args='…still not closed')]),  # content 仍未闭合
        _chunk(finish_reason="tool_calls"),  # 流结束仍不合法 → done 帧用 _raw 兜底
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    ready = [c.ready_tool_call for c in out if c.ready_tool_call is not None]
    assert ready == []  # 从未提前 ready
    done = next(c for c in out if c.done)
    assert done.tool_calls and done.tool_calls[0].id == "call_x"
    # 参数不合法 → _raw 兜底
    assert set(done.tool_calls[0].arguments.keys()) == {"_raw"}


async def test_stream_emits_reasoning_content_incrementally(monkeypatch):
    """reasoning_content 应随流式增量透出，不能只在最终 done 帧一次性出现。"""
    p = _provider()
    sdk_chunks = [
        _chunk(reasoning_content="先分析"),
        _chunk(reasoning_content="，再结论"),
        _chunk(content="答案"),
        _chunk(finish_reason="stop"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([])]

    reasoning_deltas = [c.reasoning_content for c in out if c.reasoning_content and not c.done]
    assert reasoning_deltas == ["先分析", "，再结论"]
    assert any(c.delta_text == "答案" for c in out)
    done = next(c for c in out if c.done)
    assert done.reasoning_content == "先分析，再结论"


async def test_stream_emits_reasoning_activity_without_text(monkeypatch):
    """DeepSeek 等模型先吐 reasoning_content 时，上游应能感知活动但不把它混入正文。"""
    p = _provider()
    sdk_chunks = [
        _chunk(reasoning_content="内部推演"),
        _chunk(content='{"ok":true}', finish_reason="stop"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([])]

    assert any(c.reasoning_content == "内部推演" and not c.delta_text for c in out)
    assert "".join(c.delta_text for c in out) == '{"ok":true}'


async def test_stream_skips_ready_when_args_incomplete(monkeypatch):
    """参数 JSON 不完整时不提前派发；done 帧仍用 _raw 兜底组装。"""
    p = _provider()
    sdk_chunks = [
        # index 0 参数残缺，且 index 1 紧接出现 → 此时 index 0 不应被提前派发
        _chunk(tool_calls=[_tc(0, id="c0", name="file_read", args='{"path":"/a"')]),
        _chunk(tool_calls=[_tc(1, id="c1", name="web_search", args='{"q":"y"}')]),
        _chunk(finish_reason="tool_calls"),
    ]

    async def fake_create(**kwargs):
        return _fake_stream(sdk_chunks)

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([], tools=[{"type": "function"}])]
    ready_ids = [c.ready_tool_call.id for c in out if c.ready_tool_call is not None]
    # 残缺的 c0 不提前派发；c1 正常
    assert ready_ids == ["c1"]
    done = next(c for c in out if c.done)
    assert [t.id for t in done.tool_calls] == ["c0", "c1"]


async def test_stream_mid_interrupt_wraps_with_type_name(monkeypatch):
    """流式中途网关断连：已 emit 的 delta 保留，异常被包装成带类型名的 ProviderError，
    且 retryable/category 维持「可续写」判定（不破坏 builtin 的自愈续写）。"""
    import httpx

    from crew.core.errors import ProviderError
    from crew.agent.loop.resilience import is_stream_interrupt_recoverable

    p = _provider()

    async def fake_stream_interrupt():
        # 先吐一段文本，再模拟网关提前断连（httpx 原生异常原样冒泡）
        yield _chunk(content="前22个字符")
        raise httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body",
            request=httpx.Request("POST", "https://example/x"),
        )

    async def fake_create(**kwargs):
        return fake_stream_interrupt()

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    emitted = []
    with pytest.raises(ProviderError) as ei:
        async for c in p.stream_chat([]):
            if c.delta_text:
                emitted.append(c.delta_text)

    # 已 emit 文本不丢
    assert emitted == ["前22个字符"]
    err: ProviderError = ei.value
    # 消息带类型名，确保 str(exc) 为空时上层 WARNING 日志仍可诊断
    assert "RemoteProtocolError" in str(err)
    # 续写可恢复判定保持 True（category=connection, retryable=True）
    assert err.retryable is True
    assert err.category == "connection"
    assert is_stream_interrupt_recoverable(err) is True


async def test_stream_mid_interrupt_empty_message_still_names_type(monkeypatch):
    """异常 __str__ 为空时，ProviderError 消息仍含类型名，避免上层「尝试续写: 」后空白。"""
    from crew.core.errors import ProviderError

    p = _provider()

    class EmptyStrError(Exception):
        """模拟某些 SDK 包装后 __str__ 返回空的异常。"""

        def __str__(self) -> str:  # noqa: D401
            return ""

    async def fake_stream():
        yield _chunk(content="x")
        raise EmptyStrError()

    async def fake_create(**kwargs):
        return fake_stream()

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    with pytest.raises(ProviderError) as ei:
        async for _ in p.stream_chat([]):
            pass

    # 类型名出现在消息里，且空消息被 <无消息> 占位，避免日志空白
    assert "EmptyStrError" in str(ei.value)
    assert "<无消息>" in str(ei.value)


async def test_stream_mid_interrupt_with_partial_tool_routes_to_length(monkeypatch):
    """流式中断发生在 tool_call 生成阶段（partial args + 文本极少）：采用
    PARTIAL_STREAM_STUB——不丢半截 tool args、不 re-raise，改为产出 finish_reason=length
    + _raw partial tool_calls，交主循环截断自愈（bump-retry + split-guidance）。"""
    # 模拟 openai SDK 把底层 httpx 断连包成 APIError（消息含 "peer closed connection"）
    class WrappedAPIError(Exception):
        pass

    p = _provider()

    async def fake_stream():
        yield _chunk(tool_calls=[_tc(0, id="call_z", name="file_write", args='{"path":"/tmp/big.txt","content":"半截')])
        raise WrappedAPIError("peer closed connection without sending complete message body (incomplete chunked read)")

    async def fake_create(**kwargs):
        return fake_stream()

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    out = [c async for c in p.stream_chat([])]
    done = [c for c in out if c.done]
    assert len(done) == 1
    d = done[0]
    # 转为 length 截断信号（不 re-raise）
    assert d.finish_reason == "length"
    assert d.tool_calls and len(d.tool_calls) == 1
    tc = d.tool_calls[0]
    assert tc.name == "file_write"
    # partial args 走 _raw 兜底（JSON 不完整）
    assert set(tc.arguments.keys()) == {"_raw"}
    assert "/tmp/big.txt" in tc.arguments["_raw"]


async def test_stream_mid_interrupt_with_partial_tool_non_recoverable_still_raises(monkeypatch):
    """鉴权类错误即使有 partial tool 也不转 length（不可恢复，直接抛）。"""
    from crew.core.errors import ProviderError

    p = _provider()

    class AuthIsh(Exception):
        status_code = 401

    async def fake_stream():
        yield _chunk(tool_calls=[_tc(0, id="c", name="file_write", args='{"path":"x"')])
        raise AuthIsh("unauthorized")

    async def fake_create(**kwargs):
        return fake_stream()

    monkeypatch.setattr(p._client.chat.completions, "create", fake_create)

    with pytest.raises(ProviderError):
        async for _ in p.stream_chat([]):
            pass
