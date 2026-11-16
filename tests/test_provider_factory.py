"""Provider factory dispatch tests."""

from __future__ import annotations

import pytest

from crew.app import build_provider, build_provider_for_profile
from crew.core.types import Message
from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider
from crew.state.config import Config, ModelProfile


def test_build_provider_for_profile_dispatches_anthropic():
    profile = ModelProfile(id="claude", provider="anthropic", api_key="sk", model="claude-test")
    provider = build_provider_for_profile(profile)
    assert isinstance(provider, AnthropicProvider)
    assert provider.vision is False


def test_build_provider_uses_profile_capabilities_for_vision():
    profile = ModelProfile(
        id="vision",
        api_key="sk",
        model="vision-model",
        vision=False,
        capabilities=["text", "tools", "vision"],
    )

    provider = build_provider_for_profile(profile)

    assert isinstance(provider, OpenAIProvider)
    assert provider.vision is True


def test_build_provider_defaults_openai():
    cfg = Config(api_key="sk", provider="openai", model="gpt-test")
    provider = build_provider(cfg)
    assert isinstance(provider, OpenAIProvider)


def test_build_provider_rejects_unknown_provider():
    cfg = Config(api_key="sk", provider="wat", model="x")
    with pytest.raises(ValueError, match="未知模型 provider"):
        build_provider(cfg)


# --------------------------------------------------------------------------- #
# B5：凭据每次请求解析（禁跨请求缓存），飞行中的流不受配置变更影响
# --------------------------------------------------------------------------- #
class _CreateSentinel(Exception):
    """假 create 抛出，用来截获 provider 实际发出的请求头。"""


async def test_openai_provider_resolves_key_per_request(monkeypatch):
    import openai.resources.chat.chat as chat_mod

    captured: list[dict | None] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs.get("extra_headers"))
            raise _CreateSentinel()

    monkeypatch.setattr(
        chat_mod.AsyncChat,
        "completions",
        property(lambda _self: _FakeCompletions()),
    )

    keys = iter(["key-A", "key-B"])
    provider = OpenAIProvider(api_key="initial", api_key_resolver=lambda: next(keys))
    for _ in range(2):
        with pytest.raises(Exception):  # noqa: B017 - 哨兵从 create 穿透 ProviderError 包装
            await provider.chat([Message.user("hi")])

    # 每次请求各取一次快照：第二次请求拿到变更后的 key
    assert captured == [
        {"Authorization": "Bearer key-A"},
        {"Authorization": "Bearer key-B"},
    ]


async def test_openai_provider_without_resolver_keeps_static_key(monkeypatch):
    import openai.resources.chat.chat as chat_mod

    captured: list[dict | None] = []

    class _FakeCompletions:
        async def create(self, **kwargs):
            captured.append(kwargs.get("extra_headers"))
            raise _CreateSentinel()

    monkeypatch.setattr(
        chat_mod.AsyncChat,
        "completions",
        property(lambda _self: _FakeCompletions()),
    )

    provider = OpenAIProvider(api_key="static-key")
    with pytest.raises(Exception):  # noqa: B017
        await provider.chat([Message.user("hi")])

    assert captured == [None]  # 无 resolver：不覆盖 SDK 默认凭据


def test_anthropic_provider_resolves_key_per_request():
    keys = iter(["key-A", "key-B"])
    provider = AnthropicProvider(api_key="initial", api_key_resolver=lambda: next(keys))

    assert provider._headers["x-api-key"] == "initial"
    provider._sync_api_key()
    assert provider._headers["x-api-key"] == "key-A"
    snapshot = provider._headers
    provider._sync_api_key()
    # 变化时整体替换 headers 对象：已发起的请求持有旧 dict，飞行中的流不受影响
    assert provider._headers["x-api-key"] == "key-B"
    assert provider._headers is not snapshot
    assert snapshot["x-api-key"] == "key-A"


def test_build_provider_for_profile_resolver_rereads_key_chain(monkeypatch):
    """装配出的 provider 持有 resolver：凭证变更后下一次 resolve 即取新值。"""
    monkeypatch.setattr("crew.state.config.read_stored_key", lambda owner, pid: "")
    monkeypatch.delenv("B5_TEST_KEY", raising=False)
    profile = ModelProfile(id="b5", api_key="stale", api_key_env="B5_TEST_KEY", model="gpt-test")

    provider = build_provider_for_profile(profile)

    assert isinstance(provider, OpenAIProvider)
    monkeypatch.setenv("B5_TEST_KEY", "fresh-1")
    assert provider._api_key_resolver() == "fresh-1"
    monkeypatch.setenv("B5_TEST_KEY", "fresh-2")
    assert provider._api_key_resolver() == "fresh-2"
