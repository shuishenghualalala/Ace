"""复现：_make_agent 不能依赖尚未设置的 current_owner_account_id。

根因：CrewApp.handle → agents.get(owner=...) → _make_agent 时，
ContextVar 仍为空（要到 agent.run 才 set）。若 _make_agent 只读 ContextVar，
owner 私有模型（如 MiniMax-M3）会被当成「不存在」，回退全局 provider。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from crew.app import build_app
from crew.core.runctx import current_owner_account_id
from crew.state.config import load_config
from crew.state.home import get_owner_runtime_home, owner_path_segment


def _write_placeholder_config(tmp_path: Path, crew_home: Path) -> Path:
    config_yaml = tmp_path / "placeholder-config.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": "default",
                    "models": {
                        "default": {
                            "name": "Default",
                            "api_key_env": "CREW_MODEL_API_KEY",
                            "base_url": "https://api.example.com/v1",
                            "model": "your-model-name",
                        }
                    },
                },
                "runtime": {
                    "crew_home": str(crew_home),
                    "db_path": str(tmp_path / "placeholder.db"),
                    "memory_db_path": str(tmp_path / "placeholder-memory.db"),
                    "log_level": "WARNING",
                    "llm_trace": False,
                },
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return config_yaml


@pytest.fixture
def owner_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """全局内置 alpha + owner 私有 MiniMax-M3（有 key）。"""
    crew_home = tmp_path / ".Crew"
    crew_home.mkdir()
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    monkeypatch.setenv("ALPHA_API_KEY", "sk-alpha-global")

    config_yaml = tmp_path / "config.yaml"
    config_yaml.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": "alpha",
                    "models": {
                        "alpha": {
                            "name": "Alpha",
                            "api_key_env": "ALPHA_API_KEY",
                            "provider": "openai",
                            "base_url": "https://alpha.example.com/v1",
                            "model": "alpha-1",
                        }
                    },
                },
                "runtime": {
                    "crew_home": str(crew_home),
                    "db_path": str(tmp_path / "crew.db"),
                    "log_level": "WARNING",
                    "llm_trace": False,
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    cfg = load_config(config_path=str(config_yaml))
    app = build_app(config=cfg, enable_team=False)

    owner = "dev:dev"
    assert owner_path_segment(owner).startswith("acct_")
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": "alpha",
                    "models": {
                        "MiniMax-M3": {
                            "name": "MiniMax-M3",
                            "api_key_env": "CREW_API_KEY",
                            "provider": "anthropic",
                            "base_url": "https://api.minimaxi.com/anthropic",
                            "model": "MiniMax-M3",
                            "loaded": True,
                        }
                    },
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (owner_home / ".env").write_text("CREW_API_KEY=sk-minimax-owner\n", encoding="utf-8")

    # 断言：ContextVar 默认空（模拟 handle → agents.get 时机）
    assert current_owner_account_id.get() == ""
    return app, owner


def test_agents_get_uses_owner_private_model_without_contextvar(owner_app):
    """AgentManager.get 传入的 owner 必须足以解析会话绑定的私有模型。"""
    app, owner = owner_app
    assert "MiniMax-M3" not in app.config.model_profiles
    assert "MiniMax-M3" in app.owner_model_profiles(owner)

    agent = app.agents.get(
        "web_sess_1",
        {"model_profile_id": "MiniMax-M3"},
        owner_account_id=owner,
    )

    # 修复前：回退全局 alpha（openai / alpha-1）
    # 修复后：应使用 owner 私有 MiniMax（anthropic / MiniMax-M3）
    assert getattr(agent.provider, "model", None) == "MiniMax-M3"
    provider_url = getattr(agent.provider, "_url", "") or ""
    assert "minimaxi.com" in provider_url


def test_make_agent_explicit_owner_overrides_empty_contextvar(owner_app):
    """直接调用 _make_agent 时，显式 owner_account_id 优先于空 ContextVar。"""
    app, owner = owner_app
    assert current_owner_account_id.get() == ""

    agent = app._make_agent(
        {"model_profile_id": "MiniMax-M3"},
        owner_account_id=owner,
    )
    assert getattr(agent.provider, "model", None) == "MiniMax-M3"


def test_unbound_session_inherits_owner_default_model(owner_app):
    """未显式绑模型的 Session 必须继承 owner 默认值，而不是进程级 active。"""
    app, owner = owner_app
    app.use_model("MiniMax-M3", owner_account_id=owner)

    agent = app._make_agent(app._default_agent_config(), owner_account_id=owner)
    binding = app.read_session_model_binding("unbound-session", owner_account_id=owner)

    assert app._default_agent_config()["model_profile_id"] == "inherit"
    assert getattr(agent.provider, "model", None) == "MiniMax-M3"
    assert binding["model_profile_id"] == "MiniMax-M3"


def test_existing_owner_profile_replaces_placeholder_default_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """旧 overlay 仍写 default 时，也应解析到已经可用的真实 owner 模型。"""
    crew_home = tmp_path / ".Crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    cfg = load_config(config_path=str(_write_placeholder_config(tmp_path, crew_home)))
    owner = "dev:dev"
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": "default",
                    "models": {
                        "deepseek": {
                            "name": "DeepSeek",
                            "api_key_env": "CREW_API_KEY",
                            "base_url": "https://api.deepseek.com",
                            "model": "deepseek-chat",
                            "loaded": True,
                        }
                    },
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (owner_home / ".env").write_text("CREW_API_KEY=sk-owner\n", encoding="utf-8")

    assert cfg.owner_default_model_id(owner) == "deepseek"
    assert cfg.owner_default_model_profile(owner).model == "deepseek-chat"


def test_first_ready_owner_model_replaces_and_persists_placeholder_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """新增首个可用真实模型时，应立即写成 owner 默认模型。"""
    crew_home = tmp_path / ".Crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    cfg = load_config(config_path=str(_write_placeholder_config(tmp_path, crew_home)))
    app = build_app(config=cfg, enable_team=False)
    owner = "dev:dev"

    app.add_model(
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "api_key_env": "CREW_API_KEY",
            "base_url": "https://api.deepseek.com",
            "model": "deepseek-chat",
            "api_key": "sk-owner",
        },
        owner_account_id=owner,
    )

    overlay = yaml.safe_load((get_owner_runtime_home(owner) / "config.yaml").read_text(encoding="utf-8"))
    assert overlay["llm"]["active"] == "deepseek"
    assert overlay["llm"]["default"] == "deepseek"
    assert cfg.owner_default_model_id(owner) == "deepseek"


@pytest.mark.asyncio
async def test_make_agent_without_api_key_borrows_fake_provider(tmp_path, monkeypatch):
    """无 Key 的 owner profile 必须继续使用 App 的 FakeProvider，不能构造真实客户端。"""
    from crew.core.mocks import FakeProvider
    from crew.state.config import Config, ModelProfile

    # 同进程内其他测试调用 load_config 时会把仓库 config/.env 中的真实 Key
    # 写进 os.environ 且不清理；这里清掉，保证 local owner 解析不到任何 Key。
    monkeypatch.delenv("CREW_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("CREW_API_KEY", raising=False)

    crew_home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
    profile = ModelProfile(
        id="default",
        name="Default",
        api_key="",
        api_key_env="CREW_MODEL_API_KEY",
        base_url="https://api.example.com/v1",
        model="your-model-name",
        builtin=True,
    )
    app = build_app(
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

    agent = app._make_agent(
        {"model_profile_id": "default"},
        owner_account_id="local",
    )

    assert isinstance(app.provider, FakeProvider)
    assert agent.provider is app.provider
    assert agent._owned_providers == []
    assert "FakeProvider" in str(agent.model_fallback_notice)
    assert "设置 → 模型" in str(agent.model_fallback_notice)
    await agent.aclose()


def test_make_agent_acp_accepts_top_level_external_agent_id(owner_app):
    """前端 session agent-config 会把 external_agent_id 存在顶层，ACP executor 必须能读到。"""
    app, owner = owner_app
    agent = app._make_agent(
        {"executor": "acp", "external_agent_id": "agent_e2e"},
        owner_account_id=owner,
    )

    assert getattr(agent.executor.config, "external_agent_id", "") == "agent_e2e"
    assert agent.executor.config.external_store is app.external_agents


@pytest.mark.asyncio
async def test_make_agent_builds_only_final_dynamic_provider_and_declares_ownership(
    owner_app,
    monkeypatch,
):
    """Owner 默认模型被 session 模型覆盖时，只创建最终客户端且归 Agent 所有。"""
    import crew.app as app_module

    app, owner = owner_app
    real_build = app_module.build_provider_for_profile
    built = []

    def tracking_build(profile, stream_read_timeout=None):
        provider = real_build(profile, stream_read_timeout)
        built.append(provider)
        return provider

    monkeypatch.setattr(app_module, "build_provider_for_profile", tracking_build)
    agent = app._make_agent(
        {"model_profile_id": "MiniMax-M3"},
        owner_account_id=owner,
    )

    assert built == [agent.provider]
    assert agent._owned_providers == [agent.provider]
    await agent.aclose()


@pytest.mark.asyncio
async def test_make_agent_does_not_own_borrowed_app_provider(owner_app):
    app, _owner = owner_app
    agent = app._make_agent({"model_profile_id": "inherit"}, owner_account_id="")

    assert agent.provider is app.provider
    assert app.provider not in agent._owned_providers
    await agent.aclose()


def test_session_model_fallback_sets_visible_notice(owner_app):
    """绑定不存在的模型时，agent 应携带可推到 UI 的回退说明。"""
    app, owner = owner_app
    agent = app._make_agent(
        {"model_profile_id": "DoesNotExist"},
        owner_account_id=owner,
    )
    assert agent.model_fallback_notice
    assert "DoesNotExist" in agent.model_fallback_notice
    assert getattr(agent.provider, "model", None) == "alpha-1"


@pytest.mark.asyncio
async def test_run_emits_model_fallback_status_once(owner_app):
    """run 开头应推送一次 status，随后清空 notice，避免每轮重复。"""
    from crew.core.envelope import Envelope
    from crew.core.mocks import FakeProvider

    app, owner = owner_app
    agent = app._make_agent(
        {"model_profile_id": "DoesNotExist"},
        owner_account_id=owner,
    )
    agent.provider = FakeProvider()
    if hasattr(agent.executor, "provider"):
        agent.executor.provider = agent.provider

    env = Envelope.of("hi", session_id="s-fallback", user_id=owner)
    chunks = [c async for c in agent.run(env)]
    status_msgs = [
        c.body.get("message", "")
        for c in chunks
        if c.kind == "status"
    ]
    assert any("DoesNotExist" in m for m in status_msgs)
    assert agent.model_fallback_notice is None


def test_team_mode_reads_session_config_with_owner(owner_app, tmp_path):
    """team 分支必须带 owner 读 session_agent_config，否则读不到账号隔离配置。"""
    app, owner = owner_app
    sid = "team_sess_owner"
    app.session_store.ensure_session(sid, owner_account_id=owner)
    app.session_store.set_agent_config(
        sid,
        {"team": {"external_team_id": "ext-team-1"}, "model_profile_id": "MiniMax-M3"},
        owner_account_id=owner,
    )
    # 漏传 owner → 读不到该账号下的 team 配置
    without_owner = app._session_agent_config(sid)
    assert without_owner.get("team", {}).get("external_team_id") != "ext-team-1"
    with_owner = app._session_agent_config(sid, owner_account_id=owner)
    assert with_owner.get("team", {}).get("external_team_id") == "ext-team-1"


# ---------------------------------------------------------------------------
# demo_mode：GET/PUT /api/session/{id}/model 下发的「生效 Provider = FakeProvider」
# 服务端判定，必须与 _make_agent 装配走同一条回退链。
# ---------------------------------------------------------------------------


def _build_no_key_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """全局唯一内置模型且无 Key 的 App（provider = FakeProvider）。"""
    from crew.state.config import Config, ModelProfile

    # 同进程其他测试的 load_config 会把仓库 config/.env 的真实 Key 写进 os.environ
    # 且不清理；这里清掉，保证解析不到任何 Key。
    monkeypatch.delenv("CREW_MODEL_API_KEY", raising=False)
    monkeypatch.delenv("CREW_API_KEY", raising=False)

    crew_home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(crew_home))
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


def _write_owner_overlay(owner: str, model_id: str, api_key: str) -> None:
    """给 owner 写私有 overlay：一个有 Key 的模型并设为默认。"""
    owner_home = get_owner_runtime_home(owner)
    owner_home.mkdir(parents=True, exist_ok=True)
    (owner_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": model_id,
                    "models": {
                        model_id: {
                            "name": model_id,
                            "api_key_env": "OWNER_OVERLAY_KEY",
                            "provider": "openai",
                            "base_url": "https://overlay.example.com/v1",
                            "model": model_id,
                            "loaded": True,
                        }
                    },
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (owner_home / ".env").write_text(f"OWNER_OVERLAY_KEY={api_key}\n", encoding="utf-8")


def test_demo_mode_true_when_effective_provider_is_fake(tmp_path, monkeypatch):
    """全局与会话绑定都无 Key → demo_mode=True（绑定与未绑定会话都成立）。"""
    app = _build_no_key_app(tmp_path, monkeypatch)
    owner = "local"
    app.session_store.ensure_session("s-bound", owner_account_id=owner)
    app.session_store.set_agent_config(
        "s-bound", {"model_profile_id": "default"}, owner_account_id=owner,
    )

    bound = app.read_session_model_binding("s-bound", owner_account_id=owner)
    unbound = app.read_session_model_binding("s-never-created", owner_account_id=owner)

    assert bound["demo_mode"] is True
    assert unbound["demo_mode"] is True


def test_demo_mode_false_when_owner_overlay_saves_session(tmp_path, monkeypatch):
    """全局无 Key 但 owner overlay 默认模型有 Key → demo_mode=False。

    前端只能看到全局 config 的 has_key，看不到 per-owner overlay——这正是把判定
    挪到后端要消除的假阳性。
    """
    app = _build_no_key_app(tmp_path, monkeypatch)
    owner = "dev:overlay"
    _write_owner_overlay(owner, "overlay-model", "sk-overlay")

    unbound = app.read_session_model_binding("s-inherit", owner_account_id=owner)
    app.session_store.ensure_session("s-overlay", owner_account_id=owner)
    app.session_store.set_agent_config(
        "s-overlay", {"model_profile_id": "overlay-model"}, owner_account_id=owner,
    )
    bound = app.read_session_model_binding("s-overlay", owner_account_id=owner)

    assert unbound["demo_mode"] is False
    assert bound["demo_mode"] is False


def test_demo_mode_false_when_falling_back_to_real_global_provider(owner_app):
    """会话绑定不存在但全局 active 有 Key：有 fallback 提示，却不是演示模式。"""
    app, owner = owner_app
    app.session_store.ensure_session("s-missing", owner_account_id=owner)
    app.session_store.set_agent_config(
        "s-missing", {"model_profile_id": "DoesNotExist"}, owner_account_id=owner,
    )

    binding = app.read_session_model_binding("s-missing", owner_account_id=owner)

    assert binding["demo_mode"] is False


def test_demo_mode_false_for_external_executor_sessions(tmp_path, monkeypatch):
    """外部 Agent/Team 会话不走 builtin provider 链，恒为非演示模式。"""
    app = _build_no_key_app(tmp_path, monkeypatch)
    owner = "local"
    app.session_store.ensure_session("s-ext", owner_account_id=owner)
    app.session_store.set_agent_config(
        "s-ext",
        {"executor": "external", "external_agent_id": "agent-x", "model_profile_id": "ext-model"},
        owner_account_id=owner,
    )

    binding = app.read_session_model_binding("s-ext", owner_account_id=owner)

    assert binding["demo_mode"] is False


def test_set_session_model_binding_includes_demo_mode(owner_app):
    """PUT 路径同样下发 demo_mode，前端切换模型后横幅可即时刷新。"""
    app, owner = owner_app
    app.session_store.ensure_session("s-put", owner_account_id=owner)

    binding = app.set_session_model_binding(
        "s-put", "MiniMax-M3", owner_account_id=owner, busy=False,
    )

    assert binding["model_profile_id"] == "MiniMax-M3"
    assert binding["demo_mode"] is False
