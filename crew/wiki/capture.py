"""聊天附件自动收入 Wiki 知识库。

POST /api/upload 落盘后在后台调用，把附件复制进 default 知识库：
- 文档/文本：保存原文件并解析成 markdown（不做 LLM 深度整理）；
- 图片/视频：跟随 wiki.multimodal 配置决定是否自动理解并 ingest，
  与 /api/wiki/upload 的行为一致。

面向后台任务调用：永不抛异常，失败只记录日志并把 raw source 标记为
failed，留给 Agent / 用户后续挽救。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from uuid import uuid4

from crew.state.logging import get_logger
from crew.wiki.config import WikiConfig
from crew.wiki.multimodal import describe_media, is_image_mime, is_video_mime
from crew.wiki.parser import MissingDependencyError, guess_mime_type, parse_document_from_bytes
from crew.wiki.schemas import RawSource
from crew.wiki.sources import classify_file
from crew.wiki.store._ids import filename_from_title

log = get_logger("wiki.capture")


async def capture_upload_to_wiki(
    store,
    compiler,
    config: WikiConfig,
    filename: str,
    content: bytes,
    owner_account_id: str = "",
    kb_id: str = "default",
) -> RawSource | None:
    """把上传附件收入指定知识库（默认 default），返回登记的 RawSource。

    永不抛异常；任何失败记日志并尽量把 raw source 标记为 failed。
    """
    if store is None or not content:
        return None
    try:
        return await _capture(store, compiler, config, filename, content, owner_account_id, kb_id)
    except Exception:  # noqa: BLE001
        log.warning("聊天附件自动收入 Wiki 失败: %s", filename, exc_info=True)
        return None


async def _capture(
    store,
    compiler,
    config: WikiConfig,
    filename: str,
    content: bytes,
    owner_account_id: str,
    kb_id: str,
) -> RawSource:
    source_id = f"upload_{uuid4().hex[:12]}"
    file_type = guess_mime_type(filename)
    source_kind = classify_file(filename, file_type)
    source_dir = store._source_dir(source_kind, owner_account_id, kb_id)

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
    store.save_raw(raw, owner_account_id, kb_id)

    if is_image or is_video:
        await _capture_media(store, compiler, config, raw, source_type, owner_account_id, kb_id)
        return raw

    await _capture_document(store, raw, content, filename, owner_account_id, kb_id)
    return raw


async def _capture_media(
    store,
    compiler,
    config: WikiConfig,
    raw: RawSource,
    source_type: str,
    owner_account_id: str,
    kb_id: str,
) -> None:
    """图片/视频：按多模态配置决定是否自动理解并 ingest，否则仅保留原文件。"""
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
    if not auto_process or compiler is None:
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
        store.save_raw(raw, owner_account_id, kb_id)
        log.warning("聊天附件多模态理解失败 source=%s: %s", raw.id, exc)
        return

    raw.parsed_path = store.save_parsed_markdown(
        raw.id, description, owner_account_id, kb_id
    )
    raw.parse_status = "parsed"
    store.save_raw(raw, owner_account_id, kb_id)
    await compiler.ingest(raw.id, owner_account_id=owner_account_id, kb_id=kb_id)


async def _capture_document(
    store,
    raw: RawSource,
    content: bytes,
    filename: str,
    owner_account_id: str,
    kb_id: str,
) -> None:
    """文档/文本：解析成 markdown 保存，不做 LLM 深度整理。"""
    try:
        # 解析是 CPU 密集型同步调用，丢线程池避免阻塞事件循环。
        text = await asyncio.to_thread(parse_document_from_bytes, content, filename)
    except MissingDependencyError as exc:
        raw.parse_status = "failed"
        raw.parse_error = f"缺少依赖: {exc}"
        store.save_raw(raw, owner_account_id, kb_id)
        log.warning("聊天附件解析缺少依赖 source=%s: %s", raw.id, exc)
        return
    except Exception as exc:  # noqa: BLE001
        raw.parse_status = "failed"
        raw.parse_error = f"解析失败: {exc}"
        store.save_raw(raw, owner_account_id, kb_id)
        log.warning("聊天附件解析失败 source=%s: %s", raw.id, exc)
        return

    raw.parsed_path = store.save_parsed_markdown(raw.id, text, owner_account_id, kb_id)
    raw.parse_status = "parsed"
    store.save_raw(raw, owner_account_id, kb_id)
