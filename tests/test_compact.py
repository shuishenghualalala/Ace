"""三层渐进式上下文压缩测试：crew/agent/compact。"""

from __future__ import annotations

from crew.agent.compact import estimate_tokens
from crew.agent.compact.microcompact import (
    CLEARED_PLACEHOLDER,
    FILE_UNCHANGED_STUB,
    INSTRUCTION_REPLACED_STUB,
    RESOURCE_REPLACED_STUB,
    TOOL_SUMMARY_PREFIX,
    micro_compact,
)
from crew.agent.compact.pipeline import ContextCompactor
from crew.agent.compact.store import SummaryState, SummaryStore
from crew.agent.compact.summary import SUMMARY_MARKER
from crew.core.errors import ProviderError
from crew.core.interfaces import ToolResultPolicy, ToolResultRetention
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


def _test_result_policy(tool_name: str, args: dict) -> ToolResultPolicy:
    if tool_name in {"Read", "terminal"}:
        return ToolResultPolicy(ToolResultRetention.TEMPORARY)
    if tool_name == "file_read":
        path = str(args.get("path") or "")
        return ToolResultPolicy(
            ToolResultRetention.RESOURCE,
            identity=f"path={path}" if path else "",
        )
    if tool_name == "skill_view":
        name = str(args.get("name") or "")
        file_path = str(args.get("file_path") or "")
        if file_path:
            return ToolResultPolicy(
                ToolResultRetention.RESOURCE,
                identity=f"skill={name}|file={file_path}",
            )
        return ToolResultPolicy(
            ToolResultRetention.INSTRUCTION,
            identity=f"skill={name}",
        )
    return ToolResultPolicy()


# --------------------------------------------------------------------------- #
# L1 MicroCompact
# --------------------------------------------------------------------------- #
def test_l1_clears_old_tool_results():
    msgs = [Message.user("开始")] + [_tool_msg(i) for i in range(10)]
    out = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=_test_result_policy
    )

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
    out = micro_compact(
        msgs, keep_recent_tools=6, result_policy_resolver=_test_result_policy
    )
    assert out is msgs  # 同一引用，瞬时 no-op


def test_l1_idempotent():
    msgs = [Message.user("开始")] + [_tool_msg(i) for i in range(10)]
    once = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=_test_result_policy
    )
    twice = micro_compact(
        once, keep_recent_tools=3, result_policy_resolver=_test_result_policy
    )
    assert [m.content for m in once] == [m.content for m in twice]


def test_l1_does_not_mutate_input():
    msgs = [_tool_msg(i) for i in range(10)]
    before = [m.content for m in msgs]
    micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=_test_result_policy
    )
    assert [m.content for m in msgs] == before  # 入参未被修改


def test_compact_preview_view_matches_real_l1_without_calling_provider():
    provider = FakeProvider()
    comp = ContextCompactor(
        provider,
        token_budget=1_000_000,
        keep_recent_tools=2,
        max_tool_result_chars=20_000,
        result_policy_resolver=_test_result_policy,
    )
    msgs = [Message.user("开始")] + [_tool_msg(i) for i in range(12)]

    preview = comp.compact_preview_view(msgs)
    expected = micro_compact(
        msgs,
        keep_recent_tools=2,
        max_tool_result_chars=20_000,
        result_policy_resolver=_test_result_policy,
    )

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
    out = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]

    # 最近 1 条保留原样
    assert tools[4].content == same_result
    # 同一资源只保留最新版本，前 4 条相同内容都用 unchanged stub。
    stub = FILE_UNCHANGED_STUB.format(path="/tmp/test.txt")
    assert all(t.content == stub for t in tools[:4])


def test_l1_file_read_dedup_different_content():
    """相同路径的 file_read 返回不同内容时，各自保留原内容。"""
    msgs = _file_read_msgs(5, "/tmp/test.txt", lambda i: f"内容版本{i}" * 50)
    out = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]

    # 旧版本明确标记为已被新资源替换，最新版本保持完整。
    assert all(
        t.content.startswith(RESOURCE_REPLACED_STUB.split("{", 1)[0])
        for t in tools[:4]
    )
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

    out = micro_compact(
        msgs, keep_recent_tools=2, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]

    # 每个资源只保留最近版本。
    assert tools[4].content == content_a
    assert tools[5].content == content_b
    assert tools[0].content == FILE_UNCHANGED_STUB.format(path="/tmp/a.txt")
    assert tools[1].content == FILE_UNCHANGED_STUB.format(path="/tmp/b.txt")
    assert tools[2].content == FILE_UNCHANGED_STUB.format(path="/tmp/a.txt")
    assert tools[3].content == FILE_UNCHANGED_STUB.format(path="/tmp/b.txt")


