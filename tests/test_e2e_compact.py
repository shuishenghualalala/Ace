# ruff: noqa: E402 -- e2e test configures environment/path before application imports
"""端到端测试：使用 deepseek 真实模型验证三层上下文压缩。

运行：
    CREW_MODEL_PROFILE=deepseek pytest tests/test_e2e_compact.py -v -s
    CREW_MODEL_PROFILE=deepseek python tests/test_e2e_compact.py

覆盖：
1. deepseek 模型可正常连接
2. L1 MicroCompact：旧工具结果清理
3. L3 全量摘要：首次压缩触发
4. L2 增量摘要：复用缓存 + 增量合并
5. L2 纯规则：新增很少时零 LLM 复用旧摘要
6. 历史完整性：canonical 历史不受压缩影响
7. 鲁棒性：上下文溢出兜底压缩 + 防抖跳过

注意：
- 本文件依赖真实 LLM，默认不纳入 CI。
- fixture 中降低了 compaction token_budget / keep_recent / l2_delta_threshold，
  以便用更小、更便宜的历史触发压缩。
- overflow / anti-thrash 两个边界行为通过 monkey-patch provider 模拟异常来稳定验证，
  其余用例走真实模型链路。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# 端到端/真实 LLM：默认不跑，用 `pytest -m e2e` 单独运行。
pytestmark = pytest.mark.e2e

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 默认 deepseek；可用 CREW_MODEL_PROFILE 环境变量覆盖（如 openai、minimax）。
os.environ.setdefault("CREW_MODEL_PROFILE", "deepseek")

from crew.agent.compact import estimate_tokens
from crew.agent.compact.microcompact import TOOL_SUMMARY_PREFIX
from crew.agent.compact.store import SummaryState
from crew.agent.compact.summary import SUMMARY_MARKER
from crew.app import build_app, CrewApp
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.errors import ProviderError
from crew.core.types import Message, ToolCall


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _build_long_history(n_pairs: int, chars_per_msg: int = 2000) -> list[Message]:
    """构造 n_pairs 轮 user/assistant 的长历史，用于触发 L2/L3 摘要。"""
    msgs: list[Message] = []
    filler_user = "用户提问内容长文本占位 " * (chars_per_msg // 16)
    filler_assistant = "模型回答内容长文本占位 " * (chars_per_msg // 16)
    for i in range(n_pairs):
        msgs.append(Message.user(f"【第{i:03d}轮问题】{filler_user} 标记{i:03d}"))
        msgs.append(Message.assistant(f"【第{i:03d}轮回答】{filler_assistant} 结论{i:03d}"))
    return msgs


def _build_history_with_tools(
    n_rounds: int,
    n_tools_per_round: int = 3,
    chars_per_tool: int = 1000,
) -> list[Message]:
    """构造含 assistant(tool_calls) + tool 结果的历史，用于触发 L1 清理。"""
    msgs: list[Message] = []
    filler_tool = "工具执行结果长文本占位 " * (chars_per_tool // 16)
    for i in range(n_rounds):
        msgs.append(Message.user(f"第{i:03d}轮：请执行工具"))
        tool_calls = [
            ToolCall(id=f"call_{i:03d}_{j}", name="terminal", arguments={"command": f"echo {i}_{j}"})
            for j in range(n_tools_per_round)
        ]
        msgs.append(Message.assistant(f"我来执行第{i:03d}轮工具", tool_calls=tool_calls))
        for j in range(n_tools_per_round):
            msgs.append(
                Message.tool(
                    tool_call_id=f"call_{i:03d}_{j}",
                    content=f"{filler_tool} 输出{i:03d}_{j}",
                    name="terminal",
                )
            )
    return msgs


def _read_llm_trace() -> list[dict[str, Any]]:
    """读取 .crew/logs/llm.jsonl，返回 JSON 列表。"""
    trace_path = Path(ROOT) / ".crew" / "logs" / "llm.jsonl"
    if not trace_path.exists():
        return []
    lines = trace_path.read_text(encoding="utf-8").strip().split("\n")
    return [json.loads(line) for line in lines if line.strip()]


def _session_requests(trace: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """按 session_id 过滤 request 记录。"""
    return [r for r in trace if r.get("dir") == "request" and r.get("session_id") == session_id]


def _summary_requests(trace: list[dict[str, Any]], session_id: str) -> list[dict[str, Any]]:
    """摘要请求特征：stream=False。

    注意：compactor 在 BuiltinExecutor 设置 current_session_id 之前调用，
    因此摘要请求的 session_id 可能为空字符串，这里放宽过滤条件。
    """
    return [
        r for r in trace
        if r.get("dir") == "request" and r.get("stream") is False
        and (r.get("session_id") == session_id or r.get("session_id") == "")
    ]


def _last_chat_request(trace: list[dict[str, Any]], session_id: str) -> dict[str, Any] | None:
    """某 session 最后一次真实对话请求（stream=True）。"""
    chat_reqs = [r for r in _session_requests(trace, session_id) if r.get("stream") is True]
    return chat_reqs[-1] if chat_reqs else None


def _is_summary_request(record: dict[str, Any]) -> bool:
    """通过提示词内容识别 L3/L2 摘要请求，排除标题生成等其他 stream=False 请求。"""
    if record.get("dir") != "request" or record.get("stream") is not False:
        return False
    messages = record.get("messages", [])
    if not messages:
        return False
    first_content = (messages[0].get("content") or "")
    return "你是对话历史压缩器" in first_content or "已有摘要" in first_content


def _trace_start_index() -> int:
    """当前 trace 条目数，用于测试内部分段读取新增记录。"""
    return len(_read_llm_trace())


async def _collect_chunks(app: CrewApp, envelope: Envelope) -> list[ResponseChunk]:
    """收集 app.handle 返回的所有 chunk。"""
    chunks: list[ResponseChunk] = []
    async for chunk in app.handle(envelope):
        chunks.append(chunk)
    return chunks


# --------------------------------------------------------------------------- #
# Fixture
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def app(tmp_path_factory) -> CrewApp:
    """构建使用 deepseek 的 CrewApp，使用临时数据库并降低压缩阈值。"""
    tmpdir = tmp_path_factory.mktemp("crew_compact_e2e")
    db_path = str(tmpdir / "crew.db")

    from crew.state.config import load_config

    cfg = load_config()
    cfg.db_path = db_path
    cfg.log_file = ".crew/logs/crew.log"  # 固定日志路径，便于读取 llm.jsonl
    cfg.log_level = "INFO"  # 减少 DEBUG 噪声
    cfg.timeout = 180.0  # 长 prompt 处理可能超时，放宽到 180 秒

    # 降低阈值：用更小、更便宜的历史触发压缩
    cfg.compaction_token_budget = 10000
    cfg.compaction_keep_recent = 4
    cfg.compaction_keep_recent_tools = 3
    cfg.compaction_l2_delta_threshold = 5000  # e2e 场景：2 条 2000 字符消息约 3800 token，5000 以下可触发纯规则复用

    # 重置日志配置开关，确保 build_app 会启用 llm_trace
    import crew.state.logging as _log_mod

    _log_mod._CONFIGURED = False
    _log_mod._LLM_TRACE_ENABLED = False

    _app = build_app(cfg, enable_team=False)

    _expected_model = os.environ.get("CREW_MODEL_PROFILE", "deepseek")
    assert _app.config.active_model_id == _expected_model, f"期望 {_expected_model}，实际 {_app.config.active_model_id}"
    assert _app.config.has_llm_key, f"{_expected_model} 模型未配置 API Key"
    return _app


# --------------------------------------------------------------------------- #
# 测试 1：模型连接
# --------------------------------------------------------------------------- #

async def test_deepseek_connection(app: CrewApp):
    """deepseek 最小连接验证：能收到非空 final 且无 error 帧。"""
    session_id = "e2e_compact_conn"
    envelope = Envelope.of("你好，请用一句话回复", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)

    final_chunks = [c for c in chunks if c.kind == "final"]
    assert final_chunks, f"未收到 final 帧: {[c.kind for c in chunks]}"
    assert final_chunks[0].body.get("text", "").strip(), "final 帧文本为空"
    assert not any(c.kind == "error" for c in chunks), f"收到 error 帧: {[c.body for c in chunks if c.kind == 'error']}"


# --------------------------------------------------------------------------- #
# 测试 2：L1 MicroCompact
# --------------------------------------------------------------------------- #

async def test_l1_microcompact_clears_old_tools(app: CrewApp):
    """L1 每轮自动清理旧工具结果，保留最近 keep_recent_tools 个原样。"""
    session_id = "e2e_compact_l1"

    # 20 轮 * 每轮 3 个 tool = 60 个 tool 消息，远超 keep_recent_tools=3
    history = _build_history_with_tools(20, n_tools_per_round=3, chars_per_tool=1000)
    app.session_store.save(session_id, history, owner_account_id="local")

    envelope = Envelope.of("总结一下之前的工具执行结果", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)

    assert any(c.kind == "final" for c in chunks), "应收到 final 帧"
    assert not any(c.kind == "error" for c in chunks), "不应有 error 帧"

    # 通过 LLM trace 验证 L1 生效
    trace = _read_llm_trace()
    last_req = _last_chat_request(trace, session_id)
    assert last_req is not None, "未找到该 session 的 LLM 请求 trace"

    messages = last_req.get("messages", [])
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert tool_msgs, "trace 中应包含 tool 消息"

    # 旧工具结果压缩为信息性摘要（TOOL_SUMMARY_PREFIX），保留工具语义
    cleared = [m for m in tool_msgs if TOOL_SUMMARY_PREFIX in (m.get("content") or "")]
    # 最近 keep_recent_tools 个保留原样，其余应被压缩为信息摘要
    assert len(cleared) >= len(tool_msgs) - app.config.compaction_keep_recent_tools, (
        f"应压缩大部分旧工具为信息摘要: total={len(tool_msgs)} cleared={len(cleared)}"
    )


# --------------------------------------------------------------------------- #
# 测试 3：L3 全量摘要
# --------------------------------------------------------------------------- #

async def test_l3_full_summary_first_compaction(app: CrewApp):
    """首次压缩时无可复用缓存，触发 L3 全量 LLM 摘要。"""
    session_id = "e2e_compact_l3"
    trace_start = _trace_start_index()

    # 25 轮 * 每轮约 4000 字符 ≈ 1000 token，总 token 约 25000 > 10000 budget
    history = _build_long_history(25, chars_per_msg=2000)
    assert estimate_tokens(history) > app.config.compaction_token_budget, (
        f"历史 token {estimate_tokens(history)} 未超 budget {app.config.compaction_token_budget}"
    )
    app.session_store.save(session_id, history, owner_account_id="local")

    envelope = Envelope.of("请简要总结我们之前的讨论", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)

    assert any(c.kind == "final" for c in chunks), "应收到 final 帧"
    assert not any(c.kind == "error" for c in chunks), "不应有 error 帧"

    # 验证 SummaryStore 建立了缓存
    state = app.summary_store.get(session_id, owner_account_id="local")
    assert state is not None, "L3 应创建摘要缓存"
    assert state.covered_count > 0, "缓存应覆盖部分历史"
    assert SUMMARY_MARKER in state.text or len(state.text) > 10, "缓存应包含有效摘要文本"

    # 验证 trace 中出现了 L3 摘要请求
    trace = _read_llm_trace()
    new_entries = trace[trace_start:]
    summary_reqs = [r for r in new_entries if _is_summary_request(r)]
    assert summary_reqs, "应触发 L3 全量摘要请求"

    # 验证真实对话请求中包含摘要 system 消息（在系统 prompt 之后）
    last_req = _last_chat_request(trace, session_id)
    assert last_req is not None
    messages = last_req.get("messages", [])
    assert messages, "对话请求 messages 不应为空"
    summary_msgs = [
        m for m in messages
        if m.get("role") == "system" and SUMMARY_MARKER in (m.get("content") or "")
    ]
    assert summary_msgs, "对话请求中应包含带摘要标记的 system 消息"


# --------------------------------------------------------------------------- #
# 测试 4：L2 增量摘要
# --------------------------------------------------------------------------- #

async def test_l2_incremental_summary_reuses_cache(app: CrewApp):
    """在 L3 缓存基础上追加较多消息，触发 L2 增量摘要。"""
    session_id = "e2e_compact_l2_incr"
    trace_start = _trace_start_index()

    # 第一轮：建立 L3 缓存
    history = _build_long_history(25, chars_per_msg=2000)
    app.session_store.save(session_id, history, owner_account_id="local")
    envelope1 = Envelope.of("第一轮总结", session_id=session_id)
    chunks1 = await _collect_chunks(app, envelope1)
    assert any(c.kind == "final" for c in chunks1), "第一轮应收到 final"

    state1 = app.summary_store.get(session_id, owner_account_id="local")
    assert state1 is not None, "第一轮应建立摘要缓存"
    covered_before = state1.covered_count

    # 追加 5 轮（约 10000 字符 ≈ 2500 token > l2_delta_threshold=2000），触发 L2 增量
    saved = app.session_store.load(session_id, owner_account_id="local")
    saved.extend(_build_long_history(5, chars_per_msg=2000))
    app.session_store.save(session_id, saved, owner_account_id="local")

    envelope2 = Envelope.of("第二轮总结", session_id=session_id)
    chunks2 = await _collect_chunks(app, envelope2)
    assert any(c.kind == "final" for c in chunks2), "第二轮应收到 final"

    # 验证缓存覆盖范围扩展
    state2 = app.summary_store.get(session_id, owner_account_id="local")
    assert state2 is not None, "第二轮应有缓存"
    assert state2.covered_count > covered_before, "L2 增量应扩展 covered_count"

    # 验证 trace 中有至少 2 次摘要请求（L3 + L2）
    trace = _read_llm_trace()
    new_entries = trace[trace_start:]
    summary_reqs = [r for r in new_entries if _is_summary_request(r)]
    assert len(summary_reqs) >= 2, f"应至少有 2 次摘要请求（L3 + L2），实际 {len(summary_reqs)}"


# --------------------------------------------------------------------------- #
# 测试 5：L2 纯规则复用
# --------------------------------------------------------------------------- #

async def test_l2_pure_rule_no_llm_when_delta_small(app: CrewApp):
    """缓存基础上追加极少消息，新增 token < l2_delta_threshold，应零 LLM 复用旧摘要。"""
    session_id = "e2e_compact_l2_rule"
    trace_start = _trace_start_index()

    # 第一轮：建立 L3 缓存
    history = _build_long_history(25, chars_per_msg=2000)
    app.session_store.save(session_id, history, owner_account_id="local")
    envelope1 = Envelope.of("建立缓存", session_id=session_id)
    await _collect_chunks(app, envelope1)

    state1 = app.summary_store.get(session_id, owner_account_id="local")
    assert state1 is not None, "应建立缓存"
    covered_before = state1.covered_count

    # 追加 1 条极短 user 消息，让新增轮次落入 keep_recent 窗口、new_old token < threshold
    saved = app.session_store.load(session_id, owner_account_id="local")
    saved.append(Message.user("补充问一句"))
    app.session_store.save(session_id, saved, owner_account_id="local")

    envelope2 = Envelope.of("回答", session_id=session_id)
    chunks2 = await _collect_chunks(app, envelope2)
    assert any(c.kind == "final" for c in chunks2), "应收到 final"

    # 验证没有新增 LLM 摘要请求
    trace = _read_llm_trace()
    new_entries = trace[trace_start:]
    summary_reqs = [r for r in new_entries if _is_summary_request(r)]
    # 只有第一次的 L3 摘要请求
    assert len(summary_reqs) == 1, (
        f"L2 纯规则应只有 1 次 L3 摘要请求，实际 {len(summary_reqs)}"
    )

    # 纯规则复用会更新 covered_count 为当前 split，因此应 >= covered_before
    state2 = app.summary_store.get(session_id, owner_account_id="local")
    assert state2 is not None
    assert state2.covered_count >= covered_before, "纯规则复用应至少保持/扩展 covered_count"


# --------------------------------------------------------------------------- #
# 测试 6：历史完整性
# --------------------------------------------------------------------------- #

async def test_canonical_history_preserved_after_compaction(app: CrewApp):
    """压缩只作用于 LLM 视图，session_store 中的 canonical 历史保持完整。"""
    session_id = "e2e_compact_history"

    history = _build_long_history(25, chars_per_msg=2000)
    original_contents = [m.content for m in history]
    app.session_store.save(session_id, history, owner_account_id="local")

    envelope = Envelope.of("请总结之前的内容", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)
    assert any(c.kind == "final" for c in chunks), "应收到 final"

    saved = app.session_store.load(session_id, owner_account_id="local")
    assert len(saved) > len(history), "应追加本轮新消息"

    # 原始历史中所有消息都应保留
    saved_contents = [m.content for m in saved]
    for content in original_contents:
        assert content in saved_contents, "canonical 历史中不应丢失原始消息"


# --------------------------------------------------------------------------- #
# 测试 7：鲁棒性 — 溢出兜底 + 防抖
# --------------------------------------------------------------------------- #

async def test_resilience_overflow_and_anti_thrash(app: CrewApp, caplog):
    """验证 BuiltinExecutor 命中上下文溢出时调用 force_compact，以及 compactor 防抖生效。"""
    session_id = "e2e_compact_resilience"
    trace_start = _trace_start_index()

    # 预注入一段会触发压缩的历史
    history = _build_long_history(25, chars_per_msg=2000)
    app.session_store.save(session_id, history, owner_account_id="local")

    # 获取 agent / compactor 引用
    agent = app.agents.get(session_id, owner_account_id="local")
    compactor = agent.compactor

    # ---- 子测试 A：模拟上下文溢出，验证 force_compact 兜底 ----
    original_stream_chat = app.provider.stream_chat
    overflow_triggered = False

    async def _overflow_then_ok(messages, tools=None):
        nonlocal overflow_triggered
        if not overflow_triggered:
            overflow_triggered = True
            raise ProviderError(
                "maximum context length exceeded", retryable=False
            )
        async for chunk in original_stream_chat(messages, tools):
            yield chunk

    app.provider.stream_chat = _overflow_then_ok

    envelope1 = Envelope.of("请总结", session_id=session_id)
    chunks1 = await _collect_chunks(app, envelope1)
    assert overflow_triggered, "应触发一次 overflow 异常"
    assert any(c.kind == "final" for c in chunks1), "兜底后应收到 final"
    assert not any(c.kind == "error" for c in chunks1), "overflow 不应透传为 error 帧"

    # 验证 trace 中出现了当前 session 的 force_compact 摘要请求
    trace = _read_llm_trace()
    new_entries = trace[trace_start:]
    resilience_summary_reqs = [
        r for r in new_entries
        if _is_summary_request(r) and r.get("session_id") == session_id
    ]
    assert resilience_summary_reqs, "force_compact 应触发一次当前 session 的摘要请求"

    # 恢复 provider
    app.provider.stream_chat = original_stream_chat

    # ---- 子测试 B：验证 anti-thrash ----
    # 预置 ineffective_count=2，下一轮压缩应被跳过
    compactor._mem[("local", session_id)] = SummaryState(
        text="旧摘要", covered_count=0, ineffective_count=2
    )
    if app.summary_store is not None:
        app.summary_store.put(session_id, compactor._mem[("local", session_id)], owner_account_id="local")

    # 清空 caplog，避免子测试 A 的日志干扰
    caplog.clear()

    envelope2 = Envelope.of("再总结一次", session_id=session_id)
    chunks2 = await _collect_chunks(app, envelope2)
    assert any(c.kind == "final" for c in chunks2), "防抖跳过后仍应收到 final"

    # 验证日志中出现 anti-thrash 跳过摘要的警告
    assert any(
        "连续 2 次压缩省 <10%" in record.message
        for record in caplog.records
    ), "应记录 anti-thrash 跳过摘要的警告"


# --------------------------------------------------------------------------- #
# main：直接运行
# --------------------------------------------------------------------------- #

if __name__ == "__main__":
    import time

    print("=" * 60)
    print("Crew 端到端测试 — 上下文压缩（deepseek 模型）")
    print("=" * 60)

    from crew.state.config import load_config

    cfg = load_config()
    cfg.log_file = ".crew/logs/crew.log"
    cfg.timeout = 180.0
    cfg.compaction_token_budget = 10000
    cfg.compaction_keep_recent = 4
    cfg.compaction_keep_recent_tools = 3
    cfg.compaction_l2_delta_threshold = 5000  # e2e 场景：2 条 2000 字符消息约 3800 token，5000 以下可触发纯规则复用

    import crew.state.logging as _log_mod

    _log_mod._CONFIGURED = False
    _log_mod._LLM_TRACE_ENABLED = False

    app = build_app(cfg, enable_team=False)
    print(f"活跃模型: {app.config.active_model_id} ({app.provider.model})")
    print(f"API Key 已配置: {app.config.has_llm_key}")
    print(f"压缩预算: {app.config.compaction_token_budget}")
    print()

    async def run_all():
        tests = [
            ("deepseek 连接", test_deepseek_connection),
            ("L1 MicroCompact", test_l1_microcompact_clears_old_tools),
            ("L3 全量摘要", test_l3_full_summary_first_compaction),
            ("L2 增量摘要", test_l2_incremental_summary_reuses_cache),
            ("L2 纯规则复用", test_l2_pure_rule_no_llm_when_delta_small),
            ("历史完整性", test_canonical_history_preserved_after_compaction),
            ("鲁棒性：溢出+防抖", test_resilience_overflow_and_anti_thrash),
        ]
        passed = 0
        failed = 0
        for name, fn in tests:
            t0 = time.time()
            try:
                await fn(app)
                passed += 1
                print(f"[PASS] {name}")
            except Exception as e:
                failed += 1
                print(f"[FAIL] {name}: {e}")
            dt = time.time() - t0
            print(f"  耗时: {dt:.1f}s")
            print()

        print("=" * 60)
        print(f"测试结果: {passed} passed, {failed} failed")
        print("=" * 60)
        return failed == 0

    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
