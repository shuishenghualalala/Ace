"""Wiki 素材分类与可选提取适配器状态。

目录只表达素材类型；网站平台保存在 RawSource 元数据中。这里是来源路由的
唯一权威入口，避免工具、Prompt 和存储层分别维护扩展名/域名表。
"""

from __future__ import annotations

import importlib.util
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .schemas import ExtractionState, SourceKind

SOURCE_DIRS: dict[str, str] = {
    "article": "articles",
    "pdf": "pdfs",
    "word": "words",
    "excel": "excels",
    "ppt": "ppts",
    "note": "notes",
    "session": "sessions",
    "image": "images",
    "video": "videos",
    "asset": "assets",
}

_WORD_EXTS = {".doc", ".docx", ".odt", ".rtf"}
_EXCEL_EXTS = {".xls", ".xlsx", ".csv", ".tsv", ".ods"}
_PPT_EXTS = {".ppt", ".pptx", ".odp"}
_NOTE_EXTS = {".md", ".markdown", ".txt"}
_ARTICLE_EXTS = {".html", ".htm", ".mhtml"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg"}
_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


@dataclass(frozen=True)
class SourceAdapterStatus:
    source_platform: str
    source_kind: SourceKind
    adapter_name: str
    state: ExtractionState
    detail: str
    recovery_action: str
    fallback_hint: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_platform": self.source_platform,
            "source_kind": self.source_kind,
            "adapter_name": self.adapter_name,
            "state": self.state,
            "detail": self.detail,
            "recovery_action": self.recovery_action,
            "fallback_hint": self.fallback_hint,
        }


def classify_file(path: str | Path, content_type: str = "") -> SourceKind:
    suffix = Path(path).suffix.lower()
    mime = (content_type or mimetypes.guess_type(str(path))[0] or "").lower()
    if suffix == ".pdf" or mime == "application/pdf":
        return "pdf"
    if suffix in _WORD_EXTS or "word" in mime or "opendocument.text" in mime:
        return "word"
    if suffix in _EXCEL_EXTS or "excel" in mime or "spreadsheet" in mime:
        return "excel"
    if suffix in _PPT_EXTS or "powerpoint" in mime or "presentation" in mime:
        return "ppt"
    if suffix in _ARTICLE_EXTS or mime in {"text/html", "application/xhtml+xml"}:
        return "article"
    if suffix in _NOTE_EXTS or mime.startswith("text/"):
        return "note"
    if suffix in _IMAGE_EXTS or mime.startswith("image/"):
        return "image"
    if suffix in _VIDEO_EXTS or mime.startswith("video/"):
        return "video"
    return "asset"


def classify_url(url: str) -> tuple[SourceKind, str]:
    host = (urlparse(url).hostname or "").lower()
    if host == "youtu.be" or host.endswith(".youtube.com") or host == "youtube.com":
        return "video", "youtube"
    if host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com"):
        return "article", "x"
    if host == "mp.weixin.qq.com" or host.endswith(".weixin.qq.com"):
        return "article", "wechat"
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        return "article", "zhihu"
    if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com") or host == "xhslink.com":
        return "article", "xiaohongshu"
    return "article", "web"


def adapter_status(platform: str) -> SourceAdapterStatus:
    if platform == "youtube":
        available = importlib.util.find_spec("youtube_transcript_api") is not None
        return SourceAdapterStatus(
            source_platform=platform,
            source_kind="video",
            adapter_name="youtube-transcript-api",
            state="available" if available else "not_installed",
            detail="YouTube 字幕适配器可用" if available else "未安装 youtube-transcript-api",
            recovery_action="继续提取字幕" if available else "安装可选依赖后重试，或粘贴字幕文本",
            fallback_hint="可将字幕或视频摘要作为文本添加到 Wiki",
        )
    if platform in {"x", "wechat", "zhihu", "xiaohongshu"}:
        return SourceAdapterStatus(
            source_platform=platform,
            source_kind="article",
            adapter_name="builtin-html",
            state="available",
            detail="使用内置网页提取；登录态或反爬页面可能需要手动粘贴",
            recovery_action="先自动提取，失败后改走手动入口",
            fallback_hint="复制正文并以该平台来源添加到 Wiki",
        )
    return SourceAdapterStatus(
        source_platform="web",
        source_kind="article",
        adapter_name="builtin-html",
        state="available",
        detail="内置公开网页提取可用",
        recovery_action="继续提取网页正文",
        fallback_hint="如果页面需要登录，可复制正文后添加到 Wiki",
    )


