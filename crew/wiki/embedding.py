"""Wiki 语义检索的 embedding provider 抽象与实现。

嵌入调用统一为同步接口：检索链路在 ``asyncio.to_thread`` 线程内运行（无 event loop），
本地模型（fastembed）本就是同步，OpenAI 也有同步客户端 ``openai.OpenAI``，因此不引入
async 桥接。云端/本地由 ``build_embedding_provider`` 按配置切换；构建失败返回 ``None``，
由调用方降级到纯词法检索，绝不抛异常打断搜索链路。
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from crew.state.logging import get_logger

if TYPE_CHECKING:
    from crew.wiki.config import WikiSemanticConfig

log = get_logger("wiki.embedding")

# bge v1.5 是"短查询 vs 长正文"非对称检索训练，查询侧需加指令前缀。
BGE_QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："


class EmbeddingProvider(ABC):
    """文本嵌入适配器：把一段文本映射为等长浮点向量。"""

    model: str = ""  # 稳定 id，如 "openai:text-embedding-3-small"
    dim: int = 0     # 向量维度；0 表示未知，由首个向量长度补全

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """返回与 ``texts`` 等长的向量列表，维度一致。"""
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        """嵌入单个查询文本；本地 bge 模型可覆写以追加检索指令前缀。"""
        return self.embed([text])[0]

    def close(self) -> None:
        """释放底层资源；默认无操作。"""


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容 ``/embeddings`` 端点（含 Azure/DeepSeek/Ollama/vLLM 等）。"""

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str = "text-embedding-3-small",
        timeout: float = 60.0,
    ) -> None:
        # 延迟导入，避免未装 openai 时整个包不可用（镜像 OpenAIProvider）。
        from openai import OpenAI

        self._raw_model = model
        self.model = f"openai:{model}"
        self.dim = 1536 if "3-small" in model else 3072 if "3-large" in model else 0
        self._client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )
        self._closed = False

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._raw_model, input=list(texts))
        data = sorted(resp.data, key=lambda item: item.index)
        return [list(item.embedding) for item in data]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("关闭 OpenAI embedding 客户端失败: %s", exc)


class LocalEmbeddingProvider(EmbeddingProvider):
    """本地 fastembed 模型，默认 BAAI/bge-small-zh-v1.5（中文优先）。"""

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5") -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            from crew.wiki.parser import MissingDependencyError

            raise MissingDependencyError(
                dependency="fastembed",
                install_command='uv pip install -e ".[semantic]"',
                message="本地 embedding 需要 fastembed，请安装: uv add fastembed",
            ) from exc
        self.model = f"fastembed:{model}"
        self._model = TextEmbedding(model_name=model)
        self.dim = int(getattr(self._model, "embedding_dim", None) or 0)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = list(self._model.embed(list(texts)))
        return [[float(value) for value in vector] for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([BGE_QUERY_INSTRUCTION + text])[0]


def build_embedding_provider(
    semantic: WikiSemanticConfig,
    *,
    api_key: str = "",
    base_url: str | None = None,
) -> EmbeddingProvider | None:
    """按语义配置构建 provider；不可用时返回 ``None`` 由调用方降级。"""
    if semantic is None or not getattr(semantic, "enabled", False):
        return None
    provider = str(getattr(semantic, "provider", "openai") or "openai").strip().lower()
    model = str(getattr(semantic, "model", "") or "").strip()

    if provider == "openai":
        # 语义专用 api_key_env / base_url 优先，缺省回退主模型配置。
        key_env = str(getattr(semantic, "api_key_env", "") or "").strip()
        resolved_key = api_key
        if key_env:
            resolved_key = os.environ.get(key_env, "") or api_key
        resolved_url = str(getattr(semantic, "base_url", "") or "").strip() or base_url
        if not resolved_key:
            log.warning("语义检索 provider=openai 但未配置 api_key，语义通道禁用")
            return None
        return OpenAIEmbeddingProvider(
            api_key=resolved_key,
            base_url=resolved_url,
            model=model or "text-embedding-3-small",
        )

    if provider == "local":
        from crew.wiki.parser import MissingDependencyError

        try:
            return LocalEmbeddingProvider(model=model or "BAAI/bge-small-zh-v1.5")
        except MissingDependencyError as exc:
            log.warning("语义检索 provider=local 但本地模型不可用: %s", exc)
            return None

    log.warning("未知语义检索 provider=%s，语义通道禁用", provider)
    return None
