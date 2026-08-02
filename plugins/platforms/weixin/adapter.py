"""微信（个人号 iLink）渠道适配器。

长轮询收消息（无需公网 webhook）：start() 起 poll 任务拉取 getupdates → 解析 →
访问控制 → 去重 → 转 Envelope 交 Crew 内核 → 回包经 sendmessage 发回（含媒体上传）。

功能：文本/图片/文件/语音 收发、访问控制（dm_policy 三档 + group_policy 三档 +
白名单）、context_token 会话续传、输入状态（typing）、长文本分块、出站文件
（截取回复里的 [FILE:绝对路径] 上传）、长轮询游标断线续拉、去重持久化、按 chat
串行、cron 投递（send_to_target）。aiohttp + cryptography 为可选依赖，缺失时 start
抛清晰错误。iLink 为机器人身份，普通微信群事件大多不推送，仅私聊稳定可用。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from crew.core.envelope import Envelope
from crew.core.interfaces import Channel, MessageHandler
from crew.gateway.platform_registry import PlatformConfig  # noqa: F401 - 类型参考/对外
from crew.gateway.response_filters import apply_text_filters
from crew.gateway.session_context import SessionSource, build_session_key
from crew.state.logging import get_logger

from . import ilink
from .config import WeixinSettings, decide_access

log = get_logger("platform.weixin")

_RECENT_DOWNLOAD_TTL_S = 300.0
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}

# 用户要发文件时提示 Agent 它具备发文件/图片能力（回复里写 [FILE:绝对路径] 即可）。
_SEND_FILE_HINT = (
    "[系统能力] 你就运行在本机上，能用 read/terminal 等工具直接读取本地绝对路径的文件，"
    "不要声称“无法访问本地文件”或“这是用户电脑上的文件”。要把文件/图片发给用户时，"
    "在回复里用 [FILE:/文件绝对路径] 写出该文件的绝对路径，系统会自动上传并发给用户。"
    "用户要你发文件就直接这样给出路径，不要回答“无法发送文件”或“无法访问该路径”。"
)

# 回复里识别出来的待发文件路径：[FILE:/abs/path] 或裸绝对路径
_FILE_TAG_RE = re.compile(r"\[FILE:([^\[\]]+)\]")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./\\])(/(?:[^ \t\n\r\]]|\\ )+)(?![A-Za-z0-9_])")


def detect_send_intent(text: str) -> bool:
    """用户表达发送意图时才注入「可发文件」能力提示（否定句不注入）。"""
    if not text:
        return False
    keywords = ("发文件", "发图片", "发图", "发一下", "发给我", "发送文件", "发送图片", "传文件", "上传")
    if any(k in text for k in keywords):
        neg = ("不要", "别", "不", "无需", "不用")
        return not any(n in text for n in neg)
    return False


def extract_file_paths(text: str, *, exists=lambda _: True, is_recent=lambda _: False) -> list[str]:
    """从回复里提取待发文件路径（[FILE:...] 或裸绝对路径），排除刚下载的入站文件。"""
    paths: list[str] = []
    for tag in _FILE_TAG_RE.findall(text or ""):
        candidate = tag.strip().strip("'\"")
        if candidate and Path(candidate).is_absolute() and exists(candidate) and not is_recent(candidate):
            paths.append(candidate)
    if not paths:
        for raw in _ABSOLUTE_PATH_RE.findall(text or ""):
            candidate = raw.rstrip(".,;，。；")
            if candidate.endswith((".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                                   ".txt", ".md", ".csv", ".json", ".zip", ".tar", ".gz",
                                   ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
                                   ".mp3", ".wav", ".ogg", ".m4a", ".mp4", ".mov", ".silk")) \
                    and exists(candidate) and not is_recent(candidate):
                paths.append(candidate)
    seen: set[str] = set()
    result: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def strip_file_syntax(text: str) -> str:
    return _FILE_TAG_RE.sub("", text or "").strip()


def looks_like_markdown(text: str) -> bool:
    if not text:
        return False
    return any(text.lstrip().startswith(p) for p in ("# ", "## ", "### ", "- ", "* ", "**", "```", "> ")) \
        or bool(re.search(r"\*\*[^*]+\*\*", text))


def chunk_plain_text(text: str, limit: int) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    for i in range(0, len(text), limit):
        chunks.append(text[i:i + limit])
    return chunks


class WeixinChannel(Channel):
    name = "weixin"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.settings = WeixinSettings.from_extra(
            getattr(config, "extra", {}) or {},
            use_env=getattr(config, "env_enabled", True),
        )
        self._handler: MessageHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._app: Any = None
        self._stopped = False
        self._poll_task: asyncio.Task[None] | None = None
        self._poll_session: Any = None
        self._send_session: Any = None
        self._account_id = self.settings.account_id
        self._token = self.settings.token or str(getattr(config, "token", None) or "").strip()
        self._base_url = self.settings.base_url
        self._cdn_base_url = self.settings.cdn_base_url
        self._token_store = ilink.ContextTokenStore(self.settings.accounts_dir())
        self._typing_cache = ilink.TypingTicketCache()
        self._seen: dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._dedup_revision = 0
        self._dedup_persisted_revision = 0
        self._dedup_persist_task: asyncio.Task[None] | None = None
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._recent_downloads: dict[str, float] = {}
        self._connected = False
        self._last_error = ""
        self._initial_updates: dict[str, Any] | None = None

        # account_id 命中已持久化账号但未显式给 token 时，从账号文件补全凭证。
        if self._account_id and not self._token:
            persisted = ilink.load_account(self.settings.accounts_dir(), self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = str(persisted.get("base_url") or self._base_url).strip().rstrip("/")
                log.info("weixin: 已从账号文件加载 token（account=%s）", ilink._safe_id(self._account_id))

    def bind_app(self, app: Any) -> None:
        """绑定 CrewApp，用 Active Owner 与退出协调状态对入站消息做最终门禁。"""
        self._app = app

    # -- 生命周期 ----------------------------------------------------------- #
    async def start(self, handler: MessageHandler) -> None:
        if not self._owner_may_connect():
            raise RuntimeError("Weixin 渠道绑定到其他账号，拒绝建立旧账号连接")
        if not ilink.check_weixin_requirements():
            raise RuntimeError("缺少 aiohttp/cryptography 依赖:安装 pip install .[weixin]")
        if not self._account_id:
            raise RuntimeError("Weixin 未完成配置:需要 WEIXIN_ACCOUNT_ID")
        if not self._token:
            raise RuntimeError("Weixin 未完成配置:需要 WEIXIN_TOKEN（或用 crew weixin-login 扫码登录）")

        self._handler = handler
        self._loop = asyncio.get_running_loop()
        self._stopped = False

        connector = ilink._make_ssl_connector()
        self._poll_session = __import__("aiohttp").ClientSession(trust_env=True, connector=connector)
        # aiohttp 内置 ClientTimeout 置空，超时统一由 ilink 的 asyncio.wait_for 管理。
        self._send_session = __import__("aiohttp").ClientSession(
            trust_env=True, connector=connector,
            timeout=__import__("aiohttp").ClientTimeout(total=None),
        )
        self._token_store.restore(self._account_id)
        self._load_dedup()
        await self._probe_connection()
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        for warning in self.settings.collect_warnings():
            log.warning("Weixin 配置提示: %s", warning)
        log.info("Weixin 通道已启动 account=%s base=%s",
                 ilink._safe_id(self._account_id), self._base_url)

    async def _probe_connection(self) -> None:
        """启动握手：短超时 getUpdates 验证凭证可用，成功即标记 connected。

        首轮长轮询最长 35s 才返回，不探测的话 gateway 的连通等待（15s）会误判超时；
        探测超时被 get_updates 视为正常空结果，只有真实错误（会话过期等）才会拦截启动。
        """
        sync_buf = ilink.load_sync_buf(self.settings.sync_buf_path())
        try:
            response = await ilink.get_updates(
                self._poll_session,
                base_url=self._base_url,
                token=self._token,
                sync_buf=sync_buf,
                timeout_ms=ilink.CONNECT_PROBE_TIMEOUT_MS,
            )
        except Exception as exc:
            raise RuntimeError(f"微信连接失败：{exc}") from exc
        ret = response.get("ret", 0)
        errcode = response.get("errcode", 0)
        if ret not in {0, None} or errcode not in {0, None}:
            if (
                ret == ilink.SESSION_EXPIRED_ERRCODE
                or errcode == ilink.SESSION_EXPIRED_ERRCODE
                or ilink.is_stale_session_ret(ret, errcode, response.get("errmsg"))
            ):
                raise RuntimeError("微信会话已过期，请重新扫码登录")
            raise RuntimeError(
                f"微信连接失败 ret={ret} errcode={errcode} errmsg={response.get('errmsg', '')}"
            )
        self._connected = True
        self._last_error = ""
        # 探测响应交给首轮轮询消费，避免丢弃 sync_buf 与消息。
        self._initial_updates = response

    async def stop(self) -> None:
        """停止逻辑入口：取消轮询任务并关闭会话。物理断连仍由 Gateway 受控重启完成。"""
        self._stopped = True
        self._connected = False
        self._handler = None
        poll_task = self._poll_task
        if poll_task and not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None
        for session in (self._poll_session, self._send_session):
            if session is not None and not session.closed:
                await session.close()
        self._poll_session = None
        self._send_session = None
        persist_task = self._dedup_persist_task
        if persist_task is not None:
            await asyncio.gather(persist_task, return_exceptions=True)
        if self._dedup_persisted_revision < self._dedup_revision:
            await asyncio.to_thread(self._persist_dedup)
            self._dedup_persisted_revision = self._dedup_revision

    def status_detail(self) -> dict[str, Any]:
        return {
            "transport": "ilink-long-poll",
            "account_id": ilink._safe_id(self._account_id, keep=12),
            "base_url": self._base_url,
            "dm_policy": self.settings.dm_policy,
            "group_policy": self.settings.group_policy,
            "running": bool(self._poll_task and not self._poll_task.done() and not self._stopped),
            "connected": self._connected,
            "last_error": self._last_error,
        }

    # -- Owner 门禁（与其它渠道一致）--------------------------------------- #
    def _owner_may_connect(self) -> bool:
        if self._app is None:
            return True
        try:
            lease = self._app.active_owner.current()
            if lease is None:
                return False
            coordinator = getattr(self._app, "logout_coordinator", None)
            if coordinator is not None and coordinator.is_draining():
                return False
            bindings = getattr(self._app, "channel_bindings", None)
            bound_owner = str(bindings.get_binding(self.name) or "") if bindings is not None else ""
            return not bound_owner or bound_owner == str(lease.owner_account_id or "")
        except Exception as exc:  # noqa: BLE001 - 身份事实源异常必须 fail closed
            log.warning("Weixin 连接 Owner 门禁读取失败: %s", exc)
            return False

    def _owner_available(self) -> bool:
        if self._app is None:
            return True
        try:
            lease = self._app.active_owner.current()
            if lease is None:
                return False
            coordinator = getattr(self._app, "logout_coordinator", None)
            if coordinator is not None and coordinator.is_draining():
                return False
            bindings = getattr(self._app, "channel_bindings", None)
            return bindings is not None and str(bindings.get_binding(self.name) or "") == str(
                lease.owner_account_id or ""
            )
        except Exception as exc:  # noqa: BLE001 - 身份事实源异常必须 fail closed
            log.warning("Weixin Active Owner 门禁读取失败: %s", exc)
            return False

    # -- 长轮询 ------------------------------------------------------------- #
    async def _poll_loop(self) -> None:
        sync_buf = ilink.load_sync_buf(self.settings.sync_buf_path())
        timeout_ms = ilink.LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0
        # 启动握手拿到的首个响应，首轮直接消费，不再重复请求。
        pending = self._initial_updates
        self._initial_updates = None

        while not self._stopped:
            session = self._poll_session
            if session is None:
                await asyncio.sleep(1)
                continue
            try:
                if pending is not None:
                    response = pending
                    pending = None
                else:
                    response = await ilink.get_updates(
                        session,
                        base_url=self._base_url,
                        token=self._token,
                        sync_buf=sync_buf,
                        timeout_ms=timeout_ms,
                    )
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if (
                        ret == ilink.SESSION_EXPIRED_ERRCODE
                        or errcode == ilink.SESSION_EXPIRED_ERRCODE
                        or ilink.is_stale_session_ret(ret, errcode, response.get("errmsg"))
                    ):
                        self._connected = False
                        self._last_error = "微信会话已过期，请重新扫码登录"
                        log.error("weixin: 会话已过期，暂停 10 分钟等待重新登录")
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    self._connected = False
                    self._last_error = (
                        f"getUpdates 失败 ret={ret} errcode={errcode}: {response.get('errmsg', '')}"
                    )
                    log.warning(
                        "weixin: getUpdates 失败 ret=%s errcode=%s errmsg=%s (%d/%d)",
                        ret, errcode, response.get("errmsg", ""),
                        consecutive_failures, ilink.MAX_CONSECUTIVE_FAILURES,
                    )
                    await asyncio.sleep(
                        ilink.BACKOFF_DELAY_SECONDS
                        if consecutive_failures >= ilink.MAX_CONSECUTIVE_FAILURES
                        else ilink.RETRY_DELAY_SECONDS
                    )
                    if consecutive_failures >= ilink.MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                self._connected = True
                self._last_error = ""
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    ilink.save_sync_buf(self.settings.sync_buf_path(), sync_buf)

                for message in response.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001 - 单次轮询失败不能终止通道
                consecutive_failures += 1
                self._connected = False
                self._last_error = str(exc)
                log.error(
                    "weixin: 轮询异常 (%d/%d): %s",
                    consecutive_failures, ilink.MAX_CONSECUTIVE_FAILURES, exc,
                )
                await asyncio.sleep(
                    ilink.BACKOFF_DELAY_SECONDS
                    if consecutive_failures >= ilink.MAX_CONSECUTIVE_FAILURES
                    else ilink.RETRY_DELAY_SECONDS
                )
                if consecutive_failures >= ilink.MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    async def _process_message_safe(self, message: dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("weixin: 入站处理异常 from=%s",
                          ilink._safe_id(message.get("from_user_id")))

    async def _process_message(self, message: dict[str, Any]) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedupe(message_id):
            return

        item_list = message.get("item_list") or []
        text = ilink.extract_text(item_list)
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedupe(content_key):
                log.debug("weixin: 内容去重，跳过 %s 的重复消息", sender_id)
                return

        chat_type, effective_chat_id = ilink.guess_chat_type(message, self._account_id)
        allow, reason = decide_access(
            sender_id=sender_id,
            account_id=self._account_id,
            chat_type=chat_type,
            chat_id=effective_chat_id,
            settings=self.settings,
        )
        if not allow:
            log.debug("weixin: 丢弃消息 id=%s 原因=%s", message_id or "?", reason)
            return

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))

        parsed = {
            "message_id": message_id,
            "sender_id": sender_id,
            "chat_id": effective_chat_id,
            "chat_type": chat_type,
            "text": text,
            "context_token": context_token or None,
            "resources": [],
        }
        for item in item_list:
            await self._collect_media(item, parsed["resources"])
        if not parsed["text"] and not parsed["resources"]:
            return
        log.info("weixin: 入站 from=%s type=%s media=%d",
                 ilink._safe_id(sender_id), chat_type, len(parsed["resources"]))
        await self._handle(parsed)

    async def _collect_media(self, item: dict[str, Any], resources: list[dict[str, Any]]) -> None:
        item_type = item.get("type")
        if item_type == ilink.ITEM_IMAGE:
            await self._download_media(item, "image_item", ".jpg", "image", resources, "image/jpeg")
        elif item_type == ilink.ITEM_VIDEO:
            await self._download_media(item, "video_item", ".mp4", "video", resources, "video/mp4")
        elif item_type == ilink.ITEM_VOICE:
            voice_item = item.get("voice_item") or {}
            if voice_item.get("text"):
                return
            await self._download_media(item, "voice_item", ".silk", "audio", resources, "audio/silk")
        elif item_type == ilink.ITEM_FILE:
            file_item = item.get("file_item") or {}
            filename = str(file_item.get("file_name") or "document.bin")
            suffix = Path(filename).suffix or ".bin"
            name = filename
            await self._download_media(item, "file_item", suffix, "file", resources,
                                      ilink._mime_from_filename(filename), name=name)

    async def _download_media(
        self,
        item: dict[str, Any],
        media_key: str,
        suffix: str,
        kind: str,
        resources: list[dict[str, Any]],
        mime: str,
        *,
        name: str = "",
    ) -> None:
        media = ilink._media_reference(item, media_key)
        timeout = 30.0 if kind == "image" else (120.0 if kind == "video" else 60.0)
        try:
            data = await ilink.download_and_decrypt_media(
                self._poll_session,
                cdn_base_url=self._cdn_base_url,
                encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=media.get("aes_key"),
                full_url=media.get("full_url"),
                timeout_seconds=timeout,
            )
            path = ilink.cache_media_bytes(data, suffix, self.settings.files_dir())
            self._mark_recent_download(str(path))
            resources.append({"name": name or path.name, "path": str(path), "mime": mime})
        except Exception as exc:  # noqa: BLE001 - 单个媒体下载失败降级为纯文本
            log.warning("weixin: %s 下载失败: %s", kind, exc)

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: str | None) -> None:
        if not self._poll_session or not self._token or self._typing_cache.get(user_id):
            return
        try:
            response = await ilink.get_config(
                self._poll_session,
                base_url=self._base_url,
                token=self._token,
                user_id=user_id,
                context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
        except Exception as exc:  # noqa: BLE001
            log.debug("weixin: getConfig 失败 %s: %s", ilink._safe_id(user_id), exc)

    # -- 去重 --------------------------------------------------------------- #
    def _load_dedup(self) -> None:
        path = self.settings.dedup_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                now = time.time()
                self._seen = {k: float(v) for k, v in data.items()
                              if now - float(v) < self.settings.dedup_ttl_s}
        except Exception as exc:  # noqa: BLE001
            log.debug("weixin: 去重载入失败: %s", exc)
            self._seen = {}

    def _persist_dedup(self) -> None:
        path = self.settings.dedup_path()
        try:
            with self._seen_lock:
                items = sorted(self._seen.items(), key=lambda kv: kv[1], reverse=True)[: self.settings.dedup_max]
                self._seen = dict(items)
                payload = json.dumps(self._seen)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("weixin: 去重持久化失败: %s", exc)

    async def _persist_dedup_async(self) -> None:
        while self._dedup_persisted_revision < self._dedup_revision:
            revision = self._dedup_revision
            await asyncio.to_thread(self._persist_dedup)
            self._dedup_persisted_revision = revision

    def _schedule_dedup_persist(self) -> None:
        task = self._dedup_persist_task
        if task is not None and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._persist_dedup()
            self._dedup_persisted_revision = self._dedup_revision
            return
        self._dedup_persist_task = loop.create_task(
            self._persist_dedup_async(), name="weixin-dedup-persist",
        )

    def _dedupe(self, message_id: str) -> bool:
        with self._seen_lock:
            now = time.time()
            for key, ts in list(self._seen.items()):
                if now - ts > self.settings.dedup_ttl_s:
                    self._seen.pop(key, None)
            if message_id in self._seen:
                return True
            self._seen[message_id] = now
            self._dedup_revision += 1
        self._schedule_dedup_persist()
        return False

    # -- 分发 + 回包 -------------------------------------------------------- #
    def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
            if len(self._chat_locks) > 2000:  # 软上限：清理未持有的锁
                for key, existing in list(self._chat_locks.items()):
                    if not existing.locked():
                        self._chat_locks.pop(key, None)
                    if len(self._chat_locks) <= 1000:
                        break
        return lock

    async def _handle(self, parsed: dict[str, Any]) -> None:
        handler = self._handler
        if handler is None or self._stopped or not self._owner_available():
            return
        async with self._chat_lock(parsed["chat_id"]):  # 同一 chat 串行处理
            typing_sent = await self._start_typing(parsed["chat_id"])

            attachments: list[dict[str, Any]] = []
            hints: list[str] = []
            for res in parsed["resources"]:
                attachments.append({"name": res["name"], "path": res["path"]})
                hints.append(f"[附件: {res['name']}, 路径: {res['path']}]")
            text = parsed["text"]
            if hints:
                text = (text + "\n" + "\n".join(hints)).strip() if text else "\n".join(hints)

            extra_params: dict[str, Any] = {}
            if detect_send_intent(parsed["text"]):
                extra_params["channel_system_hint"] = _SEND_FILE_HINT

            envelope = self._build_envelope(parsed, text, attachments, extra_params)
            final_text, error = "", ""
            try:
                async for chunk in handler(envelope):
                    if chunk.kind == "final":
                        final_text = chunk.body.get("text", "")
                    elif chunk.kind == "error":
                        error = chunk.body.get("message", "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("weixin: 分发失败 message_id=%s", parsed["message_id"])
                error = str(exc)
            finally:
                if typing_sent:
                    await self._stop_typing(parsed["chat_id"])
            if self._stopped or not self._owner_available():
                return
            await self._deliver(parsed, final_text, error)

    def _session_key(self, parsed: dict[str, Any]) -> str:
        return build_session_key(SessionSource(
            platform="weixin",
            chat_id=parsed["chat_id"],
            chat_type=parsed["chat_type"],
            user_id=parsed.get("sender_id") or None,
        ))

    def _build_envelope(
        self,
        parsed: dict[str, Any],
        text: str,
        attachments: list[dict[str, Any]],
        extra_params: dict[str, Any] | None = None,
    ) -> Envelope:
        params: dict[str, Any] = {
            "platform_chat_id": parsed["chat_id"],
            "platform_message_id": parsed["message_id"],
            "platform_sender": parsed["sender_id"],
            "platform_uid": parsed["sender_id"],
            "platform_chat_type": parsed["chat_type"],
        }
        if extra_params:
            params.update(extra_params)
        return Envelope.of(
            text,
            session_id=self._session_key(parsed),
            channel="weixin",
            user_id=parsed["sender_id"] or "weixin",
            workspace_id=self.settings.workspace_id,
            attachments=attachments,
            params=params,
        )

    # -- 回包 --------------------------------------------------------------- #
    async def _deliver(self, parsed: dict[str, Any], final_text: str, error: str) -> None:
        reply = (final_text or error).strip()
        if not reply:
            return
        reply = apply_text_filters(reply, {"channel": "weixin"})
        paths = extract_file_paths(reply, exists=os.path.exists, is_recent=self._is_recent_download)
        if paths:
            cleaned = strip_file_syntax(reply)
            if cleaned:
                await self._send_text_chunks(parsed["chat_id"], cleaned, parsed.get("context_token"))
            for path in paths:
                await self._send_file(parsed["chat_id"], path, parsed.get("context_token"))
            return
        await self._send_text_chunks(parsed["chat_id"], reply, parsed.get("context_token"))

    async def _send_text_chunks(self, chat_id: str, content: str, context_token: str | None) -> None:
        chunks = ilink.split_text_for_delivery(content, self.settings.text_chunk_limit, self.settings.split_multiline_messages)
        for idx, chunk in enumerate(chunks):
            if not chunk.strip():
                continue
            await self._send_text_chunk(chat_id=chat_id, chunk=chunk, context_token=context_token)
            if idx < len(chunks) - 1 and self.settings.send_chunk_delay_seconds > 0:
                await asyncio.sleep(self.settings.send_chunk_delay_seconds)

    async def _send_text_chunk(self, *, chat_id: str, chunk: str, context_token: str | None) -> None:
        """发送单条文本，带按块重试与退避。

        会话过期（errcode -14）时自动去掉 context_token 重试一次，保证主动推送仍可用。
        """
        session = self._send_session
        if session is None:
            raise RuntimeError("Weixin 未连接")
        last_error: Exception | None = None
        retried_without_token = False
        for attempt in range(self.settings.send_chunk_retries + 1):
            try:
                resp = await ilink.send_message(
                    session,
                    base_url=self._base_url,
                    token=self._token,
                    to=chat_id,
                    text=chunk,
                    context_token=context_token,
                    client_id=f"ace-weixin-{secrets.token_hex(16)}",
                )
                if resp and isinstance(resp, dict):
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                    if ret not in {0, None} or errcode not in {0, None}:
                        is_session_expired = (
                            ret == ilink.SESSION_EXPIRED_ERRCODE
                            or errcode == ilink.SESSION_EXPIRED_ERRCODE
                            or ilink.is_stale_session_ret(ret, errcode, resp.get("errmsg"))
                        )
                        if is_session_expired and not retried_without_token and context_token:
                            retried_without_token = True
                            context_token = None
                            self._token_store.drop(self._account_id, chat_id)
                            log.warning("weixin: 会话过期 %s，去掉 context_token 重试", ilink._safe_id(chat_id))
                            continue
                        if ret == ilink.RATE_LIMIT_ERRCODE or errcode == ilink.RATE_LIMIT_ERRCODE:
                            last_error = RuntimeError(
                                f"iLink sendmessage rate limited: ret={ret} errcode={errcode}"
                            )
                            if attempt >= self.settings.send_chunk_retries:
                                break
                            await asyncio.sleep(self.settings.send_chunk_retry_delay_seconds * 3)
                            continue
                        raise RuntimeError(
                            f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={resp.get('errmsg') or resp.get('msg') or 'unknown'}"
                        )
                return
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt >= self.settings.send_chunk_retries:
                    break
                wait = self.settings.send_chunk_retry_delay_seconds * (attempt + 1)
                log.warning("weixin: 发送失败 to=%s attempt=%d/%d 稍后重试: %s",
                            ilink._safe_id(chat_id), attempt + 1,
                            self.settings.send_chunk_retries + 1, exc)
                if wait > 0:
                    await asyncio.sleep(wait)
        if last_error is not None:
            raise last_error

    async def _send_file(self, chat_id: str, path: str, context_token: str | None) -> bool:
        """上传本地文件为加密媒体并发送。voice 统一走文件附件（.silk 原生气泡不稳定）。"""
        session = self._send_session
        if session is None:
            return False
        try:
            plaintext = Path(path).read_bytes()
            filekey = secrets.token_hex(16)
            aes_key = secrets.token_bytes(16)
            rawsize = len(plaintext)
            rawfilemd5 = hashlib.md5(plaintext).hexdigest()

            media_type, item = ilink.build_outbound_media_item(
                path,
                encrypted_query_param="placeholder",
                aes_key_for_api="placeholder",
                ciphertext_size=0,
                plaintext_size=rawsize,
                rawfilemd5=rawfilemd5,
            )
            upload_response = await ilink.get_upload_url(
                session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                media_type=media_type,
                filekey=filekey,
                rawsize=rawsize,
                rawfilemd5=rawfilemd5,
                filesize=ilink._aes_padded_size(rawsize),
                aeskey_hex=aes_key.hex(),
            )
            upload_param = str(upload_response.get("upload_param") or "")
            upload_full_url = str(upload_response.get("upload_full_url") or "")
            ciphertext = ilink.aes128_ecb_encrypt(plaintext, aes_key)

            if upload_full_url:
                upload_url = upload_full_url
            elif upload_param:
                upload_url = ilink._cdn_upload_url(self._cdn_base_url, upload_param, filekey)
            else:
                raise RuntimeError(f"getUploadUrl 未返回 upload_param/upload_full_url: {upload_response}")

            encrypted_query_param = await ilink.upload_ciphertext(
                session, ciphertext=ciphertext, upload_url=upload_url,
            )
            # iLink 期望 aes_key 为 base64(hex 字符串)，而非 base64(原始字节)。
            aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
            _, item = ilink.build_outbound_media_item(
                path,
                encrypted_query_param=encrypted_query_param,
                aes_key_for_api=aes_key_for_api,
                ciphertext_size=len(ciphertext),
                plaintext_size=rawsize,
                rawfilemd5=rawfilemd5,
                force_file_attachment=path.endswith(".silk"),
            )
            await ilink.send_media_item(
                session,
                base_url=self._base_url,
                token=self._token,
                to=chat_id,
                media_item=item,
                context_token=context_token,
                client_id=f"ace-weixin-{secrets.token_hex(16)}",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.error("weixin: 发送文件失败 to=%s path=%s: %s",
                      ilink._safe_id(chat_id), Path(path).name, exc)
            return False

    # -- 输入状态 ----------------------------------------------------------- #
    async def _start_typing(self, chat_id: str) -> bool:
        return await self._send_typing(chat_id, ilink.TYPING_START)

    async def _stop_typing(self, chat_id: str) -> None:
        await self._send_typing(chat_id, ilink.TYPING_STOP)

    async def _send_typing(self, chat_id: str, status: int) -> bool:
        session = self._send_session
        if session is None or not self._token:
            return False
        typing_ticket = self._typing_cache.get(chat_id)
        if not typing_ticket:
            return False
        try:
            await ilink.send_typing(
                session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                typing_ticket=typing_ticket,
                status=status,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("weixin: typing 状态发送失败 %s: %s", ilink._safe_id(chat_id), exc)
            return False

    # -- 刚下载的入站文件（避免出站路径检测把它原样回传）------------------- #
    def _mark_recent_download(self, path: str) -> None:
        self._recent_downloads[path] = time.time()

    def _is_recent_download(self, path: str) -> bool:
        ts = self._recent_downloads.get(path)
        if ts is None:
            return False
        if time.time() - ts > _RECENT_DOWNLOAD_TTL_S:
            self._recent_downloads.pop(path, None)
            return False
        return True

    # -- cron 投递 ----------------------------------------------------------- #
    async def send_to_target(self, chat_id: str, text: str, _origin: SessionSource | None = None) -> bool:
        """DeliveryRouter 回调：cron deliver ``weixin:chat_id`` 时投递文本到指定会话。"""
        if not chat_id or not text.strip() or self._send_session is None:
            return False
        context_token = self._token_store.get(self._account_id, chat_id)
        try:
            await self._send_text_chunks(chat_id, ilink.format_message(text), context_token)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("weixin: send_to_target 失败 chat_id=%s: %s", chat_id, exc)
            return False
