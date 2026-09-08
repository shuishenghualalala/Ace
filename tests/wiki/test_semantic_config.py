"""Wiki 语义检索 per-owner 配置：合并、解析与持久化（多租户）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crew.state.config import Config
from crew.state.home import get_owner_runtime_home
from crew.wiki.config import WikiConfig


def _config_with_global_semantic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    semantic: dict,
) -> Config:
    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".Crew"))
    cfg = Config(db_path=str(tmp_path / "crew.db"), memory_db_path=str(tmp_path / "mem.db"))
    cfg.wiki = WikiConfig.from_raw({"semantic": semantic})
    return cfg


def test_owner_semantic_config_overlay_overrides_global_field_by_field(tmp_path, monkeypatch):
    cfg = _config_with_global_semantic(
        tmp_path,
        monkeypatch,
        {
            "enabled": False,
            "provider": "openai",
            "model": "text-embedding-3-small",
            "base_url": "https://global.example.com/v1",
            "api_key_env": "GLOBAL_EMBED_KEY",
            "rank_weight": 40.0,
        },
    )
    owner = "dev:owner"
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "wiki": {
                    "semantic": {
                        "enabled": True,
                        "provider": "local",
                        "model": "BAAI/bge-small-zh-v1.5",
                        "rank_weight": 20.0,
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    semantic = cfg.owner_semantic_config(owner)
    assert semantic.enabled is True
    assert semantic.provider == "local"
    assert semantic.model == "BAAI/bge-small-zh-v1.5"
    assert semantic.rank_weight == 20.0
    # 未覆盖字段逐字段继承全局
    assert semantic.base_url == "https://global.example.com/v1"
    assert semantic.api_key_env == "GLOBAL_EMBED_KEY"


def test_owner_semantic_config_falls_back_to_global_without_overlay(tmp_path, monkeypatch):
    cfg = _config_with_global_semantic(
        tmp_path,
        monkeypatch,
        {"enabled": True, "provider": "openai", "model": "text-embedding-3-small"},
    )
    semantic = cfg.owner_semantic_config("other:owner")
    assert semantic.enabled is True
    assert semantic.provider == "openai"
    assert semantic.model == "text-embedding-3-small"


def test_persist_owner_semantic_config_writes_only_overlay_semantic_section(tmp_path, monkeypatch):
    cfg = _config_with_global_semantic(
        tmp_path,
        monkeypatch,
        {"enabled": False, "provider": "openai"},
    )
    owner = "dev:owner"
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    # 预先存在其它段，persist 不得破坏
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump({"llm": {"active": "alpha"}}, allow_unicode=True),
        encoding="utf-8",
    )

    cfg.persist_owner_semantic_config(
        owner,
        {"enabled": True, "provider": "openai", "model": "text-embedding-3-small", "api_key_env": "OWNER_EMBED_KEY"},
    )

    overlay = yaml.safe_load((owner_home / "config.yaml").read_text(encoding="utf-8"))
    assert overlay["llm"]["active"] == "alpha"
    assert overlay["wiki"]["semantic"]["enabled"] is True
    assert overlay["wiki"]["semantic"]["provider"] == "openai"

    # 回读解析：overlay 生效、未覆盖字段继承全局
    semantic = cfg.owner_semantic_config(owner)
    assert semantic.enabled is True
    assert semantic.model == "text-embedding-3-small"
    assert semantic.api_key_env == "OWNER_EMBED_KEY"


def test_persist_owner_semantic_config_requires_owner(tmp_path, monkeypatch):
    cfg = _config_with_global_semantic(tmp_path, monkeypatch, {})
    with pytest.raises(ValueError):
        cfg.persist_owner_semantic_config("", {"enabled": True})
