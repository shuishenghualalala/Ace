"""微信（个人号 iLink）平台插件测试：配置解析、访问控制、协议(加密/解析/分块)、分发回包、注册。

不触网：协议纯函数直接断言；iLink HTTP 调用用 monkeypatch；媒体下载/回包 monkeypatch。
长轮询（需真实 iLink token）不在单测范围。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from crew.core.envelope import ResponseChunk
from crew.gateway.platform_registry import PlatformConfig, platform_registry
from crew.gateway.routers.channels import create_channels_router
from crew.plugins.manager import PluginManager
from crew.tools.registry import Registry
from plugins.platforms.weixin import ilink
from plugins.platforms.weixin.adapter import (
    WeixinChannel,
    detect_send_intent,
    extract_file_paths,
    strip_file_syntax,
)
from plugins.platforms.weixin.config import WeixinSettings, decide_access

_CREDS = {"accountId": "bot_1", "token": "tok_1", "workspaceId": "ws1"}


@pytest.fixture(autouse=True)
def _restore_platform_registry():
    old_entries = list(platform_registry.all_entries())
    yield
    platform_registry._entries.clear()
    for entry in old_entries:
        platform_registry.register(entry)


def _channel(**extra):
    return WeixinChannel(PlatformConfig(name="weixin", extra={**_CREDS, **extra}))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
def test_settings_env_and_extra(monkeypatch):
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "env_bot")
    monkeypatch.setenv("WEIXIN_TOKEN", "env_tok")
    s = WeixinSettings.from_extra({})
    assert s.configured and s.account_id == "env_bot" and s.token == "env_tok"

    s2 = WeixinSettings.from_extra({"accountId": "cfg_bot", "token": "cfg_tok", "baseUrl": "https://x"})
    assert s2.account_id == "cfg_bot" and s2.base_url == "https://x"
    assert WeixinSettings.from_extra({}).base_url == ilink.ILINK_BASE_URL


def test_settings_access_fields(monkeypatch):
    monkeypatch.setenv("WEIXIN_ALLOWED_USERS", "u_a, u_b")
    monkeypatch.setenv("WEIXIN_GROUP_ALLOWED_USERS", "g_a")
    s = WeixinSettings.from_extra({"dmPolicy": "allowlist", "groupPolicy": "allowlist",
                                   "allowedUsers": ["u_x"]})
    assert s.dm_policy == "allowlist" and s.group_policy == "allowlist"
    assert s.allowed_users == {"u_a", "u_b", "u_x"} and s.group_allowed_users == {"g_a"}


def test_settings_invalid_policy_falls_back():
    s = WeixinSettings.from_extra({"dmPolicy": "bogus", "groupPolicy": "weird"})
    assert s.dm_policy == "open" and s.group_policy == "disabled"


def test_settings_configured_requires_account_and_token():
    assert WeixinSettings.from_extra({"accountId": "a"}).configured is False
    assert WeixinSettings.from_extra({"token": "t"}).configured is False
    assert WeixinSettings.from_extra(_CREDS).configured is True


def test_settings_warnings_for_group_policy():
    s = WeixinSettings.from_extra({**_CREDS, "groupPolicy": "open"})
    assert s.collect_warnings()
    assert WeixinSettings.from_extra(_CREDS).collect_warnings() == []


# --------------------------------------------------------------------------- #
# 访问控制
# --------------------------------------------------------------------------- #
def _settings(**kw):
    return WeixinSettings.from_extra({**_CREDS, **kw})


def test_decide_self_echo_dropped():
    ok, reason = decide_access(sender_id="bot_1", account_id="bot_1",
                               chat_type="dm", chat_id="bot_1", settings=_settings())
    assert ok is False and reason == "self-echo"


def test_decide_dm_open():
    s = _settings()
    assert decide_access(sender_id="u1", account_id="bot_1", chat_type="dm", chat_id="u1", settings=s)[0] is True


def test_decide_dm_allowlist():
    s = _settings(dmPolicy="allowlist", allowedUsers=["u_ok"])
    assert decide_access(sender_id="u_ok", account_id="bot_1", chat_type="dm", chat_id="u_ok", settings=s)[0] is True
    ok, reason = decide_access(sender_id="u_no", account_id="bot_1", chat_type="dm", chat_id="u_no", settings=s)
    assert ok is False and reason == "dm-not-allowlisted"


def test_decide_dm_disabled():
    s = _settings(dmPolicy="disabled")
    ok, reason = decide_access(sender_id="u1", account_id="bot_1", chat_type="dm", chat_id="u1", settings=s)
    assert ok is False and reason == "dm-disabled"


def test_decide_group_policies():
    s_dis = _settings(groupPolicy="disabled")
    assert decide_access(sender_id="u1", account_id="bot_1", chat_type="group", chat_id="g1", settings=s_dis)[0] is False

    s_allow = _settings(groupPolicy="allowlist", groupAllowedUsers=["g_ok"])
    assert decide_access(sender_id="u1", account_id="bot_1", chat_type="group", chat_id="g_ok", settings=s_allow)[0] is True
    ok, reason = decide_access(sender_id="u1", account_id="bot_1", chat_type="group", chat_id="g_no", settings=s_allow)
    assert ok is False and reason == "group-not-allowlisted"

    s_open = _settings(groupPolicy="open")
    assert decide_access(sender_id="u1", account_id="bot_1", chat_type="group", chat_id="g1", settings=s_open)[0] is True


# --------------------------------------------------------------------------- #
# 协议：加密
# --------------------------------------------------------------------------- #
def test_aes_roundtrip():
    key = b"0123456789abcdef"
    cipher = ilink.aes128_ecb_encrypt(b"hello weixin", key)
    assert ilink.aes128_ecb_decrypt(cipher, key) == b"hello weixin"


def test_parse_aes_key_formats():
    import base64

    raw16 = b"\x01" * 16
    assert ilink._parse_aes_key(base64.b64encode(raw16).decode()) == raw16
    hex32 = "ab" * 16
    assert ilink._parse_aes_key(base64.b64encode(hex32.encode("ascii")).decode()) == bytes.fromhex(hex32)
    with pytest.raises(ValueError):
        ilink._parse_aes_key(base64.b64encode(b"short").decode())


# --------------------------------------------------------------------------- #
# 协议：入站解析
# --------------------------------------------------------------------------- #
def test_extract_text():
    assert ilink.extract_text([{"type": 1, "text_item": {"text": "hi"}}]) == "hi"
    assert ilink.extract_text([{"type": 3, "voice_item": {"text": "voice-tts"}}]) == "voice-tts"
    ref = ilink.extract_text([{"type": 1, "text_item": {"text": "看看"},
                               "ref_msg": {"title": "图片.jpg", "message_item": {"type": 2}}}])
    assert "图片.jpg" in ref and "看看" in ref
    assert ilink.extract_text([{"type": 2, "image_item": {}}]) == ""


def test_guess_chat_type():
    dm = {"from_user_id": "u1", "to_user_id": "bot_1", "msg_type": 1}
    assert ilink.guess_chat_type(dm, "bot_1") == ("dm", "u1")
    group = {"from_user_id": "u1", "to_user_id": "bot_1", "room_id": "room_1", "msg_type": 1}
    assert ilink.guess_chat_type(group, "bot_1") == ("group", "room_1")


def test_is_stale_session_ret():
    assert ilink.is_stale_session_ret(-2, None, "unknown error") is True
    assert ilink.is_stale_session_ret(-2, None, "rate limited") is False
    assert ilink.is_stale_session_ret(0, 0, "") is False


# --------------------------------------------------------------------------- #
# 协议：出站文本
# --------------------------------------------------------------------------- #
def test_split_text_compact_single_message():
    long_text = "这是一条很长的消息。" * 50
    chunks = ilink.split_text_for_delivery(long_text, 2000, False)
    assert len(chunks) == 1 and chunks[0] == long_text


def test_split_text_oversized_chunks():
    big = "A" * 5000
    chunks = ilink.split_text_for_delivery(big, 2000, False)
    assert sum(len(c) for c in chunks) == 5000
    assert all(len(c) <= 2000 for c in chunks)


def test_split_text_per_line():
    content = "第一行\n第二行"
    assert ilink.split_text_for_delivery(content, 2000, True) == ["第一行", "第二行"]


def test_format_message_normalizes_blank_runs():
    raw = "标题\n\n\n\n正文"
    assert ilink.format_message(raw) == "标题\n\n正文"


# --------------------------------------------------------------------------- #
# 协议：持久化
# --------------------------------------------------------------------------- #
def test_account_save_load(tmp_path):
    ilink.save_account(tmp_path, account_id="bot_1", token="tok", base_url="https://x", user_id="u")
    data = ilink.load_account(tmp_path, "bot_1")
    assert data["token"] == "tok" and data["base_url"] == "https://x"
    assert ilink.load_account(tmp_path, "missing") is None


def test_context_token_store_persist(tmp_path):
    store = ilink.ContextTokenStore(tmp_path)
    store.set("bot_1", "u1", "ctx-abc")
    store2 = ilink.ContextTokenStore(tmp_path)
    store2.restore("bot_1")
    assert store2.get("bot_1", "u1") == "ctx-abc"
    store2.drop("bot_1", "u1")
    assert store2.get("bot_1", "u1") is None


def test_build_outbound_media_item(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(b"png")
    media_type, item = ilink.build_outbound_media_item(
        str(img), encrypted_query_param="p", aes_key_for_api="k",
        ciphertext_size=8, plaintext_size=3, rawfilemd5="md5",
    )
    assert media_type == ilink.MEDIA_IMAGE
    assert item["image_item"]["media"]["encrypt_query_param"] == "p"

    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"pdf")
    media_type, item = ilink.build_outbound_media_item(
        str(doc), encrypted_query_param="p", aes_key_for_api="k",
        ciphertext_size=8, plaintext_size=3, rawfilemd5="md5",
    )
    assert media_type == ilink.MEDIA_FILE
    assert item["file_item"]["file_name"] == "a.pdf"


# --------------------------------------------------------------------------- #
# 适配器：路径提取 / 意图
# --------------------------------------------------------------------------- #
def test_extract_and_strip_file_paths(tmp_path):
    f = tmp_path / "out.pdf"
    f.write_bytes(b"x")
    paths = extract_file_paths(f"[FILE:{f}]", exists=lambda _: True)
    assert paths == [str(f)]
    assert strip_file_syntax(f"[FILE:{f}] 正文") == "正文"
    assert extract_file_paths(f"[FILE:{f}]", exists=lambda _: False) == []


def test_detect_send_intent():
    assert detect_send_intent("把报告发给我")
    assert detect_send_intent("帮我发送文件")
    assert not detect_send_intent("不要发文件给我")
    assert not detect_send_intent("今天天气怎么样")


# --------------------------------------------------------------------------- #
# 适配器：去重 / 分发 / 回包
# --------------------------------------------------------------------------- #
def test_dedupe():
    ch = _channel()
    assert ch._dedupe("m1") is False
    assert ch._dedupe("m1") is True
    assert ch._dedupe("m2") is False


async def test_handle_round_trip():
    ch = _channel()
    sent = []
    seen = []

    async def fake_send(chat_id, content, ctx):
        sent.append((chat_id, content))

    ch._send_text_chunks = fake_send

    async def handler(env):
        seen.append(env)
        yield ResponseChunk.delta(env.request_id, "...")
        yield ResponseChunk.final(env.request_id, f"echo:{env.query}")

    ch._handler = handler
    parsed = {"message_id": "m1", "sender_id": "u1", "chat_id": "u1",
              "chat_type": "dm", "text": "你好", "context_token": None, "resources": []}
    await ch._handle(parsed)

    assert sent == [("u1", "echo:你好")]
    env = seen[0]
    assert env.channel == "weixin" and env.user_id == "u1" and env.workspace_id == "ws1"
    assert env.session_id.startswith("agent:main:weixin:dm:u1")
    assert env.params["platform_chat_id"] == "u1"
    assert env.params["platform_uid"] == "u1"


async def test_handle_error_sent_back():
    ch = _channel()
    sent = []

    async def fake_send(chat_id, content, ctx):
        sent.append((chat_id, content))

    ch._send_text_chunks = fake_send

    async def handler(env):
        yield ResponseChunk.error(env.request_id, "boom")

    ch._handler = handler
    await ch._handle({"message_id": "m1", "sender_id": "u1", "chat_id": "u1",
                      "chat_type": "dm", "text": "hi", "context_token": None, "resources": []})
    assert sent == [("u1", "boom")]


async def test_process_message_access_control(monkeypatch):
    ch = _channel(dmPolicy="allowlist", allowedUsers=["u_ok"])
    ch._poll_session = object()
    handled = []

    async def fake_handle(parsed):
        handled.append(parsed)

    monkeypatch.setattr(ch, "_handle", fake_handle)

    allowed_msg = {"from_user_id": "u_ok", "message_id": "m1",
                   "to_user_id": "bot_1", "msg_type": 1,
                   "item_list": [{"type": 1, "text_item": {"text": "hi"}}]}
    denied_msg = {"from_user_id": "u_no", "message_id": "m2",
                  "to_user_id": "bot_1", "msg_type": 1,
                  "item_list": [{"type": 1, "text_item": {"text": "hi"}}]}
    self_msg = {"from_user_id": "bot_1", "message_id": "m3",
                "to_user_id": "u_ok", "msg_type": 1,
                "item_list": [{"type": 1, "text_item": {"text": "hi"}}]}

    await ch._process_message(self_msg)
    await ch._process_message(denied_msg)
    await ch._process_message(allowed_msg)

    assert len(handled) == 1 and handled[0]["sender_id"] == "u_ok"


async def test_process_message_text_dedupe(monkeypatch):
    ch = _channel()
    ch._poll_session = object()
    handled = []

    async def fake_handle(parsed):
        handled.append(parsed)

    monkeypatch.setattr(ch, "_handle", fake_handle)
    msg = {"from_user_id": "u1", "message_id": "m1", "to_user_id": "bot_1", "msg_type": 1,
           "item_list": [{"type": 1, "text_item": {"text": "重复"}}]}
    await ch._process_message(msg)
    await ch._process_message(msg)
    assert len(handled) == 1


async def test_send_to_target():
    ch = _channel()
    ch._send_session = object()
    ch._token_store.set("bot_1", "u_target", "ctx")
    sent = []

    async def fake_send(chat_id, content, ctx):
        sent.append((chat_id, content, ctx))

    ch._send_text_chunks = fake_send

    ok = await ch.send_to_target("u_target", "主动通知")
    assert ok is True
    assert sent[0][0] == "u_target" and sent[0][2] == "ctx"


async def test_send_to_target_without_session_returns_false():
    ch = _channel()
    ch._send_session = None
    assert await ch.send_to_target("u_target", "主动通知") is False


async def test_start_requires_account_and_token(monkeypatch):
    monkeypatch.delenv("WEIXIN_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("WEIXIN_TOKEN", raising=False)
    ch = WeixinChannel(PlatformConfig(name="weixin", extra={}))

    async def handler(env):  # pragma: no cover
        if False:
            yield

    with pytest.raises(RuntimeError, match="WEIXIN_ACCOUNT_ID"):
        await ch.start(handler)
    ch2 = WeixinChannel(PlatformConfig(name="weixin", extra={"accountId": "bot_1"}))
    with pytest.raises(RuntimeError, match="WEIXIN_TOKEN"):
        await ch2.start(handler)


async def test_start_probe_marks_connected(tmp_path, monkeypatch):
    """启动握手成功：status_detail 报 connected，stop 后回落。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))

    async def fake_get_updates(session, *, base_url, token, sync_buf, timeout_ms):
        return {"ret": 0, "get_updates_buf": sync_buf or "buf_1", "msgs": []}

    monkeypatch.setattr(ilink, "get_updates", fake_get_updates)
    ch = _channel()

    async def handler(env):  # pragma: no cover
        if False:
            yield

    assert ch.status_detail()["connected"] is False
    await ch.start(handler)
    try:
        detail = ch.status_detail()
        assert detail["connected"] is True
        assert detail["last_error"] == ""
        assert detail["running"] is True
    finally:
        await ch.stop()
    assert ch.status_detail()["connected"] is False


