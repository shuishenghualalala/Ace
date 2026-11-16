"""厂商档案（crew.providers.vendors）与 OpenAIProvider compat 接入单测。

不联网：SDK 调用全部 monkeypatch，断言 payload 组装 / 流解析 / 装配默认值。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crew.app import build_provider, build_provider_for_profile
from crew.core.types import Message
from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider, _thinking_extra_body
from crew.providers.vendors import (
    REASONING_LEVELS,
    VENDORS,
    VendorCompat,
    compat_for_model,
    get_vendor,
    resolve_vendor,
)
from crew.state.config import Config, ModelProfile


# ---------------------------------------------------------------------------
# 档案表完整性
# ---------------------------------------------------------------------------

def test_vendor_table_integrity():
    assert len(VENDORS) >= 9
    for vendor in VENDORS.values():
        assert vendor.base_url.startswith("https://")
        assert vendor.api_key_env
        assert vendor.protocol in ("openai", "anthropic")
        assert vendor.models
        assert len({m.id for m in vendor.models}) == len(vendor.models)


def test_reasoning_levels_covered():
    assert set(REASONING_LEVELS) == {"minimal", "low", "medium", "high", "xhigh", "max"}


# ---------------------------------------------------------------------------
# 厂商解析
# ---------------------------------------------------------------------------

def test_resolve_by_provider_id():
    assert resolve_vendor("deepseek").id == "deepseek"
    assert resolve_vendor("Kimi-Coding").protocol == "anthropic"
    assert resolve_vendor("deepseek").compat.thinking_format == "deepseek"


def test_resolve_by_base_url_host():
    assert resolve_vendor("openai", "https://api.deepseek.com/v1", "gpt-x").id == "deepseek"
    assert resolve_vendor("openai", "https://api.minimaxi.com/anthropic", "x").id == "minimax-cn"
    assert resolve_vendor("", "https://api.moonshot.cn/v1", "x").id == "moonshotai-cn"


def test_resolve_by_model_id_for_generic_provider():
    assert resolve_vendor("openai", "", "deepseek-reasoner").id == "deepseek"
    assert resolve_vendor("anthropic", "", "kimi-k3").id == "kimi-coding"


def test_resolve_does_not_hijack_explicit_protocol():
    # provider 明确是 anthropic 时，模型 id 只能在 anthropic 协议厂商里匹配
    assert resolve_vendor("anthropic", "", "glm-4.6") is None
    assert resolve_vendor("openai", "", "MiniMax-M3") is None


def test_resolve_unknown_provider_id_returns_none():
    assert resolve_vendor("wat", "", "deepseek-chat") is None


def test_match_host_wins_over_model():
    assert resolve_vendor("openai", "https://api.deepseek.com", "glm-4.6").id == "deepseek"


def test_get_vendor_none():
    assert get_vendor("") is None
    assert get_vendor(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# compat_for_model：按模型修正
# ---------------------------------------------------------------------------

def test_compat_reasoning_model_keeps_format():
    compat = compat_for_model(VENDORS["deepseek"], "deepseek-reasoner")
    assert compat.thinking_format == "deepseek"
    assert compat.requires_reasoning_echo is True


def test_compat_non_reasoning_model_disables_thinking():
    compat = compat_for_model(VENDORS["deepseek"], "deepseek-chat")
    assert compat.thinking_format == "none"
    assert compat.requires_reasoning_echo is False


def test_compat_fills_level_map():
    compat = compat_for_model(VENDORS["qwen-token-plan"], "qwen3.8-max")
    assert compat.thinking_format == "qwen"
    assert compat.thinking_level_map["high"] is None
    assert compat.thinking_level_map["low"] == "low"


def test_compat_unknown_model_keeps_vendor_compat():
    compat = compat_for_model(VENDORS["zai"], "some-new-glm")
    assert compat.thinking_format == "zai"


# ---------------------------------------------------------------------------
# 思考参数翻译
# ---------------------------------------------------------------------------

def test_thinking_none_mode_sends_nothing():
    compat = VendorCompat(thinking_format="deepseek")
    assert _thinking_extra_body(compat, None) == {}


def test_thinking_off_deepseek_and_zai():
    assert _thinking_extra_body(VendorCompat(thinking_format="deepseek"), "off") == {"thinking": {"type": "disabled"}}
    assert _thinking_extra_body(VendorCompat(thinking_format="deepseek"), "disabled") == {"thinking": {"type": "disabled"}}
    assert _thinking_extra_body(VendorCompat(thinking_format="zai"), "off") == {"thinking": {"type": "disabled"}}


def test_thinking_off_qwen():
    assert _thinking_extra_body(VendorCompat(thinking_format="qwen"), "off") == {"enable_thinking": False}


def test_thinking_level_deepseek_sends_effort():
    body = _thinking_extra_body(VendorCompat(thinking_format="deepseek"), "high")
    assert body == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}


def test_thinking_level_zai_omits_effort():
    body = _thinking_extra_body(VendorCompat(thinking_format="zai"), "high")
    assert body == {"thinking": {"type": "enabled"}}


def test_thinking_level_qwen_uses_level_map():
    compat = VendorCompat(thinking_format="qwen", thinking_level_map={"high": None, "low": "low"})
    assert _thinking_extra_body(compat, "high") == {"enable_thinking": True}
    assert _thinking_extra_body(compat, "low") == {"enable_thinking": True, "reasoning_effort": "low"}


def test_thinking_format_none_sends_nothing_even_with_level():
    assert _thinking_extra_body(VendorCompat(), "high") == {}
    assert _thinking_extra_body(VendorCompat(), "not-a-level") == {}


# ---------------------------------------------------------------------------
# chat / stream_chat payload（monkeypatch SDK）
# ---------------------------------------------------------------------------

def _provider(vendor_id: str, model: str) -> OpenAIProvider:
    vendor = VENDORS[vendor_id]
    return OpenAIProvider(
        api_key="sk-test",
        model=model,
        compat=compat_for_model(vendor, model),
    )


async def _capture_chat(provider: OpenAIProvider, *args, **kwargs):
    captured: dict = {}

    async def fake_create(**kw):
        captured.update(kw)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
            usage=None,
        )

    provider._client.chat.completions.create = fake_create  # type: ignore[method-assign]
    response = await provider.chat(*args, **kwargs)
    return captured, response


@pytest.mark.asyncio
async def test_chat_deepseek_off_sends_thinking_disabled():
    captured, _ = await _capture_chat(_provider("deepseek", "deepseek-reasoner"), [Message.user("hi")], reasoning_mode="off")
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}


@pytest.mark.asyncio
async def test_chat_deepseek_default_sends_no_thinking():
    captured, _ = await _capture_chat(_provider("deepseek", "deepseek-reasoner"), [Message.user("hi")])
    assert "extra_body" not in captured


@pytest.mark.asyncio
async def test_chat_deepseek_chat_sends_no_thinking():
    captured, _ = await _capture_chat(_provider("deepseek", "deepseek-chat"), [Message.user("hi")], reasoning_mode="off")
    assert "extra_body" not in captured


@pytest.mark.asyncio
async def test_chat_zai_level_sends_enabled_without_effort():
    captured, _ = await _capture_chat(_provider("zai", "glm-4.6"), [Message.user("hi")], reasoning_mode="high")
    assert captured["extra_body"] == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_chat_qwen_level_sends_enable_thinking_and_effort():
    captured, _ = await _capture_chat(_provider("qwen-token-plan", "qwen3.7-plus"), [Message.user("hi")], reasoning_mode="max")
    assert captured["extra_body"] == {"enable_thinking": True, "reasoning_effort": "max"}


@pytest.mark.asyncio
async def test_chat_generic_provider_sends_no_thinking():
    p = OpenAIProvider(api_key="sk-test", model="gpt-test")
    captured, _ = await _capture_chat(p, [Message.user("hi")], reasoning_mode="high")
    assert "extra_body" not in captured


@pytest.mark.asyncio
async def test_chat_echoes_reasoning_on_assistant_messages():
    captured, _ = await _capture_chat(
        _provider("deepseek", "deepseek-reasoner"),
        [
            Message(role="assistant", content="之前回复", thinking="之前的思考"),
            Message.user("hi"),
        ],
    )
    assistant = next(m for m in captured["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "之前的思考"


@pytest.mark.asyncio
async def test_chat_echo_fills_empty_reasoning():
    captured, _ = await _capture_chat(
        _provider("deepseek", "deepseek-reasoner"),
        [
            Message(role="assistant", content="之前回复"),
            Message.user("hi"),
        ],
    )
    assistant = next(m for m in captured["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == ""


@pytest.mark.asyncio
async def test_chat_generic_does_not_echo():
    p = OpenAIProvider(api_key="sk-test", model="gpt-test")
    captured, _ = await _capture_chat(p, [Message(role="assistant", content="之前回复"), Message.user("hi")])
    assistant = next(m for m in captured["messages"] if m["role"] == "assistant")
    assert "reasoning_content" not in assistant


def _stream_chunk(reasoning_field: str, value: str):
    delta = SimpleNamespace(content="", tool_calls=None)
    setattr(delta, reasoning_field, value)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)])


async def _collect_stream(provider: OpenAIProvider, chunks):
    async def fake_create(**kw):
        async def gen():
            for c in chunks:
                yield c

        return gen()

    provider._client.chat.completions.create = fake_create  # type: ignore[method-assign]
    return [chunk async for chunk in provider.stream_chat([Message.user("hi")])]


@pytest.mark.asyncio
async def test_stream_parses_reasoning_alias_fields():
    for field_name in ("reasoning_content", "reasoning", "reasoning_text"):
        p = _provider("deepseek", "deepseek-reasoner")
        chunks = await _collect_stream(p, [_stream_chunk(field_name, "思考片段")])
        assert any(c.reasoning_content == "思考片段" for c in chunks), field_name


@pytest.mark.asyncio
async def test_stream_generic_still_parses_reasoning_content():
    p = OpenAIProvider(api_key="sk-test", model="gpt-test")
    chunks = await _collect_stream(p, [_stream_chunk("reasoning_content", "思考")])
    assert any(c.reasoning_content == "思考" for c in chunks)


# ---------------------------------------------------------------------------
# 装配（app.py）
# ---------------------------------------------------------------------------

def test_profile_vendor_gets_default_base_url_and_compat():
    profile = ModelProfile(id="ds", api_key="sk", provider="deepseek", model="deepseek-reasoner")
    provider = build_provider_for_profile(profile)
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://api.deepseek.com"
    assert provider._compat.thinking_format == "deepseek"
    assert provider._compat.requires_reasoning_echo is True


def test_profile_vendor_vision_default_from_catalog():
    vision_profile = ModelProfile(id="ds", api_key="sk", provider="deepseek", model="deepseek-v4-flash-vision-exp")
    assert build_provider_for_profile(vision_profile).vision is True

    text_profile = ModelProfile(id="ds", api_key="sk", provider="deepseek", model="deepseek-v4-pro")
    assert build_provider_for_profile(text_profile).vision is False


def test_profile_vendor_vision_max_pixels_from_catalog():
    """厂商档案收录的 per-model 像素预算随装配注入 provider；未收录模型为 None。"""
    vision_profile = ModelProfile(id="ds", api_key="sk", provider="deepseek", model="deepseek-v4-flash-vision-exp")
    assert build_provider_for_profile(vision_profile).vision_max_pixels == 640_000

    text_profile = ModelProfile(id="ds", api_key="sk", provider="deepseek", model="deepseek-v4-pro")
    assert build_provider_for_profile(text_profile).vision_max_pixels is None


def test_profile_explicit_base_url_wins():
    profile = ModelProfile(id="ds", api_key="sk", provider="deepseek", model="deepseek-chat", base_url="https://proxy.example/v1")
    provider = build_provider_for_profile(profile)
    assert provider.base_url == "https://proxy.example/v1"


def test_profile_vendor_env_key_fallback(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    profile = ModelProfile(id="ds", api_key="", provider="deepseek", model="deepseek-chat")
    provider = build_provider_for_profile(profile)
    assert provider._client.api_key == "sk-env"


def test_profile_configured_key_wins_over_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
    profile = ModelProfile(id="ds", api_key="sk-config", provider="deepseek", model="deepseek-chat")
    provider = build_provider_for_profile(profile)
    assert provider._client.api_key == "sk-config"


def test_profile_anthropic_vendor_url():
    provider = build_provider_for_profile(ModelProfile(id="kc", api_key="sk", provider="kimi-coding", model="kimi-for-coding"))
    assert isinstance(provider, AnthropicProvider)
    assert provider._url == "https://api.kimi.com/coding/v1/messages"

    provider = build_provider_for_profile(ModelProfile(id="mx", api_key="sk", provider="minimax-cn", model="MiniMax-M2.7"))
    assert isinstance(provider, AnthropicProvider)
    assert provider._url == "https://api.minimaxi.com/anthropic/v1/messages"


def test_build_provider_vendor_env_only_key(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-moon")
    cfg = Config(api_key="", provider="moonshotai", model="kimi-k2.6")
    provider = build_provider(cfg)
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://api.moonshot.ai/v1"
    assert provider._client.api_key == "sk-moon"


def test_build_provider_keyless_generic_falls_back_to_fake():
    from crew.core.mocks import FakeProvider

    cfg = Config(api_key="", provider="openai", model="gpt-test")
    assert isinstance(build_provider(cfg), FakeProvider)


def test_build_provider_rejects_unknown_provider():
    cfg = Config(api_key="sk", provider="wat", model="x")
    with pytest.raises(ValueError, match="未知模型 provider"):
        build_provider(cfg)
