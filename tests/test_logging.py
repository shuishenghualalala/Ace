"""LLM 收发 trace 日志测试。"""

import json
import logging
import threading

import crew.state.logging as clog
from crew.core.runctx import current_owner_account_id


def _reset_llm_trace():
    """清理 crew.llm logger 的 handler 与全局开关，保证测试隔离。"""
    logger = logging.getLogger("crew.llm")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        h.close()
    clog._LLM_TRACE_ENABLED = False


def test_llm_trace_disabled_is_noop(tmp_path):
    """未开启时 llm_trace 不写任何文件。"""
    _reset_llm_trace()
    trace_file = tmp_path / "logs" / "llm.jsonl"
    clog.llm_trace("request", {"session_id": "s1", "model": "m"})
    assert not trace_file.exists()


def test_llm_trace_writes_jsonl(tmp_path):
    """开启后请求/响应各写一行可解析的 JSON。"""
    _reset_llm_trace()
    log_file = tmp_path / "logs" / "crew.log"
    try:
        clog._setup_llm_trace(str(log_file))
        clog.llm_trace("request", {"session_id": "s1", "model": "m", "messages": [{"role": "user", "content": "hi"}]})
        clog.llm_trace("response", {"session_id": "s1", "model": "m", "text": "hello"})

        trace_file = log_file.parent / "llm.jsonl"
        lines = trace_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        req = json.loads(lines[0])
        resp = json.loads(lines[1])
        assert req["dir"] == "request" and req["session_id"] == "s1"
        assert resp["dir"] == "response" and resp["text"] == "hello"
        assert "ts" in req
    finally:
        _reset_llm_trace()


def test_llm_trace_injects_current_owner(tmp_path):
    """Trace records inherit owner from the active request context."""
    from crew.core.runctx import current_owner_account_id

    _reset_llm_trace()
    log_file = tmp_path / "logs" / "crew.log"
    token = current_owner_account_id.set("A:uid-a")
    try:
        clog._setup_llm_trace(str(log_file))
        clog.llm_trace("request", {"session_id": "same"})

        trace_file = log_file.parent / "llm.jsonl"
        event = json.loads(trace_file.read_text(encoding="utf-8").strip())
        assert event["owner_account_id"] == "A:uid-a"
    finally:
        current_owner_account_id.reset(token)
        _reset_llm_trace()


def test_make_console_disables_legacy_windows():
    """Rich Console 必须关闭 legacy_windows，避免 GBK LegacyWindowsTerm。"""
    console = clog._make_console()
    assert console.legacy_windows is False


def test_ensure_utf8_stdio_is_safe_noop(monkeypatch):
    """stdio 无 reconfigure 时不应抛错（管道/假流场景）。"""
    class _NoReconfigure:
        pass

    monkeypatch.setattr(clog.sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(clog.sys, "stderr", _NoReconfigure())
    clog._ensure_utf8_stdio()  # 不应抛


def test_logging_plugin_truncates_long_args():
    """file_write 等大 content 入参只打短预览，避免控制台刷屏。"""
    from crew.plugins.builtin import _preview_args

    long_content = "❤" + ("水" * 500)
    preview = _preview_args({"path": "a.html", "content": long_content}, limit=80)
    assert len(preview) < 120
    assert "chars)" in preview
    assert "❤" in preview or "\\u2764" in preview or preview.startswith("{")


def test_ring_buffer_filters_by_causal_owner_and_keeps_raw_thread_logs_system_only():
    """Owner is captured at emit time; raw threads without context stay admin-only."""
    handler = clog.RingBufferHandler()

    def emit(message: str) -> None:
        record = logging.LogRecord(
            "crew.test.causal",
            logging.INFO,
            __file__,
            1,
            message,
            (),
            None,
        )
        handler.emit(record)

    token_a = current_owner_account_id.set("A:uid-a")
    try:
        emit("causal-a")
    finally:
        current_owner_account_id.reset(token_a)
    token_b = current_owner_account_id.set("B:uid-b")
    try:
        emit("causal-b")
    finally:
        current_owner_account_id.reset(token_b)

    thread = threading.Thread(target=emit, args=("causal-system",))
    thread.start()
    thread.join()

    owner_a = handler.query(owner_account_id="A:uid-a")
    owner_b = handler.query(owner_account_id="B:uid-b")
    admin = handler.query()

    assert [item["message"] for item in owner_a["items"]] == ["causal-a"]
    assert [item["message"] for item in owner_b["items"]] == ["causal-b"]
    assert {item["message"] for item in admin["items"]} == {
        "causal-a",
        "causal-b",
        "causal-system",
    }
    assert next(item for item in admin["items"] if item["message"] == "causal-system")[
        "owner_account_id"
    ] == ""
