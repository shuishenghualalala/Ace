"""Feishu/Lark channel adapter（lark-oapi WebSocket 长连接，全功能）。

机器人主动连飞书、无需公网 webhook(用于 gateway/platforms/feishu.py 的 ws
长连接模式)。收 im.message 事件 → 解析(全类型)→ 访问控制 → 转 Envelope 交 Crew
内核 → 回复经 reply/create 发回。

连接模型:start() 起后台守护线程跑 lark ws 客户端(SDK 的 start() 阻塞且自带重连);
事件回调在 SDK 线程触发,经 run_coroutine_threadsafe 桥接到 Crew 事件循环分发,
回包/上传/reaction 等阻塞调用统一丢线程池(asyncio.to_thread)。

功能:文本/富文本/图片/文件/音频/视频 收发、访问控制(group_policy 五档 + 白黑名单 +
管理员 + allow_bots + require_mention)、@机器人 精确判定(启动拉 bot 身份)、线程内回复、
markdown→飞书富文本、长文本分块、出站文件(扫回复路径上传)、处理中 reaction、
去重持久化、按 chat 串行。lark-oapi 为可选依赖,缺失时 start 抛清晰错误。

webhook fallback 与 WebSocket 只负责拆传输包，随后共同进入有界异步入口；访问控制、
附件、去重、按 chat 串行、reaction、Agent 分发与回包只保留一套。send_to_target 供
DeliveryRouter 把 cron 结果投递到 feishu:chat_id。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from crew.core.envelope import Envelope
from crew.core.interfaces import Channel, MessageHandler
from crew.gateway.platform_registry import PlatformConfig  # noqa: F401 - 类型参考/对外
from crew.gateway.response_filters import apply_text_filters
from crew.gateway.session_context import SessionSource, build_session_key
from crew.state.logging import get_logger

from . import files as filemod
from . import protocol as proto
from .access import BotIdentity, decide
from .config import FeishuSettings

log = get_logger("platform.feishu")

# 处理中/失败 reaction（值取自 Crew，已在真实飞书验证有效）
REACTION_PROCESSING = "Typing"
REACTION_FAILURE = "CrossMark"
_RECENT_DOWNLOAD_TTL_S = 300.0
_INGRESS_QUEUE_SIZE = 64
_INGRESS_WORKERS = 4

# 用户要发文件时提示 Agent 它具备发文件/图片能力（回复里写 [FILE:绝对路径] 即可），
# 否则通用 LLM 会误以为无法发送而拒绝（出站发文件靠被动截取回复里的路径）。
_SEND_FILE_HINT = (
    "[系统能力] 你就运行在本机上，能用 read/terminal 等工具直接读取本地绝对路径的文件，"
    "不要声称“无法访问本地文件”或“这是用户电脑上的文件”。要把文件/图片发给用户时，"
    "在回复里用 [FILE:/文件绝对路径] 写出该文件的绝对路径，系统会自动上传并发给用户。"
    "用户要你发文件就直接这样给出路径，不要回答“无法发送文件”或“无法访问该路径”。"
)


def lark_available() -> bool:
    """lark-oapi 是否可导入(可选依赖)。"""
    import importlib.util

    return importlib.util.find_spec("lark_oapi") is not None


def _serve_ws(ws_client: Any) -> None:
    """ws 后台线程主体:在本线程建独立事件循环并覆盖 lark 的模块级全局 loop,再跑 ws 客户端。

    lark 的 ws.client 在 *import 时* 用 asyncio.get_event_loop() 抓一个模块级全局 loop,
    其 start() 直接 loop.run_until_complete(...)。我们在 Crew 运行中的事件循环里懒加载它,
    它抓到的就是正在运行的主 loop → start() 会报 "This event loop is already running"。
    故在本后台线程内建独立的新 loop 并覆盖该全局,让 ws 客户端跑在自己的循环上。
    """
    import lark_oapi.ws.client as _ws_mod

    thread_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(thread_loop)
    _ws_mod.loop = thread_loop
    ws_client.start()


def fetch_bot_identity(app_id: str, app_secret: str, domain: str) -> BotIdentity | None:
    """启动时拉取机器人身份(open_id/name),用于 @机器人 精确判定与自回声过滤。

    纯 HTTP(tenant_access_token → /bot/v3/info),失败返回 None。
    """
    import urllib.request

    def _post_json(path: str, body: dict[str, Any] | None, token: str = "") -> dict[str, Any]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        method = "POST" if body is not None else "GET"
        req = urllib.request.Request(domain + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - 显式集成
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    try:
        tok = _post_json("/open-apis/auth/v3/tenant_access_token/internal",
                         {"app_id": app_id, "app_secret": app_secret})
        token = tok.get("tenant_access_token")
        if tok.get("code") != 0 or not token:
            return None
        info = _post_json("/open-apis/bot/v3/info", None, token=str(token))
        if info.get("code") != 0:
            return None
        bot = info.get("bot") or {}
        return BotIdentity(open_id=str(bot.get("open_id") or ""), name=str(bot.get("app_name") or ""))
    except Exception as exc:  # noqa: BLE001
        log.debug("Feishu bot 身份拉取异常: %s", exc)
        return None


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, config: Any) -> None:
        self.config = config
        self.settings = FeishuSettings.from_extra(
            getattr(config, "extra", {}) or {},
            use_env=getattr(config, "env_enabled", True),
        )
        self._handler: MessageHandler | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._client: Any = None
        self._stopped = False
        self._bot = BotIdentity(self.settings.bot_open_id, self.settings.bot_user_id, self.settings.bot_name)
        self._seen: dict[str, float] = {}
        self._seen_lock = threading.Lock()
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._recent_downloads: dict[str, float] = {}
        self._app: Any = None
        self._ingress_queue: asyncio.Queue[tuple[Any, Any, str]] | None = None
        self._ingress_workers: set[asyncio.Task[None]] = set()
        self._dedup_revision = 0
        self._dedup_persisted_revision = 0
        self._dedup_persist_task: asyncio.Task[None] | None = None

    def bind_app(self, app: Any) -> None:
        """绑定 CrewApp，用 Active Owner 与退出协调状态对入站消息做最终门禁。"""
        self._app = app

    def _gateway_owner(self) -> str:
        return str(getattr(self, "_gateway_owner_account_id", "") or "").strip()

    def _owner_lease(self, owner: str):
        active_owner = self._app.active_owner
        getter = getattr(active_owner, "get", None)
        return getter(owner) if owner and callable(getter) else active_owner.current()

    def _owner_is_draining(self, owner: str) -> bool:
        coordinator = getattr(self._app, "logout_coordinator", None)
        if coordinator is None:
            return False
        try:
            return bool(coordinator.is_draining(owner))
        except TypeError:
            return bool(coordinator.is_draining())

    def _owner_binding(self, owner: str) -> str:
        bindings = getattr(self._app, "channel_bindings", None)
        if bindings is None:
            return ""
        try:
            return str(bindings.get_binding(self.name, owner) or "")
        except TypeError:
            return str(bindings.get_binding(self.name) or "")

    # -- 生命周期 ----------------------------------------------------------- #
    async def start(self, handler: MessageHandler) -> None:
        if not self._owner_may_connect():
            raise RuntimeError("Feishu 渠道绑定到其他账号，拒绝建立旧账号连接")
        if not self.settings.configured:
            raise RuntimeError("Feishu 未完成配置:需要 FEISHU_APP_ID / FEISHU_APP_SECRET")
        if not lark_available():
            raise RuntimeError("缺少 lark-oapi 依赖:安装 'lark-oapi==1.5.3'(或 pip install .[feishu])")
        import lark_oapi as lark
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.ws import Client as FeishuWSClient

        self._handler = handler
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        self._client = (
            lark.Client.builder().app_id(self.settings.app_id).app_secret(self.settings.app_secret)
            .domain(self.settings.domain).build()
        )

        # 拉取 bot 身份（@机器人 精确判定 + 自回声过滤）；失败则退化匹配。
        ident = await asyncio.to_thread(
            fetch_bot_identity, self.settings.app_id, self.settings.app_secret, self.settings.domain
        )
        if ident and (ident.open_id or ident.name):
            self._bot = BotIdentity(
                open_id=ident.open_id or self._bot.open_id,
                user_id=self._bot.user_id,
                name=ident.name or self._bot.name,
            )
            log.info("Feishu bot 身份: open_id=%s… name=%s",
                     (self._bot.open_id or "?")[:8], self._bot.name or "?")
        elif not self._bot.known:
            log.warning("Feishu bot 身份未知,@机器人 判定退化为「有@即视作@机器人」")

        self._load_dedup()

        dispatcher = (
            EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_message_event)
            .register_p2_im_message_message_read_v1(self._on_read_event)
            .build()
        )
        ws_client = FeishuWSClient(
            self.settings.app_id, self.settings.app_secret,
            event_handler=dispatcher, domain=self.settings.domain,
        )
        self._start_ingress(handler)
        self._ws_thread = threading.Thread(target=_serve_ws, args=(ws_client,), name="feishu-ws", daemon=True)
        self._ws_thread.start()
        for warning in self.settings.collect_warnings():
            log.warning("Feishu 配置提示: %s", warning)
        log.info("Feishu 通道已启动(ws 长连接,domain=%s,group_policy=%s,reactions=%s)",
                 self.settings.domain, self.settings.group_policy, self.settings.reactions_enabled)

    async def stop(self) -> None:
        """停止逻辑入口，丢弃排队消息并取消正在执行的渠道对话。

        lark SDK 未暴露可靠的热 stop；物理断连仍由 Gateway 受控重启完成。这里先关闭
        所有业务入口，确保退出期间不再解析、分发或回包。
        """
        self._stopped = True
        self._handler = None
        workers = tuple(self._ingress_workers)
        for task in workers:
            task.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._ingress_workers.clear()
        queue = self._ingress_queue
        if queue is not None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                else:
                    queue.task_done()
        self._ingress_queue = None
        persist_task = self._dedup_persist_task
        if persist_task is not None:
            await asyncio.gather(persist_task, return_exceptions=True)
        if self._dedup_persisted_revision < self._dedup_revision:
            await asyncio.to_thread(self._persist_dedup)
            self._dedup_persisted_revision = self._dedup_revision

    def status_detail(self) -> dict[str, Any]:
        return {
            "transport": "ws-long-connection",
            "domain": self.settings.domain,
            "group_policy": self.settings.group_policy,
            "bot_identity_known": self._bot.known,
            "running": bool(self._ws_thread and self._ws_thread.is_alive() and not self._stopped),
        }

    # -- 事件回调(在 lark SDK 线程触发)------------------------------------ #
    def _on_read_event(self, data: Any) -> None:
        """消息已读回执（im.message.message_read_v1）：注册空处理器，避免 lark
        报「processor not found」噪音。Crew 无需处理已读回执。"""
        return

    def _on_message_event(self, data: Any) -> None:
        if self._stopped or self._handler is None or self._loop is None:
            return
        event = getattr(data, "event", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        # SDK 回调线程只拆传输包；解析与所有业务门禁统一在 Crew 事件循环执行。
        self._loop.call_soon_threadsafe(self._enqueue_ingress, message, sender, "websocket")

    def _start_ingress(self, handler: MessageHandler) -> None:
        """在当前 Crew 事件循环启动有界入口 worker。"""
        self._handler = handler
        self._stopped = False
        self._ingress_queue = asyncio.Queue(maxsize=_INGRESS_QUEUE_SIZE)
        for index in range(_INGRESS_WORKERS):
            task = asyncio.create_task(
                self._ingress_worker(),
                name=f"feishu-ingress-{index}",
            )
            self._ingress_workers.add(task)
            task.add_done_callback(self._ingress_workers.discard)

    def _owner_available(self, owner_account_id: str | None = None) -> bool:
        """返回该渠道实例的 Owner 是否仍可接收入站事件。"""
        if self._app is None:
            return True
        try:
            owner = str(owner_account_id or self._gateway_owner()).strip()
            lease = self._owner_lease(owner)
            if lease is None:
                return False
            active_owner = str(lease.owner_account_id or "")
            if self._owner_is_draining(active_owner):
                return False
            bound_owner = self._owner_binding(active_owner)
            if not bound_owner:
                return True
            return bound_owner == active_owner
        except Exception as exc:  # noqa: BLE001 - 身份事实源异常必须 fail closed
            log.warning("Feishu Active Owner 门禁读取失败: %s", exc)
            return False

    def _owner_may_connect(self) -> bool:
        """连接前只检查当前渠道实例自己的 Owner 会话。"""
        if self._app is None:
            return True
        try:
            owner = self._gateway_owner()
            lease = self._owner_lease(owner)
            if lease is None:
                return False
            active_owner = str(lease.owner_account_id or "")
            if self._owner_is_draining(active_owner):
                return False
            bound_owner = self._owner_binding(active_owner)
            return not bound_owner or bound_owner == active_owner
        except Exception as exc:  # noqa: BLE001 - 身份事实源异常必须 fail closed
            log.warning("Feishu 连接 Owner 门禁读取失败: %s", exc)
            return False

    def ingress_available(self, owner_account_id: str | None = None) -> bool:
        """供 Gateway 在读取 webhook 正文前检查渠道是否可接收入站事件。"""
        return bool(
            not self._stopped
            and self._handler is not None
            and self._ingress_queue is not None
            and self._owner_available(owner_account_id)
        )

    def _enqueue_ingress(self, message: Any, sender: Any, transport: str) -> str:
        if not self.ingress_available():
            return "unavailable"
        queue = self._ingress_queue
        if queue is None:
            return "unavailable"
        try:
            queue.put_nowait((message, sender, transport))
        except asyncio.QueueFull:
            log.warning("Feishu 入站队列已满，拒绝 transport=%s", transport)
            return "queue_full"
        return "accepted"

    def enqueue_webhook_event(self, payload: dict[str, Any]) -> str:
        """拆出 webhook 传输字段并立即尝试入队；不在 HTTP 请求内执行业务处理。"""
        event = payload.get("event")
        if not isinstance(event, dict):
            return "invalid_event"
        return self._enqueue_ingress(event.get("message"), event.get("sender"), "webhook")

    async def _wait_ingress_idle(self) -> None:
        """等待当前已接收事件处理完毕，主要用于生命周期验证与测试。"""
        queue = self._ingress_queue
        if queue is not None:
            await queue.join()

    async def _ingress_worker(self) -> None:
        queue = self._ingress_queue
        if queue is None:
            return
        while True:
            message, sender, transport = await queue.get()
            try:
                if not self.ingress_available():
                    continue
                parsed = proto.parse_inbound(message, sender)
                if parsed is None:
                    continue
                allow, reason = decide(parsed, self.settings, self._bot)
                if not allow:
                    log.debug("Feishu 丢弃消息 id=%s 原因=%s", parsed["message_id"], reason)
                    continue
                if self._dedupe(parsed["message_id"]):
                    continue
                await self._handle(parsed)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 单条外部事件失败不能终止入口 worker
                log.exception("Feishu 入站处理失败 transport=%s", transport)
            finally:
                queue.task_done()

    # -- 去重(lark 线程,持久化)------------------------------------------- #
    def _load_dedup(self) -> None:
        path = self.settings.dedup_path()
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                now = time.time()
                self._seen = {k: float(v) for k, v in data.items()
                              if now - float(v) < self.settings.dedup_ttl_s}
        except Exception as exc:  # noqa: BLE001
            log.debug("Feishu dedup 载入失败: %s", exc)
            self._seen = {}

    def _persist_dedup(self) -> None:
        path = self.settings.dedup_path()
        try:
            with self._seen_lock:
                items = sorted(
                    self._seen.items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                )[: self.settings.dedup_max]
                self._seen = dict(items)
                payload = json.dumps(self._seen)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            log.debug("Feishu dedup 持久化失败: %s", exc)

    async def _persist_dedup_async(self) -> None:
        """Serialize best-effort snapshots off the Gateway event loop."""
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
            self._persist_dedup_async(),
            name="feishu-dedup-persist",
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

    # -- 分发 + 回包(在 Crew 事件循环上)----------------------------------- #
    def _chat_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock
            if len(self._chat_locks) > 2000:  # 软上限:清理未持有的锁
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
            reaction_id = None
            if self.settings.reactions_enabled:
                reaction_id = await self._add_reaction(parsed["message_id"], REACTION_PROCESSING)

            attachments, hints = await self._download_attachments(parsed)
            text = parsed["text"]
            if hints:
                text = (text + "\n" + "\n".join(hints)).strip() if text else "\n".join(hints)
            # 用户表达发送意图时，提示 Agent 它能发文件（避免误以为无法发送而拒绝）。
            # 该提示作为渠道系统提示注入，不拼入用户消息正文，避免泄露到对话历史。
            extra_params: dict[str, Any] = {}
            if proto.detect_send_intent(parsed["text"]):
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
            except Exception as exc:  # noqa: BLE001
                log.exception("Feishu 分发失败: %s", parsed["message_id"])
                error = str(exc)
            finally:
                if reaction_id:
                    await self._remove_reaction(parsed["message_id"], reaction_id)
            if self._stopped or not self._owner_available():
                return
            await self._deliver(parsed, final_text, error)
            if error and self.settings.reactions_enabled:
                await self._add_reaction(parsed["message_id"], REACTION_FAILURE)

    async def _download_attachments(self, parsed: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        attachments: list[dict[str, Any]] = []
        hints: list[str] = []
        for res in parsed["resources"]:
            dl = await filemod.download_resource(
                self._client, parsed["message_id"], res["key"], res["kind"],
                base_dir=self.settings.files_dir(), default_name=res.get("name", ""),
                max_bytes=self.settings.max_file_bytes,
            )
            if dl:
                self._mark_recent_download(dl["path"])
                attachments.append({"name": dl["name"], "path": dl["path"]})
                hints.append(f"[附件: {dl['name']}, 路径: {dl['path']}]")
        return attachments, hints

    def _session_key(self, parsed: dict[str, Any]) -> str:
        """统一会话 key：feishu p2p→dm，群聊按 open_id 隔离参与者。"""
        chat_type = parsed.get("chat_type") or "p2p"
        return build_session_key(SessionSource(
            platform="feishu",
            chat_id=parsed["chat_id"],
            chat_type="dm" if chat_type == "p2p" else chat_type,
            user_id=parsed.get("sender_open_id") or None,
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
            "platform_sender": parsed["sender_open_id"],
            "platform_uid": parsed["sender_open_id"],
            "platform_chat_type": parsed["chat_type"],
        }
        if extra_params:
            params.update(extra_params)
        return Envelope.of(
            text,
            session_id=self._session_key(parsed),
            channel="feishu",
            user_id=parsed["sender_open_id"] or "feishu",
            workspace_id=self.settings.workspace_id,
            attachments=attachments,
            params=params,
        )

    # -- 回包 --------------------------------------------------------------- #
    async def _deliver(self, parsed: dict[str, Any], final_text: str, error: str) -> None:
        reply = (final_text or error).strip()
        if not reply:
            return
        # IM 渠道绕过 outbound.py，这里手动套用出站过滤链（密钥脱敏 + IM 内联 thinking 剥离），
        # 与 Web/桌面端的安全基线对齐。
        reply = apply_text_filters(reply, {"channel": "feishu"})
        paths = proto.extract_file_paths(reply, is_recent=self._is_recent_download)
        if paths:
            cleaned = proto.strip_file_syntax(reply)
            if cleaned:
                await self._send_text(parsed, cleaned)
            for path in paths:
                await self._send_file(parsed, path)
            return
        for chunk in proto.chunk_text(reply, self.settings.text_chunk_limit):
            await self._send_text(parsed, chunk)

    async def _send_text(self, parsed: dict[str, Any], text: str) -> None:
        # markdown → 尝试富文本 post（仅 reply，失败直接回退纯文本）
        if proto.looks_like_markdown(text):
            ok = await asyncio.to_thread(
                self._send_sync, parsed, "post", proto.render_post_content(text), False
            )
            if ok:
                return
        await asyncio.to_thread(self._send_sync, parsed, "text", proto.reply_content(text), True)

    async def _send_file(self, parsed: dict[str, Any], path: str) -> None:
        from crew.tools.security_guard import authorize_file_tool

        approved_path = await authorize_file_tool(
            {"path": path},
            operation="read",
            tool_name="feishu_send_file",
            workspace_store=getattr(self._app, "workspace_store", None),
            security_service=getattr(self._app, "security_service", None),
        )
        if proto.is_image_file(path):
            key = await filemod.upload_image(self._client, str(approved_path))
            if key:
                await asyncio.to_thread(self._send_sync, parsed, "image",
                                        json.dumps({"image_key": key}), True)
        else:
            key = await filemod.upload_file(self._client, str(approved_path))
            if key:
                await asyncio.to_thread(self._send_sync, parsed, "file",
                                        json.dumps({"file_key": key}), True)

    def _send_sync(self, parsed: dict[str, Any], msg_type: str, content: str, allow_create_fallback: bool) -> bool:
        """先 reply(线程内回复原消息)；失败且允许时回退 create 到会话。返回是否成功。"""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
            ReplyMessageRequest,
            ReplyMessageRequestBody,
        )

        if parsed.get("message_id"):
            try:
                request = (
                    ReplyMessageRequest.builder()
                    .message_id(parsed["message_id"])
                    .request_body(
                        ReplyMessageRequestBody.builder()
                        .content(content).msg_type(msg_type).reply_in_thread(False).build()
                    )
                    .build()
                )
                resp = self._client.im.v1.message.reply(request)
                if resp.success():
                    return True
                log.warning("Feishu reply 失败 type=%s code=%s msg=%s", msg_type,
                            getattr(resp, "code", "?"), getattr(resp, "msg", "?"))
            except Exception as exc:  # noqa: BLE001
                log.warning("Feishu reply 异常 type=%s: %s", msg_type, exc)

        if not allow_create_fallback:
            return False
        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type("chat_id")
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(parsed["chat_id"]).msg_type(msg_type).content(content).build()
                )
                .build()
            )
            resp = self._client.im.v1.message.create(request)
            if resp.success():
                return True
            log.warning("Feishu create 失败 type=%s code=%s msg=%s", msg_type,
                        getattr(resp, "code", "?"), getattr(resp, "msg", "?"))
        except Exception as exc:  # noqa: BLE001
            log.exception("Feishu create 异常: %s", exc)
        return False

    # -- reaction(best-effort,失败不影响主流程)---------------------------- #
    async def _add_reaction(self, message_id: str, emoji: str) -> str | None:
        try:
            return await asyncio.to_thread(self._add_reaction_sync, message_id, emoji)
        except Exception as exc:  # noqa: BLE001
            log.debug("Feishu reaction 添加异常: %s", exc)
            return None

    def _add_reaction_sync(self, message_id: str, emoji: str) -> str | None:
        from lark_oapi.api.im.v1 import (
            CreateMessageReactionRequest,
            CreateMessageReactionRequestBody,
            Emoji,
        )

        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji).build()).build()
            )
            .build()
        )
        resp = self._client.im.v1.message_reaction.create(request)
        if not resp.success():
            log.debug("Feishu reaction 添加失败 code=%s msg=%s",
                      getattr(resp, "code", "?"), getattr(resp, "msg", "?"))
            return None
        return getattr(getattr(resp, "data", None), "reaction_id", None)

    async def _remove_reaction(self, message_id: str, reaction_id: str) -> None:
        try:
            await asyncio.to_thread(self._remove_reaction_sync, message_id, reaction_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("Feishu reaction 移除异常: %s", exc)

    def _remove_reaction_sync(self, message_id: str, reaction_id: str) -> None:
        from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

        request = DeleteMessageReactionRequest.builder().message_id(message_id).reaction_id(reaction_id).build()
        self._client.im.v1.message_reaction.delete(request)

    # -- 刚下载的入站文件(避免出站路径检测把它原样回传)-------------------- #
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

    # ===================================================================== #
    # webhook fallback + delivery 回调（gateway HTTP 端点 / DeliveryRouter） #
    # ===================================================================== #
    def verify_webhook(
        self,
        payload: dict[str, Any],
        *,
        allow_missing_token: bool = False,
    ) -> bool:
        """校验 webhook 事件来源。

        生产环境未配置 verification_token 时拒绝；只有 Gateway 显式声明开发模式才可
        ``allow_missing_token``。配置后比对 v2 ``header.token`` 或 v1 顶层 ``token``。
        """
        expected = self.settings.verification_token
        if not expected:
            return allow_missing_token
        header = payload.get("header")
        token = ""
        if isinstance(header, dict):
            token = str(header.get("token") or "")
        if not token:
            token = str(payload.get("token") or "")
        return token == expected

    def challenge_response(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """webhook 模式 challenge 应答。gateway ``/api/feishu/events`` 直接转发。"""
        challenge = payload.get("challenge")
        if isinstance(challenge, str):
            return {"challenge": challenge}
        if payload.get("type") == "url_verification" and isinstance(challenge, str):
            return {"challenge": challenge}
        return None

    async def send_to_target(self, chat_id: str, text: str, _origin: SessionSource | None = None) -> bool:
        """DeliveryRouter 回调:cron deliver ``feishu:chat_id`` 时投递文本到指定会话。

        无原消息可 reply,直接 create 到 chat_id。复用 _send_sync 的 create fallback 路径。
        lark client 未就绪(ws 未启动)时返回 False。
        """
        if not chat_id or not text.strip() or self._client is None:
            return False
        parsed = {"chat_id": chat_id, "message_id": ""}
        try:
            return await asyncio.to_thread(
                self._send_sync, parsed, "text", proto.reply_content(text), True
            )
        except Exception:  # noqa: BLE001
            log.debug("Feishu send_to_target 失败 chat_id=%s", chat_id)
            return False
