"""三层渐进式上下文压缩测试：crew/agent/compact。"""

from __future__ import annotations

from crew.agent.compact import estimate_tokens
from crew.agent.compact.microcompact import (
    CLEARED_PLACEHOLDER,
    FILE_UNCHANGED_STUB,
    TOOL_SUMMARY_PREFIX,
    micro_compact,
)
from crew.agent.compact.pipeline import ContextCompactor
from crew.agent.compact.store import SummaryState, SummaryStore
from crew.agent.compact.summary import SUMMARY_MARKER
from crew.core.errors import ProviderError
from crew.core.types import ChatResponse, Message, ToolCall


# --------------------------------------------------------------------------- #
# 测试替身
# --------------------------------------------------------------------------- #
class FakeProvider:
    """记录每次 chat 的入参，返回固定摘要。"""

    def __init__(self, reply: str = "摘要内容") -> None:
        self.reply = reply
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools=None):  # noqa: ANN001
        self.calls.append(messages)
        return ChatResponse(text=self.reply)

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        yield  # pragma: no cover


class BoomProvider:
    """chat 永远抛异常，用于验证容错。"""

    async def chat(self, messages, tools=None):  # noqa: ANN001
        raise RuntimeError("provider down")

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        yield  # pragma: no cover


def _tool_msg(i: int) -> Message:
    return Message.tool(tool_call_id=f"call_{i}", content=f"工具结果{i}" * 50, name="Read")


# --------------------------------------------------------------------------- #
# L1 MicroCompact
# --------------------------------------------------------------------------- #
def test_l1_clears_old_tool_results():
    msgs = [Message.user("开始")] + [_tool_msg(i) for i in range(10)]
    out = micro_compact(msgs, keep_recent_tools=3)

    assert len(out) == len(msgs)  # 长度不变
    tools = [m for m in out if m.role == "tool"]
    # 最近 3 条保留原内容，更早 7 条压缩为信息摘要（保留工具语义）
    assert all(m.content.startswith(TOOL_SUMMARY_PREFIX) for m in tools[:7])
    assert all(not m.content.startswith(TOOL_SUMMARY_PREFIX) for m in tools[7:])
    # 不再使用纯占位符（信息摘要替代）
    assert all(m.content != CLEARED_PLACEHOLDER for m in tools)
    # tool_call_id 全部保留
    assert [m.tool_call_id for m in tools] == [f"call_{i}" for i in range(10)]


def test_l1_noop_when_under_threshold():
    msgs = [Message.user("hi")] + [_tool_msg(i) for i in range(3)]
    out = micro_compact(msgs, keep_recent_tools=6)
    assert out is msgs  # 同一引用，瞬时 no-op


def test_l1_idempotent():
    msgs = [Message.user("开始")] + [_tool_msg(i) for i in range(10)]
    once = micro_compact(msgs, keep_recent_tools=3)
    twice = micro_compact(once, keep_recent_tools=3)
    assert [m.content for m in once] == [m.content for m in twice]


def test_l1_does_not_mutate_input():
    msgs = [_tool_msg(i) for i in range(10)]
    before = [m.content for m in msgs]
    micro_compact(msgs, keep_recent_tools=3)
    assert [m.content for m in msgs] == before  # 入参未被修改


def test_compact_preview_view_matches_real_l1_without_calling_provider():
    provider = FakeProvider()
    comp = ContextCompactor(
        provider,
        token_budget=1_000_000,
        keep_recent_tools=2,
        max_tool_result_chars=20_000,
    )
    msgs = [Message.user("开始")] + [_tool_msg(i) for i in range(12)]

    preview = comp.compact_preview_view(msgs)
    expected = micro_compact(msgs, keep_recent_tools=2, max_tool_result_chars=20_000)

    assert [message.content for message in preview] == [message.content for message in expected]
    assert estimate_tokens(preview) < estimate_tokens(msgs)
    assert provider.calls == []


