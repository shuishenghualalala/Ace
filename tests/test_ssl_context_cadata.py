"""Bug1 回归测试：cacert.pem 走 cadata 一次性加载，SSL 校验不降级。

回归背景：httpx 默认 cafile=certifi.where() 触发 OpenSSL BIO 11.7 万次 2 字节读，
叠加安全软件扫描卡启动 40s。改 cadata + 共享 SSLContext 修复。本测试锁死两个不变量：
1) 两个 provider 都用共享 ctx（cadata 路径，cafile 病态读不再发生）；
2) 校验语义与默认一致——check_hostname=True / CERT_REQUIRED，绝不能关。
"""

from __future__ import annotations

import importlib
import ssl

import certifi
import httpx
import pytest

from crew.providers.ssl_context import get_shared_ssl_context


def test_shared_ssl_context_uses_cadata_and_keeps_verification():
    """单例 ctx：校验全开 + CA 数与 certifi 一致（证明 cadata 加载成功）。"""
    ctx = get_shared_ssl_context()
    assert ctx.check_hostname is True
    assert ctx.verify_mode == ssl.CERT_REQUIRED

    # cadata 路径加载的 CA 数应等于 certifi cacert.pem 里的 CA 数
    stats = ctx.cert_store_stats()
    expected = _ca_count_in_certifi()
    assert stats["x509_ca"] == expected, (
        f"cadata 未正确加载：ctx 内 {stats['x509_ca']} CA，certifi 含 {expected} CA"
    )


def test_shared_ssl_context_is_singleton():
    """多次获取返回同一对象（进程级单例，避免每个 provider 各建一遍）。"""
    assert get_shared_ssl_context() is get_shared_ssl_context()


def test_build_ssl_context_reads_cacert_once(monkeypatch):
    """cadata 路径：整份 PEM 一次性 open().read()，而非 cafile 的逐字节 BIO 读。

    通过计数 open() 调用次数断言——cadata 路径对 cacert.pem 只 open 一次。
    """
    import builtins

    import crew.providers.ssl_context as mod

    real_open = builtins.open
    opens: list[str] = []
    cacert = certifi.where()

    def counting_open(path, *args, **kwargs):
        try:
            if str(path) == cacert:
                opens.append(str(path))
        except Exception:
            pass
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting_open)
    mod._cached_ctx = None  # 重置单例，强制重建
    try:
        ctx = mod.build_ssl_context()
    finally:
        mod._cached_ctx = None  # 清理，避免污染其他测试
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert len(opens) == 1, f"cadata 路径应只 open cacert 一次，实际 {len(opens)} 次"


@pytest.mark.parametrize(
    "provider_path,base_url",
    [
        ("crew.providers.openai_provider.OpenAIProvider", "https://example.test/v1"),
        ("crew.providers.anthropic_provider.AnthropicProvider", "https://example.test"),
    ],
    ids=["openai", "anthropic"],
)
def test_provider_uses_shared_ssl_context(provider_path, base_url):
    """Provider 的 httpx client verify 指向共享 ctx。"""
    module_path, cls_name = provider_path.rsplit(".", 1)
    provider_cls = getattr(importlib.import_module(module_path), cls_name)
    provider = provider_cls(api_key="sk-test", base_url=base_url)
    client = provider._client
    # OpenAI SDK 内部还包一层 AsyncOpenAI，其 ._client 才是 httpx client；
    # Anthropic provider._client 直接就是 httpx.AsyncClient
    httpx_client = getattr(client, "_client", client)
    assert isinstance(httpx_client, httpx.AsyncClient)
    # httpx 把 verify=<SSLContext> 存到 transport._pool._ssl_context；直接比对单例对象
    shared = get_shared_ssl_context()
    assert _extract_ssl_context(httpx_client) is shared


def _extract_ssl_context(client: httpx.AsyncClient) -> ssl.SSLContext | None:
    """从 httpx AsyncClient 提取其内部 SSLContext（兼容不同 httpx 版本字段位置）。"""
    transport = getattr(client, "_transport", None)
    pool = getattr(transport, "_pool", None)
    # httpx < 0.28: pool._ssl_context；httpx >= 0.28: pool.ssl_context
    for attr in ("_ssl_context", "ssl_context"):
        ctx = getattr(pool, attr, None)
        if isinstance(ctx, ssl.SSLContext):
            return ctx
    return None


def _ca_count_in_certifi() -> int:
    """数 certifi cacert.pem 里 'BEGIN CERTIFICATE' 出现次数，作为 cadata 应加载的 CA 数。"""
    pem = certifi.where()
    with open(pem, "rb") as f:
        content = f.read().decode("ascii")
    return content.count("BEGIN CERTIFICATE")