def test_l1_non_file_read_tools_use_summary():
    """非 file_read 工具被清理时生成信息摘要（保留工具名/行数），不再用纯占位符。"""
    msgs = [Message.user("开始")]
    for i in range(5):
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=f"terminal结果{i}" * 50, name="terminal"))
    out = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]
    assert tools[4].content == "terminal结果4" * 50  # 最近 1 条保留原内容
    # 前 4 条压缩为信息摘要，保留工具名
    assert all(m.content.startswith(TOOL_SUMMARY_PREFIX) for m in tools[:4])
    assert all("[terminal]" in m.content for m in tools[:4])


def test_l1_unknown_tool_defaults_to_important():
    """拿不到工具名时按重要结果保护，未知插件结果不会被误清理。"""
    msgs = [Message.user("开始")]
    for i in range(5):
        # name="" 且无对应 assistant(tool_calls)
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=f"结果{i}" * 50, name=""))
    out = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]
    assert tools[4].content == "结果4" * 50
    assert all(m.content == f"结果{i}" * 50 for i, m in enumerate(tools))


def test_l1_file_read_dedup_idempotent():
    """已包含 FILE_UNCHANGED_STUB 的结果再次压缩应保持不变。"""
    same_result = "相同的文件内容" * 50
    msgs = _file_read_msgs(5, "/tmp/test.txt", lambda _i: same_result)
    once = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    twice = micro_compact(
        once, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    assert [m.content for m in once] == [m.content for m in twice]


def test_l1_resource_without_identity_defaults_to_full_result():
    """资源缺少稳定标识时不猜测、不清理，避免误删未知内容。"""
    msgs = [
        Message.tool(tool_call_id="call_0", content="文件内容" * 50, name="file_read"),
        Message.tool(tool_call_id="call_1", content="文件内容" * 50, name="file_read"),
    ]
    out = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]
    assert tools[1].content == "文件内容" * 50  # 最近 1 条保留
    assert tools[0].content == "文件内容" * 50


def test_l1_many_temporary_results_do_not_evict_loaded_skill():
    """Skill 后出现超过保留窗口的并行结果时，完整指令仍对模型可见。"""
    skill_call = ToolCall(
        id="skill_call",
        name="skill_view",
        arguments={"name": "sites-building"},
    )
    skill_content = "完整建站指令" * 100
    msgs = [
        Message.user("创建网站"),
        Message.assistant("读取技能", tool_calls=[skill_call]),
        Message.tool("skill_call", skill_content, name="skill_view"),
    ]
    for i in range(12):
        msgs.append(
            Message.tool(
                tool_call_id=f"terminal_{i}",
                content=f"临时输出{i}" * 50,
                name="terminal",
            )
        )

    out = micro_compact(
        msgs, keep_recent_tools=3, result_policy_resolver=_test_result_policy
    )
    skill_result = next(m for m in out if m.tool_call_id == "skill_call")
    terminal_results = [m for m in out if m.name == "terminal"]
    assert skill_result.content == skill_content
    assert sum(m.content.startswith(TOOL_SUMMARY_PREFIX) for m in terminal_results) == 9


def test_l1_important_result_does_not_consume_temporary_window():
    """用户回答等重要结果既不被清理，也不挤占临时结果保留窗口。"""
    msgs = [
        Message.tool("answer", '{"answers":[{"value":"保留方案A"}]}', name="ask_followup_question"),
        *[
            Message.tool(f"terminal_{i}", f"输出{i}" * 50, name="terminal")
            for i in range(5)
        ],
    ]
    out = micro_compact(
        msgs, keep_recent_tools=2, result_policy_resolver=_test_result_policy
    )
    assert out[0].content == '{"answers":[{"value":"保留方案A"}]}'
    assert all(m.content.startswith(TOOL_SUMMARY_PREFIX) for m in out[1:4])
    assert all(not m.content.startswith(TOOL_SUMMARY_PREFIX) for m in out[4:])


