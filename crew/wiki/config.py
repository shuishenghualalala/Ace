"""Wiki 模块专属配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WikiStorageConfig:
    """Wiki 数据目录配置。

    ``root`` 为空时沿用 owner runtime home 下的 ``wiki_lib``，保证已有数据
    无需迁移；配置后则使用 ``{root}/accounts/{owner_hash}/wiki_lib``。
    """

    root: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> "WikiStorageConfig":
        if not isinstance(raw, dict):
            return cls()
        return cls(root=str(raw.get("root") or "").strip())

    def resolved_root(self) -> Path | None:
        value = str(os.getenv("CREW_WIKI_HOME") or self.root or "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = Path.home() / path
        return path.resolve()


@dataclass
class WikiMultimodalConfig:
    """Wiki 多模态材料（图片/视频）理解配置。

    支持两条通路：
    - 前端点击上传：由 auto_image / auto_video 控制是否在上传时自动理解并 ingest。
    - 对话式：Agent 通过 wiki_parse_source 按来源类型调用多模态能力，
      视频需用户在对话中显式确认（confirm_upload=true）。
    """

    enabled: bool = True
    auto_image: bool = True          # 前端上传图片时是否自动理解并 ingest
    auto_video: bool = False         # 前端上传视频时是否自动理解并 ingest（默认关闭，需外传）
    video_upload_confirmed: bool = False  # 自动视频通路：用户已了解视频外传风险并同意
    prompt_image: str = "请详细描述这张图片的内容，包括文字、图表、人物、场景等关键信息"
    prompt_video: str = "请详细描述这个视频的内容，包括画面、对话、关键事件等"

    @classmethod
    def from_raw(cls, raw: Any) -> "WikiMultimodalConfig":
        if not isinstance(raw, dict):
            return cls()

        def _bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        return cls(
            enabled=_bool(raw.get("enabled"), True),
            auto_image=_bool(raw.get("auto_image"), True),
            auto_video=_bool(raw.get("auto_video"), False),
            video_upload_confirmed=_bool(raw.get("video_upload_confirmed"), False),
            prompt_image=str(raw.get("prompt_image") or cls.prompt_image),
            prompt_video=str(raw.get("prompt_video") or cls.prompt_video),
        )


@dataclass
class WikiIngestConfig:
    """Wiki 深度整理流程配置。"""

    # plan 成功后是否自动应用；默认 false，要求用户对深度整理先确认
    auto_apply: bool = False
    # 捕获后是否自动生成轻量摘要（第二层）
    auto_summarize: bool = True
    # 捕获后是否自动深度整理（第三层）；默认关闭，避免污染知识库
    auto_ingest: bool = False

    @classmethod
    def from_raw(cls, raw: Any) -> "WikiIngestConfig":
        if not isinstance(raw, dict):
            return cls()

        def _bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        return cls(
            auto_apply=_bool(raw.get("auto_apply"), False),
            auto_summarize=_bool(raw.get("auto_summarize"), True),
            auto_ingest=_bool(raw.get("auto_ingest"), False),
        )


@dataclass
class WikiConfig:
    """Wiki 功能配置。

    从 config.yaml 的 `wiki:` 节加载；敏感信息不在这里。
    """

    enabled: bool = True          # 是否启用 Wiki 与相关 API
    # 编译/摘要使用的模型档案 id（llm.models 下的 key）；空 = 跟随当前会话模型，
    # 无会话上下文时回退 owner 默认模型。
    model: str = ""
    capture_attachments: bool = True  # 聊天附件上传后是否自动收入 default 知识库
    storage: WikiStorageConfig = field(default_factory=WikiStorageConfig)
    ingest: WikiIngestConfig = field(default_factory=WikiIngestConfig)
    multimodal: WikiMultimodalConfig = field(default_factory=WikiMultimodalConfig)

    @classmethod
    def from_raw(cls, raw: Any) -> "WikiConfig":
        """从 config.yaml 的 wiki 节构造配置。"""
        if not isinstance(raw, dict):
            return cls()

        def _bool(value: Any, default: bool) -> bool:
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return default

        mm_raw = raw.get("multimodal")
        ingest_raw = raw.get("ingest")
        storage_raw = raw.get("storage")
        return cls(
            enabled=_bool(raw.get("enabled"), True),
            model=str(raw.get("model") or "").strip(),
            capture_attachments=_bool(raw.get("capture_attachments"), True),
            storage=WikiStorageConfig.from_raw(storage_raw),
            ingest=WikiIngestConfig.from_raw(ingest_raw),
            multimodal=WikiMultimodalConfig.from_raw(mm_raw) if isinstance(mm_raw, dict) else WikiMultimodalConfig(),
        )
