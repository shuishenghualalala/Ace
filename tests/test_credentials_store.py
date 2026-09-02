"""凭证库（crew.state.credentials）与模型 CRUD key 写路径回归测试。

背景缺陷：多模型共用默认 api_key_env（CREW_API_KEY）时，add/update 把新 key
写入共享 env 变量，导致所有模型解析到"最新的 key"。

修复后的语义：
- key 默认按 profile id 存入凭证库（owner 作用域隔离）；
- 显式传 api_key_env 才走 .env（共享名覆盖需 overwrite_shared_key 显式确认）；
- 解析链：凭证库 → api_key_env 环境变量 →（仅全局作用域）CREW_API_KEY 兜底。
"""

from __future__ import annotations

import json
import sys

import pytest

from crew.app import build_app
from crew.providers.openai_provider import OpenAIProvider
from crew.state.config import Config, _build_profile_from_payload, resolve_profile_api_key
from crew.state.credentials import (
    credentials_path,
    delete_stored_key,
    read_stored_key,
    store_key,
)


@pytest.fixture()
def crew_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CREW_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture()
def app(crew_home, tmp_path):
    cfg = Config(db_path=str(crew_home / "crew.db"))
    cfg.config_path = str(tmp_path / "config.yaml")
    return build_app(config=cfg, enable_team=False)


# ---------------------------------------------------------------------------
# 凭证库单元
# ---------------------------------------------------------------------------

def test_store_roundtrip(crew_home):
    assert read_stored_key("", "p1") == ""
    store_key("", "p1", "sk-a")
    assert read_stored_key("", "p1") == "sk-a"
    store_key("", "p1", "sk-b")
    assert read_stored_key("", "p1") == "sk-b"
    delete_stored_key("", "p1")
    assert read_stored_key("", "p1") == ""


def test_owner_scope_isolated(crew_home):
    store_key("acc-a", "p1", "sk-owner")
    store_key("", "p1", "sk-global")
    store_key("acc-b", "p1", "sk-b")
    assert read_stored_key("acc-a", "p1") == "sk-owner"
    assert read_stored_key("acc-b", "p1") == "sk-b"
    assert read_stored_key("", "p1") == "sk-global"
    assert credentials_path("acc-a") != credentials_path("")


def test_empty_key_deletes_entry(crew_home):
    store_key("", "p1", "sk-a")
    store_key("", "p1", "")
    assert read_stored_key("", "p1") == ""
    store_key("", "p1", "")
    assert read_stored_key("", "p1") == ""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位")
def test_store_file_permissions(crew_home):
    store_key("", "p1", "sk-a")
    path = credentials_path("")
    assert path.exists()
    assert (path.stat().st_mode & 0o777) == 0o600


def test_corrupt_store_treated_as_empty_and_repaired(crew_home):
    path = credentials_path("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not-json{", encoding="utf-8")
    assert read_stored_key("", "p1") == ""
    store_key("", "p1", "sk-a")
    assert read_stored_key("", "p1") == "sk-a"
    assert json.loads(path.read_text(encoding="utf-8")) == {"p1": "sk-a"}


# ---------------------------------------------------------------------------
# 解析链：凭证库 → env → 全局兜底
# ---------------------------------------------------------------------------

def test_resolve_prefers_store_over_env(crew_home, monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "sk-env")
    assert resolve_profile_api_key("p1", "ALPHA_KEY", fallback_global=True) == "sk-env"
    store_key("", "p1", "sk-store")
    assert resolve_profile_api_key("p1", "ALPHA_KEY", fallback_global=True) == "sk-store"
    # 其它 profile 不受该 store 条目影响，仍走 env
    assert resolve_profile_api_key("p2", "ALPHA_KEY", fallback_global=True) == "sk-env"


def test_resolve_owner_scope_uses_owner_store(crew_home):
    store_key("acc-a", "p1", "sk-owner")
    assert (
        resolve_profile_api_key("p1", "ALPHA_KEY", fallback_global=False, owner_account_id="acc-a")
        == "sk-owner"
    )
    assert resolve_profile_api_key("p1", "ALPHA_KEY", fallback_global=False) == ""


def test_build_profile_from_payload_resolves_by_scope(crew_home, monkeypatch):
    monkeypatch.setenv("GAMMA_KEY", "sk-env")
    store_key("", "g1", "sk-global-store")
    store_key("acc-a", "o1", "sk-owner-store")

    # 全局作用域：凭证库命中
    p = _build_profile_from_payload("g1", {"api_key_env": "GAMMA_KEY"})
    assert p.api_key == "sk-global-store"
    # 无 store 条目 → env；有 store 条目 → 覆盖 env
    assert _build_profile_from_payload("g2", {"api_key_env": "GAMMA_KEY"}).api_key == "sk-env"
    # owner 作用域：owner store 优先于 owner env_map，且不回落全局 env
    p2 = _build_profile_from_payload(
        "o1",
        {"api_key_env": "GAMMA_KEY"},
        owner_account_id="acc-a",
        env_map={"GAMMA_KEY": "sk-owner-env"},
    )
    assert p2.api_key == "sk-owner-store"
    p3 = _build_profile_from_payload(
        "o2",
        {"api_key_env": "GAMMA_KEY"},
        owner_account_id="acc-a",
        env_map={"GAMMA_KEY": "sk-owner-env"},
    )
    assert p3.api_key == "sk-owner-env"