def test_l1_instruction_keeps_only_latest_loaded_version():
    msgs: list[Message] = []
    for i, content in enumerate(("旧版指令", "新版指令")):
        call = ToolCall(
            id=f"skill_{i}",
            name="skill_view",
            arguments={"name": "sites-building"},
        )
        msgs.extend(
            [
                Message.assistant("读取技能", tool_calls=[call]),
                Message.tool(call.id, content, name="skill_view"),
            ]
        )
    out = micro_compact(
        msgs, keep_recent_tools=1, result_policy_resolver=_test_result_policy
    )
    tools = [m for m in out if m.role == "tool"]
    assert tools[0].content == INSTRUCTION_REPLACED_STUB.format(
        identity="skill=sites-building"
    )
    assert tools[1].content == "新版指令"


# --------------------------------------------------------------------------- #
# Post-Compact 文件恢复（按 path 去重 + 磁盘重读最新内容）
# --------------------------------------------------------------------------- #
def _build_file_read_history(paths_args: list[tuple[str, dict]]) -> list[Message]:
    """构造多轮 file_read 历史（工具结果内容是压缩时的旧快照）。"""
    msgs: list[Message] = []
    for i, (path, args) in enumerate(paths_args):
        tc = [ToolCall(id=f"call_{i}", name="file_read", arguments={"path": path, **args})]
        msgs.append(Message.assistant("读取文件", tool_calls=tc))
        msgs.append(Message.tool(tool_call_id=f"call_{i}", content=f"旧快照{i}", name="file_read"))
    return msgs


def test_post_compact_collects_recent_file_reads_dedups_shards(tmp_path):
    """同一文件的多个 offset/limit 分片合并为单个文件级条目（保留最后一次分片参数）。"""
    from crew.agent.compact.post_compact import collect_recent_file_reads

    p = str(tmp_path / "a.txt")
    msgs = _build_file_read_history([
        (p, {"offset": 1, "limit": 100}),
        (p, {"offset": 101, "limit": 100}),
    ])
    reads = collect_recent_file_reads(msgs, max_files=3)
    assert len(reads) == 1
    assert reads[0][0] == p
    assert reads[0][1]["offset"] == 101


def test_post_compact_file_limit_uses_actual_read_order():
    """文件恢复按最后读取顺序选择，而不是按路径字母顺序。"""
    from crew.agent.compact.post_compact import collect_recent_file_reads

    msgs = _build_file_read_history([
        ("/tmp/z-first.txt", {}),
        ("/tmp/a-latest.txt", {}),
    ])
    reads = collect_recent_file_reads(msgs, max_files=1)
    assert [path for path, _args in reads] == ["/tmp/a-latest.txt"]


def test_post_compact_rereads_latest_content_from_disk(tmp_path):
    """恢复内容来自磁盘最新内容，而非压缩时的旧快照。"""
    from crew.agent.compact.post_compact import (
        POST_COMPACT_FILES_MARKER,
        build_post_compact_file_attachments,
    )

    p = tmp_path / "a.txt"
    p.write_text("磁盘上的新内容", encoding="utf-8")
    msgs = _build_file_read_history([(str(p), {})])
    attachments = build_post_compact_file_attachments(msgs)
    assert len(attachments) == 1
    assert attachments[0].role == "system"
    assert POST_COMPACT_FILES_MARKER in attachments[0].content
    assert "磁盘上的新内容" in attachments[0].content
    assert "旧快照0" not in attachments[0].content


def test_post_compact_file_attachments_respect_shard_pagination(tmp_path):
    """按原 offset/limit 分片重读：只恢复当时读取的行区间。"""
    from crew.agent.compact.post_compact import build_post_compact_file_attachments

    p = tmp_path / "lines.txt"
    p.write_text("\n".join(f"第{i}行" for i in range(1, 11)), encoding="utf-8")
    msgs = _build_file_read_history([(str(p), {"offset": 3, "limit": 2})])
    attachments = build_post_compact_file_attachments(msgs)
    assert len(attachments) == 1
    assert "第3行" in attachments[0].content
    assert "第4行" in attachments[0].content
    assert "第1行" not in attachments[0].content


