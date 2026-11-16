"""WikiConfig 配置解析测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from crew.wiki.config import WikiConfig


def test_wiki_config_model_defaults_to_empty():
    cfg = WikiConfig.from_raw({})
    assert cfg.model == ""
    assert cfg.ingest.auto_apply is False
    assert cfg.ingest.auto_summarize is True
    assert cfg.ingest.auto_ingest is False


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


def test_rerank_is_disabled_by_default_and_parses_independent_model_settings():
    assert WikiConfig.from_raw({}).rerank.enabled is False
    cfg = WikiConfig.from_raw({"rerank": {
        "enabled": "yes", "model": "test-reranker", "base_url": "https://example.test/v1",
        "api_key_env": "RERANK_KEY", "timeout": 5, "max_chunks_per_doc": 4,
    }, "query": {"retrieval_candidates": 80, "rerank_candidates": 40}})
    assert cfg.rerank.enabled is True
    assert cfg.rerank.api_key_env == "RERANK_KEY"
    assert cfg.rerank.max_chunks_per_doc == 4
    assert cfg.query.retrieval_candidates == 80
    assert cfg.query.rerank_candidates == 40


@pytest.mark.parametrize("raw", [{"timeout": 0}, {"timeout": "nan"}, {"max_chunks_per_doc": 0}])
def test_rerank_rejects_unbounded_or_invalid_limits(raw):
    with pytest.raises(ValueError):
        WikiConfig.from_raw({"rerank": raw})
