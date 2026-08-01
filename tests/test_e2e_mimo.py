# ruff: noqa: E402 -- e2e test configures environment/path before application imports
"""端到端测试：使用 MiMo 模型走通完整对话链路。

测试覆盖：
1. build_app → mimo 模型 Provider 构建成功
2. 单轮纯文本对话（无工具调用）
3. 单轮工具调用对话（terminal 工具）
4. 多轮对话上下文保持
5. 流式输出正常
6. 会话持久化可回放
"""

import os
import sys
import asyncio
import pytest

# 端到端/真实 LLM：默认不跑，用 `pytest -m e2e` 单独运行。
pytestmark = pytest.mark.e2e

# 确保项目根在 path 中
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 切换到 mimo 模型
os.environ["CREW_MODEL_PROFILE"] = "mimo"

from crew.app import build_app, CrewApp
from crew.core.envelope import Envelope, ResponseChunk


@pytest.fixture(scope="module")
def app() -> CrewApp:
    """构建使用 mimo 模型的 CrewApp。"""
    _app = build_app(enable_team=False)
    assert _app.config.active_model_id == "mimo", f"期望活跃模型为 mimo，实际为 {_app.config.active_model_id}"
    assert _app.provider.model == "mimo-v2-pro", f"期望模型名为 mimo-v2-pro，实际为 {_app.provider.model}"
    assert _app.config.has_llm_key, "mimo 模型未配置 API Key"
    return _app


async def _collect_chunks(app: CrewApp, envelope: Envelope) -> list[ResponseChunk]:
    """收集 handle 返回的所有 ResponseChunk。"""
    chunks = []
    async for chunk in app.handle(envelope):
        chunks.append(chunk)
    return chunks


# ---- 测试 1: 纯文本对话 ----
async def test_mimo_plain_chat(app: CrewApp):
    """MiMo 纯文本对话：发一句话，收到非空 final 回复。"""
    session_id = "e2e_mimo_plain"
    envelope = Envelope.of("你好，请用一句话介绍你自己。", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)

    # 应有 final 帧
    final_chunks = [c for c in chunks if c.kind == "final"]
    assert final_chunks, f"未收到 final 帧，收到的 kind: {[c.kind for c in chunks]}"
    final_text = final_chunks[0].body.get("text", "")
    assert len(final_text) > 0, "final 帧文本为空"

    # 不应有 error 帧
    error_chunks = [c for c in chunks if c.kind == "error"]
    assert not error_chunks, f"收到 error 帧: {error_chunks[0].body}"

    print(f"[PASS] 纯文本对话: {final_text[:80]}...")


# ---- 测试 2: 工具调用对话 ----
async def test_mimo_tool_call(app: CrewApp):
    """MiMo 工具调用：请求执行命令，验证工具事件 + final 回复。"""
    session_id = "e2e_mimo_tool"
    envelope = Envelope.of("请执行命令 echo hello_crew，告诉我结果。", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)

    kinds = [c.kind for c in chunks]
    # 应有 tool 事件（start 和/或 result）
    tool_chunks = [c for c in chunks if c.kind == "tool"]
    final_chunks = [c for c in chunks if c.kind == "final"]

    # 允许模型直接回答（不调工具），但优先验证有工具调用
    if tool_chunks:
        tool_names = [c.body.get("name") for c in tool_chunks]
        assert "terminal" in tool_names, f"期望调用 terminal 工具，实际调用: {tool_names}"
        print(f"[PASS] 工具调用对话: 调用了 {tool_names}")
    else:
        print(f"[PASS] 工具调用对话: 模型直接回答（未调工具），kinds={kinds}")

    assert final_chunks, f"未收到 final 帧，kinds={kinds}"
    final_text = final_chunks[0].body.get("text", "")
    assert len(final_text) > 0, "final 帧文本为空"


# ---- 测试 3: 流式 delta 输出 ----
async def test_mimo_streaming_delta(app: CrewApp):
    """MiMo 流式输出：验证收到 delta 帧，且拼接后非空。"""
    session_id = "e2e_mimo_stream"
    envelope = Envelope.of("用三句话描述春天的景色。", session_id=session_id)
    chunks = await _collect_chunks(app, envelope)

    delta_chunks = [c for c in chunks if c.kind == "delta"]
    assert delta_chunks, "未收到 delta 帧，流式输出可能异常"

    full_text = "".join(c.body.get("text", "") for c in delta_chunks)
    assert len(full_text) > 0, "delta 帧拼接后文本为空"

    print(f"[PASS] 流式输出: 收到 {len(delta_chunks)} 个 delta 帧，总文本 {len(full_text)} 字符")


# ---- 测试 4: 多轮对话上下文保持 ----
async def test_mimo_multi_turn(app: CrewApp):
    """MiMo 多轮对话：第二轮能引用第一轮内容。"""
    session_id = "e2e_mimo_multi"

    # 第一轮
    envelope1 = Envelope.of("我喜欢的颜色是蓝色，请记住。", session_id=session_id)
    chunks1 = await _collect_chunks(app, envelope1)
    final1 = [c for c in chunks1 if c.kind == "final"]
    assert final1, "第一轮未收到 final 帧"

    # 第二轮
    envelope2 = Envelope.of("我刚才说我喜欢什么颜色？", session_id=session_id)
    chunks2 = await _collect_chunks(app, envelope2)
    final2 = [c for c in chunks2 if c.kind == "final"]
    assert final2, "第二轮未收到 final 帧"

    text2 = final2[0].body.get("text", "")
    assert "蓝" in text2, f"第二轮回复未提及'蓝': {text2[:200]}"

    print("[PASS] 多轮对话: 第二轮回复包含'蓝'")


# ---- 测试 5: 会话持久化 ----
async def test_mimo_session_persistence(app: CrewApp):
    """MiMo 会话持久化：对话后可通过 session_store 回放历史。"""
    session_id = "e2e_mimo_persist"
    envelope = Envelope.of("1+1等于几？", session_id=session_id)
    await _collect_chunks(app, envelope)

    # 通过 session_store 回放
    history = app.session_store.load(session_id)
    assert len(history) >= 2, f"历史记录不足 2 条: {len(history)}"

    roles = [m.role for m in history]
    assert "user" in roles, "历史中缺少 user 消息"
    assert "assistant" in roles, "历史中缺少 assistant 消息"

    print(f"[PASS] 会话持久化: {len(history)} 条历史记录, roles={roles}")


if __name__ == "__main__":
    """直接运行端到端测试。"""
    import time

    print("=" * 60)
    print("Crew 端到端测试 — MiMo 模型")
    print("=" * 60)

    app = build_app(enable_team=False)
    print(f"活跃模型: {app.config.active_model_id} ({app.provider.model})")
    print(f"API Key 已配置: {app.config.has_llm_key}")
    print()

    async def run_all():
        tests = [
            ("纯文本对话", test_mimo_plain_chat),
            ("工具调用对话", test_mimo_tool_call),
            ("流式输出", test_mimo_streaming_delta),
            ("多轮对话", test_mimo_multi_turn),
            ("会话持久化", test_mimo_session_persistence),
        ]
        passed = 0
        failed = 0
        for name, fn in tests:
            t0 = time.time()
            try:
                await fn(app)
                passed += 1
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
