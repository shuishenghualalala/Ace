"""web_extract 抓取结果的进程内缓存。

key 一律用模型传入的原始请求 URL（不做规范化）；TTL 15 分钟 + 字节账本 LRU。
只缓存成功抓取的正文——重定向、HTTP 错误等失败路径抛异常，不会走到 put。
缓存绝不绕过授权：调用方必须先过 ``authorize_network_tool`` 再查缓存，
命中与否都要过（缓存只省网络，不省审批）。

与参照实现的两处显式差异：
- 加 ``threading.Lock``：web_extract 在并行安全名单里，同一 turn 可并发调用；
  锁内全是内存字典操作，不跨 await、不做 I/O；
- 除字节上限外加条数硬顶 ``CACHE_MAX_ENTRIES``，防"少而大"的条目长期占满账本。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

CACHE_TTL_SECONDS = 15 * 60.0
CACHE_MAX_BYTES = 50 * 1024 * 1024
CACHE_MAX_ENTRIES = 256


@dataclass(frozen=True)
class CachedFetch:
    """一次成功抓取的提取结果；markdown 为渲染后正文（受 render_html 源码上限约束）。"""

    final_url: str
    title: str
    markdown: str
    source_truncated: bool
    persisted_path: str | None = None  # 全文落盘路径（正文超内联预算时才有）


# entry = (value, size_bytes, expires_at)；size 预计算，命中/逐出路径不重复 encode
_ENTRIES: "OrderedDict[str, tuple[CachedFetch, int, float]]" = OrderedDict()
_BYTES = 0
_LOCK = threading.Lock()


def get_cached_fetch(key: str) -> CachedFetch | None:
    """命中返回条目并做 LRU touch；过期即删并返回 None。"""
    global _BYTES
    if not key:
        return None
    now = time.monotonic()
    with _LOCK:
        item = _ENTRIES.get(key)
        if item is None:
            return None
        value, size, expires_at = item
        if expires_at <= now:
            del _ENTRIES[key]
            _BYTES -= size
            return None
        _ENTRIES.move_to_end(key)
        return value


def put_cached_fetch(key: str, value: CachedFetch) -> None:
    """写入条目并刷新 TTL；单条超字节上限直接丢弃，随后按过期→LRU 逐出收敛。"""
    global _BYTES
    if not key:
        return
    size = len(value.markdown.encode("utf-8", "replace"))
    if size > CACHE_MAX_BYTES:
        return
    now = time.monotonic()
    with _LOCK:
        previous = _ENTRIES.pop(key, None)
        if previous is not None:
            _BYTES -= previous[1]
        _ENTRIES[key] = (value, size, now + CACHE_TTL_SECONDS)
        _BYTES += size
        _prune_locked(now)


def _prune_locked(now: float) -> None:
    """先清过期，再按插入序（=LRU 序）从最旧逐出，直到双上限都满足。调用方持锁。"""
    global _BYTES
    stale = [key for key, (_v, size, expires_at) in _ENTRIES.items() if expires_at <= now]
    for key in stale:
        _BYTES -= _ENTRIES.pop(key)[1]
    while _ENTRIES and (_BYTES > CACHE_MAX_BYTES or len(_ENTRIES) > CACHE_MAX_ENTRIES):
        _key, (_value, size, _expires_at) = _ENTRIES.popitem(last=False)
        _BYTES -= size


def clear_web_fetch_cache() -> None:
    """清空缓存（测试与诊断用）。"""
    global _BYTES
    with _LOCK:
        _ENTRIES.clear()
        _BYTES = 0


def cache_stats() -> tuple[int, int]:
    """(条数, 字节数)：测试与诊断用。"""
    with _LOCK:
        return len(_ENTRIES), _BYTES
