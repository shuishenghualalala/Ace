"""CrewApp per-owner embedding provider 缓存与失效（多租户语义检索）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crew.app import build_app
from crew.state.config import Config, ModelProfile
from crew.state.home import get_owner_runtime_home


def _build_no_key_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """全局唯一内置模型且无 Key 的 App（provider = FakeProvider），语义默认禁用。"""
    monkeypatch.delenv("CREW_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("CREW_API_KEY", raising=False)
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".Crew"))

    profile = ModelProfile(
        id="default",
        name="Default",
        api_key="",
        api_key_env="CREW_MODEL_API_KEY",
        base_url="https://api.example.com/v1",
        model="your-model-name",
        builtin=True,
    )
    return build_app(
        config=Config(
            api_key="",
            active_model_id="default",
            model_profiles={"default": profile},
            db_path=str(tmp_path / "crew.db"),
            memory_db_path=str(tmp_path / "memory.db"),
            log_level="WARNING",
        ),
        enable_team=False,
    )


def test_owner_embedding_provider_caches_and_rebuilds_after_invalidation(tmp_path, monkeypatch):
    import crew.wiki.embedding as embedding_module

    app = _build_no_key_app(tmp_path, monkeypatch)

    owner = "dev:embed"
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"wiki": {"semantic": {"enabled": True, "provider": "local", "model": "bge-zh"}}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    class _FakeProvider:
        model = "fake"
        dim = 0

        def close(self) -> None:
            pass

    built: list[_FakeProvider] = []

    def fake_build(semantic, *args, **kwargs):
        provider = _FakeProvider()
        built.append(provider)
        return provider

    monkeypatch.setattr(embedding_module, "build_embedding_provider", fake_build)

    first = app.owner_embedding_provider(owner)
    second = app.owner_embedding_provider(owner)
    assert first is second  # 缓存命中，未重复构建
    assert len(built) == 1

    app._invalidate_owner_embedding_provider(owner)

    third = app.owner_embedding_provider(owner)
    assert third is not first  # 失效后重建
    assert len(built) == 2


def test_owner_embedding_provider_caches_none_for_disabled_owner(tmp_path, monkeypatch):
    import crew.wiki.embedding as embedding_module

    app = _build_no_key_app(tmp_path, monkeypatch)

    owner = "dev:disabled"
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"wiki": {"semantic": {"enabled": False, "provider": "openai"}}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    calls = {"count": 0}

    def fake_build(semantic, *args, **kwargs):
        calls["count"] += 1
        raise AssertionError("禁用 owner 不应构建 provider")

    monkeypatch.setattr(embedding_module, "build_embedding_provider", fake_build)

    # 语义禁用 → 返回 None 且缓存（含 None），不触发构建
    assert app.owner_embedding_provider(owner) is None
    assert app.owner_embedding_provider(owner) is None
    assert calls["count"] == 0


def test_owner_embedding_provider_empty_owner_returns_global(tmp_path, monkeypatch):
    import crew.wiki.embedding as embedding_module

    app = _build_no_key_app(tmp_path, monkeypatch)
    # 全局语义禁用，_embedding_provider = None
    assert app._embedding_provider is None

    calls = {"count": 0}

    def fake_build(semantic, *args, **kwargs):
        calls["count"] += 1
        raise AssertionError("空 owner 应直接回退全局 provider，不解析 overlay")

    monkeypatch.setattr(embedding_module, "build_embedding_provider", fake_build)

    assert app.owner_embedding_provider("") is None
    assert calls["count"] == 0