# --------------------------------------------------------------------------- #
# L1 file_read 去重
# --------------------------------------------------------------------------- #
def _file_read_msgs(n: int, path: str, content_factory) -> list[Message]:
    """构造 n 轮 file_read 的 user/assistant/tool 消息。"""
    msgs: list[Message] = []
    for i in range(n):
        msgs.append(Message.user(f"第{i}轮：读取文件"))
        tool_calls = [ToolCall(id=f"call_{i}", name="file_read", arguments={"path": path})]
        msgs.append(Message.assistant("读取文件", tool_calls=tool_calls))
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=content_factory(i), name="file_read"))
    return msgs


def test_l1_file_read_dedup_same_content():
    """相同路径的 file_read 重复返回相同内容时，旧结果用 FILE_UNCHANGED_STUB。"""
    same_result = "相同的文件内容" * 100
    msgs = _file_read_msgs(5, "/tmp/test.txt", lambda _i: same_result)
    out = micro_compact(msgs, keep_recent_tools=1)
    tools = [m for m in out if m.role == "tool"]

    # 最近 1 条保留原样
    assert tools[4].content == same_result
    # 第 0 条首次出现，保留原内容
    assert tools[0].content == same_result
    # 第 1-3 条与最近一次相同，用 stub
    stub = FILE_UNCHANGED_STUB.format(path="/tmp/test.txt")
    assert all(t.content == stub for t in tools[1:4])


def test_l1_file_read_dedup_different_content():
    """相同路径的 file_read 返回不同内容时，各自保留原内容。"""
    msgs = _file_read_msgs(5, "/tmp/test.txt", lambda i: f"内容版本{i}" * 50)
    out = micro_compact(msgs, keep_recent_tools=1)
    tools = [m for m in out if m.role == "tool"]

    # 所有内容都不同，不应出现 stub
    assert all(FILE_UNCHANGED_STUB.split("{", 1)[0] not in t.content for t in tools[:4])
    assert tools[4].content == "内容版本4" * 50


def test_l1_file_read_dedup_multiple_paths():
    """不同路径的 file_read 独立去重，互不干扰。"""
    content_a = "文件A的内容" * 50
    content_b = "文件B的内容" * 50

    msgs: list[Message] = []
    for i in range(3):
        msgs.append(Message.user(f"第{i}轮：读A"))
        tc_a = [ToolCall(id=f"call_a_{i}", name="file_read", arguments={"path": "/tmp/a.txt"})]
        msgs.append(Message.assistant("读A", tool_calls=tc_a))
        msgs.append(Message.tool(tool_call_id=f"call_a_{i}", content=content_a, name="file_read"))

        msgs.append(Message.user(f"第{i}轮：读B"))
        tc_b = [ToolCall(id=f"call_b_{i}", name="file_read", arguments={"path": "/tmp/b.txt"})]
        msgs.append(Message.assistant("读B", tool_calls=tc_b))
        msgs.append(Message.tool(tool_call_id=f"call_b_{i}", content=content_b, name="file_read"))

    out = micro_compact(msgs, keep_recent_tools=2)
    tools = [m for m in out if m.role == "tool"]

    # 最近 2 条保留
    assert tools[4].content == content_a
    assert tools[5].content == content_b
    # 前 4 条：第 0 轮首次出现保留，第 1 轮重复用 stub
    assert tools[0].content == content_a
    assert tools[1].content == content_b
    assert tools[2].content == FILE_UNCHANGED_STUB.format(path="/tmp/a.txt")
    assert tools[3].content == FILE_UNCHANGED_STUB.format(path="/tmp/b.txt")


def test_l1_non_file_read_tools_use_summary():
    """非 file_read 工具被清理时生成信息摘要（保留工具名/行数），不再用纯占位符。"""
    msgs = [Message.user("开始")]
    for i in range(5):
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=f"terminal结果{i}" * 50, name="terminal"))
    out = micro_compact(msgs, keep_recent_tools=1)
    tools = [m for m in out if m.role == "tool"]
    assert tools[4].content == "terminal结果4" * 50  # 最近 1 条保留原内容
    # 前 4 条压缩为信息摘要，保留工具名
    assert all(m.content.startswith(TOOL_SUMMARY_PREFIX) for m in tools[:4])
    assert all("[terminal]" in m.content for m in tools[:4])


