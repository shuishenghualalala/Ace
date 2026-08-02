"""微信个人号 iLink 协议客户端。

封装腾讯 iLink Bot API 的纯协议能力，供 adapter 与扫码登录使用：

- 长轮询收消息（getupdates，无需公网 webhook）
- 文本发送（sendmessage，含 context_token 会话续传）
- 输入状态（getconfig 取 typing ticket + sendtyping）
- 媒体收发：AES-128-ECB 加密 CDN（getuploadurl → 上传密文 → sendmessage；下载 + 解密）
- 账号/上下文 token/长轮询游标 的磁盘持久化（落到 crew home）
- 入站消息解析（文本抽取、会话类型推断）
- 出站文本排版（markdown 规整 + 微信复制友好换行 + 分块）
- 扫码登录（qr_login）

本模块不依赖 Ace 网关内部结构，全部以显式参数传 home 目录与设置。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import secrets
import struct
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000
# 启动握手探测：短超时验证凭证可用，避免 gateway 连通等待被首轮长轮询拖超时。
CONNECT_PROBE_TIMEOUT_MS = 5_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2  # iLink 频率限制 —— 退避重试（排除 "unknown error" 的过期会话信号）

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")

WEIXIN_COPY_LINE_WIDTH = 120

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # 可选依赖缺失时能力降级
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
except ImportError:  # 可选依赖缺失时能力降级
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False


def check_weixin_requirements() -> bool:
    """运行依赖是否就绪：aiohttp + cryptography。"""
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


def _make_ssl_connector() -> aiohttp.TCPConnector | None:
    """返回带 certifi CA 包的连接器；certifi 缺失则返回 None 走 aiohttp 默认。

    iLink 服务器在部分系统 CA 存储（如 macOS Apple Silicon 的 Homebrew OpenSSL）下
    校验失败，certifi 的 Mozilla CA 包可保证握手成功。
    """
    try:
        import ssl

        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _safe_id(value: str | None, keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    return raw if len(raw) <= keep else raw[:keep]


def _json_dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# 加密（AES-128-ECB + PKCS7）
# --------------------------------------------------------------------------- #
def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: str | None, body: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


# --------------------------------------------------------------------------- #
# HTTP 调用（aiohttp + asyncio.wait_for，避免在 cron/线程桥下报
# "Timeout context manager should be used inside a task"）
# --------------------------------------------------------------------------- #
async def api_post(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    payload: dict[str, Any],
    token: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"

    async def _do() -> dict[str, Any]:
        async with session.post(url, data=body, headers=_headers(token, body)) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def api_get(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }

    async def _do() -> dict[str, Any]:
        async with session.get(url, headers=headers) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def get_updates(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> dict[str, Any]:
    try:
        return await api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except TimeoutError:
        # 长轮询超时属正常：空返回继续拉
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def send_message(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: str | None,
    client_id: str,
) -> dict[str, Any]:
    """发送文本消息。返回原始响应 dict（含 errcode 供调用方判断会话过期/限流）。"""
    if not text or not text.strip():
        raise ValueError("send_message: text must not be empty")
    message: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def send_media_item(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to: str,
    media_item: dict[str, Any],
    context_token: str | None,
    client_id: str,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [media_item],
    }
    if context_token:
        message["context_token"] = context_token
    return await api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def send_typing(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int,
) -> None:
    await api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_TYPING,
        payload={
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        },
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def get_config(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    user_id: str,
    context_token: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_CONFIG,
        payload=payload,
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def get_upload_url(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    media_type: int,
    filekey: str,
    rawsize: int,
    rawfilemd5: str,
    filesize: int,
    aeskey_hex: str,
) -> dict[str, Any]:
    return await api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aeskey_hex,
        },
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def upload_ciphertext(
    session: aiohttp.ClientSession,
    *,
    ciphertext: bytes,
    upload_url: str,
) -> str:
    """上传加密媒体到 CDN，返回响应头的 x-encrypted-param。"""
    async def _do_upload() -> str:
        async with session.post(
            upload_url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}
        ) as response:
            if response.status == 200:
                encrypted_param = response.headers.get("x-encrypted-param")
                if encrypted_param:
                    await response.read()
                    return encrypted_param
                raw = await response.text()
                raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
            raw = await response.text()
            raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")

    return await asyncio.wait_for(_do_upload(), timeout=120)


async def download_bytes(
    session: aiohttp.ClientSession,
    *,
    url: str,
    timeout_seconds: float = 60.0,
) -> bytes:
    async def _do_download() -> bytes:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()

    return await asyncio.wait_for(_do_download(), timeout=timeout_seconds)


# --------------------------------------------------------------------------- #
# CDN 下载 + 解密
# --------------------------------------------------------------------------- #
_WEIXIN_CDN_ALLOWLIST = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


def _assert_weixin_cdn_url(url: str) -> None:
    """拒绝指向已知微信 CDN 之外的 URL（防 SSRF）。"""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
    except Exception as exc:
        raise ValueError(f"Unparseable media URL: {url!r}") from exc

    if scheme not in {"http", "https"}:
        raise ValueError(f"Media URL has disallowed scheme {scheme!r}; only http/https are permitted.")
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise ValueError(
            f"Media URL host {host!r} is not in the WeChat CDN allowlist. "
            "Refusing to fetch to prevent SSRF."
        )


def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _media_reference(item: dict[str, Any], key: str) -> dict[str, Any]:
    return (item.get(key) or {}).get("media") or {}


async def download_and_decrypt_media(
    session: aiohttp.ClientSession,
    *,
    cdn_base_url: str,
    encrypted_query_param: str | None,
    aes_key_b64: str | None,
    full_url: str | None,
    timeout_seconds: float,
) -> bytes:
    if encrypted_query_param:
        raw = await download_bytes(
            session,
            url=_cdn_download_url(cdn_base_url, encrypted_query_param),
            timeout_seconds=timeout_seconds,
        )
    elif full_url:
        _assert_weixin_cdn_url(full_url)
        raw = await download_bytes(session, url=full_url, timeout_seconds=timeout_seconds)
    else:
        raise RuntimeError("media item had neither encrypt_query_param nor full_url")
    if aes_key_b64:
        raw = aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
    return raw


def _mime_from_filename(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


# --------------------------------------------------------------------------- #
# 入站解析
# --------------------------------------------------------------------------- #
def extract_text(item_list: list[dict[str, Any]]) -> str:
    """从 item_list 抽取文本；媒体引用（图片/文件等）以 [引用媒体: 标题] 标注。"""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: list[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


def guess_chat_type(message: dict[str, Any], account_id: str) -> tuple[str, str]:
    """推断会话类型：群聊返回 ("group", chat_id)，私聊返回 ("dm", from_user_id)。"""
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1)
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


# --------------------------------------------------------------------------- #
# 磁盘持久化（账号 / context token / 长轮询游标 / 媒体文件）
# --------------------------------------------------------------------------- #
def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def save_account(accounts_dir: Path, *, account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    """持久化扫码登录得到的账号凭证。"""
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = accounts_dir / f"{account_id}.json"
    _atomic_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_account(accounts_dir: Path, account_id: str) -> dict[str, Any] | None:
    """读取已持久化的账号凭证；缺失或损坏返回 None。"""
    path = accounts_dir / f"{account_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 损坏/缺失按未登录处理
        return None


class ContextTokenStore:
    """磁盘缓存 context_token，键为 account_id:user_id（会话续传必需）。"""

    def __init__(self, accounts_dir: Path):
        self._root = accounts_dir
        self._cache: dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 缓存损坏降级为空
            logger.warning("weixin: 恢复 context tokens 失败 %s: %s", _safe_id(account_id), exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            logger.info("weixin: 已恢复 %d 个 context token（account=%s）", restored, _safe_id(account_id))

    def get(self, account_id: str, user_id: str) -> str | None:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def drop(self, account_id: str, user_id: str) -> None:
        self._cache.pop(self._key(account_id, user_id), None)
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix):]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            _atomic_write(self._path(account_id), payload)
        except Exception as exc:  # noqa: BLE001 - 磁盘失败降级为内存态
            logger.warning("weixin: 持久化 context tokens 失败 %s: %s", _safe_id(account_id), exc)


class TypingTicketCache:
    """getconfig 返回的 typing ticket 短时缓存。"""

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, user_id: str) -> str | None:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


def load_sync_buf(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
    except Exception:  # noqa: BLE001 - 游标损坏从空开始拉
        return ""


def save_sync_buf(path: Path, sync_buf: str) -> None:
    _atomic_write(path, {"get_updates_buf": sync_buf})


def cache_media_bytes(data: bytes, suffix: str, files_dir: Path) -> Path:
    """把媒体字节落盘到 files_dir，返回绝对路径。"""
    files_dir.mkdir(parents=True, exist_ok=True)
    name = f"wx_{time.strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}{suffix}"
    path = files_dir / name
    path.write_bytes(data)
    return path


# --------------------------------------------------------------------------- #
# 出站文本排版（markdown 规整 + 微信复制友好 + 分块）
# --------------------------------------------------------------------------- #
def _normalize_markdown_blocks(content: str) -> str:
    lines = content.splitlines()
    result: list[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue

        if in_code_block:
            result.append(line)
            continue

        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue

        blank_run = 0
        result.append(line)

    return "\n".join(result).strip()


def _wrap_copy_friendly_lines(content: str) -> str:
    """把过长的展示行按固定宽度换行，避免微信客户端长行难复制。"""
    if not content:
        return content

    wrapped: list[str] = []
    in_code_block = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue

        if (
            in_code_block
            or len(line) <= WEIXIN_COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
            continue

        wrapped_lines = textwrap.wrap(
            line,
            width=WEIXIN_COPY_LINE_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=True,
        )
        wrapped.extend(wrapped_lines or [line])

    return "\n".join(wrapped).strip()


def _split_markdown_blocks(content: str) -> list[str]:
    if not content:
        return []

    blocks: list[str] = []
    lines = content.splitlines()
    current: list[str] = []
    in_code_block = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue

        if in_code_block:
            current.append(line)
            continue

        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _split_delivery_units(content: str) -> list[str]:
    """把格式化内容拆成聊天友好的投递单元（代码块整体保留，缩进续行挂到上一行）。"""
    units: list[str] = []

    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue

        current: list[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if not line.strip():
                if current:
                    units.append("\n".join(current).strip())
                    current = []
                continue

            is_continuation = bool(current) and raw_line.startswith((" ", "\t"))
            if is_continuation:
                current.append(line)
                continue

            if current:
                units.append("\n".join(current).strip())
            current = [line]

        if current:
            units.append("\n".join(current).strip())

    return [unit for unit in units if unit]


def _looks_like_chatty_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 48:
        return False
    if line.startswith((" ", "\t")):
        return False
    if stripped.startswith((">", "-", "*", "【", "#", "|")):
        return False
    if _TABLE_RULE_RE.match(stripped):
        return False
    return not (re.match(r"^\*\*[^*]+\*\*$", stripped) or re.match(r"^\d+\.\s", stripped))


def _looks_like_heading_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _HEADER_RE.match(stripped):
        return True
    return len(stripped) <= 24 and stripped.endswith((":", "："))


def _should_split_short_chat_block(block: str) -> bool:
    """短对话块拆成独立气泡，提升聊天观感。"""
    lines = [line for line in block.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6:
        return False
    if _looks_like_heading_line(lines[0]):
        return False
    return all(_looks_like_chatty_line(line) for line in lines)


def _pack_markdown_blocks(content: str, max_length: int) -> list[str]:
    if len(content) <= max_length:
        return [content]

    packed: list[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        # 超长块按字符硬切
        packed.extend(block[i:i + max_length] for i in range(0, len(block), max_length))
    if current:
        packed.append(current)
    return packed


def split_text_for_delivery(content: str, max_length: int, split_per_line: bool = False) -> list[str]:
    """把内容拆成连续的微信消息。

    compact（默认）：不超限时保持单条；仅在超出平台上限时回退到块感知打包。
    per_line：顶层换行拆成独立消息；超长单元仍块感知打包。
    """
    if not content:
        return []
    if split_per_line:
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks: list[str] = []
        for unit in _split_delivery_units(content):
            if len(unit) <= max_length:
                chunks.append(unit)
                continue
            chunks.extend(_pack_markdown_blocks(unit, max_length))
        return [c for c in chunks if c] or [content]

    if len(content) <= max_length:
        return (
            [u for u in _split_delivery_units(content) if u]
            if _should_split_short_chat_block(content)
            else [content]
        )
    return _pack_markdown_blocks(content, max_length) or [content]


def format_message(content: str | None) -> str:
    """出站文本规整：压缩多余空行 + 微信复制友好换行。"""
    if content is None:
        return ""
    return _wrap_copy_friendly_lines(_normalize_markdown_blocks(content))


# --------------------------------------------------------------------------- #
# 出站媒体项构造
# --------------------------------------------------------------------------- #
def build_outbound_media_item(
    path: str,
    *,
    encrypted_query_param: str,
    aes_key_for_api: str,
    ciphertext_size: int,
    plaintext_size: int,
    rawfilemd5: str,
    force_file_attachment: bool = False,
) -> tuple[int, dict[str, Any]]:
    """按文件类型构造 iLink media item，返回 (media_type, item)。"""
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    filename = Path(path).name
    media_ref = {
        "encrypt_query_param": encrypted_query_param,
        "aes_key": aes_key_for_api,
        "encrypt_type": 1,
    }
    if mime.startswith("image/"):
        return MEDIA_IMAGE, {
            "type": ITEM_IMAGE,
            "image_item": {"media": media_ref, "mid_size": ciphertext_size},
        }
    if mime.startswith("video/"):
        return MEDIA_VIDEO, {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": media_ref,
                "video_size": ciphertext_size,
                "play_length": 0,
                "video_md5": rawfilemd5,
            },
        }
    if path.endswith(".silk") and not force_file_attachment:
        return MEDIA_VOICE, {
            "type": ITEM_VOICE,
            "voice_item": {
                "media": media_ref,
                "encode_type": 6,
                "bits_per_sample": 16,
                "sample_rate": 24000,
                "playtime": 0,
            },
        }
    return MEDIA_FILE, {
        "type": ITEM_FILE,
        "file_item": {
            "media": media_ref,
            "file_name": filename,
            "len": str(plaintext_size),
        },
    }


# --------------------------------------------------------------------------- #
# 扫码登录
# --------------------------------------------------------------------------- #
async def fetch_qr_code(
    bot_type: str = "3",
) -> tuple[str, str, str] | None:
    """请求 iLink 登录二维码。返回 (qrcode_value, qr_scan_data, qrcode_url)，失败返回 None。

    qrcode_value 是轮询用的 hex token；qr_scan_data 是微信实际要扫的 liteapp URL；
    qrcode_url 用于无二维码渲染时让用户直接打开。
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")
    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一失败返回
            logger.error("weixin: 获取二维码失败: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: 二维码响应缺少 qrcode")
            return None
        qr_scan_data = qrcode_url if qrcode_url else qrcode_value
        return qrcode_value, qr_scan_data, qrcode_url


