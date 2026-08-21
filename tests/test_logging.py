"""LLM 收发 trace 日志测试。"""

import io
import json
import logging
import sys
import threading

import pytest

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


def test_llm_trace_redacts_credentials_and_url_secrets(tmp_path):
    _reset_llm_trace()
    log_file = tmp_path / "logs" / "crew.log"
    try:
        clog._setup_llm_trace(str(log_file))
        clog.llm_trace(
            "request",
            {
                "api_key": "plain-api-secret",
                "headers": {"Authorization": "Bearer plain-bearer-secret"},
                "url": (
                    "https://user:password@example.test/path"
                    "?access_token=query-secret"
                ),
            },
        )

        persisted = (log_file.parent / "llm.jsonl").read_text(encoding="utf-8")
        assert "plain-api-secret" not in persisted
        assert "plain-bearer-secret" not in persisted
        assert "password@" not in persisted
        assert "query-secret" not in persisted
        event = json.loads(persisted)
        assert event["api_key"] in {"***", "<secret_redacted>"}
        assert event["headers"]["Authorization"] in {"***", "<secret_redacted>"}
    finally:
        _reset_llm_trace()


def test_sensitive_log_filter_redacts_message_exception_and_env_secret(monkeypatch):
    secret = "opaque-runtime-credential"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    try:
        raise RuntimeError(
            f"failed with {secret} at "
            "https://user:password@example.test/?token=query-secret"
        )
    except RuntimeError:
        record = logging.LogRecord(
            "crew.test.security",
            logging.ERROR,
            __file__,
            1,
            "Authorization: Bearer %s",
            (secret,),
            sys.exc_info(),
        )

    assert clog.SensitiveLogFilter().filter(record) is True
    rendered = logging.Formatter("%(message)s").format(record)
    assert secret not in rendered
    assert "password@" not in rendered
    assert "query-secret" not in rendered
    assert "Traceback" in rendered


@pytest.mark.parametrize(
    "proxy_var",
    ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"],
)
def test_sensitive_log_filter_redacts_proxy_userinfo_and_query(monkeypatch, proxy_var):
    proxy = (
        "http://proxy-user:proxy-password@proxy.example.test:3128/"
        "?access_token=proxy-query-secret"
    )
    monkeypatch.setenv(proxy_var, proxy)
    record = logging.LogRecord(
        "crew.test.security",
        logging.INFO,
        __file__,
        1,
        "using proxy %s",
        (proxy,),
        None,
    )

    assert clog.SensitiveLogFilter().filter(record) is True
    rendered = logging.Formatter("%(message)s").format(record)
    assert "proxy-password" not in rendered
    assert "proxy-query-secret" not in rendered
    assert "proxy.example.test" in rendered


def test_setup_logging_secures_preexisting_root_handlers(monkeypatch):
    root = logging.getLogger()
    previous_handlers = list(root.handlers)
    previous_filters = list(root.filters)
    previous_level = root.level
    previous_configured = clog._CONFIGURED
    previous_ring = clog._RING
    output = io.StringIO()
    existing = logging.StreamHandler(output)
    secret = "preexisting-handler-secret"
    try:
        for handler in previous_handlers:
            root.removeHandler(handler)
        root.addHandler(existing)
        clog._CONFIGURED = False
        clog._RING = None
        monkeypatch.setattr(clog, "_ensure_utf8_stdio", lambda: None)
        monkeypatch.setattr(
            clog,
            "RichHandler",
            lambda **_kwargs: logging.NullHandler(),
        )

        clog.setup_logging(level="INFO")
        logging.getLogger("crew.test.preexisting").error(
            "Authorization: Bearer %s",
            secret,
        )

        assert secret not in output.getvalue()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            if handler not in previous_handlers:
                handler.close()
        for handler in previous_handlers:
            root.addHandler(handler)
        root.filters[:] = previous_filters
        root.setLevel(previous_level)
        clog._CONFIGURED = previous_configured
        clog._RING = previous_ring


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


