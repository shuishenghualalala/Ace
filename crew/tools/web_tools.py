"""网络与媒体工具：web_search、web_extract、vision_analyze。

Browser Use 由 ``crew.browser`` 通过 Electron 内置 Chromium 实现；本模块不再保留
会误导模型的文本态伪浏览器。
"""

from __future__ import annotations

import asyncio
import html
import re
import struct
import urllib.parse
from typing import Any

from crew.core.errors import ToolError
from crew.tools.file_utils import FileConflictError, _truncate, read_verified_bytes
from crew.tools.registry import Registry, tool_result
from crew.tools.redact import safe_public_error
from crew.tools.security_guard import (
    AuthorizedFileTarget,
    authorize_file_tool,
    authorize_network_url,
    fetch_authorized_url,
)

_MAX_OUTPUT = 12000
_TEXT_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_LINK_RE = re.compile(
    r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL
)


def _fetch_url(plan: Any, timeout: float = 10.0) -> tuple[str, str]:
    response = fetch_authorized_url(plan, timeout=timeout, max_bytes=2_000_000)
    return response.final_url, response.body.decode(response.charset, errors="replace")


def _html_to_text(source: str) -> str:
    source = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", source)
    text = _TEXT_RE.sub(" ", source)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# web_search / web_extract
# ---------------------------------------------------------------------------

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "使用公开搜索页面做轻量网页搜索，返回标题和链接。",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "limit": {"type": "integer", "description": "最多返回多少条，默认 5"},
        },
        "required": ["query"],
    },
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "抓取 URL 并提取标题与正文文本。",
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "网页 URL"}},
        "required": ["url"],
    },
}


async def handle_web_search(
    args: dict[str, Any], *, workspace_store: Any | None, security_service: Any | None
) -> str:
    query = str(args.get("query", "")).strip()
    limit = int(args.get("limit") or 5)
    if not query:
        raise ToolError("query 不能为空")
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    plan = await authorize_network_url(
        url,
        tool_name="web_search",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    _, source = await asyncio.to_thread(_fetch_url, plan)
    results = []
    for href, label in _LINK_RE.findall(source):
        text = _html_to_text(label)
        if not text or href.startswith("#"):
            continue
        results.append({"title": text, "url": html.unescape(href)})
        if len(results) >= limit:
            break
    return tool_result(success=True, query=query, results=results)


async def handle_web_extract(
    args: dict[str, Any], *, workspace_store: Any | None, security_service: Any | None
) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("url 不能为空")
    plan = await authorize_network_url(
        url,
        tool_name="web_extract",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    final_url, source = await asyncio.to_thread(_fetch_url, plan)
    title_match = _TITLE_RE.search(source)
    title = _html_to_text(title_match.group(1)) if title_match else ""
    return tool_result(success=True, url=final_url, title=title, text=_truncate(_html_to_text(source)))


# ---------------------------------------------------------------------------
# vision_analyze
# ---------------------------------------------------------------------------

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": "分析本地图片的基础元信息；当前支持 PNG/JPEG 尺寸识别。",
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "本地图片路径"}},
        "required": ["path"],
    },
}


_MAX_VISION_BYTES = 16 * 1024 * 1024


def _image_size(data: bytes) -> dict[str, Any]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return {"format": "png", "width": width, "height": height}
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            size = int.from_bytes(data[i + 2 : i + 4], "big")
            if marker in {0xC0, 0xC2}:
                height = int.from_bytes(data[i + 5 : i + 7], "big")
                width = int.from_bytes(data[i + 7 : i + 9], "big")
                return {"format": "jpeg", "width": width, "height": height}
            i += 2 + size
    return {"format": "unknown"}


async def handle_vision_analyze(
    args: dict[str, Any], *, workspace_store: Any | None, security_service: Any | None
) -> str:
    authorized = await authorize_file_tool(
        args,
        operation="read",
        tool_name="vision_analyze",
        workspace_store=workspace_store,
        security_service=security_service,
        bind_identity=True,
    )
    if not isinstance(authorized, AuthorizedFileTarget):
        raise ToolError("文件授权未绑定目标身份")
    path = authorized.path
    try:
        data = await asyncio.to_thread(
            read_verified_bytes,
            path,
            max_bytes=_MAX_VISION_BYTES,
            expected_identity=authorized.identity,
        )
        info = _image_size(data)
    except FileNotFoundError as exc:
        raise ToolError(f"图片不存在: {path}") from exc
    except (OSError, ValueError, FileConflictError) as exc:
        raise ToolError(safe_public_error(exc, "图片读取失败")) from exc
    info.update({"path": str(path), "size": len(data)})
    return tool_result(success=True, image=info)


# ---------------------------------------------------------------------------
# schema 与注册
# ---------------------------------------------------------------------------

def register_web_tools(
    registry: Registry, *, workspace_store: Any | None = None, security_service: Any | None = None
) -> None:
    registry.register(
        name="web_search",
        toolset="web",
        schema=WEB_SEARCH_SCHEMA,
        handler=lambda args: handle_web_search(
            args, workspace_store=workspace_store, security_service=security_service
        ),
        is_async=True,
        display_name="网页搜索",
        ui_label_template="搜索 {query}",
        should_defer=True,
        search_hint="web search internet query pages current information",
    )
    registry.register(
        name="web_extract",
        toolset="web",
        schema=WEB_EXTRACT_SCHEMA,
        handler=lambda args: handle_web_extract(
            args, workspace_store=workspace_store, security_service=security_service
        ),
        is_async=True,
        display_name="提取网页",
        ui_label_template="读取网页 {url}",
        should_defer=True,
        search_hint="fetch extract webpage url article content",
    )
    registry.register(
        name="vision_analyze",
        toolset="vision",
        schema=VISION_ANALYZE_SCHEMA,
        handler=lambda args: handle_vision_analyze(
            args, workspace_store=workspace_store, security_service=security_service
        ),
        is_async=True,
        display_name="分析图片",
        ui_label_template="分析图片 {path}",
        should_defer=True,
        search_hint="vision image analyze dimensions local picture",
    )