def test_l1_no_tool_name_falls_back_to_placeholder():
    """完全拿不到工具名（无 tool_call_map 且 m.name 为空）时降级为纯占位符。"""
    msgs = [Message.user("开始")]
    for i in range(5):
        # name="" 且无对应 assistant(tool_calls)
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=f"结果{i}" * 50, name=""))
    out = micro_compact(msgs, keep_recent_tools=1)
    tools = [m for m in out if m.role == "tool"]
    assert tools[4].content == "结果4" * 50
    assert all(m.content == CLEARED_PLACEHOLDER for m in tools[:4])


def test_l1_file_read_dedup_idempotent():
    """已包含 FILE_UNCHANGED_STUB 的结果再次压缩应保持不变。"""
    same_result = "相同的文件内容" * 50
    msgs = _file_read_msgs(5, "/tmp/test.txt", lambda _i: same_result)
    once = micro_compact(msgs, keep_recent_tools=1)
    twice = micro_compact(once, keep_recent_tools=1)
    assert [m.content for m in once] == [m.content for m in twice]


def test_l1_file_read_without_tool_call_map_fallback():
    """file_read 消息找不到 assistant(tool_calls) 且无参数时，用 m.name 生成信息摘要。"""
    msgs = [
        Message.tool(tool_call_id="call_0", content="文件内容" * 50, name="file_read"),
        Message.tool(tool_call_id="call_1", content="文件内容" * 50, name="file_read"),
    ]
    out = micro_compact(msgs, keep_recent_tools=1)
    tools = [m for m in out if m.role == "tool"]
    assert tools[1].content == "文件内容" * 50  # 最近 1 条保留
    # 无参数的 file_read 降级为信息摘要（保留工具名 + 内容长度）
    assert tools[0].content.startswith(TOOL_SUMMARY_PREFIX)
    assert "[file_read]" in tools[0].content


# --------------------------------------------------------------------------- #
# Post-Compact 文件恢复
# --------------------------------------------------------------------------- #
def _build_file_read_history(paths_contents: list[tuple[str, str]]) -> list[Message]:
    """构造多轮 file_read 历史。"""
    msgs: list[Message] = []
    for i, (path, content) in enumerate(paths_contents):
        msgs.append(Message.user(f"第{i}轮"))
        tc = [ToolCall(id=f"call_{i}", name="file_read", arguments={"path": path})]
        msgs.append(Message.assistant("读取文件", tool_calls=tc))
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=content, name="file_read"))
    return msgs


def test_post_compact_collects_recent_file_contents():
    """从 old 段中收集最近读取的文件内容。"""
    from crew.agent.compact.post_compact import collect_recent_file_contents

    msgs = _build_file_read_history([
        ("/tmp/a.txt", "A 旧内容"),
        ("/tmp/a.txt", "A 新内容"),
        ("/tmp/b.txt", "B 内容"),
    ])
    contents = collect_recent_file_contents(msgs, max_files=3, max_chars_per_file=5000)
    assert contents == {"/tmp/a.txt": "A 新内容", "/tmp/b.txt": "B 内容"}


def test_post_compact_limits_file_count_and_length():
    """最多保留 N 个文件，且单文件超长时截断。"""
    from crew.agent.compact.post_compact import collect_recent_file_contents

    msgs = _build_file_read_history([
        ("/tmp/a.txt", "A"),
        ("/tmp/b.txt", "B"),
        ("/tmp/c.txt", "C"),
        ("/tmp/d.txt", "D" * 100),
    ])
    contents = collect_recent_file_contents(msgs, max_files=2, max_chars_per_file=10)
    # 只保留最近 2 个文件（按路径排序后取最后）
    assert set(contents.keys()) == {"/tmp/c.txt", "/tmp/d.txt"}
    assert contents["/tmp/d.txt"].startswith("D" * 10)
    assert "内容已截断" in contents["/tmp/d.txt"]