def test_post_compact_file_attachments_skip_missing_files(tmp_path):
    """已删除的文件跳过；全部不可读时返回空列表。"""
    from crew.agent.compact.post_compact import build_post_compact_file_attachments

    alive = tmp_path / "alive.txt"
    alive.write_text("还在", encoding="utf-8")
    msgs = _build_file_read_history([
        (str(tmp_path / "gone.txt"), {}),
        (str(alive), {}),
    ])
    attachments = build_post_compact_file_attachments(msgs)
    assert len(attachments) == 1
    assert "还在" in attachments[0].content
    assert "gone.txt" not in attachments[0].content

    msgs_gone = _build_file_read_history([(str(tmp_path / "gone.txt"), {})])
    assert build_post_compact_file_attachments(msgs_gone) == []


def test_post_compact_file_attachments_survive_multiple_compactions(tmp_path):
    """上一轮压缩生成的文件附件在下一轮压缩时仍被识别并重读。"""
    from crew.agent.compact.post_compact import build_post_compact_file_attachments

    p = tmp_path / "a.txt"
    p.write_text("跨压缩存活", encoding="utf-8")
    first = build_post_compact_file_attachments(_build_file_read_history([(str(p), {})]))
    assert len(first) == 1

    # 第二轮压缩：old 段只剩上一轮的附件（原 file_read tool_calls 已被摘要掉）
    second = build_post_compact_file_attachments([Message.user("后续"), *first])
    assert len(second) == 1
    assert "跨压缩存活" in second[0].content


def test_post_compact_no_attachments_when_no_files():
    """没有 file_read 调用时返回空列表。"""
    from crew.agent.compact.post_compact import build_post_compact_file_attachments

    msgs = [
        Message.user("开始"),
        Message.assistant("hi"),
    ]
    attachments = build_post_compact_file_attachments(msgs)
    assert attachments == []


def test_post_compact_protected_results_survive_multiple_compactions():
    from crew.agent.compact.post_compact import (
        POST_COMPACT_RESULTS_MARKER,
        build_post_compact_attachments,
    )

    skill_call = ToolCall(
        id="skill", name="skill_view", arguments={"name": "sites-building"}
    )
    answer_call = ToolCall(
        id="answer", name="ask_followup_question", arguments={"questions": []}
    )
    msgs = [
        Message.assistant("读取", tool_calls=[skill_call, answer_call]),
        Message.tool("skill", "必须遵循的完整 Skill 指令", name="skill_view"),
        Message.tool("answer", "用户选择：方案 A", name="ask_followup_question"),
    ]
    first = build_post_compact_attachments(
        msgs,
        result_policy_resolver=_test_result_policy,
        max_resources=3,
        max_chars_per_resource=5000,
    )
    second = build_post_compact_attachments(
        first,
        result_policy_resolver=_test_result_policy,
        max_resources=3,
        max_chars_per_resource=5000,
    )

    assert len(first) == len(second) == 2
    assert all(POST_COMPACT_RESULTS_MARKER in m.content for m in second)
    assert any("完整 Skill 指令" in m.content for m in second)
    assert any("用户选择：方案 A" in m.content for m in second)


def test_registry_exposes_skill_and_unknown_tool_result_policies():
    from crew.tools.registry import Registry
    from crew.tools.skills_tools import register_skills_tools

    registry = Registry()
    register_skills_tools(registry)
    registry.register(
        name="plugin_unknown",
        schema={"name": "plugin_unknown", "parameters": {}},
        handler=lambda _args: "result",
    )

    main = registry.result_policy("skill_view", {"name": "sites-building"})
    explicit_main = registry.result_policy(
        "skill_view", {"name": "sites-building", "file_path": "SKILL.md"}
    )
    reference = registry.result_policy(
        "skill_view",
        {"name": "sites-building", "file_path": "references/runtime.md"},
    )
    unknown = registry.result_policy("plugin_unknown", {})

    assert main.retention is ToolResultRetention.INSTRUCTION
    assert explicit_main.retention is ToolResultRetention.INSTRUCTION
    assert reference.retention is ToolResultRetention.RESOURCE
    assert unknown.retention is ToolResultRetention.IMPORTANT


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
    # assistant(tool_calls) 后跟 tool 结果，split 不得落在 tool 上切断配对
    msgs = [
        Message.user("q1"),
        Message.assistant("", [ToolCall(id="c1", name="Read")]),
        Message.tool("c1", "结果"),
        Message.user("q2"),
        Message.assistant("", [ToolCall(id="c2", name="Read")]),
        Message.tool("c2", "结果"),
    ]
    split = ContextCompactor._safe_split(msgs, keep_recent=2)
    # 倒数 2 是 [assistant, tool]：assistant 是安全边界，配对完整保留在 recent
    assert split == 4
    assert msgs[split].role == "assistant"
    assert [m.role for m in msgs[split:]] == ["assistant", "tool"]


