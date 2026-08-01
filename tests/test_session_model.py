"""会话级模型绑定单元测试。"""

from __future__ import annotations

import pytest

from crew.state.config import Config, ModelProfile
from crew.state.session_model import (
    promote_pending_session_model,
    read_binding,
    set_session_model,
)
from crew.core.types import Message
from crew.state.session_store import SQLiteSessionStore


def _cfg() -> Config:
    cfg = Config()
    cfg.model_profiles = {
        "default": ModelProfile(id="default", name="Default", model="gpt-test", loaded=True),
        "alt": ModelProfile(id="alt", name="Alt", model="alt-model", loaded=True),
    }
    cfg.active_model_id = "default"
    for p in cfg.model_profiles.values():
        p.api_key = "test-key"
    return cfg


@pytest.fixture
def store(tmp_path):
    return SQLiteSessionStore(str(tmp_path / "crew.db"))


def test_set_session_model_idle_writes_active(store):
    cfg = _cfg()
    store.ensure_session("s1", owner_account_id="u1")
    binding = set_session_model(store, cfg, cfg.model_profiles, "s1", "alt", owner_account_id="u1", busy=False)
    assert binding["model_profile_id"] == "alt"
    assert binding.get("pending_model_profile_id") is None
    stored = store.get_agent_config("s1", owner_account_id="u1")
    assert stored["model_profile_id"] == "alt"


def test_set_session_model_busy_writes_pending_only(store):
    cfg = _cfg()
    store.ensure_session("s1", owner_account_id="u1")
    store.set_agent_config("s1", {"model_profile_id": "default"}, owner_account_id="u1")
    binding = set_session_model(store, cfg, cfg.model_profiles, "s1", "alt", owner_account_id="u1", busy=True)
    assert binding["model_profile_id"] == "default"
    assert binding["pending_model_profile_id"] == "alt"
    assert binding["has_pending"] is True


def test_promote_pending_when_idle(store):
    cfg = _cfg()
    store.ensure_session("s1", owner_account_id="u1")
    store.set_agent_config(
        "s1",
        {"model_profile_id": "default", "pending_model_profile_id": "alt"},
        owner_account_id="u1",
    )
    assert promote_pending_session_model(store, cfg, cfg.model_profiles, "s1", owner_account_id="u1") is True
    binding = read_binding(store.get_agent_config("s1", owner_account_id="u1"), cfg)
    assert binding["model_profile_id"] == "alt"
    assert binding.get("pending_model_profile_id") is None


def test_read_binding_falls_back_to_default():
    cfg = _cfg()
    binding = read_binding(None, cfg)
    assert binding["model_profile_id"] == "default"


def test_message_model_round_trip_preserves_turn_model(store):
    store.save(
        "s-model-history",
        [Message.user("hello"), Message.assistant("world", model="model-at-turn")],
        owner_account_id="u1",
    )

    loaded = store.load("s-model-history", owner_account_id="u1")

    assert loaded[-1].model == "model-at-turn"