async def poll_qr_status(
    qrcode_value: str,
    base_url: str = ILINK_BASE_URL,
) -> dict[str, Any] | None:
    """轮询一次二维码状态。返回 iLink status dict；网络/超时/异常返回 None。"""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")
    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            return await api_get(
                session,
                base_url=base_url,
                endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except TimeoutError:
            return None
        except Exception as exc:  # noqa: BLE001 - 单次轮询失败返回 pending
            logger.warning("weixin: 二维码轮询异常: %s", exc)
            return None


def render_qr_svg(data: str) -> str | None:
    """把扫描内容渲染为 QR SVG 字符串（不依赖 pillow）。缺 qrcode 包返回 None。"""
    try:
        import io

        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(border=1)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception as exc:  # noqa: BLE001 - 渲染失败不影响主流程
        logger.warning("weixin: 二维码 SVG 渲染失败: %s", exc)
        return None


async def qr_login(
    accounts_dir: Path,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> dict[str, str] | None:
    """交互式 iLink 扫码登录。成功返回 {account_id, token, base_url, user_id}，失败/超时返回 None。"""
    fetched = await fetch_qr_code(bot_type=bot_type)
    if fetched is None:
        return None
    qrcode_value, qr_scan_data, qrcode_url = fetched

    print("\n请使用微信扫描以下二维码：")
    if qrcode_url:
        print(qrcode_url)
    try:
        import qrcode

        qr = qrcode.QRCode()
        qr.add_data(qr_scan_data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as _qr_exc:  # noqa: BLE001 - 终端渲染失败不影响流程
        print(f"（终端二维码渲染失败: {_qr_exc}，请直接打开上面的二维码链接）")

    deadline = time.monotonic() + timeout_seconds
    current_base_url = ILINK_BASE_URL
    refresh_count = 0

    while time.monotonic() < deadline:
        status_resp = await poll_qr_status(qrcode_value, base_url=current_base_url)
        if status_resp is None:
            await asyncio.sleep(1)
            continue

        status = str(status_resp.get("status") or "wait")
        if status == "wait":
            print(".", end="", flush=True)
        elif status == "scaned":
            print("\n已扫码，请在微信里确认...")
        elif status == "scaned_but_redirect":
            redirect_host = str(status_resp.get("redirect_host") or "")
            if redirect_host:
                current_base_url = f"https://{redirect_host}"
        elif status == "expired":
            refresh_count += 1
            if refresh_count > 3:
                print("\n二维码多次过期，请重新执行登录。")
                return None
            print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
            refreshed = await fetch_qr_code(bot_type=bot_type)
            if refreshed is None:
                return None
            qrcode_value, qr_scan_data, qrcode_url = refreshed
            if qrcode_url:
                print(qrcode_url)
            try:
                import qrcode as _qrcode

                qr = _qrcode.QRCode()
                qr.add_data(qr_scan_data)
                qr.make(fit=True)
                qr.print_ascii(invert=True)
            except Exception:  # noqa: BLE001 - 终端二维码渲染失败不影响流程
                logger.debug("weixin: 终端二维码渲染失败")
        elif status == "confirmed":
            account_id = str(status_resp.get("ilink_bot_id") or "")
            token = str(status_resp.get("bot_token") or "")
            base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
            user_id = str(status_resp.get("ilink_user_id") or "")
            if not account_id or not token:
                logger.error("weixin: 扫码确认但凭证不完整")
                return None
            save_account(
                accounts_dir,
                account_id=account_id,
                token=token,
                base_url=base_url,
                user_id=user_id,
            )
            print(f"\n微信连接成功，account_id={account_id}")
            return {
                "account_id": account_id,
                "token": token,
                "base_url": base_url,
                "user_id": user_id,
            }
        await asyncio.sleep(1)

    print("\n微信登录超时。")
    return None


# 会话过期/限流错误判定
def is_stale_session_ret(ret: int | None, errcode: int | None, errmsg: str | None) -> bool:
    """ret=-2/errcode=-2 且 errmsg 为 'unknown error' 时是过期会话信号而非真限流。"""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"
