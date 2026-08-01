"""Provider factory dispatch tests."""

from __future__ import annotations

import pytest

from crew.app import build_provider, build_provider_for_profile
from crew.providers.anthropic_provider import AnthropicProvider
from crew.providers.openai_provider import OpenAIProvider
from crew.state.config import Config, ModelProfile


def test_build_provider_for_profile_dispatches_anthropic():
    profile = ModelProfile(id="claude", provider="anthropic", api_key="sk", model="claude-test")
    provider = build_provider_for_profile(profile)
    assert isinstance(provider, AnthropicProvider)


def test_build_provider_defaults_openai():
    cfg = Config(api_key="sk", provider="openai", model="gpt-test")
    provider = build_provider(cfg)
    assert isinstance(provider, OpenAIProvider)


def test_build_provider_rejects_unknown_provider():
    cfg = Config(api_key="sk", provider="wat", model="x")
    with pytest.raises(ValueError, match="未知模型 provider"):
        build_provider(cfg)