def all_adapter_statuses() -> list[SourceAdapterStatus]:
    return [adapter_status(name) for name in ("web", "x", "wechat", "zhihu", "xiaohongshu", "youtube")]


def extract_youtube_video_id(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host == "youtu.be":
        value = parsed.path.strip("/").split("/", 1)[0]
    elif host == "youtube.com" or host.endswith(".youtube.com"):
        if parsed.path == "/watch":
            value = parse_qs(parsed.query).get("v", [""])[0]
        else:
            match = re.match(r"^/(?:embed|shorts|live)/([A-Za-z0-9_-]{11})", parsed.path)
            value = match.group(1) if match else ""
    else:
        value = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        raise ValueError("无法从 URL 识别 YouTube 视频 ID")
    return value


def fetch_youtube_transcript(
    url: str,
    *,
    timestamps: bool = True,
    authorizations: tuple[object, ...] = (),
) -> tuple[str, str]:
    """返回 ``(markdown, video_id)``；依赖缺失或无字幕时向上抛出。"""
    import json

    import requests
    from youtube_transcript_api import YouTubeTranscriptApi

    from crew.core.errors import ToolError
    from crew.security.outbound import canonicalize_host
    from crew.tools.security_guard import fetch_authorized_url, fetch_public_url

    video_id = extract_youtube_video_id(url)
    class _PinnedNoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            method = str(args[0] if args else kwargs.pop("method", "GET")).upper()
            target = str(args[1] if len(args) > 1 else kwargs.pop("url", ""))
            params = kwargs.pop("params", None)
            if params:
                from urllib.parse import urlencode

                separator = "&" if "?" in target else "?"
                target += separator + urlencode(params, doseq=True)
            headers = {str(key): str(value) for key, value in (kwargs.pop("headers", {}) or {}).items()}
            if self.cookies:
                headers.setdefault(
                    "Cookie",
                    "; ".join(f"{key}={value}" for key, value in self.cookies.get_dict().items()),
                )
            body = kwargs.pop("data", None)
            json_body = kwargs.pop("json", None)
            if json_body is not None:
                body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            if isinstance(body, str):
                body = body.encode("utf-8")
            timeout = kwargs.pop("timeout", 15.0)
            if isinstance(timeout, tuple):
                timeout = max(timeout)
            if authorizations:
                parsed_target = urlparse(target)
                try:
                    target_origin = (
                        parsed_target.scheme.lower(),
                        canonicalize_host(parsed_target.hostname or ""),
                        parsed_target.port
                        or (443 if parsed_target.scheme.lower() == "https" else 80),
                    )
                except ValueError as exc:
                    raise ToolError(
                        '{"code":"SECURITY_OUTBOUND_DENIED",'
                        '"reason":"invalid_nested_endpoint"}'
                    ) from exc
                authorization = next(
                    (
                        item
                        for item in authorizations
                        if getattr(item, "origin", None) == target_origin
                    ),
                    None,
                )
                if authorization is None:
                    raise ToolError(
                        '{"code":"SECURITY_OUTBOUND_DENIED",'
                        '"reason":"authorization_mismatch"}'
                    )
                plan = authorization.plan(target, method=method)
                response = fetch_authorized_url(
                    plan,
                    body=body,
                    headers=headers,
                    timeout=float(timeout),
                    max_bytes=10_000_000,
                )
            else:
                response = fetch_public_url(
                    target,
                    method=method,
                    body=body,
                    headers=headers,
                    timeout=float(timeout),
                    max_bytes=10_000_000,
                )
            result = requests.Response()
            result.status_code = response.status
            result.url = response.final_url
            result.headers = requests.structures.CaseInsensitiveDict(response.headers)
            result._content = response.body
            result.encoding = response.charset
            result.request = requests.Request(method, target, headers=headers, data=body).prepare()
            return result

    session = _PinnedNoRedirectSession()
    try:
        transcript = YouTubeTranscriptApi(http_client=session).fetch(video_id)
    finally:
        session.close()
    lines: list[str] = [f"# YouTube Transcript: {video_id}", ""]
    for snippet in transcript:
        text = str(getattr(snippet, "text", "")).strip()
        if not text:
            continue
        if timestamps:
            seconds = int(float(getattr(snippet, "start", 0.0)))
            hours, remainder = divmod(seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            stamp = f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
            lines.append(f"- [{stamp}] {text}")
        else:
            lines.append(text)
    return "\n".join(lines).strip(), video_id
