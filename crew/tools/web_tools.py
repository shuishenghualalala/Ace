"""网络与媒体工具：web_search、web_extract、vision_analyze。

Browser Use 由 ``crew.browser`` 通过 Electron 内置 Chromium 实现；本模块不再保留
会误导模型的文本态伪浏览器。
"""

from __future__ import annotations

import html
import re
import struct
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from crew.core.errors import ToolError
from crew.tools.file_utils import _resolve_path, _truncate
from crew.tools.registry import Registry, tool_result

_MAX_OUTPUT = 12000
_TEXT_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_LINK_RE = re.compile(r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)


def _fetch_url(url: str, timeout: float = 10.0) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "Crew/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - tool-controlled URL fetch
        raw = resp.read(2_000_000)
        ctype = resp.headers.get_content_charset() or "utf-8"
        final_url = resp.geturl()
    return final_url, raw.decode(ctype, errors="replace")


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


def handle_web_search(args: dict[str, Any]) -> str:
    query = str(args.get("query", "")).strip()
    limit = int(args.get("limit") or 5)
    if not query:
        raise ToolError("query 不能为空")
    url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    _, source = _fetch_url(url)
    results = []
    for href, label in _LINK_RE.findall(source):
        text = _html_to_text(label)
        if not text or href.startswith("#"):
            continue
        results.append({"title": text, "url": html.unescape(href)})
        if len(results) >= limit:
            break
    return tool_result(success=True, query=query, results=results)


def handle_web_extract(args: dict[str, Any]) -> str:
    url = str(args.get("url", "")).strip()
    if not url:
        raise ToolError("url 不能为空")
    final_url, source = _fetch_url(url)
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


def _image_size(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
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


def handle_vision_analyze(args: dict[str, Any]) -> str:
    path = _resolve_path(str(args.get("path", "")))
    if not path.is_file():
        raise ToolError(f"图片不存在: {path}")
    info = _image_size(path)
    info.update({"path": str(path), "size": path.stat().st_size})
    return tool_result(success=True, image=info)


# ---------------------------------------------------------------------------
# schema 与注册
# ---------------------------------------------------------------------------

def register_web_tools(registry: Registry) -> None:
    registry.register(
        name="web_search",
        toolset="web",
        schema=WEB_SEARCH_SCHEMA,
        handler=handle_web_search,
        is_async=False,
        display_name="网页搜索",
        ui_label_template="搜索 {query}",
        should_defer=True,
        search_hint="web search internet query pages current information",
    )
    registry.register(
        name="web_extract",
        toolset="web",
        schema=WEB_EXTRACT_SCHEMA,
        handler=handle_web_extract,
        is_async=False,
        display_name="提取网页",
        ui_label_template="读取网页 {url}",
        should_defer=True,
        search_hint="fetch extract webpage url article content",
    )
    registry.register(
        name="vision_analyze",
        toolset="vision",
        schema=VISION_ANALYZE_SCHEMA,
        handler=handle_vision_analyze,
        is_async=False,
        display_name="分析图片",
        ui_label_template="分析图片 {path}",
        should_defer=True,
        search_hint="vision image analyze dimensions local picture",
    )