def test_safe_split_accepts_assistant_boundary_within_long_turn():
    """单个长回合内只有回合开头一个 user 边界：split 落在 assistant 边界，
    回合内早期迭代由此可被摘要（修复前退回回合开头，整个回合受保护）。"""
    msgs = [Message.user("长回合开始")]
    for i in range(20):
        msgs.append(Message.assistant(f"步骤{i}", [ToolCall(id=f"c{i}", name="terminal")]))
        msgs.append(Message.tool(f"c{i}", f"输出{i}"))
    split = ContextCompactor._safe_split(msgs, keep_recent=6)
    assert split > 1  # 未退回回合开头的 user，也未降级为 0
    assert msgs[split].role == "assistant"  # recent 不以 tool 开头：配对完整


def test_safe_split_falls_back_without_boundary():
    """没有任何 user/assistant 边界时安全降级为 0（不压缩）。"""
    msgs = [Message.system("sys")] + [Message.tool(f"c{i}", "结果") for i in range(10)]
    assert ContextCompactor._safe_split(msgs, keep_recent=2) == 0


def test_builtin_file_read_is_temporary():
    """内置 file_read 声明为 TEMPORARY：旧分片由 L1 清理，恢复靠磁盘重读。"""
    from crew.tools.builtin import register_builtin_tools
    from crew.tools.registry import Registry

    registry = Registry()
    register_builtin_tools(registry)
    policy = registry.result_policy("file_read", {"path": "/tmp/x", "offset": 1, "limit": 10})
    assert policy.retention is ToolResultRetention.TEMPORARY


async def test_compact_view_compacts_within_single_long_turn():
    """单个长回合（一次 user + 多次工具迭代）内超水位时，L3 能在 assistant 边界下刀。"""
    provider = FakeProvider(reply="短摘要")
    comp = ContextCompactor(provider, token_budget=10, keep_recent=4)
    msgs = [Message.user("长回合")]
    for i in range(12):
        msgs.append(Message.assistant(f"步骤{i}", [ToolCall(id=f"c{i}", name="terminal")]))
        msgs.append(Message.tool(f"c{i}", f"输出{i} " * 100))
    out = await comp.compact_view(msgs, "intra-turn")
    assert len(provider.calls) == 1  # 修复前：找不到 user 边界，L3 永不触发
    assert out[0].content.startswith(SUMMARY_MARKER)
    # 最近的工具迭代逐字保留
    assert any("输出11" in (m.content or "") for m in out)


async def test_summary_message_includes_history_hint():
    """摘要消息附完整历史回溯指引（数据库路径 + session_id）。"""
    provider = FakeProvider(reply="摘要")
    comp = ContextCompactor(
        provider, token_budget=10, keep_recent=2, history_db_path="/data/crew.db"
    )
    history = await _big_history(10)
    out = await comp.maybe_compact(history, "sess-hint")
    assert "crew.db" in out[0].content
    assert "sess-hint" in out[0].content


def test_post_compact_attachment_budgets_configurable():
    """指令/重要结论的恢复条数预算可由调用方配置（默认 5/8，最近优先）。"""
    from crew.agent.compact.post_compact import build_post_compact_attachments

    msgs: list[Message] = []
    for i in range(5):
        call = ToolCall(id=f"a{i}", name="ask_followup_question", arguments={})
        msgs.append(Message.assistant("问", tool_calls=[call]))
        msgs.append(Message.tool(f"a{i}", f"回答{i}", name="ask_followup_question"))
    out = build_post_compact_attachments(
        msgs,
        result_policy_resolver=_test_result_policy,
        max_important=2,
    )
    assert len(out) == 2
    assert any("回答4" in m.content for m in out)  # 最近优先
    assert not any("回答0" in m.content for m in out)


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
