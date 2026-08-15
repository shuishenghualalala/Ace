"""内容收入 Wiki 知识库的入口模块，覆盖两条捕获路径：

1. 聊天附件自动捕获（capture_upload_to_wiki）：POST /api/upload 落盘后在后台调用，
   把附件复制进 default 知识库——文档/文本解析成 markdown，图片/视频跟随
   wiki.multimodal 配置决定是否自动理解。面向后台任务：永不抛异常，失败只记录
   日志并把 raw source 标记为 failed，留给 Agent / 用户后续挽救。
2. 文本捕获流水线（capture_text_source / save_parsed_source）：「文本/解析结果 →
   RawSource → parsed markdown → 内容去重 → 发布全文 Source 页面」的唯一实现，
   被 wiki_capture_text / wiki_capture_session 等工具（crew/wiki/tools.py）与
   POST /api/wiki/capture（crew/gateway/routers/wiki.py，供面板「存入 Wiki」）
   共用。入口层只负责各自的参数解析与结果包装（tool_result / JSONResponse），
   以及各自的通知机制（工具侧发会话 pending change；HTTP 侧由响应直接告知调用方）。

默认都不做 LLM 深度整理（生成 entity/topic/relationship），
深度整理需要用户通过 wiki_list_inbox 查看推荐后确认，或显式调用 wiki_plan_ingest。
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
from crew.wiki.parser import (
    DocumentParseQualityError,
    MissingDependencyError,
    guess_mime_type,
    parse_document_from_bytes,
    validate_parsed_text,
)
from crew.wiki.schemas import RawSource, WikiPage
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


# ---------------------------------------------------------------------------
# 文本捕获流水线：工具与 HTTP 接口共用的唯一实现。
# ---------------------------------------------------------------------------

# 已知内容平台的文本按 article 归类，其余按 note。
_ARTICLE_PLATFORMS = frozenset({"web", "wechat", "zhihu", "x", "xiaohongshu"})


class CaptureError(Exception):
    """文本捕获失败；source_id 非空表示 RawSource 已落库，可供 Wiki Agent 挽救。"""

    def __init__(self, message: str, *, source_id: str = "") -> None:
        super().__init__(message)
        self.source_id = source_id


class CaptureValidationError(CaptureError, ValueError):
    """内容校验失败（入口层映射为 HTTP 400 / tool_error）。"""


@dataclass
class CaptureTextResult:
    """capture_text_source 的结果；page 与 duplicate 至多一个非空。"""

    raw: RawSource
    parsed_path: str
    page: WikiPage | None
    duplicate: RawSource | None


def save_parsed_source(
    store: WikiStore,
    compiler: WikiCompiler,
    raw: RawSource,
    content: str,
    owner_account_id: str,
    kb_id: str,
    *,
    log_message: str,
) -> tuple[str, WikiPage | None, RawSource | None]:
    """统一保存解析文本，并在发布 Source 页面前完成内容去重。

    返回 (parsed_path, page, duplicate)：内容与已有来源重复时不发布页面，page 为 None。
    """
    try:
        text = validate_parsed_text(content, str(raw.title or raw.id))
    except DocumentParseQualityError as exc:
        # 内容质量校验是入口层的 400/tool_error 语义，统一转译（原异常是 RuntimeError，
        # 不应泄漏给入口层逐个识别）。
        raise CaptureValidationError(str(exc), source_id=str(raw.id)) from exc
    parsed_path = str(
        store.save_parsed_markdown(
            raw.id,
            text,
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
    )
    raw.parsed_path = parsed_path
    raw.parse_status = "parsed"
    raw.parse_error = None
    saved_raw = store.load_raw(raw.id, owner_account_id=owner_account_id, kb_id=kb_id)
    if saved_raw is not None:
        raw.content_sha256 = saved_raw.content_sha256
    duplicate = store.check_source_duplicate(
        raw,
        owner_account_id=owner_account_id,
        kb_id=kb_id,
    )
    if not isinstance(duplicate, RawSource):
        duplicate = None
    raw.is_duplicate = duplicate is not None
    store.save_raw(raw, owner_account_id, kb_id)
    if duplicate is not None:
        compiler.finalize_write(
            f"解析 source {raw.id}，内容与 {duplicate.id} 重复，跳过 Source 页面发布",
            owner_account_id=owner_account_id,
            kb_id=kb_id,
        )
        return parsed_path, None, duplicate
    page = compiler.publish_source_page(
        raw.id,
        owner_account_id=owner_account_id,
        kb_id=kb_id,
    )
    compiler.finalize_write(log_message, owner_account_id=owner_account_id, kb_id=kb_id)
    return parsed_path, page, None


def capture_text_source(
    store: WikiStore,
    compiler: WikiCompiler,
    *,
    title: str,
    content: str,
    owner_account_id: str,
    kb_id: str,
    source_type: str = "paste",
    source_platform: str = "",
    source_url: str = "",
    session_id: str = "",
) -> CaptureTextResult:
    """把一段文本存为不可变 RawSource 并发布全文 Source 页面（重复内容只落库不发页）。

    Raises:
        CaptureValidationError: 标题/内容为空，或解析文本校验失败（RawSource 已落库）。
        CaptureError: 落库或发布失败；RawSource 已落库，可供 Wiki Agent 挽救。
    """
    if not title.strip() or not content.strip():
        raise CaptureValidationError("标题和内容不能为空")
    source_id = f"{source_type}_{uuid4().hex[:12]}"
    if source_type == "session":
        material_kind = "session"
    elif source_platform in _ARTICLE_PLATFORMS:
        material_kind = "article"
    else:
        material_kind = "note"
    raw = RawSource(
        id=source_id,
        title=title.strip(),
        source_type=source_type,  # type: ignore[arg-type]
        parsed_path="",
        file_type="text/markdown",
        size=len(content.encode("utf-8")),
        created_at=time.time(),
        session_id=session_id or None,
        source_kind=material_kind,  # type: ignore[arg-type]
        source_platform="crew" if source_type == "session" else source_platform,
        adapter_name="builtin-session" if source_type == "session" else "builtin-text",
        source_url=str(source_url or "").strip() or None,
    )
    store.save_raw(raw, owner_account_id, kb_id)
    try:
        parsed_path, page, duplicate = save_parsed_source(
            store,
            compiler,
            raw,
            content,
            owner_account_id,
            kb_id,
            log_message=f"捕获文本并发布全文 Source 页面 {source_id} ({raw.title})",
        )
    except CaptureError:
        raise  # 流水线内部已带好 source_id，直接透传
    except ValueError as exc:
        raise CaptureValidationError(str(exc), source_id=source_id) from exc
    except Exception as exc:  # noqa: BLE001 - 统一包装以带出 source_id 供挽救
        raise CaptureError(str(exc), source_id=source_id) from exc
    return CaptureTextResult(raw=raw, parsed_path=parsed_path, page=page, duplicate=duplicate)
