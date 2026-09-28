"""web_fetch_cache：TTL + 字节/条数双上限 LRU 的单元行为。"""

from __future__ import annotations

import threading

import pytest

from crew.tools import web_fetch_cache
from crew.tools.web_fetch_cache import CachedFetch, cache_stats


def _entry(markdown: str = "m", url: str = "https://e.com/f") -> CachedFetch:
    return CachedFetch(
        final_url=url, title="t", markdown=markdown, source_truncated=False
    )


@pytest.fixture(autouse=True)
def _clean_cache():
    web_fetch_cache.clear_web_fetch_cache()
    yield
    web_fetch_cache.clear_web_fetch_cache()


def test_put_then_get_returns_entry():
    web_fetch_cache.put_cached_fetch("https://e.com", _entry())

    hit = web_fetch_cache.get_cached_fetch("https://e.com")

    assert hit is not None
    assert hit.final_url == "https://e.com/f"
    assert hit.markdown == "m"


def test_get_unknown_key_returns_none():
    assert web_fetch_cache.get_cached_fetch("https://missing") is None


def test_get_touches_lru_order(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_fetch_cache, "CACHE_MAX_BYTES", 300)
    # 各 100 字节：put A、put B 后 get A（A 变最新），塞入 C 触发逐出时应淘汰 B
    web_fetch_cache.put_cached_fetch("a", _entry("x" * 100))
    web_fetch_cache.put_cached_fetch("b", _entry("y" * 100))
    web_fetch_cache.get_cached_fetch("a")
    web_fetch_cache.put_cached_fetch("c", _entry("z" * 150))

    assert web_fetch_cache.get_cached_fetch("a") is not None
    assert web_fetch_cache.get_cached_fetch("b") is None
    assert web_fetch_cache.get_cached_fetch("c") is not None


def test_expired_entry_is_dropped_and_returns_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_fetch_cache, "CACHE_TTL_SECONDS", 0.0)
    web_fetch_cache.put_cached_fetch("https://e.com", _entry())

    assert web_fetch_cache.get_cached_fetch("https://e.com") is None
    assert cache_stats() == (0, 0)


def test_entry_over_byte_cap_is_rejected(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_fetch_cache, "CACHE_MAX_BYTES", 10)
    web_fetch_cache.put_cached_fetch("https://e.com", _entry("x" * 100))

    assert cache_stats() == (0, 0)
    assert web_fetch_cache.get_cached_fetch("https://e.com") is None


def test_evicts_oldest_until_under_byte_cap(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_fetch_cache, "CACHE_MAX_BYTES", 250)
    for i in range(5):
        web_fetch_cache.put_cached_fetch(f"k{i}", _entry("x" * 100))

    count, total = cache_stats()
    assert total <= 250
    assert count == 2  # 250 // 100 = 2 条
    assert web_fetch_cache.get_cached_fetch("k3") is not None
    assert web_fetch_cache.get_cached_fetch("k4") is not None
    assert web_fetch_cache.get_cached_fetch("k0") is None


def test_over_count_cap_evicts_oldest(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(web_fetch_cache, "CACHE_MAX_ENTRIES", 3)
    for i in range(5):
        web_fetch_cache.put_cached_fetch(f"k{i}", _entry())

    count, _ = cache_stats()
    assert count == 3
    assert web_fetch_cache.get_cached_fetch("k4") is not None
    assert web_fetch_cache.get_cached_fetch("k0") is None


def test_overwrite_same_key_replaces_size_and_refreshes_ttl():
    web_fetch_cache.put_cached_fetch("k", _entry("x" * 100))
    web_fetch_cache.put_cached_fetch("k", _entry("y" * 200))

    assert cache_stats() == (1, 200)


def test_empty_key_is_ignored():
    web_fetch_cache.put_cached_fetch("", _entry())

    assert cache_stats() == (0, 0)
    assert web_fetch_cache.get_cached_fetch("") is None


def test_clear_web_fetch_cache_resets_ledger():
    web_fetch_cache.put_cached_fetch("k", _entry("x" * 100))

    web_fetch_cache.clear_web_fetch_cache()

    assert cache_stats() == (0, 0)


def test_concurrent_put_get_is_consistent():
    def worker(prefix: str) -> None:
        for i in range(200):
            key = f"{prefix}{i}"
            web_fetch_cache.put_cached_fetch(key, _entry("x" * 100))
            web_fetch_cache.get_cached_fetch(key)

    threads = [threading.Thread(target=worker, args=(f"t{n}-",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    count, total = cache_stats()
    # 1600 个唯一 key × 100 字节 < 字节上限，但超过条数硬顶 → 收敛到 256 条
    assert count == 256
    assert total == 256 * 100
