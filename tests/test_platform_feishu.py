"""飞书平台插件测试:解析(全类型)、配置、访问控制、分发回包、出站渲染、注册、回归。

不触网:纯函数直接断言;lark 事件用 SimpleNamespace/dict mock;发消息/反应 monkeypatch;
ws 长连接(需真实飞书应用)不在单测范围。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from crew.core.envelope import ResponseChunk
from crew.gateway.platform_registry import PlatformConfig, platform_registry
from crew.plugins.manager import PluginManager
from crew.tools.registry import Registry
from plugins.platforms.feishu.access import (
    BotIdentity,
    decide,
    is_bot_sender,
    is_self_message,
    mentions_self,
)
from plugins.platforms.feishu.adapter import FeishuChannel, _serve_ws, lark_available
from plugins.platforms.feishu.config import FeishuSettings
from plugins.platforms.feishu.protocol import (
    chunk_text,
    extract_file_paths,
    extract_text,
    is_image_file,
    looks_like_markdown,
    parse_card_text,
    parse_inbound,
    parse_mentions,
    parse_post,
    reply_content,
    strip_file_syntax,
)

_CREDS = {"appId": "cli_app", "appSecret": "secret", "workspaceId": "ws1", "reactions": False}


@pytest.fixture(autouse=True)
def _restore_platform_registry():
    old_entries = list(platform_registry.all_entries())
    yield
    platform_registry._entries.clear()
    for entry in old_entries:
        platform_registry.register(entry)


def _msg(text="你好", chat_type="p2p", chat_id="oc_1", msg_id="om_1", msg_type="text",
         open_id="ou_user", mentions=None, sender_type="user", content=None):
    message = SimpleNamespace(
        message_type=msg_type,
        content=content if content is not None else json.dumps({"text": text}),
        chat_id=chat_id, message_id=msg_id, chat_type=chat_type, mentions=mentions,
        parent_id=None, root_id=None, thread_id=None,
    )
    sender = SimpleNamespace(
        sender_id=SimpleNamespace(open_id=open_id, user_id="", union_id=""),
        sender_type=sender_type,
    )
    return SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))


def _parsed(**kw):
    base = {"message_id": "m1", "chat_id": "oc_1", "chat_type": "p2p", "msg_type": "text",
            "text": "你好", "mentions": [], "resources": [], "sender_open_id": "ou_u",
            "sender_user_id": "", "sender_union_id": "", "sender_type": "user",
            "parent_id": "", "thread_id": ""}
    base.update(kw)
    return base


def _webhook_payload(*, msg_id: str = "om_1", text: str = "你好") -> dict:
    return {
        "header": {"token": "verify-token"},
        "event": {
            "message": {
                "message_type": "text",
                "content": json.dumps({"text": text}),
                "chat_id": "oc_1",
                "message_id": msg_id,
                "chat_type": "p2p",
                "mentions": [],
            },
            "sender": {
                "sender_id": {"open_id": "ou_user", "user_id": "", "union_id": ""},
                "sender_type": "user",
            },
        },
    }


# --------------------------------------------------------------------------- #
# 解析:文本 / 富文本 / 媒体 / 卡片
# --------------------------------------------------------------------------- #
def test_extract_text():
    assert extract_text('{"text":"  hi  "}') == "hi"
    assert extract_text({"text": "x"}) == "x"
    assert extract_text("plain") == "plain"
    assert extract_text(None) == ""


def test_parse_inbound_text_normalizes_mentions():
    ev = _msg(text="@_user_1 在吗", mentions=[{"key": "@_user_1", "id": {"open_id": "ou_bot"}, "name": "小助手"}])
    p = parse_inbound(ev.event.message, ev.event.sender)
    assert p["text"] == "@小助手 在吗"
    assert p["msg_type"] == "text" and p["chat_id"] == "oc_1"
    assert p["mentions"][0]["open_id"] == "ou_bot"


def test_parse_inbound_image_file_audio():
    img = parse_inbound(_msg(msg_type="image", content='{"image_key":"img_x"}').event.message, _msg().event.sender)
    assert img["text"] == "" and img["resources"] == [{"kind": "image", "key": "img_x", "name": ""}]
    f = parse_inbound(_msg(msg_type="file", content='{"file_key":"fk","file_name":"a.pdf"}').event.message, _msg().event.sender)
    assert f["resources"] == [{"kind": "file", "key": "fk", "name": "a.pdf"}]
    au = parse_inbound(_msg(msg_type="audio", content='{"file_key":"ak"}').event.message, _msg().event.sender)
    assert au["resources"][0]["kind"] == "audio"


def test_parse_post():
    content = json.dumps({"title": "标题", "content": [
        [{"tag": "text", "text": "你好"}, {"tag": "a", "text": "链接", "href": "http://x"}],
        [{"tag": "at", "user_name": "张三"}, {"tag": "img", "image_key": "img_1"}],
    ]})
    text, imgs = parse_post(content)
    assert "标题" in text and "你好" in text and "链接(http://x)" in text and "@张三" in text
    assert imgs == ["img_1"]


def test_parse_inbound_post_collects_images():
    content = json.dumps({"content": [[{"tag": "text", "text": "图"}, {"tag": "img", "image_key": "ik"}]]})
    p = parse_inbound(_msg(msg_type="post", content=content).event.message, _msg().event.sender)
    assert p["text"] == "图" and p["resources"] == [{"kind": "image", "key": "ik", "name": ""}]


def test_parse_card_text():
    content = json.dumps({"elements": [{"tag": "div", "text": {"content": "卡片正文"}}], "header": {"title": {"content": "卡片标题"}}})
    assert "卡片正文" in parse_card_text(content) and "卡片标题" in parse_card_text(content)


@pytest.mark.parametrize("ev", [
    _msg(content='{"text":"   "}'),                       # 空文本
    SimpleNamespace(event=SimpleNamespace(message=None, sender=None)),  # 无 message
])
def test_parse_inbound_rejects_empty(ev):
    msg = ev.event.message
    assert parse_inbound(msg, ev.event.sender) is None


def test_parse_mentions_is_all():
    msg = SimpleNamespace(mentions=[{"key": "@_all", "id": {}, "name": "所有人"}])
    ms = parse_mentions(msg)
    assert ms[0]["is_all"] is True


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_settings_env_and_extra(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "envid")
    monkeypatch.setenv("FEISHU_APP_SECRET", "envsec")
    s = FeishuSettings.from_extra({})
    assert s.configured and s.app_id == "envid"
    s2 = FeishuSettings.from_extra({"appId": "cfgid", "appSecret": "cfgsec", "domain": "lark"})
    assert s2.app_id == "cfgid" and s2.domain == "https://open.larksuite.com"
    assert FeishuSettings.from_extra({"appId": "a", "appSecret": "b"}).domain == "https://open.feishu.cn"


def test_settings_access_fields(monkeypatch):
    monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_a, ou_b")
    monkeypatch.setenv("FEISHU_ADMINS", "ou_admin")
    s = FeishuSettings.from_extra({"groupPolicy": "allowlist", "allowBots": "mentions",
                                   "blockedUsers": ["ou_x"]})
    assert s.group_policy == "allowlist" and s.allow_bots == "mentions"
    assert s.allowed_users == {"ou_a", "ou_b"} and s.admins == {"ou_admin"} and s.blocked_users == {"ou_x"}


def test_settings_invalid_policy_falls_back(monkeypatch):
    s = FeishuSettings.from_extra({"groupPolicy": "bogus", "allowBots": "weird"})
    assert s.group_policy == "open" and s.allow_bots == "none"


def test_settings_group_rules():
    s = FeishuSettings.from_extra({"groupRules": {"oc_g": {"policy": "allowlist", "allowlist": ["ou_1"],
                                                           "require_mention": False}}})
    assert s.group_rules["oc_g"]["policy"] == "allowlist"
    assert s.group_rules["oc_g"]["allowlist"] == {"ou_1"} and s.group_rules["oc_g"]["require_mention"] is False


# --------------------------------------------------------------------------- #
# 访问控制
# --------------------------------------------------------------------------- #
_BOT = BotIdentity(open_id="ou_bot", name="小助手")


def test_self_echo_dropped():
    p = _parsed(sender_open_id="ou_bot")
    assert is_self_message(p, _BOT) is True
    assert decide(p, FeishuSettings.from_extra(_CREDS), _BOT)[0] is False


def test_bot_sender_policy():
    s_none = FeishuSettings.from_extra({**_CREDS, "allowBots": "none"})
    s_all = FeishuSettings.from_extra({**_CREDS, "allowBots": "all"})
    p = _parsed(sender_type="app")
    assert is_bot_sender(p) is True
    assert decide(p, s_none, _BOT)[0] is False
    assert decide(p, s_all, _BOT)[0] is True


def test_dm_allowlist():
    s = FeishuSettings.from_extra({**_CREDS, "allowedUsers": ["ou_ok"]})
    assert decide(_parsed(sender_open_id="ou_ok"), s, _BOT)[0] is True
    assert decide(_parsed(sender_open_id="ou_no"), s, _BOT)[0] is False


def test_group_policies():
    base = dict(chat_type="group", chat_id="oc_g", mentions=[{"open_id": "ou_bot"}])  # @了机器人
    # open
    s_open = FeishuSettings.from_extra(_CREDS)
    assert decide(_parsed(**base), s_open, _BOT)[0] is True
    # disabled
    s_dis = FeishuSettings.from_extra({**_CREDS, "groupPolicy": "disabled"})
    assert decide(_parsed(**base), s_dis, _BOT)[0] is False
    # allowlist
    s_allow = FeishuSettings.from_extra({**_CREDS, "groupPolicy": "allowlist", "allowedUsers": ["ou_ok"]})
    assert decide(_parsed(**base, sender_open_id="ou_ok"), s_allow, _BOT)[0] is True
    assert decide(_parsed(**base, sender_open_id="ou_no"), s_allow, _BOT)[0] is False
    # blacklist
    s_black = FeishuSettings.from_extra({**_CREDS, "groupPolicy": "blacklist", "blockedUsers": ["ou_bad"]})
    assert decide(_parsed(**base, sender_open_id="ou_bad"), s_black, _BOT)[0] is False
    assert decide(_parsed(**base, sender_open_id="ou_ok"), s_black, _BOT)[0] is True


def test_group_require_mention():
    s = FeishuSettings.from_extra(_CREDS)  # require_mention 默认 True
    at = _parsed(chat_type="group", chat_id="oc_g", mentions=[{"open_id": "ou_bot"}])
    noat = _parsed(chat_type="group", chat_id="oc_g", mentions=[])
    assert decide(at, s, _BOT)[0] is True
    assert decide(noat, s, _BOT)[1] == "group-no-mention"


def test_admin_bypasses_policy():
    s = FeishuSettings.from_extra({**_CREDS, "groupPolicy": "disabled", "admins": ["ou_admin"]})
    # 管理员绕过策略，但群内 require_mention 仍需 @
    p = _parsed(chat_type="group", chat_id="oc_g", sender_open_id="ou_admin",
                mentions=[{"open_id": "ou_bot"}])
    assert decide(p, s, _BOT)[0] is True


def test_mentions_self_degraded_when_bot_unknown():
    unknown = BotIdentity()
    assert mentions_self(_parsed(mentions=[{"open_id": "ou_other"}]), unknown) is True   # 退化:有@即视作@机器人
    assert mentions_self(_parsed(mentions=[{"open_id": "ou_other"}]), _BOT) is False      # 已知身份:精确不匹配


# --------------------------------------------------------------------------- #
# 出站渲染
# --------------------------------------------------------------------------- #
def test_reply_content():
    assert json.loads(reply_content("结果")) == {"text": "结果"}


def test_extract_and_strip_file_paths():
    text = "见 [FILE:/tmp/a.pdf] 和 /var/data/x.png 还有 C:\\docs\\y.docx"
    paths = extract_file_paths(text, exists=lambda _: True)
    assert "/tmp/a.pdf" in paths and "/var/data/x.png" in paths and "C:\\docs\\y.docx" in paths
    assert strip_file_syntax("a [FILE:/tmp/x] b") == "a  b"


def test_extract_file_paths_excludes_recent_and_missing():
    assert extract_file_paths("[FILE:/tmp/a.pdf]", exists=lambda _: False) == []
    assert extract_file_paths("[FILE:/tmp/a.pdf]", exists=lambda _: True, is_recent=lambda _: True) == []


def test_is_image_file():
    assert is_image_file("a.PNG") is True and is_image_file("b.pdf") is False


def test_chunk_text():
    assert chunk_text("", 100) == []
    assert chunk_text("short", 100) == ["short"]
    chunks = chunk_text("a" * 5000, 4000)
    assert [len(c) for c in chunks] == [4000, 1000]


def test_looks_like_markdown():
    assert looks_like_markdown("# 标题") and looks_like_markdown("- 列表") and looks_like_markdown("**粗**")
    assert not looks_like_markdown("就是一句普通的话")


def test_lark_available():
    # 可选依赖：未装 lark-oapi 时跳过，不把本机缺包当成产品失败
    if not lark_available():
        pytest.skip("lark-oapi 未安装")
    assert lark_available() is True


# --------------------------------------------------------------------------- #
# 传输拆包 + 统一异步入口
# --------------------------------------------------------------------------- #
def _channel(**extra):
    ch = FeishuChannel(PlatformConfig(name="feishu", extra={**_CREDS, **extra}))
    ch._loop = object()
    ch._handler = lambda env: None
    ch._stopped = False
    ch._bot = _BOT
    return ch


def test_on_message_event_only_bridges_raw_event_to_ingress(monkeypatch):
    ch = _channel()
    calls = []

    class ImmediateLoop:
        @staticmethod
        def call_soon_threadsafe(callback, *args):
            callback(*args)

    ch._loop = ImmediateLoop()
    monkeypatch.setattr(ch, "_enqueue_ingress", lambda *args: calls.append(args))
    event = _msg(chat_type="group", chat_id="g", msg_id="g1")
    ch._on_message_event(event)

    assert calls == [(event.event.message, event.event.sender, "websocket")]


def test_webhook_verification_fails_closed_without_token():
    ch = _channel()

    assert ch.verify_webhook({}) is False
    assert ch.verify_webhook({}, allow_missing_token=True) is True


async def test_ws_and_webhook_share_ingress_and_dedupe(monkeypatch, tmp_path):
    ch = FeishuChannel(PlatformConfig(
        name="feishu",
        extra={**_CREDS, "verificationToken": "verify-token"},
    ))
    ch._loop = asyncio.get_running_loop()
    ch._bot = _BOT
    ch._client = object()
    monkeypatch.setattr(ch.settings, "dedup_path", lambda: tmp_path / "seen.json")
    sent = []
    ch._send_sync = lambda parsed, mt, content, fb: (sent.append(content) or True)
    seen = []

    async def handler(env):
        seen.append(env)
        yield ResponseChunk.final(env.request_id, "ok")

    ch._start_ingress(handler)
    ch._on_message_event(_msg(msg_id="same-message"))
    assert ch.enqueue_webhook_event(_webhook_payload(msg_id="same-message")) == "accepted"
    await asyncio.sleep(0)
    await ch._wait_ingress_idle()

    assert len(seen) == 1
    assert len(sent) == 1
    await ch.stop()


async def test_dedupe_persistence_does_not_block_ingress_event_loop(monkeypatch, tmp_path):
    ch = _channel()
    monkeypatch.setattr(ch.settings, "dedup_path", lambda: tmp_path / "seen.json")
    original_persist = ch._persist_dedup

    def slow_persist() -> None:
        time.sleep(0.05)
        original_persist()

    monkeypatch.setattr(ch, "_persist_dedup", slow_persist)

    started = asyncio.get_running_loop().time()
    assert ch._dedupe("message-1") is False
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.02
    await ch.stop()
    assert json.loads((tmp_path / "seen.json").read_text(encoding="utf-8")) == {
        "message-1": pytest.approx(ch._seen["message-1"]),
    }


async def test_stop_discards_queue_and_cancels_inflight_reply():
    ch = _channel()
    ch._loop = asyncio.get_running_loop()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    sent = []
    ch._send_sync = lambda parsed, mt, content, fb: (sent.append(content) or True)

    async def handler(env):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        yield ResponseChunk.final(env.request_id, "late")

    ch._start_ingress(handler)
    assert ch.enqueue_webhook_event(_webhook_payload(msg_id="cancel-me")) == "accepted"
    await started.wait()
    await ch.stop()

    assert cancelled.is_set()
    assert sent == []


async def test_start_rejects_reconnecting_previous_owner_account():
    ch = _channel()
    ch.bind_app(SimpleNamespace(
        active_owner=SimpleNamespace(
            current=lambda: SimpleNamespace(owner_account_id="B:uid-b"),
        ),
        channel_bindings=SimpleNamespace(get_binding=lambda platform: "A:uid-a"),
        logout_coordinator=SimpleNamespace(is_draining=lambda: False),
    ))

    async def handler(env):  # pragma: no cover - owner gate rejects before transport setup
        if False:
            yield env

    with pytest.raises(RuntimeError, match="绑定到其他账号"):
        await ch.start(handler)


# --------------------------------------------------------------------------- #
# 分发 + 回包(_handle)
# --------------------------------------------------------------------------- #
async def test_handle_round_trip():
    ch = _channel()
    sent = []
    ch._send_sync = lambda parsed, mt, content, fb: (sent.append((mt, content)) or True)
    seen = []

    async def handler(env):
        seen.append(env)
        yield ResponseChunk.delta(env.request_id, "...")
        yield ResponseChunk.final(env.request_id, f"echo:{env.query}")

    ch._handler = handler
    await ch._handle(_parsed(text="你好", chat_id="oc_1", message_id="m1", sender_open_id="ou_u"))
    assert sent == [("text", reply_content("echo:你好"))]
    assert seen[0].channel == "feishu" and seen[0].session_id == "agent:main:feishu:dm:oc_1"
    assert seen[0].user_id == "ou_u" and seen[0].workspace_id == "ws1"
    assert seen[0].attachments == []
    assert seen[0].params["platform_chat_id"] == "oc_1"
    assert seen[0].params["platform_uid"] == "ou_u"


async def test_handle_send_intent_injects_channel_hint_not_user_query():
    """发送意图时，[FILE] 能力提示应注入 params 而非拼入用户消息正文，避免泄露到历史。"""
    ch = _channel()
    ch._send_sync = lambda parsed, mt, content, fb: True
    seen = []

    async def handler(env):
        seen.append(env)
        yield ResponseChunk.final(env.request_id, "ok")

    ch._handler = handler
    await ch._handle(_parsed(text="把报告发给我", chat_id="oc_1", message_id="m2", sender_open_id="ou_u"))
    env = seen[0]
    assert env.query == "把报告发给我"
    assert "[系统能力]" in env.params.get("channel_system_hint", "")
    assert "[FILE:" in env.params.get("channel_system_hint", "")


async def test_handle_error_sent_back():
    ch = _channel()
    sent = []
    ch._send_sync = lambda parsed, mt, content, fb: (sent.append(content) or True)

    async def handler(env):
        yield ResponseChunk.error(env.request_id, "boom")

    ch._handler = handler
    await ch._handle(_parsed())
    assert json.loads(sent[0]) == {"text": "boom"}


async def test_handle_outbound_file(tmp_path, monkeypatch):
    ch = _channel()
    f = tmp_path / "out.png"
    f.write_bytes(b"x")
    sent = []
    ch._send_sync = lambda parsed, mt, content, fb: (sent.append((mt, content)) or True)

    async def fake_upload_image(client, path):
        return "img_key_1"

    import plugins.platforms.feishu.adapter as mod
    monkeypatch.setattr(mod.filemod, "upload_image", fake_upload_image)

    async def handler(env):
        yield ResponseChunk.final(env.request_id, f"见 [FILE:{f}]")

    ch._handler = handler
    await ch._handle(_parsed(text="给图"))

    image_msgs = [content for mt, content in sent if mt == "image"]
    assert image_msgs and json.loads(image_msgs[0]) == {"image_key": "img_key_1"}


async def test_send_to_target_creates_message():
    ch = _channel()
    ch._client = object()
    calls = []
    ch._send_sync = lambda parsed, mt, content, fb: (calls.append((parsed, mt, content, fb)) or True)

    ok = await ch.send_to_target("oc_target", "主动通知")

    assert ok is True
    parsed, msg_type, content, allow_fallback = calls[0]
    assert parsed == {"chat_id": "oc_target", "message_id": ""}
    assert msg_type == "text"
    assert json.loads(content) == {"text": "主动通知"}
    assert allow_fallback is True


async def test_send_to_target_without_client_returns_false():
    ch = _channel()
    ch._client = None

    ok = await ch.send_to_target("oc_target", "主动通知")

    assert ok is False


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def test_feishu_platform_registers(monkeypatch):
    if not lark_available():
        pytest.skip("lark-oapi 未安装")
    monkeypatch.setenv("FEISHU_APP_ID", "app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "secret")
    plugins = PluginManager(registry=Registry())
    plugins.discover_and_load([Path("plugins")], enabled=["feishu-platform"])
    loaded = [p for p in plugins.loaded_plugins if p.manifest.name == "feishu-platform"][0]
    assert loaded.enabled and loaded.platforms_registered == ["feishu"]
    entry = platform_registry.get("feishu")
    assert entry.available() is True  # lark 已装


def test_feishu_platform_configured_with_owner_scoped_credentials(monkeypatch):
    if not lark_available():
        pytest.skip("lark-oapi 未安装")
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    plugins = PluginManager(registry=Registry())
    plugins.discover_and_load([Path("plugins")], enabled=["feishu-platform"])

    entry = platform_registry.get("feishu")
    cfg = entry.build_config({"enabled": True, "appId": "owner-app", "appSecret": "owner-secret"})

    assert entry.available() is True
    assert entry.configured(cfg) is True
    assert entry.connected(cfg) is True


def test_feishu_owner_scoped_credentials_ignore_process_env(monkeypatch):
    if not lark_available():
        pytest.skip("lark-oapi 未安装")
    monkeypatch.setenv("FEISHU_APP_ID", "global-app")
    monkeypatch.setenv("FEISHU_APP_SECRET", "global-secret")
    monkeypatch.setenv("FEISHU_ALLOWED_USERS", "ou_global_allowed")
    monkeypatch.setenv("FEISHU_BLOCKED_USERS", "ou_global_blocked")
    monkeypatch.setenv("FEISHU_ADMINS", "ou_global_admin")
    plugins = PluginManager(registry=Registry())
    plugins.discover_and_load([Path("plugins")], enabled=["feishu-platform"])

    entry = platform_registry.get("feishu")
    missing_secret = entry.build_config({"enabled": True, "appId": "owner-app"}, include_env=False)
    cfg = entry.build_config(
        {"enabled": True, "appId": "owner-app", "appSecret": "owner-secret"},
        include_env=False,
    )
    channel = FeishuChannel(cfg)

    assert entry.configured(missing_secret) is False
    assert entry.configured(cfg) is True
    assert cfg.extra["appId"] == "owner-app"
    assert cfg.extra["appSecret"] == "owner-secret"
    assert channel.settings.app_id == "owner-app"
    assert channel.settings.app_secret == "owner-secret"
    assert channel.settings.allowed_users == set()
    assert channel.settings.blocked_users == set()
    assert channel.settings.admins == set()


# --------------------------------------------------------------------------- #
# 生命周期 / 回归
# --------------------------------------------------------------------------- #
async def test_start_requires_config(monkeypatch):
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    ch = FeishuChannel(PlatformConfig(name="feishu", extra={}))

    async def handler(env):  # pragma: no cover
        if False:
            yield

    with pytest.raises(RuntimeError):
        await ch.start(handler)


async def test_serve_ws_runs_on_fresh_loop_under_running_loop():
    """回归:start() 在「运行中的事件循环」里被调用时，ws 线程必须换独立 loop。

    复现真机 bug:lark 的 ws.client 在 import 时把模块级全局 loop 抓成了当前正在运行的主 loop。
    若 _serve_ws 不覆盖它，ws_client.start() 里的 loop.run_until_complete(...) 会抛
    'This event loop is already running'(网关启动时必现，原单测未覆盖)。
    """
    pytest.importorskip("lark_oapi")
    import threading

    import lark_oapi.ws.client as ws_mod

    main_loop = asyncio.get_running_loop()
    original = ws_mod.loop
    ws_mod.loop = main_loop  # 制造 bug 条件:全局 loop = 正在运行的主 loop
    try:
        seen: dict = {}

        class FakeWS:
            def start(self):
                import lark_oapi.ws.client as m

                seen["module_loop_is_main"] = m.loop is main_loop
                m.loop.run_until_complete(asyncio.sleep(0))  # 若是运行中的主 loop 会抛
                seen["ran"] = True

        t = threading.Thread(target=_serve_ws, args=(FakeWS(),), daemon=True)
        t.start()
        t.join(2)
        assert seen.get("module_loop_is_main") is False  # 已换成线程独立的新 loop
        assert seen.get("ran") is True                   # run_until_complete 成功，无 already-running
    finally:
        ws_mod.loop = original


def test_detect_send_intent_for_outbound_file_hint():
    """用户表达发送意图时才注入「可发文件」能力提示（否定句/闲聊不注入）。"""
    from plugins.platforms.feishu import protocol as proto
    assert proto.detect_send_intent("把报告发给我")
    assert proto.detect_send_intent("帮我查找并发送 1.png")
    assert not proto.detect_send_intent("不要发文件给我")
    assert not proto.detect_send_intent("今天天气怎么样")