# ---------------------------------------------------------------------------
# CRUD 写路径（用户反馈缺陷的回归）
# ---------------------------------------------------------------------------

def test_add_two_models_keys_do_not_clobber(app):
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test", "api_key": "sk-first"})
    crew.add_model({"id": "m2", "model": "gpt-test", "api_key": "sk-second"})
    profiles = crew.config.model_profiles
    assert profiles["m1"].api_key == "sk-first"
    assert profiles["m2"].api_key == "sk-second"
    assert profiles["m1"].has_key and profiles["m2"].has_key
    # 默认写路径不碰 .env
    env_map = crew.config.owner_env_map("")
    assert "sk-first" not in env_map.values()
    assert "sk-second" not in env_map.values()


def test_update_model_key_only_touches_target(app):
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test", "api_key": "sk-first"})
    crew.add_model({"id": "m2", "model": "gpt-test", "api_key": "sk-second"})
    crew.update_model("m1", {"api_key": "sk-first-2"})
    assert crew.config.model_profiles["m1"].api_key == "sk-first-2"
    assert crew.config.model_profiles["m2"].api_key == "sk-second"


def test_legacy_shared_env_profile_not_clobbered_by_new_store_key(app, monkeypatch):
    """存量场景：旧模型 key 来自共享 CREW_API_KEY，新模型默认写凭证库后互不影响。"""
    monkeypatch.setenv("CREW_API_KEY", "sk-legacy")
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test"})
    assert crew.config.model_profiles["m1"].api_key == "sk-legacy"
    crew.add_model({"id": "m2", "model": "gpt-test", "api_key": "sk-new"})
    assert crew.config.model_profiles["m2"].api_key == "sk-new"
    assert crew.config.model_profiles["m1"].api_key == "sk-legacy"


def test_shared_env_key_overwrite_blocked_unless_explicit(app):
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test", "api_key_env": "SHARED_API_KEY", "api_key": "sk-one"})
    crew.add_model({"id": "m2", "model": "gpt-test", "api_key": "sk-two-default"})
    # m2 引用同一个共享变量名 + 不同新值 → 拦截
    with pytest.raises(ValueError, match="SHARED_API_KEY"):
        crew.update_model("m2", {"api_key_env": "SHARED_API_KEY", "api_key": "sk-two"})
    # 显式确认后放行，且不影响 m1
    crew.update_model("m2", {"api_key_env": "SHARED_API_KEY", "api_key": "sk-two", "overwrite_shared_key": True})
    profiles = crew.config.model_profiles
    assert profiles["m1"].api_key == "sk-one"
    assert profiles["m2"].api_key == "sk-two"
    # 同值共享是合法场景，直接放行
    crew.add_model({"id": "m3", "model": "gpt-test", "api_key_env": "SHARED_API_KEY", "api_key": "sk-two"})
    assert crew.config.model_profiles["m3"].api_key == "sk-two"


def test_explicit_env_rename_clears_store_entry(app):
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test", "api_key": "sk-store"})
    assert read_stored_key("", "m1") == "sk-store"
    # 用户显式把该模型切到 env 管理 → 清除 store 条目，env 成为 key 来源
    crew.update_model("m1", {"api_key_env": "M1_API_KEY", "api_key": "sk-env-managed"})
    assert read_stored_key("", "m1") == ""
    assert crew.config.model_profiles["m1"].api_key == "sk-env-managed"


def test_remove_model_cleans_store(app):
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test", "api_key": "sk-one"})
    crew.add_model({"id": "m2", "model": "gpt-test", "api_key": "sk-two"})
    crew.remove_model("m2")
    assert read_stored_key("", "m2") == ""
    assert read_stored_key("", "m1") == "sk-one"


def test_store_key_satisfies_provider_gate(app):
    crew = app
    crew.add_model({"id": "m1", "model": "gpt-test", "api_key": "sk-store"})
    crew.use_model("m1")
    assert isinstance(crew.provider, OpenAIProvider)
    assert crew.provider._client.api_key == "sk-store"


def test_owner_scoped_add_model_uses_owner_store(app):
    crew = app
    crew.add_model({"id": "own1", "model": "gpt-test", "api_key": "sk-owner"}, owner_account_id="acc-a")
    assert read_stored_key("acc-a", "own1") == "sk-owner"
    assert read_stored_key("", "own1") == ""
    profiles = crew.owner_model_profiles("acc-a")
    assert profiles["own1"].api_key == "sk-owner"
