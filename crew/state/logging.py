"""统一日志。基于 rich 输出，全模块通过 get_logger(name) 取 logger。

另提供 LLM 收发全量 trace：开启后每次调用模型的「请求/响应」各写一行 JSON 到
.crew/logs/llm.jsonl，便于排查「发给模型的 / 模型回复的」完整内容。

另维护一个进程内环形缓冲（RingBufferHandler），供 /api/system/logs 查询，
让前端日志页能实时看到所有 crew.* 日志，按级别/关键词筛选。

额外支持 contextvars 级别的角色前缀：Dynamic Kanban 执行某个 agent_call
时，可用 ``with log_role_prefix("analyst:gather"):`` 让该作用域内所有 crew.* 日志
自动带上角色标识，方便从混在一起的日志里定位具体角色。
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import re
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterator

from rich.console import Console
from rich.logging import RichHandler

from crew.core.runctx import current_owner_account_id


def _ensure_utf8_stdio() -> None:
    """把 stdout/stderr 切到 UTF-8，避免 Windows 默认 GBK 写 emoji 时炸日志。

    Rich LegacyWindowsTerm 会按控制台编码 encode；含 ❤（U+2764）等字符时
    触发 UnicodeEncodeError，进而刷屏 Logging error。errors=replace 保证
    即便个别码点仍失败也不拖垮主流程。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - 日志启动路径不能因编码失败而中断
            try:
                stream.reconfigure(errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _make_console() -> Console:
    """构造对 Windows 控制台安全的 Rich Console。

    legacy_windows=False：走现代 VT 路径，避开 LegacyWindowsTerm 的 GBK write。
    force_terminal=True：Electron 管道场景下仍按终端渲染（可读），配合 UTF-8 stdio。
    """
    return Console(
        file=sys.stderr,
        force_terminal=True,
        legacy_windows=False,
        soft_wrap=True,
    )

# 进程内角色前缀：供 Dynamic Kanban 等并发执行多个角色的场景使用。
_LOG_ROLE_PREFIX: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "crew_log_role_prefix", default=None
)