def test_post_compact_builds_attachment_message():
    """构造的附件消息包含文件路径和内容。"""
    from crew.agent.compact.post_compact import (
        POST_COMPACT_FILES_MARKER,
        build_post_compact_file_attachments,
    )

    msgs = _build_file_read_history([
        ("/tmp/a.txt", "A 内容"),
        ("/tmp/b.txt", "B 内容"),
    ])
    attachments = build_post_compact_file_attachments(msgs, max_files=3, max_chars_per_file=5000)
    assert len(attachments) == 1
    assert attachments[0].role == "system"
    assert POST_COMPACT_FILES_MARKER in attachments[0].content
    assert "### 文件：/tmp/a.txt" in attachments[0].content
    assert "A 内容" in attachments[0].content
    assert "B 内容" in attachments[0].content


def test_post_compact_no_attachments_when_no_files():
    """没有 file_read 结果时返回空列表。"""
    from crew.agent.compact.post_compact import build_post_compact_file_attachments

    msgs = [
        Message.user("开始"),
        Message.assistant("hi"),
    ]
    attachments = build_post_compact_file_attachments(msgs)
    assert attachments == []


# --------------------------------------------------------------------------- #
# 摘要输入预处理：媒体剥离 + 参数截断
# --------------------------------------------------------------------------- #
def test_summary_strips_base64_image_content():
    """_transcript 应剥离消息内容中的 base64/图片数据。"""
    from crew.agent.compact.summary import _transcript

    base64_data = "data:image/png;base64," + "A" * 200
    msgs = [
        Message.user("请看图"),
        Message.assistant(f"图片内容：{base64_data}"),
    ]
    transcript = _transcript(msgs)
    assert base64_data not in transcript
    assert "[base64 内容已剥离]" in transcript


def test_summary_strips_long_base64_without_data_uri():
    """_transcript 应剥离没有 data URI 前缀的长 base64 串。"""
    from crew.agent.compact.summary import _transcript

    base64_data = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" * 10
    msgs = [Message.assistant(f"结果：{base64_data}")]
    transcript = _transcript(msgs)
    assert base64_data not in transcript
    assert "[base64 内容已剥离]" in transcript


def test_summary_truncates_long_tool_arguments():
    """_transcript 应截断工具参数中的超长字符串值。"""
    from crew.agent.compact.summary import _transcript

    long_value = "x" * 5000
    msgs = [
        Message.user("执行工具"),
        Message.assistant(
            "执行",
            tool_calls=[ToolCall(id="c1", name="write", arguments={"content": long_value, "path": "/tmp/x.txt"})],
        ),
    ]
    transcript = _transcript(msgs)
    assert long_value not in transcript
    assert "内容已截断" in transcript
    assert "/tmp/x.txt" in transcript


def test_summary_keeps_short_tool_arguments_intact():
    """_transcript 不截断短字符串值。"""
    from crew.agent.compact.summary import _transcript

    msgs = [
        Message.user("执行工具"),
        Message.assistant(
            "执行",
            tool_calls=[ToolCall(id="c1", name="terminal", arguments={"command": "echo hi"})],
        ),
    ]
    transcript = _transcript(msgs)
    assert 'echo hi' in transcript
    assert "内容已截断" not in transcript


# --------------------------------------------------------------------------- #
# L1 单条 tool result 预算
# --------------------------------------------------------------------------- #
def test_l1_truncates_long_tool_result():
    """max_tool_result_chars 会截断单条过长的 tool result。"""
    long_content = "A" * 1000
    msgs = [
        Message.user("执行"),
        Message.assistant("执行", tool_calls=[ToolCall(id="c1", name="terminal", arguments={})]),
        Message.tool(tool_call_id="c1", content=long_content, name="terminal"),
    ]
    out = micro_compact(msgs, keep_recent_tools=1, max_tool_result_chars=100)
    tool_msg = [m for m in out if m.role == "tool"][0]
    assert len(tool_msg.content) < len(long_content)
    assert "tool result 已截断" in tool_msg.content


def test_l1_keeps_short_tool_result_unchanged():
    """未超过预算的 tool result 不被截断。"""
    short_content = "short result"
    msgs = [
        Message.user("执行"),
        Message.assistant("执行", tool_calls=[ToolCall(id="c1", name="terminal", arguments={})]),
        Message.tool(tool_call_id="c1", content=short_content, name="terminal"),
    ]
    out = micro_compact(msgs, keep_recent_tools=1, max_tool_result_chars=100)
    tool_msg = [m for m in out if m.role == "tool"][0]
    assert tool_msg.content == short_content


