"""共享 SSLContext：避免 cacert.pem 病态读取拖慢启动。

根因：httpx 默认 ``ssl.create_default_context(cafile=certifi.where())`` 走 OpenSSL
``BIO_new_file`` + ``PEM_read_bio_X509_AUX`` 循环，对 cacert.pem（234KB）做 2 字节
顺序读约 11.7 万次；叠加安全软件对每次微读的实时扫描，启动期可卡 40 秒。

修复：进程级单例 SSLContext——一次性 ``open().read()`` 读入整份 PEM（1 次读 = 1 次扫描），
再用 ``load_verify_locations(cadata=...)`` 让 OpenSSL 从内存解析，把 11.7 万次微读
压成 1 次。校验语义与默认完全一致（check_hostname=True / CERT_REQUIRED）。

复用：所有 httpx/openai 客户端共享同一 ctx，避免每个 provider 各建一遍。
"""

from __future__ import annotations

import ssl
import threading
from functools import lru_cache

import certifi

_cached_ctx: ssl.SSLContext | None = None
_lock = threading.Lock()


def build_ssl_context() -> ssl.SSLContext:
    """构造一个与 httpx 默认等价、但用 cadata 一次性加载 CA 的 SSLContext。

    - check_hostname=True / verify_mode=CERT_REQUIRED：与 ``ssl.create_default_context``
      默认一致，证书校验完全保留，**不得**降级。
    - cadata 路径：整份 PEM 一次读入，避免 cafile 的 OpenSSL BIO 细粒度读。
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # 一次性读入 cacert.pem（1 次 ReadFile），交给 OpenSSL 从内存解析
    cacert_path = certifi.where()
    with open(cacert_path, "rb") as f:
        pem = f.read().decode("ascii")
    ctx.load_verify_locations(cadata=pem)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    return ctx


def get_shared_ssl_context() -> ssl.SSLContext:
    """返回进程级单例 SSLContext（线程安全，惰性首次构造）。"""
    global _cached_ctx
    if _cached_ctx is not None:
        return _cached_ctx
    with _lock:
        if _cached_ctx is None:
            _cached_ctx = build_ssl_context()
    return _cached_ctx


@lru_cache(maxsize=1)
def _cacert_read_count_probe() -> int:
    """测试用：返回 cadata 加载后 ctx 内 CA 数，供测试断言 cadata 路径生效。"""
    return get_shared_ssl_context().cert_store_stats().get("x509_ca", 0)
