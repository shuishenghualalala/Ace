"""飞书消息资源下载/上传（基于 lark-oapi im.v1）。

入站：``message_resource.get`` 按 message_id+file_key 拉二进制 → 落盘。
出站：``image.create`` / ``file.create`` 上传，返回 image_key / file_key 供发消息。

lark 调用阻塞 → 统一用 asyncio.to_thread 包到线程池；lark 仅在此模块函数内导入
（可选依赖，adapter 已确保启用 feishu 时已安装）。
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import time
from pathlib import Path
from typing import Any

from crew.state.logging import get_logger
from crew.tools.file_utils import read_verified_bytes

log = get_logger("platform.feishu")

# kind → message_resource type 参数（image 用 "image"，其余 file/audio/media 用 "file"）
_RESOURCE_TYPE = {"image": "image", "file": "file", "audio": "file", "media": "file"}

# 出站 file.create 的 file_type（飞书取值：opus/mp4/pdf/doc/xls/ppt/stream）
_FILE_TYPE_BY_EXT = {
    "opus": "opus", "ogg": "opus",
    "mp4": "mp4", "mov": "mp4",
    "pdf": "pdf",
    "doc": "doc", "docx": "doc",
    "xls": "xls", "xlsx": "xls",
    "ppt": "ppt", "pptx": "ppt",
}


def _save_buffer(buffer: bytes, filename: str, base_dir: Path) -> str:
    base_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix
    stem = re.sub(r"[^\w-]", "_", Path(filename).stem)[:40] or "attachment"
    dest = base_dir / f"{stem}_{int(time.time() * 1000)}{suffix}"
    dest.write_bytes(buffer)
    return str(dest)


def _download_sync(client: Any, message_id: str, key: str, rtype: str, max_bytes: int) -> tuple[bytes, str]:
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    request = (
        GetMessageResourceRequest.builder()
        .message_id(message_id).file_key(key).type(rtype).build()
    )
    resp = client.im.v1.message_resource.get(request)
    if hasattr(resp, "success") and not resp.success():
        raise RuntimeError(f"resource get failed: code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}")
    f = getattr(resp, "file", None)
    data = f.read() if hasattr(f, "read") else (f or b"")
    if len(data) > max_bytes:
        raise RuntimeError(f"resource too large: {len(data)} bytes (max {max_bytes})")
    name = getattr(resp, "file_name", None) or key
    return data, str(name)


async def download_resource(
    client: Any, message_id: str, key: str, kind: str, *,
    base_dir: Path, default_name: str = "", max_bytes: int = 30 * 1024 * 1024,
) -> dict[str, Any] | None:
    """下载一个消息资源并落盘，返回 {name, path}；失败返回 None。"""
    rtype = _RESOURCE_TYPE.get(kind, "file")
    try:
        data, resp_name = await asyncio.to_thread(_download_sync, client, message_id, key, rtype, max_bytes)
    except Exception as exc:  # noqa: BLE001
        log.warning("Feishu 资源下载失败 key=%s kind=%s: %s", key, kind, exc)
        return None
    filename = default_name or resp_name or f"{kind}_{key}"
    path = await asyncio.to_thread(_save_buffer, data, filename, base_dir)
    return {"name": filename, "path": path}


def _file_type_for(filename: str) -> str:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    return _FILE_TYPE_BY_EXT.get(ext, "stream")


def _upload_image_sync(client: Any, path: str) -> str:
    from lark_oapi.api.im.v1 import CreateImageRequest, CreateImageRequestBody

    with io.BytesIO(read_verified_bytes(Path(path), max_bytes=30 * 1024 * 1024)) as fh:
        body = CreateImageRequestBody.builder().image_type("message").image(fh).build()
        request = CreateImageRequest.builder().request_body(body).build()
        resp = client.im.v1.image.create(request)
    if hasattr(resp, "success") and not resp.success():
        raise RuntimeError(f"image create failed: code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}")
    key = getattr(getattr(resp, "data", None), "image_key", None)
    if not key:
        raise RuntimeError("image_key missing in response")
    return str(key)


def _upload_file_sync(client: Any, path: str) -> str:
    from lark_oapi.api.im.v1 import CreateFileRequest, CreateFileRequestBody

    filename = os.path.basename(path)
    with io.BytesIO(read_verified_bytes(Path(path), max_bytes=30 * 1024 * 1024)) as fh:
        body = (
            CreateFileRequestBody.builder()
            .file_type(_file_type_for(filename)).file_name(filename).file(fh).build()
        )
        request = CreateFileRequest.builder().request_body(body).build()
        resp = client.im.v1.file.create(request)
    if hasattr(resp, "success") and not resp.success():
        raise RuntimeError(f"file create failed: code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '?')}")
    key = getattr(getattr(resp, "data", None), "file_key", None)
    if not key:
        raise RuntimeError("file_key missing in response")
    return str(key)


async def upload_image(client: Any, path: str) -> str | None:
    """上传图片，返回 image_key；失败返回 None。"""
    try:
        return await asyncio.to_thread(_upload_image_sync, client, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Feishu 图片上传失败 path=%s: %s", path, exc)
        return None


async def upload_file(client: Any, path: str) -> str | None:
    """上传文件，返回 file_key；失败返回 None。"""
    try:
        return await asyncio.to_thread(_upload_file_sync, client, path)
    except Exception as exc:  # noqa: BLE001
        log.warning("Feishu 文件上传失败 path=%s: %s", path, exc)
        return None


# IO 备用：从 bytes 直接上传（预留，当前出站走本地路径）
def bytes_io(data: bytes) -> io.BytesIO:
    return io.BytesIO(data)