# --------------------------------------------------------------------------- #
# 边界对齐
# --------------------------------------------------------------------------- #
def test_safe_split_does_not_break_tool_pairs():
    # assistant(tool_calls) 后跟 tool 结果，split 必须落在 user 边界
    msgs = [
        Message.user("q1"),
        Message.assistant("", [ToolCall(id="c1", name="Read")]),
        Message.tool("c1", "结果"),
        Message.user("q2"),
        Message.assistant("", [ToolCall(id="c2", name="Read")]),
        Message.tool("c2", "结果"),
    ]
    split = ContextCompactor._safe_split(msgs, keep_recent=2)
    # 倒数 2 是 [assistant, tool]，向前回退到 user 边界（下标 3）
    assert msgs[split].role == "user"


# --------------------------------------------------------------------------- #
# L2 / L3 摘要
# --------------------------------------------------------------------------- #
async def _big_history(n_pairs: int) -> list[Message]:
    """构造 n_pairs 轮 user/assistant 的长历史。"""
    msgs: list[Message] = []
    for i in range(n_pairs):
        msgs.append(Message.user(f"问题{i} " * 100))
        msgs.append(Message.assistant(f"回答{i} " * 100))
    return msgs


async def test_l3_full_then_l2_incremental():
    provider = FakeProvider(reply="结构化摘要")
    comp = ContextCompactor(
        provider,
        token_budget=10,  # 极低，必触发摘要
        keep_recent=2,
        l2_delta_threshold=10,  # 极低，新增也走增量 LLM
    )
    history = await _big_history(10)

    # 第一次：无缓存 → L3 全量
    out1 = await comp.maybe_compact(history, "s1")
    assert out1[0].role == "system" and out1[0].content.startswith(SUMMARY_MARKER)
    assert len(provider.calls) == 1
    # L3 收到的是整段 old（很多消息）
    l3_transcript = provider.calls[0][1].content
    assert "问题0" in l3_transcript

    # 追加几轮后再压缩：有缓存 → L2 增量（只摘要新增）
    history2 = history + [Message.user("新问题 " * 100), Message.assistant("新回答 " * 100)]
    out2 = await comp.maybe_compact(history2, "s1")
    assert len(provider.calls) == 2
    # L2 增量：只摘要「上轮从 recent 滑出、本轮落入 old」的 delta（问题9），
    # 已折叠进缓存摘要的最早内容（问题0）不再重复发送。
    merge_input = provider.calls[1][1].content
    assert "已有摘要" in merge_input
    assert "问题9" in merge_input
    assert "问题0" not in merge_input
    assert out2[0].content.startswith(SUMMARY_MARKER)
    # 新增轮次仍在 recent 窗口内逐字保留，未被摘要
    assert any("新问题" in m.content for m in out2[1:])


async def test_l2_pure_rule_no_llm_when_delta_small():
    provider = FakeProvider(reply="结构化摘要")
    comp = ContextCompactor(
        provider,
        token_budget=10,
        keep_recent=2,
        l2_delta_threshold=100000,  # 极高，新增永远低于阈值 → 纯规则
    )
    history = await _big_history(10)

    await comp.maybe_compact(history, "s2")  # L3 建缓存
    assert len(provider.calls) == 1

    # 追加少量轮次 → L2 纯规则，复用旧摘要，不再调 LLM
    history2 = history + [Message.user("小问题"), Message.assistant("小回答")]
    out = await comp.maybe_compact(history2, "s2")
    assert len(provider.calls) == 1  # provider 未被再次调用
    assert out[0].content.startswith(SUMMARY_MARKER)


async def test_compaction_resilient_to_provider_failure():
    comp = ContextCompactor(BoomProvider(), token_budget=10, keep_recent=2)
    history = await _big_history(10)
    out = await comp.maybe_compact(history, "s3")
    # 摘要失败 → 返回 L1 处理后的列表，不抛异常（长度不应被错误截断为摘要+recent）
    assert out is not None
    assert len(out) == len(history)


