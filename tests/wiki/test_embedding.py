"""Wiki embedding provider 测试。"""

import pytest

from crew.wiki.config import WikiSemanticConfig
from crew.wiki.embedding import (
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    build_embedding_provider,
)


class _FakeEmbedding(EmbeddingProvider):
    """确定性：把任意文本映射为固定向量，用于测试接口行为。"""

    model = "test:fake"
    dim = 4

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_embed_query_defaults_to_embed_first():
    provider = _FakeEmbedding()
    assert provider.embed_query("任意查询") == [1.0, 0.0, 0.0, 0.0]


def test_build_disabled_returns_none():
    assert build_embedding_provider(WikiSemanticConfig(enabled=False)) is None


def test_build_openai_missing_key_returns_none():
    semantic = WikiSemanticConfig(enabled=True, provider="openai")
    assert build_embedding_provider(semantic, api_key="") is None


def test_build_unknown_provider_returns_none():
    semantic = WikiSemanticConfig(enabled=True, provider="bogus")
    assert build_embedding_provider(semantic, api_key="key") is None


def test_build_openai_resolves_semantic_endpoint(monkeypatch):
    monkeypatch.setenv("SEM_KEY", "semantic-key")
    semantic = WikiSemanticConfig(
        enabled=True,
        provider="openai",
        model="BAAI/bge-m3",
        base_url="https://api.siliconflow.cn/v1",
        api_key_env="SEM_KEY",
    )
    provider = build_embedding_provider(
        semantic,
        api_key="main-key",
        base_url="https://api.openai.com/v1",
    )
    try:
        assert provider is not None
        assert provider.model == "openai:BAAI/bge-m3"
        assert provider._client.api_key == "semantic-key"
        assert str(provider._client.base_url).startswith("https://api.siliconflow.cn/v1")
    finally:
        if provider is not None:
            provider.close()


def test_openai_embed_empty_returns_empty():
    # 仅构造与空输入路径，不触发网络。
    provider = OpenAIEmbeddingProvider(api_key="test-key")
    try:
        assert provider.embed([]) == []
    finally:
        provider.close()


def test_local_provider_raises_missing_dependency(monkeypatch):
    import builtins

    from crew.wiki.embedding import LocalEmbeddingProvider
    from crew.wiki.parser import MissingDependencyError

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "fastembed":
            raise ImportError("No module named 'fastembed'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    with pytest.raises(MissingDependencyError):
        LocalEmbeddingProvider()
