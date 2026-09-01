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
from functools import partial
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from crew.core.errors import ToolError
from crew.core.interfaces import ToolResultRetention
from crew.security.outbound import (
    PublicRedirectApprovalRequired,
    fetch_public_http,
    parse_public_http_target,
)
from crew.tools.file_utils import _truncate, read_verified_bytes
from crew.tools.registry import Registry, tool_result
from crew.tools.security_guard import authorize_file_tool, authorize_network_tool

_MAX_OUTPUT = 12000
_TEXT_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
# Bing/DuckDuckGo 等搜索页对 bot UA 不友好，用真实浏览器 UA 更稳。
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _fetch_url(
    url: str,
    timeout: float = 10.0,
    allowed_targets: set[tuple[str, int, str]] | None = None,
) -> tuple[str, str]:
    final_url, raw, _content_type, charset = fetch_public_http(
        url,
        timeout=timeout,
        max_bytes=2_000_000,
        headers={"User-Agent": _USER_AGENT},
        allowed_targets=allowed_targets,
    )
    return final_url, raw.decode(charset, errors="replace")


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


class _BingResults(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[tuple[str, str]] = []
        self._href = ""
        self._text: list[str] = []
        self._result_depth = 0
        self._heading_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.lower()
        values = {name.lower(): value or "" for name, value in attrs}
        if self._result_depth:
            self._result_depth += 1
        elif normalized == "li" and "b_algo" in values.get("class", "").split():
            self._result_depth = 1
        if not self._result_depth:
            return
        if normalized == "h2":
            self._heading_depth = self._result_depth
        if normalized == "a" and self._heading_depth and values.get("href") and not self._href:
            self._href = values["href"]
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized == "a" and self._href:
            self.results.append((self._href, "".join(self._text).strip()))
            self._href = ""
            self._text = []
        if self._heading_depth == self._result_depth and normalized == "h2":
            self._heading_depth = 0
        if self._result_depth:
            self._result_depth -= 1


def _search_result_url(raw: str) -> str:
    href = html.unescape(raw)
    if href.startswith("//"):
        href = f"https:{href}"
    parse_public_http_target(href)
    return href


async def handle_web_search(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    query = str(args.get("query", "")).strip()
    limit = max(1, min(20, int(args.get("limit") or 5)))
    if not query:
        raise ToolError("query 不能为空")
    url = "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": query})
    try:
        _, source = await _authorized_fetch(
            url,
            tool_name="web_search",
            workspace_store=workspace_store,
            security_service=security_service,
        )
    except (OSError, ValueError) as exc:
        raise ToolError(f"网页搜索失败: {exc}") from exc
    parser = _BingResults()
    parser.feed(source)
    results = []
    for href, label in parser.results:
        text = re.sub(r"\s+", " ", label).strip()
        if not text:
            continue
        try:
            result_url = _search_result_url(href)
        except ValueError:
            continue
        results.append({"title": text, "url": result_url})
        if len(results) >= limit:
            break
    return tool_result(success=True, query=query, results=results)


async def handle_web_extract(
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("url 不能为空")
    try:
        final_url, source = await _authorized_fetch(
            url,
            tool_name="web_extract",
            workspace_store=workspace_store,
            security_service=security_service,
        )
    except (OSError, ValueError) as exc:
        raise ToolError(f"网页提取失败: {exc}") from exc
    title_match = _TITLE_RE.search(source)
    title = _html_to_text(title_match.group(1)) if title_match else ""
    return tool_result(success=True, url=final_url, title=title, text=_truncate(_html_to_text(source)))


async def _authorized_fetch(
    url: str,
    *,
    tool_name: str,
    workspace_store: Any | None,
    security_service: Any | None,
) -> tuple[str, str]:
    """Authorize every exact redirect authority before following it."""
    next_target = url
    allowed: set[tuple[str, int, str]] = set()
    for _attempt in range(6):
        await authorize_network_tool(
            next_target,
            tool_name=tool_name,
            workspace_store=workspace_store,
            security_service=security_service,
        )
        allowed.add(parse_public_http_target(next_target).authority)
        try:
            return await asyncio.to_thread(_fetch_url, url, 10.0, allowed)
        except PublicRedirectApprovalRequired as exc:
            next_target = exc.url
    raise ToolError("网页重定向次数过多")


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


def _image_size(path: Path) -> dict[str, Any]:
    data = read_verified_bytes(path, max_bytes=64 * 1024 * 1024)
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
    args: dict[str, Any],
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> str:
    """Inspect one image only after the same canonical file authorization as file_read."""
    path = await authorize_file_tool(
        args,
        operation="read",
        tool_name="vision_analyze",
        workspace_store=workspace_store,
        security_service=security_service,
    )
    if not path.is_file():
        raise ToolError(f"图片不存在: {path}")
    info = _image_size(path)
    info.update({"path": str(path), "size": path.stat().st_size})
    return tool_result(success=True, image=info)


# ---------------------------------------------------------------------------
# schema 与注册
# ---------------------------------------------------------------------------

def register_web_tools(
    registry: Registry,
    *,
    workspace_store: Any | None = None,
    security_service: Any | None = None,
) -> None:
    registry.register(
        name="web_search",
        toolset="web",
        schema=WEB_SEARCH_SCHEMA,
        handler=partial(
            handle_web_search,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="网页搜索",
        ui_label_template="搜索 {query}",
        should_defer=True,
        search_hint="web search internet query pages current information",
        result_retention=ToolResultRetention.TEMPORARY,
    )
    registry.register(
        name="web_extract",
        toolset="web",
        schema=WEB_EXTRACT_SCHEMA,
        handler=partial(
            handle_web_extract,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="提取网页",
        ui_label_template="读取网页 {url}",
        should_defer=True,
        search_hint="fetch extract webpage url article content",
        result_retention=ToolResultRetention.TEMPORARY,
    )
    registry.register(
        name="vision_analyze",
        toolset="vision",
        schema=VISION_ANALYZE_SCHEMA,
        handler=partial(
            handle_vision_analyze,
            workspace_store=workspace_store,
            security_service=security_service,
        ),
        is_async=True,
        display_name="分析图片",
        ui_label_template="分析图片 {path}",
        should_defer=True,
        search_hint="vision image analyze dimensions local picture",
        # 视觉结论可能昂贵且无法从普通文本工具恢复，按重要结果保护。
        result_retention=ToolResultRetention.IMPORTANT,
    )
