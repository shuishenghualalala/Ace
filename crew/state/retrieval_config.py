"""Credential-free retrieval configuration shared by owner state and feature packages."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class WikiSemanticConfig:
    """Wiki 语义检索（embedding）配置。

    默认关闭；开启后在 FTS/关键词之外增加向量召回与相似度重排，失败时回退纯词法。
    """

    enabled: bool = False
    provider: str = "openai"    # openai | local
    model: str = ""             # openai 模型名，或 provider=local 时的 HF 模型 id
    base_url: str = ""          # 语义专用 OpenAI 兼容端点；空则回退主模型 base_url
    api_key_env: str = ""       # 语义专用 api key 的环境变量名；空则回退主模型 key
    rank_weight: float = 40.0   # store.search 里 sim→relevance 乘数
    fuse_weight: float = 0.9    # RRF 第三通道（向量）权重
    min_similarity: float = 0.0  # 重排信号下限（丢弃负 cosine 噪声）
    batch_size: int = 64        # rebuild 每次 embed 文本数

    @classmethod
    def from_raw(cls, raw: Any) -> "WikiSemanticConfig":
        if not isinstance(raw, dict):
            return cls()

        def _bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        return cls(
            enabled=_bool(raw.get("enabled"), False),
            provider=str(raw.get("provider") or "openai").strip().lower(),
            model=str(raw.get("model") or "").strip(),
            base_url=str(raw.get("base_url") or "").strip(),
            api_key_env=str(raw.get("api_key_env") or "").strip(),
            rank_weight=float(raw.get("rank_weight", 40.0)),
            fuse_weight=float(raw.get("fuse_weight", 0.9)),
            min_similarity=float(raw.get("min_similarity", 0.0)),
            batch_size=int(raw.get("batch_size", 64)),
        )


@dataclass
class WikiRerankConfig:
    """Independent reranking service; disabled until explicitly configured."""

    enabled: bool = False
    model: str = "BAAI/bge-reranker-v2-m3"
    base_url: str = ""  # Empty: inherit this owner's embedding endpoint.
    api_key_env: str = ""  # Empty: inherit this owner's embedding credential variable.
    timeout: float = 30.0
    max_chunks_per_doc: int = 8
    max_retries: int = 2

    @classmethod
    def from_raw(cls, raw: Any) -> WikiRerankConfig:
        if not isinstance(raw, dict):
            return cls()
        enabled = raw.get("enabled", False)
        config = cls(
            enabled=enabled if isinstance(enabled, bool) else str(enabled).lower() in {"true", "1", "yes", "on"},
            model=str(raw.get("model") or "BAAI/bge-reranker-v2-m3").strip(),
            base_url=str(raw.get("base_url") or "").strip(),
            api_key_env=str(raw.get("api_key_env") or "").strip(),
            timeout=float(raw.get("timeout", 30.0)),
            max_chunks_per_doc=int(raw.get("max_chunks_per_doc", 8)),
            max_retries=int(raw.get("max_retries", 2)),
        )
        if not math.isfinite(config.timeout) or config.timeout <= 0 or config.max_chunks_per_doc <= 0:
            raise ValueError("wiki.rerank timeout and max_chunks_per_doc must be positive")
        if config.max_retries < 0:
            raise ValueError("wiki.rerank max_retries must be nonnegative")
        return config
