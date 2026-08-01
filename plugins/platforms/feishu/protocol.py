"""飞书消息编解码：入站全类型解析 + 出站渲染。

用于 gateway/platforms/feishu.py 的消息归一化与发送层。纯函数、不依赖 lark
对象的具体类型(getattr/dict 双兼容)，便于单测。

入站类型：text / post(富文本) / image / file / audio / media(视频) /
merge_forward / share_chat / interactive(card)。
出站：文件路径检测、markdown→飞书 post 渲染、长文本分块。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

# 入站消息类型
MSG_TEXT = "text"
MSG_POST = "post"
MSG_IMAGE = "image"
MSG_FILE = "file"
MSG_AUDIO = "audio"
MSG_MEDIA = "media"          # 视频
MSG_MERGE_FORWARD = "merge_forward"
MSG_SHARE_CHAT = "share_chat"
MSG_INTERACTIVE = "interactive"
MSG_CARD = "card"

_IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}

# 取文本兜底文案（用于 FALLBACK_*）
FALLBACK_FORWARD = "[合并转发消息]"
FALLBACK_SHARE_CHAT = "[分享群名片]"
FALLBACK_INTERACTIVE = "[卡片消息]"


# --------------------------------------------------------------------------- #
# 小工具：getattr / dict 双兼容访问
# --------------------------------------------------------------------------- #
def _get(obj: Any, attr: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(attr, default)
    return getattr(obj, attr, default)


def _loads(content: Any) -> Any:
    """content 通常是 JSON 字符串；解析失败时原样返回。"""
    if isinstance(content, (dict, list)):
        return content
    if not isinstance(content, str):
        return None
    try:
        return json.loads(content)
    except (ValueError, TypeError):
        return content


# --------------------------------------------------------------------------- #
# 文本 / 富文本提取
# --------------------------------------------------------------------------- #
def extract_text(content: Any) -> str:
    """从 text 类型 content 提取纯文本。content 通常是 '{"text":"..."}'。"""
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "").strip()
    if not isinstance(content, str):
        return ""
    parsed = _loads(content)
    if isinstance(parsed, dict):
        return str(parsed.get("text") or parsed.get("content") or "").strip()
    if isinstance(parsed, str):
        return parsed.strip()
    return ""


def _post_rows(parsed: Any) -> list[Any]:
    """从 post content 取出元素行(支持直接 content 或带 locale 包装)。"""
    if not isinstance(parsed, dict):
        return []
    if isinstance(parsed.get("content"), list):
        return parsed["content"]
    # 带 locale 包装：{"zh_cn": {"title","content"}, "en_us": {...}}
    for key in ("zh_cn", "en_us", "ja_jp"):
        loc = parsed.get(key)
        if isinstance(loc, dict) and isinstance(loc.get("content"), list):
            return loc["content"]
    # 任取一个 locale
    for loc in parsed.values():
        if isinstance(loc, dict) and isinstance(loc.get("content"), list):
            return loc["content"]
    return []


def parse_post(content: Any) -> tuple[str, list[str]]:
    """解析富文本 post → (纯文本, 内嵌 image_key 列表)。"""
    parsed = _loads(content)
    rows = _post_rows(parsed)
    title = ""
    if isinstance(parsed, dict):
        title = str(parsed.get("title") or "")
        if not title:
            for key in ("zh_cn", "en_us", "ja_jp"):
                loc = parsed.get(key)
                if isinstance(loc, dict) and loc.get("title"):
                    title = str(loc["title"])
                    break
    lines: list[str] = []
    image_keys: list[str] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        segs: list[str] = []
        for el in row:
            if not isinstance(el, dict):
                continue
            tag = el.get("tag")
            if tag in ("text", "a"):
                segs.append(str(el.get("text") or ""))
                if tag == "a" and el.get("href"):
                    segs.append(f"({el['href']})")
            elif tag == "at":
                name = el.get("user_name") or el.get("name") or el.get("user_id") or "someone"
                segs.append(f"@{name}")
            elif tag == "img" and el.get("image_key"):
                image_keys.append(str(el["image_key"]))
            elif tag == "md":
                segs.append(str(el.get("text") or ""))
        if segs:
            lines.append("".join(segs))
    text = "\n".join([s for s in [title, *lines] if s]).strip()
    return text, image_keys


# --------------------------------------------------------------------------- #
# mentions
# --------------------------------------------------------------------------- #
def parse_mentions(message: Any) -> list[dict[str, Any]]:
    """解析 message.mentions → [{key, open_id, user_id, name, is_all}]。"""
    raw = _get(message, "mentions") or []
    out: list[dict[str, Any]] = []
    for m in raw:
        mid = _get(m, "id")
        open_id = str(_get(mid, "open_id", "") or "")
        user_id = str(_get(mid, "user_id", "") or "")
        union_id = str(_get(mid, "union_id", "") or "")
        name = str(_get(m, "name", "") or "")
        key = str(_get(m, "key", "") or "")
        is_all = key.lower().endswith("all") or name in ("所有人", "all", "everyone")
        out.append({"key": key, "open_id": open_id, "user_id": user_id,
                    "union_id": union_id, "name": name, "is_all": is_all})
    return out


def normalize_text(text: str, mentions: list[dict[str, Any]]) -> str:
    """把文本里的 @_user_N 占位替换成可读的 @姓名。"""
    if not text:
        return ""
    for m in mentions:
        if m.get("key") and m.get("name"):
            text = text.replace(m["key"], f"@{m['name']}")
    return text.strip()


# --------------------------------------------------------------------------- #
# 卡片 / 合并转发 取文本兜底
# --------------------------------------------------------------------------- #
_CARD_TEXT_KEYS = ("text", "content", "title", "label", "value", "name",
                   "summary", "subtitle", "description", "plain_text")


def _walk_card_text(node: Any, acc: list[str], depth: int = 0) -> None:
    if depth > 12 or node is None:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            if k in _CARD_TEXT_KEYS and isinstance(v, str) and v.strip():
                acc.append(v.strip())
            else:
                _walk_card_text(v, acc, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk_card_text(item, acc, depth + 1)


def parse_card_text(content: Any) -> str:
    parsed = _loads(content)
    acc: list[str] = []
    _walk_card_text(parsed, acc)
    # 去重保序
    seen: set[str] = set()
    uniq = [s for s in acc if not (s in seen or seen.add(s))]
    return "\n".join(uniq).strip()


# --------------------------------------------------------------------------- #
# 入站主解析
# --------------------------------------------------------------------------- #
def parse_inbound(message: Any, sender: Any) -> dict[str, Any] | None:
    """把 lark im.message 事件归一成统一结构；无 message/chat_id 时返回 None。

    resources 为待下载资源 [{kind, key, name}]，kind ∈ image|file|audio|media。
    text 可能为空(纯媒体)。调用方据 text+resources 决定是否分发。
    """
    if message is None:
        return None
    chat_id = str(_get(message, "chat_id", "") or "")
    if not chat_id:
        return None
    msg_type = str(_get(message, "message_type", "") or "")
    content = _get(message, "content")
    mentions = parse_mentions(message)

    text = ""
    resources: list[dict[str, Any]] = []

    if msg_type == MSG_TEXT:
        text = normalize_text(extract_text(content), mentions)
    elif msg_type == MSG_POST:
        text, img_keys = parse_post(content)
        text = normalize_text(text, mentions)
        resources += [{"kind": "image", "key": k, "name": ""} for k in img_keys]
    elif msg_type == MSG_IMAGE:
        parsed = _loads(content) or {}
        if isinstance(parsed, dict) and parsed.get("image_key"):
            resources.append({"kind": "image", "key": str(parsed["image_key"]), "name": ""})
    elif msg_type in (MSG_FILE, MSG_AUDIO, MSG_MEDIA):
        parsed = _loads(content) or {}
        if isinstance(parsed, dict) and parsed.get("file_key"):
            resources.append({
                "kind": msg_type, "key": str(parsed["file_key"]),
                "name": str(parsed.get("file_name") or ""),
            })
    elif msg_type == MSG_MERGE_FORWARD:
        text = FALLBACK_FORWARD
    elif msg_type == MSG_SHARE_CHAT:
        text = FALLBACK_SHARE_CHAT
    elif msg_type in (MSG_INTERACTIVE, MSG_CARD):
        text = parse_card_text(content) or FALLBACK_INTERACTIVE
    else:
        # 未知类型：尽力取文本，否则放弃
        text = extract_text(content)

    if not text and not resources:
        return None

    sender_id = _get(sender, "sender_id")
    return {
        "message_id": str(_get(message, "message_id", "") or chat_id),
        "chat_id": chat_id,
        "chat_type": str(_get(message, "chat_type", "") or "p2p"),
        "msg_type": msg_type,
        "text": text,
        "mentions": mentions,
        "resources": resources,
        "sender_open_id": str(_get(sender_id, "open_id", "") or ""),
        "sender_user_id": str(_get(sender_id, "user_id", "") or ""),
        "sender_union_id": str(_get(sender_id, "union_id", "") or ""),
        "sender_type": str(_get(sender, "sender_type", "") or "user"),
        "parent_id": str(_get(message, "parent_id", "") or _get(message, "root_id", "") or ""),
        "thread_id": str(_get(message, "thread_id", "") or ""),
    }


# --------------------------------------------------------------------------- #
# 出站：文本 content / 文件路径检测 / 分块 / markdown→post
# --------------------------------------------------------------------------- #
def reply_content(text: str) -> str:
    """飞书 text 消息 content(JSON 字符串)。"""
    return json.dumps({"text": text}, ensure_ascii=False)


_FILE_SYNTAX_RE = re.compile(r"\[FILE:([^\]]+)\]")
_FILE_EXTS = (
    r"mp4|mov|webm|3gp|avi|mp3|wav|m4a|aac|ogg|amr|jpg|jpeg|png|gif|bmp|webp|"
    r"pdf|docx?|xlsx?|pptx?|zip|rar|txt|md"
)
_UNIX_FILE_RE = re.compile(rf"(/[^\n\s\"'<>]+\.(?:{_FILE_EXTS}))", re.IGNORECASE)
_WIN_FILE_RE = re.compile(rf"([A-Za-z]:\\[^\n\s\"'<>]+\.(?:{_FILE_EXTS}))", re.IGNORECASE)


def extract_file_paths(text: str, *, exists=None, is_recent=None) -> list[str]:
    """从回复文本提取要发送的文件路径([FILE:path] 优先，其次绝对路径)。

    去重并排除 is_recent(刚下载的入站文件)与不存在的路径。注入便于测试。
    """
    exists = exists or (lambda p: os.path.isfile(p))
    is_recent = is_recent or (lambda p: False)
    paths: list[str] = []
    for m in _FILE_SYNTAX_RE.finditer(text or ""):
        p = m.group(1).strip()
        if p:
            paths.append(p)
    for regex in (_UNIX_FILE_RE, _WIN_FILE_RE):
        paths.extend(m.group(1) for m in regex.finditer(text or ""))
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        if is_recent(p) or not exists(p):
            continue
        out.append(p)
    return out


def strip_file_syntax(text: str) -> str:
    return _FILE_SYNTAX_RE.sub("", text or "").strip()


# 出站发文件是被动截取 Agent 回复里的 [FILE:路径]/路径；Agent 本身不知道这个约定，
# 用户表达发送意图时注入能力提示，引导它写出路径而非回答“无法发送文件”。
_SEND_KEYWORDS = ("发我", "发送", "给我", "发给", "发给我", "发一下", "发个", "分享", "传给")
_SEARCH_KEYWORDS = ("查找", "搜索", "打开", "查看", "找一下", "下载")
_NEGATIVE_KEYWORDS = ("不要", "别", "不用", "勿", "停止", "取消")


def detect_send_intent(original_user_text: str) -> bool:
    """用户原始消息是否表达「发送/获取文件」意图（否定句短路为 False）。"""
    msg = original_user_text or ""
    if any(k in msg for k in _NEGATIVE_KEYWORDS):
        return False
    return any(k in msg for k in _SEND_KEYWORDS) or any(k in msg for k in _SEARCH_KEYWORDS)


def is_image_file(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower().lstrip(".") in _IMAGE_EXTS


def chunk_text(text: str, limit: int) -> list[str]:
    """按 limit 切片，尽量在换行处断开，避免割裂单词/句子。"""
    if limit <= 0 or len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    buf = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:           # 单行超长，硬切
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(buf) + len(line) > limit:
            chunks.append(buf)
            buf = line
        else:
            buf += line
    if buf:
        chunks.append(buf)
    return [c.strip("\n") for c in chunks if c.strip()]


_MD_HINT_RE = re.compile(r"(^|\n)\s{0,3}(#{1,6}\s|[-*]\s|\d+\.\s|>\s|```)|[*_`]{1,3}\S|\|.*\|")


def looks_like_markdown(text: str) -> bool:
    """粗判文本是否含 markdown 结构(决定是否尝试富文本 post 渲染)。"""
    return bool(_MD_HINT_RE.search(text or ""))


def render_post_content(text: str, title: str = "") -> str:
    """markdown 文本 → 飞书 post content(JSON)。用 md 元素，失败由调用方回退 text。"""
    payload = {"zh_cn": {"title": title or "", "content": [[{"tag": "md", "text": text}]]}}
    return json.dumps(payload, ensure_ascii=False)