def test_cross_sink_secret_canary_console_file_and_audit(tmp_path, monkeypatch):
    from crew.security.audit import AuditEvent, SQLiteSecurityAudit
    from crew.security.context import SecurityContext

    canary = "sk-crosssinkcanarysecret1234567890"
    proxies = {
        "HTTP_PROXY": (
            "http://http-user:http-password@proxy.example.test:8080/"
            "?access_token=http-query-secret"
        ),
        "HTTPS_PROXY": (
            "http://https-user:https-password@proxy.example.test:8443/"
            "?access_token=https-query-secret"
        ),
        "ALL_PROXY": (
            "http://all-user:all-password@proxy.example.test:8888/"
            "?access_token=all-query-secret"
        ),
        "NO_PROXY": (
            "http://no-user:no-password@proxy.example.test/"
            "?access_token=no-query-secret"
        ),
    }
    for proxy_var, proxy_value in proxies.items():
        monkeypatch.setenv(proxy_var, proxy_value)

    logger = logging.getLogger("crew.cross_sink_canary")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addFilter(clog.SensitiveLogFilter())

    console = io.StringIO()
    log_file = tmp_path / "logs" / "crew.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    console_handler = logging.StreamHandler(console)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    ring_handler = clog.RingBufferHandler()
    ring_handler.addFilter(clog.SensitiveLogFilter())
    for handler in (console_handler, file_handler, ring_handler):
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    audit = SQLiteSecurityAudit(
        tmp_path / "audit.db",
        integrity_key=b"audit-test-key-material-that-is-32-bytes",
    )
    try:
        for proxy_var, proxy_value in proxies.items():
            logger.info("secret=%s %s=%s", canary, proxy_var, proxy_value)
        try:
            raise ValueError(proxies["HTTPS_PROXY"])
        except ValueError:
            logger.exception("proxy request failed")
        context = SecurityContext(
            os_user="os-a",
            owner_account_id="owner-a",
            workspace_id="project-a",
            workspace_root=tmp_path,
            session_id="session-a",
            request_id="request-a",
            task_id="task-a",
            cwd=tmp_path,
        )
        audit.record(
            AuditEvent.for_tool_decision(
                context,
                tool_name="browser_use",
                args={"token": canary, "proxy": proxies["ALL_PROXY"]},
                decision="deny",
                decision_source="policy",
            )
        )
        exported = audit.export_jsonl(owner_account_id="owner-a")
        ring = json.dumps(ring_handler.query(), ensure_ascii=False)
    finally:
        audit.close()
        logger.handlers.clear()
        logger.filters.clear()

    combined = (
        console.getvalue()
        + log_file.read_text(encoding="utf-8")
        + exported
        + ring
    )
    assert canary not in combined
    for secret in (
        "http-password",
        "https-password",
        "all-password",
        "no-password",
        "http-query-secret",
        "https-query-secret",
        "all-query-secret",
        "no-query-secret",
    ):
        assert secret not in combined
    for proxy_value in proxies.values():
        assert proxy_value not in combined
    assert "proxy.example.test" in combined

def test_setup_logging_without_env_keeps_passed_level(monkeypatch):
    """未设置 CREW_LOG_LEVEL 时保持调用方传入的级别（gateway 行为不变）。"""
    root = logging.getLogger()
    old_handlers, old_filters, old_level = root.handlers[:], root.filters[:], root.level
    old_configured, old_ring = clog._CONFIGURED, clog._RING
    monkeypatch.delenv("CREW_LOG_LEVEL", raising=False)
    clog._CONFIGURED = False
    try:
        clog.setup_logging("INFO")
        assert root.level == logging.INFO
    finally:
        for h in root.handlers[:]:
            if h not in old_handlers:
                root.removeHandler(h)
        root.filters[:] = old_filters
        root.setLevel(old_level)
        clog._CONFIGURED = old_configured
        clog._RING = old_ring
