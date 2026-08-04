"""WikiConfig 配置解析测试。"""

from __future__ import annotations

from pathlib import Path

from crew.wiki.config import WikiConfig


def test_wiki_config_model_defaults_to_empty():
    cfg = WikiConfig.from_raw({})
    assert cfg.model == ""
    assert cfg.ingest.auto_apply is True


def test_wiki_config_model_from_raw():
    cfg = WikiConfig.from_raw({"model": "deepseek"})
    assert cfg.model == "deepseek"


def test_wiki_config_model_strips_whitespace():
    cfg = WikiConfig.from_raw({"model": "  deepseek  "})
    assert cfg.model == "deepseek"


def test_wiki_ingest_auto_apply_can_be_disabled():
    cfg = WikiConfig.from_raw({"ingest": {"auto_apply": False}})
    assert cfg.ingest.auto_apply is False


def test_wiki_ingest_auto_apply_parses_string_value():
    cfg = WikiConfig.from_raw({"ingest": {"auto_apply": "off"}})
    assert cfg.ingest.auto_apply is False


def test_capture_attachments_defaults_to_true():
    assert WikiConfig.from_raw({}).capture_attachments is True


def test_capture_attachments_can_be_disabled():
    assert WikiConfig.from_raw({"capture_attachments": False}).capture_attachments is False
    assert WikiConfig.from_raw({"capture_attachments": "off"}).capture_attachments is False


def test_wiki_storage_root_defaults_to_current_layout():
    cfg = WikiConfig.from_raw({})
    assert cfg.storage.root == ""
    assert cfg.storage.resolved_root() is None


def test_wiki_storage_root_resolves_relative_to_user_home(monkeypatch):
    monkeypatch.delenv("CREW_WIKI_HOME", raising=False)
    cfg = WikiConfig.from_raw({"storage": {"root": "CrewWiki"}})
    assert cfg.storage.resolved_root() == (Path.home() / "CrewWiki").resolve()


def test_wiki_storage_env_overrides_yaml(monkeypatch, tmp_path):
    override = tmp_path / "wiki-data"
    monkeypatch.setenv("CREW_WIKI_HOME", str(override))
    cfg = WikiConfig.from_raw({"storage": {"root": "ignored"}})
    assert cfg.storage.resolved_root() == override.resolve()