async def test_disabled_returns_input():
    comp = ContextCompactor(FakeProvider(), enabled=False, token_budget=1)
    history = await _big_history(5)
    out = await comp.maybe_compact(history, "s4")
    assert out is history


async def test_under_budget_skips_summary():
    provider = FakeProvider()
    comp = ContextCompactor(provider, token_budget=10_000_000, keep_recent=2)
    history = await _big_history(3)
    out = await comp.maybe_compact(history, "s5")
    assert len(provider.calls) == 0  # 未超预算，不摘要
    assert out is not None


def test_estimate_tokens():
    msgs = [Message.user("a" * 400)]
    # 旧实现：400 / 4 = 100
    # 新实现：ASCII 400 字符，bytes_estimate=133, chars_estimate=100,
    #        base=133, padding=133*4//3=177
    assert estimate_tokens(msgs) == 177


# --------------------------------------------------------------------------- #
# PTL 砍头重试
# --------------------------------------------------------------------------- #
class OverflowThenOkProvider:
    """前 fail_times 次抛上下文溢出，之后成功返回摘要。"""

    def __init__(self, fail_times: int, reply: str = "摘要") -> None:
        self.fail_times = fail_times
        self.reply = reply
        self.calls: list[list[Message]] = []

    async def chat(self, messages, tools=None):  # noqa: ANN001
        self.calls.append(messages)
        if len(self.calls) <= self.fail_times:
            raise ProviderError("maximum context length exceeded", retryable=False)
        return ChatResponse(text=self.reply)

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        yield  # pragma: no cover


async def test_l3_ptl_truncates_head_and_retries():
    provider = OverflowThenOkProvider(fail_times=2, reply="砍头后成功")
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    history = await _big_history(12)
    out = await comp.maybe_compact(history, "ptl1")
    # 前两次溢出 → 砍头重试 → 第三次成功
    assert len(provider.calls) == 3
    assert out[0].content.startswith(SUMMARY_MARKER) and "砍头后成功" in out[0].content
    # 每次重试发送的 transcript 越来越短
    lens = [len(c[1].content) for c in provider.calls]
    assert lens[0] > lens[1] > lens[2]


async def test_l3_ptl_gives_up_returns_input():
    provider = OverflowThenOkProvider(fail_times=99)  # 永远溢出
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    history = await _big_history(12)
    out = await comp.maybe_compact(history, "ptl2")
    # 多次砍头仍失败 → 返回 L1 处理后的列表，不崩
    assert len(out) == len(history)


# --------------------------------------------------------------------------- #
# 防抖 anti-thrash
# --------------------------------------------------------------------------- #
async def test_anti_thrash_skips_after_two_ineffective():
    provider = FakeProvider()
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    # 预置：该 session 已连续 2 次无效压缩
    comp._mem[("", "t1")] = SummaryState(text="旧摘要", covered_count=0, ineffective_count=2)
    history = await _big_history(10)
    out = await comp.maybe_compact(history, "t1")
    # 跳过摘要：provider 未被调用，结果不含摘要标记
    assert len(provider.calls) == 0
    assert not out[0].content.startswith(SUMMARY_MARKER)


async def test_ineffective_count_increments_on_low_savings():
    # summary 巨大 → 压缩后反而更大 → 省 <10% → 计数 +1
    provider = FakeProvider(reply="摘要" * 50000)
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    history = await _big_history(10)
    await comp.maybe_compact(history, "t2")
    assert comp._mem[("", "t2")].ineffective_count == 1


async def test_ineffective_count_resets_on_good_savings():
    provider = FakeProvider(reply="短摘要")  # 极小 → 省很多
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    comp._mem[("", "t3")] = SummaryState(text="x", covered_count=0, ineffective_count=1)
    history = await _big_history(10)
    await comp.maybe_compact(history, "t3")
    assert comp._mem[("", "t3")].ineffective_count == 0