async def test_start_probe_session_expired_raises(tmp_path, monkeypatch):
    """握手发现会话过期：启动直接报错，提示重新扫码。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))

    async def fake_get_updates(session, *, base_url, token, sync_buf, timeout_ms):
        return {"ret": ilink.SESSION_EXPIRED_ERRCODE, "errcode": 0, "errmsg": "session expired"}

    monkeypatch.setattr(ilink, "get_updates", fake_get_updates)
    ch = _channel()

    async def handler(env):  # pragma: no cover
        if False:
            yield

    with pytest.raises(RuntimeError, match="重新扫码登录"):
        await ch.start(handler)
    assert ch.status_detail()["connected"] is False
    await ch.stop()


async def test_poll_failure_marks_disconnected(tmp_path, monkeypatch):
    """长轮询持续失败：connected 回落并记录 last_error。"""
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    calls = {"n": 0}

    async def fake_get_updates(session, *, base_url, token, sync_buf, timeout_ms):
        calls["n"] += 1
        if calls["n"] == 1:  # 启动握手
            return {"ret": 0, "get_updates_buf": "buf_1", "msgs": []}
        return {"ret": -1, "errcode": 0, "errmsg": "boom"}

    monkeypatch.setattr(ilink, "get_updates", fake_get_updates)
    monkeypatch.setattr(ilink, "RETRY_DELAY_SECONDS", 0)
    ch = _channel()

    async def handler(env):  # pragma: no cover
        if False:
            yield

    await ch.start(handler)
    try:
        assert ch.status_detail()["connected"] is True
        for _ in range(50):
            await asyncio.sleep(0.05)
            if not ch.status_detail()["connected"]:
                break
        detail = ch.status_detail()
        assert detail["connected"] is False
        assert "boom" in detail["last_error"]
    finally:
        await ch.stop()


# --------------------------------------------------------------------------- #
# 扫码登录接口
# --------------------------------------------------------------------------- #
def _qr_client() -> TestClient:
    """仅挂 channels 路由的轻量 app，扫码接口不触碰 crew 内部状态。"""
    app = FastAPI()
    router = create_channels_router(SimpleNamespace(), dispatcher=None, channel_manager=None)
    app.include_router(router)
    return TestClient(app)


def test_weixin_qr_login_start_returns_image(tmp_path, monkeypatch):
    from crew.gateway.routers import channels as ch_mod

    monkeypatch.setattr(ch_mod, "_WEIXIN_QR_STATES", {})

    async def fake_fetch():
        return ("qr_abc", "https://wx.example/scan", "https://wx.example/scan")

    monkeypatch.setattr(ilink, "fetch_qr_code", fake_fetch)
    monkeypatch.setattr(ilink, "render_qr_svg", lambda data: "<svg/>")
    client = _qr_client()

    resp = client.post("/api/platforms/weixin/qr-login/start")
    body = resp.json()
    assert resp.status_code == 200 and body["ok"] is True
    assert body["qr_id"] == "qr_abc"
    assert body["qr_image"].startswith("data:image/svg+xml;base64,")
    assert body["qrcode_url"] == "https://wx.example/scan"


def test_weixin_qr_login_non_weixin_rejected():
    client = _qr_client()
    resp = client.post("/api/platforms/feishu/qr-login/start")
    assert resp.status_code == 400


def test_weixin_qr_login_confirm_persists_account(tmp_path, monkeypatch):
    from crew.gateway.routers import channels as ch_mod

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    monkeypatch.setattr(ch_mod, "_WEIXIN_QR_STATES", {"qr_abc": {"base_url": "https://ilink", "updated_at": 0}})

    async def fake_poll(qr_id, base_url):
        return {
            "status": "confirmed",
            "ilink_bot_id": "bot_qr",
            "bot_token": "tok_qr",
            "baseurl": "https://api.example",
            "ilink_user_id": "u_qr",
        }

    monkeypatch.setattr(ilink, "poll_qr_status", fake_poll)
    client = _qr_client()

    resp = client.post("/api/platforms/weixin/qr-login/status", json={"qr_id": "qr_abc"})
    body = resp.json()
    assert body["status"] == "confirmed" and body["account_id"] == "bot_qr"

    settings = WeixinSettings.from_extra({})
    persisted = ilink.load_account(settings.accounts_dir(), "bot_qr")
    assert persisted and persisted["token"] == "tok_qr"


def test_weixin_qr_login_pending_when_poll_fails(monkeypatch):
    from crew.gateway.routers import channels as ch_mod

    monkeypatch.setattr(ch_mod, "_WEIXIN_QR_STATES", {})

    async def fake_poll(qr_id, base_url):
        return None

    monkeypatch.setattr(ilink, "poll_qr_status", fake_poll)
    client = _qr_client()

    resp = client.post("/api/platforms/weixin/qr-login/status", json={"qr_id": "qr_x"})
    assert resp.json()["status"] == "pending"


def test_weixin_qr_login_redirect_tracks_base_url(monkeypatch):
    from crew.gateway.routers import channels as ch_mod

    monkeypatch.setattr(ch_mod, "_WEIXIN_QR_STATES", {"qr_abc": {"base_url": "https://ilink", "updated_at": 0}})

    async def fake_poll(qr_id, base_url):
        return {"status": "scaned_but_redirect", "redirect_host": "wx-redirect.example"}

    monkeypatch.setattr(ilink, "poll_qr_status", fake_poll)
    client = _qr_client()

    resp = client.post("/api/platforms/weixin/qr-login/status", json={"qr_id": "qr_abc"})
    assert resp.json()["status"] == "scaned"
    assert ch_mod._WEIXIN_QR_STATES["qr_abc"]["base_url"] == "https://wx-redirect.example"


# --------------------------------------------------------------------------- #
# 注册
# --------------------------------------------------------------------------- #
def test_weixin_platform_registers(monkeypatch):
    monkeypatch.setenv("WEIXIN_ACCOUNT_ID", "bot_env")
    monkeypatch.setenv("WEIXIN_TOKEN", "tok_env")
    plugins = PluginManager(registry=Registry())
    plugins.discover_and_load([Path("plugins")], enabled=["weixin-platform"])
    loaded = [p for p in plugins.loaded_plugins if p.manifest.name == "weixin-platform"][0]
    assert loaded.enabled and loaded.platforms_registered == ["weixin"]

    entry = platform_registry.get("weixin")
    assert entry.available() is True
    cfg = entry.build_config({"enabled": True})
    assert entry.configured(cfg) is True


def test_adapter_loads_token_from_account_file(monkeypatch):
    """仅配 account_id 时，adapter 从账号文件补全 token/base_url。"""
    monkeypatch.delenv("WEIXIN_TOKEN", raising=False)
    monkeypatch.setattr(
        ilink, "load_account",
        lambda *a, **k: {"token": "file_tok", "base_url": "https://cdn.example"},
    )
    ch = WeixinChannel(PlatformConfig(name="weixin", extra={"accountId": "bot_file"}))
    assert ch._token == "file_tok"
    assert ch._base_url == "https://cdn.example"
