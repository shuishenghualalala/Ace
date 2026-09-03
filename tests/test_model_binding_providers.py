"""模型选择装配面回归：会话绑定 / wiki / 团队的 provider 组装。

desktop「选中一个模型 → 下次对话用这个模型」= setSessionModel →
set_session_model_binding（状态记账）→ 下一轮 _resolve_session_provider_profile
解析绑定 profile（has_key 门控）→ build_provider_for_profile 构建。

本文件验证该链路在厂商档案（crew.providers.vendors）与凭证库 key 下行为正确，
并覆盖 wiki / 团队共享的 provider_for_owner 注入点。外援（external agents）是
独立 CLI runtime，不经过 Ace 的 LLMProvider 组装，不在本文件范围。
"""

from __future__ import annotations

import pytest

from crew.app import build_app, build_provider_for_profile
from crew.providers.openai_provider import OpenAIProvider
from crew.state.config import Config
from crew.state.credentials import read_stored_key

SESSION_ID = "agent:main:test:s1"


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture()
def app(crew_home, tmp_path):
    cfg = Config(db_path=str(crew_home / "crew.db"))
    cfg.config_path = str(tmp_path / "config.yaml")
    return build_app(config=cfg, enable_team=False)


def test_session_bound_vendor_model_used_next_turn(app):
    """桌面端链路：回环流量 owner 固定为 "local"（gateway/auth.py）。"""
    crew = app
    crew.add_model(
        {"id": "ds1", "provider": "deepseek", "model": "deepseek-reasoner", "api_key": "sk-ds"},
        owner_account_id="local",
    )
    # 桌面端 setSessionModel → 会话绑定（非 busy 立即生效）
    crew.set_session_model_binding(SESSION_ID, "ds1", owner_account_id="local", busy=False)
    # 下一轮对话：agent 缓存按 session 清掉后重建
    crew.agents.drop(SESSION_ID, owner_account_id="local")
    profile, owns_provider, notice = crew._resolve_session_provider_profile("local", "ds1")
    assert owns_provider is True
    assert notice is None
    assert profile.id == "ds1" and profile.has_key
    provider = build_provider_for_profile(profile, crew.config.stream_read_timeout)
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://api.deepseek.com"
    assert provider._compat.thinking_format == "deepseek"
    assert provider._client.api_key == "sk-ds"
    # 绑定持久：再下一轮读取仍在
    binding = crew.read_session_model_binding(SESSION_ID, owner_account_id="local")
    assert binding["model_profile_id"] == "ds1"
    # key 存在 local owner 作用域的凭证库里，与全局隔离
    assert read_stored_key("local", "ds1") == "sk-ds"


def test_session_binding_builtin_model_global_scope(app):
    """CLI（无 owner 上下文）绑定 config.yaml 共享层模型；key 按加载语义解析。"""
    from crew.state.config import _build_model_profile

    from crew.state.credentials import store_key

    crew = app
    # 与 load_config 相同的构建函数：store/env 解析进 profile.api_key
    store_key("", "ds1", "sk-ds")
    crew.config.model_profiles["ds1"] = _build_model_profile(
        "ds1",
        {
            "api_key_env": "DEEPSEEK_API_KEY",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "builtin": True,
        },
    )
    crew.set_session_model_binding(SESSION_ID, "ds1", busy=False)
    profile, owns_provider, notice = crew._resolve_session_provider_profile("", "ds1")
    assert owns_provider is True and notice is None and profile.id == "ds1"
    assert profile.has_key
    provider = build_provider_for_profile(profile, crew.config.stream_read_timeout)
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://api.deepseek.com"
    assert provider._client.api_key == "sk-ds"
    binding = crew.read_session_model_binding(SESSION_ID)
    assert binding["model_profile_id"] == "ds1"


def test_session_binding_unknown_model_falls_back_with_notice(app):
    crew = app
    profile, owns_provider, notice = crew._resolve_session_provider_profile("", "nope")
    assert owns_provider is False
    assert notice and "nope" in notice
    assert profile is crew.config.active_model


def test_owner_default_vendor_model_drives_team_and_wiki(app):
    crew = app
    crew.add_model(
        {"id": "ds1", "provider": "zai", "model": "glm-4.6", "api_key": "sk-zai"},
        owner_account_id="acc-a",
    )
    crew.use_model("ds1", owner_account_id="acc-a")
    provider = crew.owner_team_provider("acc-a")
    assert isinstance(provider, OpenAIProvider)
    assert provider.base_url == "https://api.z.ai/api/coding/paas/v4"
    assert provider._compat.thinking_format == "zai"
    assert provider._client.api_key == "sk-zai"
    # wiki 摘要/编译在无会话上下文时回落 owner 默认 provider（同一缓存实例）
    assert crew._wiki_summarizer._provider_for_owner("acc-a") is provider


def test_wiki_resolver_prefers_current_session_provider(app):
    from crew.core.mocks import FakeProvider
    from crew.core.runctx import current_provider

    crew = app
    sentinel = FakeProvider()
    token = current_provider.set(sentinel)
    try:
        # 会话内切换模型同样作用于 wiki 工具内部的二次 LLM 调用
        assert crew._wiki_summarizer._provider_for_owner("") is sentinel
    finally:
        current_provider.reset(token)


def test_owner_update_builtin_model_key_lands_global_scope(app):
    """owner 更新内置模型：key 必须落全局凭证库，不得进 owner 私有库。

    内置模型归共享 config.yaml 层，update_model 会把 owner 归置为全局
    作用域（""）再写 key；若误用原始 owner_account_id，key 会写进 owner
    私有库/overlay .env，而全局 profile 的解析链读不到（表现为内置模型
    更新后 key "消失"）。
    """
    from crew.state.config import _build_model_profile

    crew = app
    # 与 load_config 相同的构建函数：注册一个全局层内置模型
    crew.config.model_profiles["ds1"] = _build_model_profile(
        "ds1",
        {
            "api_key_env": "DEEPSEEK_API_KEY",
            "provider": "deepseek",
            "model": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "builtin": True,
        },
    )

    crew.update_model("ds1", {"api_key": "sk-builtin-x"}, owner_account_id="acc-a")

    # 归置后的 owner 为全局作用域：key 必须写全局库
    assert read_stored_key("", "ds1") == "sk-builtin-x"
    # owner 私有库不得出现该条目
    assert read_stored_key("acc-a", "ds1") == ""
