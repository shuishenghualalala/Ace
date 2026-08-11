"""聊天附件自动收入 Wiki 知识库。

POST /api/upload 落盘后在后台调用，把附件复制进 default 知识库：
- 文档/文本：保存原文件并解析成 markdown，可选自动生成轻量摘要/标签；
- 图片/视频：跟随 wiki.multimodal 配置决定是否自动理解，可选自动生成轻量摘要。

默认不做 LLM 深度整理（生成 entity/topic/relationship），
深度整理需要用户通过 wiki_list_inbox 查看推荐后确认，或显式调用 wiki_plan_ingest。

面向后台任务调用：永不抛异常，失败只记录日志并把 raw source 标记为
failed，留给 Agent / 用户后续挽救。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from crew.core.interfaces import LLMProvider
from crew.state.logging import get_logger
from crew.wiki.compiler import WikiCompiler
from crew.wiki.config import WikiConfig
from crew.wiki.multimodal import describe_media, is_image_mime, is_video_mime
from crew.wiki.parser import MissingDependencyError, guess_mime_type, parse_document_from_bytes
from crew.wiki.schemas import RawSource
from crew.wiki.source_metadata import generate_source_metadata
from crew.wiki.sources import classify_file
from crew.wiki.store import WikiStore
from crew.wiki.store._ids import filename_from_title

log = get_logger("wiki.capture")


@dataclass
class _Ctx:
    """内部上下文，收拢 _capture / _capture_media / _capture_document 的共享参数。"""
    store: WikiStore
    compiler: WikiCompiler | None
    config: WikiConfig
    owner_account_id: str
    kb_id: str
    provider: LLMProvider | None = None


async def capture_upload_to_wiki(
    store,
    compiler,
    config: WikiConfig,
    filename: str,
    content: bytes,
    owner_account_id: str = "",
    kb_id: str = "default",
    provider: LLMProvider | None = None,
) -> RawSource | None:
    """把上传附件收入指定知识库（默认 default），返回登记的 RawSource。

    永不抛异常；任何失败记日志并尽量把 raw source 标记为 failed。
    """
    if store is None or not content:
        return None
    try:
        ctx = _Ctx(
            store=store,
            compiler=compiler,
            config=config,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
            provider=provider,
        )
        return await _capture(ctx, filename, content)
    except Exception:  # noqa: BLE001
        log.warning("聊天附件自动收入 Wiki 失败: %s", filename, exc_info=True)
        return None


async def _capture(ctx: _Ctx, filename: str, content: bytes) -> RawSource:
    source_id = f"upload_{uuid4().hex[:12]}"
    file_type = guess_mime_type(filename)
    source_kind = classify_file(filename, file_type)
    source_dir = ctx.store._source_dir(source_kind, ctx.owner_account_id, ctx.kb_id)

    ext = Path(filename).suffix.lower() or ".bin"
    original_path = source_dir / f"{source_id}-{filename_from_title(Path(filename).stem)}{ext}"
    original_path.write_bytes(content)

    is_image = is_image_mime(file_type)
    is_video = not is_image and is_video_mime(file_type)
    source_type = "image" if is_image else "video" if is_video else "upload"
    raw = RawSource(
        id=source_id,
        title=filename,
        source_type=source_type,  # type: ignore[arg-type]
        parsed_path="",
        original_path=str(original_path),
        file_type=file_type,
        size=len(content),
        created_at=time.time(),
        source_kind=source_kind,
        source_platform="local",
        adapter_name="builtin-file",
        original_ref=filename,
    )
    ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)

    if is_image or is_video:
        await _capture_media(ctx, raw, source_type)
        return raw

    await _capture_document(ctx, raw, content, filename)
    return raw


async def _capture_media(ctx: _Ctx, raw: RawSource, source_type: str) -> None:
    """图片/视频：按多模态配置决定是否自动理解，可选生成轻量元数据；默认不自动深度 ingest。"""
    config = ctx.config
    multimodal = config.multimodal
    auto_process = False
    if multimodal.enabled and source_type == "image" and multimodal.auto_image:
        auto_process = True
    if (
        multimodal.enabled
        and source_type == "video"
        and multimodal.auto_video
        and multimodal.video_upload_confirmed
    ):
        auto_process = True
    if not auto_process:
        return

    prompt = multimodal.prompt_image if source_type == "image" else multimodal.prompt_video
    try:
        # describe_media 是同步 LLM 调用，丢线程池避免阻塞事件循环。
        description = await asyncio.to_thread(
            describe_media,
            str(raw.original_path),
            str(raw.file_type or ""),
            prompt,
            confirm_upload=(source_type == "video"),
        )
    except Exception as exc:  # noqa: BLE001
        raw.parse_status = "failed"
        raw.parse_error = f"多模态理解失败: {exc}"
        ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)
        log.warning("聊天附件多模态理解失败 source=%s: %s", raw.id, exc)
        return

    raw.parsed_path = ctx.store.save_parsed_markdown(
        raw.id, description, ctx.owner_account_id, ctx.kb_id
    )
    raw.parse_status = "parsed"

    # 第二层：自动生成轻量摘要/标签/整理建议
    if config.ingest.auto_summarize and ctx.provider is not None:
        try:
            metadata = await generate_source_metadata(ctx.provider, description)
            raw.summary = metadata["summary"]
            raw.tags = metadata["tags"]
            raw.doc_type = metadata["doc_type"]
            raw.ingest_recommend = metadata["ingest_recommend"]
            raw.ingest_reason = metadata["ingest_reason"]
        except Exception:  # noqa: BLE001
            log.warning("生成 media 元数据失败 source=%s", raw.id, exc_info=True)

    ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)

    # 第三层：默认不自动深度 ingest；显式开启 auto_ingest 即表示用户确认。
    if config.ingest.auto_ingest and ctx.compiler is not None:
        try:
            await ctx.compiler.ingest(raw.id, owner_account_id=ctx.owner_account_id, kb_id=ctx.kb_id)
            raw.ingest_status = "ingested"
            ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)
        except Exception:  # noqa: BLE001
            log.warning("自动 ingest media 失败 source=%s", raw.id, exc_info=True)


async def _capture_document(ctx: _Ctx, raw: RawSource, content: bytes, filename: str) -> None:
    """文档/文本：解析成 markdown、生成轻量元数据、发布全文来源页；默认不深度整理。"""
    try:
        # 解析是 CPU 密集型同步调用，丢线程池避免阻塞事件循环。
        text = await asyncio.to_thread(parse_document_from_bytes, content, filename)
    except MissingDependencyError as exc:
        raw.parse_status = "failed"
        raw.parse_error = f"缺少依赖: {exc}"
        ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)
        log.warning("聊天附件解析缺少依赖 source=%s: %s", raw.id, exc)
        return
    except Exception as exc:  # noqa: BLE001
        raw.parse_status = "failed"
        raw.parse_error = f"解析失败: {exc}"
        ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)
        log.warning("聊天附件解析失败 source=%s: %s", raw.id, exc)
        return

    raw.parsed_path = ctx.store.save_parsed_markdown(raw.id, text, ctx.owner_account_id, ctx.kb_id)
    raw.parse_status = "parsed"

    # 第二层：自动生成轻量摘要/标签/整理建议
    if ctx.config.ingest.auto_summarize and ctx.provider is not None:
        try:
            metadata = await generate_source_metadata(ctx.provider, text)
            raw.summary = metadata["summary"]
            raw.tags = metadata["tags"]
            raw.doc_type = metadata["doc_type"]
            raw.ingest_recommend = metadata["ingest_recommend"]
            raw.ingest_reason = metadata["ingest_reason"]
        except Exception:  # noqa: BLE001
            log.warning("生成 source 元数据失败 source=%s", raw.id, exc_info=True)

    ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)

    # 发布来源页让附件在 Wiki 文件树「来源摘要」中可见：Wiki 树只渲染 page，
    # 仅落 raw source 的附件在桌面端/ Web 端都无处可见。publish_source_page
    # 不做 LLM 结构化分析，与 capture 的轻量定位一致；失败不阻断 capture。
    if ctx.compiler is not None:
        try:
            ctx.compiler.publish_source_page(raw.id, ctx.owner_account_id, ctx.kb_id)
        except Exception:  # noqa: BLE001
            log.warning("聊天附件发布来源页失败 source=%s", raw.id, exc_info=True)

    # 第三层：默认不自动深度 ingest；显式开启 auto_ingest 即表示用户确认。
    if ctx.config.ingest.auto_ingest and ctx.compiler is not None:
        try:
            await ctx.compiler.ingest(raw.id, owner_account_id=ctx.owner_account_id, kb_id=ctx.kb_id)
            raw.ingest_status = "ingested"
            ctx.store.save_raw(raw, ctx.owner_account_id, ctx.kb_id)
        except Exception:  # noqa: BLE001
            log.warning("自动 ingest 文档失败 source=%s", raw.id, exc_info=True)