async def test_force_compact_bypasses_anti_thrash_and_resets():
    provider = FakeProvider(reply="短摘要")
    comp = ContextCompactor(provider, token_budget=10, keep_recent=4)
    comp._mem[("", "t4")] = SummaryState(text="x", covered_count=0, ineffective_count=2)
    history = await _big_history(10)
    out = await comp.force_compact(history, "t4")
    # 兜底不受防抖限制：照常压缩并把计数清零
    assert out[0].content.startswith(SUMMARY_MARKER)
    assert comp._mem[("", "t4")].ineffective_count == 0


# --------------------------------------------------------------------------- #
# 断路器：连续摘要失败
# --------------------------------------------------------------------------- #
class CountingBoomProvider:
    """前 N 次调用记录次数并抛异常，用于验证断路器。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("provider down")

    async def stream_chat(self, messages, tools=None):  # noqa: ANN001
        yield  # pragma: no cover


async def test_circuit_breaker_skips_after_three_failures():
    """连续 3 次摘要失败后，第 4 次应跳过摘要，不再调用 provider。"""
    provider = CountingBoomProvider()
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    history = await _big_history(10)

    for _ in range(3):
        await comp.maybe_compact(history, "cb1")
    assert provider.calls == 3

    # 第 4 次：provider 不应再被调用
    out = await comp.maybe_compact(history, "cb1")
    assert provider.calls == 3
    assert not any(SUMMARY_MARKER in (m.content or "") for m in out)


async def test_circuit_breaker_resets_on_success():
    """摘要成功后，断路器计数清零。"""
    provider = BoomProvider()
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    history = await _big_history(10)

    for _ in range(2):
        await comp.maybe_compact(history, "cb2")
    assert comp._failure_counts.get(("", "cb2"), 0) == 2

    # 换成功 provider 后再压缩
    comp.provider = FakeProvider(reply="摘要")
    await comp.maybe_compact(history, "cb2")
    assert comp._failure_counts.get(("", "cb2"), 0) == 0


# --------------------------------------------------------------------------- #
# L2 缓存持久化（SQLite）
# --------------------------------------------------------------------------- #
def test_summary_store_roundtrip(tmp_path):
    store = SummaryStore(str(tmp_path / "c.db"))
    assert store.get("s") is None
    store.put("s", SummaryState(text="摘要A", covered_count=5, ineffective_count=1))
    got = store.get("s")
    assert got.text == "摘要A" and got.covered_count == 5 and got.ineffective_count == 1
    # 覆盖更新
    store.put("s", SummaryState(text="摘要B", covered_count=9))
    got2 = store.get("s")
    assert got2.text == "摘要B" and got2.covered_count == 9 and got2.ineffective_count == 0
    store.delete("s")
    assert store.get("s") is None


def test_summary_store_is_owner_scoped(tmp_path):
    store = SummaryStore(str(tmp_path / "c.db"))
    store.put("same", SummaryState(text="摘要A", covered_count=5), owner_account_id="A:uid-a")
    store.put("same", SummaryState(text="摘要B", covered_count=7), owner_account_id="B:uid-b")

    assert store.get("same", owner_account_id="A:uid-a").text == "摘要A"
    assert store.get("same", owner_account_id="B:uid-b").text == "摘要B"
    assert store.get("same") is None


async def test_compactor_in_memory_cache_is_owner_scoped():
    history = await _big_history(10)
    comp = ContextCompactor(
        FakeProvider(reply="摘要A"),
        token_budget=10,
        keep_recent=2,
        l2_delta_threshold=100000,
        store=None,
    )
    await comp.maybe_compact(history, "same", owner_account_id="A:uid-a")
    comp.provider = FakeProvider(reply="摘要B")
    await comp.maybe_compact(history, "same", owner_account_id="B:uid-b")

    assert comp._mem[("A:uid-a", "same")].text == "摘要A"
    assert comp._mem[("B:uid-b", "same")].text == "摘要B"


async def test_l2_reuse_persists_across_instances(tmp_path):
    db = str(tmp_path / "c.db")
    # 实例 1：首次压缩，摘要落盘
    p1 = FakeProvider(reply="持久摘要")
    comp1 = ContextCompactor(
        p1, token_budget=10, keep_recent=2,
        l2_delta_threshold=100000,  # 高阈值 → 后续走纯规则
        store=SummaryStore(db),
    )
    history = await _big_history(10)
    await comp1.maybe_compact(history, "sp")
    assert len(p1.calls) == 1

    # 实例 2：新进程模拟（新 provider + 新 store 连同一 db 文件）
    p2 = FakeProvider(reply="不应被调用")
    comp2 = ContextCompactor(
        p2, token_budget=10, keep_recent=2,
        l2_delta_threshold=100000,
        store=SummaryStore(db),
    )
    history2 = history + [Message.user("小问题"), Message.assistant("小回答")]
    out = await comp2.maybe_compact(history2, "sp")
    # 命中持久化的旧摘要 + 新增很小 → L2 纯规则，零 LLM
    assert len(p2.calls) == 0
    assert "持久摘要" in out[0].content


# --------------------------------------------------------------------------- #
# compact_view：executor 循环内每轮调用的视图压缩（stateless 相对 canonical L2）
# --------------------------------------------------------------------------- #
async def test_compact_view_under_budget_no_llm():
    """未触水位：只跑 L1 micro_compact，不调 LLM。"""
    provider = FakeProvider()
    comp = ContextCompactor(provider, token_budget=10_000_000, keep_recent=2)
    msgs = [Message.user("hello world " * 5) for _ in range(5)]
    out = await comp.compact_view(msgs, "s1")
    assert provider.calls == []  # 零 LLM
    assert len(out) == len(msgs)  # 无工具结果可清理，原样返回


async def test_compact_view_includes_fixed_request_overhead_in_threshold():
    """system/tools 等统一请求开销必须参与 compact 水位判断。"""
    provider = FakeProvider(reply="短摘要")
    comp = ContextCompactor(provider, token_budget=100, keep_recent=2)
    msgs = [Message.user("small") for _ in range(5)]

    assert comp.will_compact_view(
        msgs,
        "s-overhead",
        prompt_overhead_tokens=1_000,
    ) is True
    out = await comp.compact_view(
        msgs,
        "s-overhead",
        prompt_overhead_tokens=1_000,
    )

    assert len(provider.calls) == 1
    assert any(SUMMARY_MARKER in (message.content or "") for message in out)


async def test_compact_view_over_budget_triggers_l3():
    """超水位：走 L3 全量摘要，视图变短且含 SUMMARY_MARKER。"""
    provider = FakeProvider(reply="短摘要")
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    msgs = [Message.user("hello world " * 50) for _ in range(5)]
    out = await comp.compact_view(msgs, "s1")
    assert len(provider.calls) == 1  # L3 调一次
    assert len(out) < len(msgs)
    assert any(SUMMARY_MARKER in (m.content or "") for m in out)


async def test_compact_view_does_not_pollute_canonical_state():
    """compact_view 不读写 canonical L2 state：maybe_compact 写入的 covered_count 不被改写。"""
    provider = FakeProvider(reply="摘要")
    comp = ContextCompactor(provider, token_budget=10, keep_recent=2)
    canonical = [Message.user("x" * 200) for _ in range(5)]
    await comp.maybe_compact(canonical, "s1")  # 写 canonical state
    canonical_state = comp._get_state("s1")
    assert canonical_state is not None

    view = [Message.user("y" * 200) for _ in range(5)]
    await comp.compact_view(view, "s1")
    await comp.compact_view(view, "s1")  # 反复调，仍不污染
    assert comp._get_state("s1") == canonical_state  # canonical state 不变


async def test_compact_view_circuit_breaker_after_consecutive_failures():
    """连续 3 次摘要失败后断路器跳过，不再调 LLM。"""
    comp = ContextCompactor(BoomProvider(), token_budget=10, keep_recent=2)
    msgs = [Message.user("x" * 200) for _ in range(5)]
    for _ in range(3):
        await comp.compact_view(msgs, "s1")  # 3 次失败，累计断路器
    out = await comp.compact_view(msgs, "s1")  # 第 4 次：断路器跳过
    assert len(out) == len(msgs)  # 原样返回