class RolePrefixFilter(logging.Filter):
    """给当前作用域设置了角色前缀的日志自动加前缀，避免重复添加。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, "_role_prefixed", False):
            return True
        prefix = _LOG_ROLE_PREFIX.get()
        if prefix:
            record.msg = f"[{prefix}] {record.msg}"
            record._role_prefixed = True  # type: ignore[attr-defined]
        return True


@contextlib.contextmanager
def log_role_prefix(prefix: str | None) -> Iterator[None]:
    """临时设置日志角色前缀的上下文管理器。"""
    if not prefix:
        yield
        return
    token = _LOG_ROLE_PREFIX.set(prefix)
    try:
        yield
    finally:
        _LOG_ROLE_PREFIX.reset(token)

_CONFIGURED = False
_LLM_TRACE_ENABLED = False
_LLM_TRACE_FILE = ".crew/logs/llm.jsonl"

_BROWSER_BOUNDARY_RE = re.compile(
    r"<untrusted_browser_(?:content|console)>.*?</untrusted_browser_(?:content|console)>",
    re.DOTALL,
)
_DATA_IMAGE_RE = re.compile(r"data:image/[^;,\s]+;base64,[A-Za-z0-9+/=]+", re.DOTALL)


def _sanitize_llm_trace(value: Any, *, browser_scope: bool = False) -> Any:
    """Remove Browser Use page data, form values and image bytes from disk traces."""
    if isinstance(value, dict):
        if value.get("type") == "image" or (
            value.get("type") == "base64" and str(value.get("media_type") or "").startswith("image/")
        ):
            return {"type": "image", "redacted": True}
        name = str(value.get("name") or "")
        local_browser = (
            browser_scope
            or name.startswith("browser_")
            or name == "record_replay"
        )
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if local_browser and key in {"arguments", "input", "content", "text", "value", "data"}:
                clean[key] = "<browser_data_redacted>"
            else:
                clean[key] = _sanitize_llm_trace(item, browser_scope=local_browser)
        return clean
    if isinstance(value, list):
        return [_sanitize_llm_trace(item, browser_scope=browser_scope) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_llm_trace(item, browser_scope=browser_scope) for item in value]
    if isinstance(value, str):
        if browser_scope:
            return "<browser_data_redacted>"
        text = _BROWSER_BOUNDARY_RE.sub("<browser_content_redacted>", value)
        return _DATA_IMAGE_RE.sub("<browser_image_redacted>", text)
    return value

# 环形缓冲容量（条）。足够前端回看近期日志，又不至于无限增长占内存。
_RING_CAPACITY = 2000


class RingBufferHandler(logging.Handler):
    """把每条日志格式化成 dict 存进 deque，供 /api/system/logs 查询。

    线程安全靠 deque + 一把锁（append/查询互斥）。datetime 用 epoch ms，
    避免序列化问题。只缓冲 crew.* 命名空间的日志，避免捕获第三方噪音。
    """

    def __init__(self, capacity: int = _RING_CAPACITY) -> None:
        super().__init__()
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "name": record.name,
                "message": self.format(record),
                # Capture ownership when the event is created. Query-time identity
                # cannot reconstruct which request/background task caused a log.
                "owner_account_id": str(current_owner_account_id.get() or "").strip(),
            }
        except Exception:  # noqa: BLE001 - 缓冲失败不能影响主流程
            return
        with self._lock:
            self._buf.append(entry)

    def query(
        self,
        *,
        level: str | None = None,
        keyword: str | None = None,
        owner_account_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        """按级别/关键词过滤，返回最新的 limit 条（倒序，最新在前）。

        level: 不区分大小写，如 'WARNING'；None 表示全部。
        keyword: 对 name+message 做子串匹配；None 表示不过滤。
        owner_account_id: 指定时仅返回该因果 Owner；None 表示管理员全量视图。
        offset: 从最新往前跳过多少条（分页）。
        返回 {items, total}，total 为过滤后的总条数（便于前端显示）。
        """
        with self._lock:
            snapshot = list(self._buf)
        # 过滤
        lvl = level.upper() if level else None
        kw = (keyword or "").lower()
        owner = str(owner_account_id).strip() if owner_account_id is not None else None
        filtered = [
            e for e in snapshot
            if (lvl is None or e["level"] == lvl)
            and (owner is None or e["owner_account_id"] == owner)
            and (not kw or kw in e["name"].lower() or kw in e["message"].lower())
        ]
        total = len(filtered)
        # 倒序（最新在前）后切片
        filtered.reverse()
        items = filtered[offset: offset + limit] if limit > 0 else filtered[offset:]
        return {"items": items, "total": total}


# 进程内单例，setup_logging 时挂到 crew logger，query_logs 时读取。
_RING: RingBufferHandler | None = None


def setup_logging(level: str = "INFO", log_file: str = "", llm_trace: bool = False) -> None:
    global _CONFIGURED, _RING
    if _CONFIGURED:
        return
    # Windows 默认 GBK：先切 UTF-8，再挂 RichHandler，避免 emoji/中文刷屏 Logging error。
    _ensure_utf8_stdio()
    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)
    handlers: list[logging.Handler] = [
        # 关闭 Rich console 的 markup 解析：业务日志经常包含 LLM 输出、路径、JSON 等带 [] 的文本，
        # 开启 markup 会导致解析异常并抛 MarkupError，反而把正常流程打挂。
        # console=：强制非 legacy Windows 路径，配合 UTF-8 stdio。
        RichHandler(
            console=_make_console(),
            rich_tracebacks=True,
            show_path=False,
            markup=False,
        ),
    ]
    if log_file:
        log_path = Path(log_file).expanduser()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handlers.append(file_handler)
    # 显式挂到 root，避免 basicConfig 在 root 已有 handler 时变成 no-op
    # 导致环形缓冲（供 /api/system/logs）漏挂。
    for h in handlers:
        root.addHandler(h)
    # 环形缓冲：捕获 crew.* 全部日志，供 /api/system/logs 查询
    _RING = RingBufferHandler()
    root.addHandler(_RING)
    # 角色前缀 filter：Dynamic Kanban 场景下自动标识当前执行角色
    root.addFilter(RolePrefixFilter())
    if llm_trace:
        _setup_llm_trace(log_file)
    _CONFIGURED = True


def query_logs(
    *,
    level: str | None = None,
    keyword: str | None = None,
    owner_account_id: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """查询进程内环形缓冲日志。setup_logging 未调用时返回空集。"""
    if _RING is None:
        return {"items": [], "total": 0}
    return _RING.query(
        level=level,
        keyword=keyword,
        owner_account_id=owner_account_id,
        limit=limit,
        offset=offset,
    )


def _setup_llm_trace(log_file: str = "") -> None:
    """配置专用的 LLM trace logger（crew.llm），独立写 jsonl，不污染主日志/控制台。"""
    global _LLM_TRACE_ENABLED
    # trace 文件放在主日志同目录，否则用默认 .crew/logs/
    if log_file:
        trace_path = Path(log_file).expanduser().parent / "llm.jsonl"
    else:
        trace_path = Path(_LLM_TRACE_FILE).expanduser()
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("crew.llm")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # 只写自己的文件，不冒泡到 root（避免重复/截断）
    handler = logging.FileHandler(trace_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    _LLM_TRACE_ENABLED = True


def llm_trace(direction: str, payload: dict[str, Any]) -> None:
    """把一次 LLM 收发写一行 JSON 到 llm.jsonl。direction = request | response。

    未开启（setup_logging 未传 llm_trace=True）时为 no-op，零开销。
    """
    if not _LLM_TRACE_ENABLED:
        return
    if "owner_account_id" not in payload:
        try:
            from crew.core.runctx import current_owner_account_id

            owner_account_id = current_owner_account_id.get()
        except Exception:  # noqa: BLE001 - trace 不能影响主流程
            owner_account_id = ""
        if owner_account_id:
            payload = {**payload, "owner_account_id": owner_account_id}
    record = _sanitize_llm_trace({"ts": round(time.time(), 3), "dir": direction, **payload})
    try:
        line = json.dumps(record, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001 - 序列化失败也不能影响主流程
        line = json.dumps({"ts": record["ts"], "dir": direction, "_error": "serialize_failed"})
    logging.getLogger("crew.llm").info(line)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"crew.{name}")
