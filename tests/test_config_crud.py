"""Config 层 ModelProfile CRUD 单元测试。

覆盖：
- Config.add_model / update_model / remove_model 的语义
- persist_model_profiles 写回 yaml（结构、不写敏感字段、原子替换）
- write_env_key 写回 .env（新增 / 替换 / 创建）
- 边界：id 重复、id 不存在、删除最后一个
- 加载后行为：load_config → CRUD → 再 load，验证持久化生效
"""

from __future__ import annotations

import os
import logging
from pathlib import Path

import pytest
import yaml

from crew.state.config import (
    Config,
    _serialize_profile_for_yaml,
    load_config,
    remove_env_key,
    resolve_writable_env_path,
    write_env_key,
)
from crew.state.home import owner_path_segment


# ----------------------- fixtures -----------------------


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    """构造一个最小可用的 config.yaml，含 2 个模型 profile。"""
    data = {
        "llm": {
            "active": "alpha",
            "models": {
                "alpha": {
                    "name": "Alpha",
                    "api_key_env": "ALPHA_KEY",
                    "provider": "anthropic",
                    "base_url": "https://alpha.example.com/v1",
                    "model": "alpha-1",
                    "temperature": 0.5,
                    "max_tokens": 8192,
                    "context_window": 32000,
                    "timeout": 30.0,
                },
                "beta": {
                    "name": "Beta",
                    "api_key_env": "BETA_KEY",
                    "base_url": "https://beta.example.com/v1",
                    "model": "beta-1",
                },
            },
        },
        "runtime": {"log_level": "DEBUG"},  # 用于验证非 llm 段在写回后保留
    }
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return p


@pytest.fixture
def cfg(tmp_yaml: Path) -> Config:
    """从 tmp_yaml 加载 Config，注入伪 key 让 has_key 为真。"""
    os.environ["ALPHA_KEY"] = "sk-alpha"
    os.environ["BETA_KEY"] = "sk-beta"
    cfg = load_config(config_path=str(tmp_yaml))
    yield cfg
    # 清理 env，避免污染后续测试
    os.environ.pop("ALPHA_KEY", None)
    os.environ.pop("BETA_KEY", None)
    os.environ.pop("GAMMA_KEY", None)
    os.environ.pop("DELTA_KEY", None)


# ----------------------- Config.add_model -----------------------


def test_add_model_basic(cfg: Config):
    profile = cfg.add_model({
        "id": "gamma",
        "name": "Gamma",
        "api_key_env": "GAMMA_KEY",
        "base_url": "https://gamma.example.com/v1",
        "model": "gamma-1",
    })
    assert profile.id == "gamma"
    assert profile.name == "Gamma"
    assert profile.provider == "openai"
    assert profile.base_url == "https://gamma.example.com/v1"
    assert "gamma" in cfg.model_profiles
    # 新增不应改变激活模型
    assert cfg.active_model_id == "alpha"


def test_load_config_reads_external_security_switch(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"external_agents": {"enabled": True, "security_enabled": False}},
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    loaded = load_config(config_path=str(config_path))

    assert loaded.external_agents_enabled is True
    assert loaded.external_security_enabled is False


def test_load_config_defaults_external_security_to_disabled(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"external_agents": {"enabled": True}}, allow_unicode=True),
        encoding="utf-8",
    )

    loaded = load_config(config_path=str(config_path))

    assert loaded.external_security_enabled is False


def test_load_config_defaults_security_to_disabled(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n", encoding="utf-8")

    loaded = load_config(config_path=str(config_path))

    assert loaded.security_enabled is False


def test_load_config_reads_enabled_security(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("security:\n  enabled: true\n", encoding="utf-8")

    loaded = load_config(config_path=str(config_path))

    assert loaded.security_enabled is True


def test_add_model_rejects_empty_id(cfg: Config):
    with pytest.raises(ValueError, match="不能为空"):
        cfg.add_model({"id": "", "name": "x"})


def test_add_model_rejects_duplicate(cfg: Config):
    with pytest.raises(ValueError, match="已存在"):
        cfg.add_model({"id": "alpha"})


def test_add_model_uses_defaults(cfg: Config):
    profile = cfg.add_model({"id": "minimal"})
    # 缺省字段应有合理默认值
    assert profile.api_key_env == "CREW_API_KEY"
    assert profile.model == "gpt-4o-mini"
    assert profile.temperature == 0.7


# ----------------------- Config.update_model -----------------------


def test_update_model_partial(cfg: Config):
    cfg.update_model("alpha", {"temperature": 0.1, "base_url": "https://new.example.com", "provider": "openai"})
    p = cfg.model_profiles["alpha"]
    assert p.temperature == 0.1
    assert p.provider == "openai"
    assert p.base_url == "https://new.example.com"
    # 未传入的字段保留
    assert p.model == "alpha-1"
    assert p.api_key_env == "ALPHA_KEY"


def test_update_model_not_found(cfg: Config):
    with pytest.raises(KeyError):
        cfg.update_model("nonexistent", {"temperature": 0.1})


def test_update_model_id_immutable(cfg: Config):
    """update_model 不允许改 id（path 参数为准）。"""
    cfg.update_model("alpha", {"id": "renamed"})
    # id 仍是 alpha
    assert "alpha" in cfg.model_profiles
    assert "renamed" not in cfg.model_profiles


# ----------------------- Config.remove_model -----------------------


def test_remove_model_basic(cfg: Config):
    removed = cfg.remove_model("beta")
    assert removed.id == "beta"
    assert "beta" not in cfg.model_profiles


def test_remove_model_not_found(cfg: Config):
    with pytest.raises(KeyError):
        cfg.remove_model("nonexistent")


def test_remove_last_model_forbidden(cfg: Config):
    """至少保留一个，删完最后一个应抛 ValueError。"""
    cfg.remove_model("beta")
    assert len(cfg.model_profiles) == 1
    with pytest.raises(ValueError, match="至少保留"):
        cfg.remove_model("alpha")


# ----------------------- Config.persist_model_profiles -----------------------


def test_persist_writes_back_full_models(cfg: Config, tmp_yaml: Path):
    cfg.add_model({"id": "gamma", "api_key_env": "GAMMA_KEY", "model": "g-1"})
    cfg.update_model("alpha", {"temperature": 0.99})
    cfg.remove_model("beta")
    cfg.persist_model_profiles()

    # 重新加载，验证持久化生效
    os.environ["ALPHA_KEY"] = "sk-alpha"
    os.environ["GAMMA_KEY"] = "sk-gamma"
    cfg2 = load_config(config_path=str(tmp_yaml))
    assert set(cfg2.model_profiles.keys()) == {"alpha", "gamma"}
    assert cfg2.model_profiles["alpha"].provider == "anthropic"
    assert cfg2.model_profiles["alpha"].temperature == 0.99
    assert cfg2.model_profiles["gamma"].model == "g-1"


def test_persist_preserves_other_sections(cfg: Config, tmp_yaml: Path):
    """写回 llm.models 时，runtime 段应原样保留。"""
    cfg.add_model({"id": "gamma", "api_key_env": "GAMMA_KEY"})
    cfg.persist_model_profiles()

    data = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8"))
    assert data["runtime"]["log_level"] == "DEBUG"


def test_load_config_invalid_active_uses_sorted_profile_id(tmp_yaml: Path):
    """激活 id 失效时，应回退到 profile id 的字典序第一个，而不是依赖插入顺序。"""
    data = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8")) or {}
    data["llm"]["active"] = "missing"
    data["llm"]["models"]["zeta"] = {
        "name": "Zeta",
        "api_key_env": "ZETA_KEY",
        "base_url": "https://zeta.example.com/v1",
        "model": "zeta-1",
    }
    tmp_yaml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.environ["ALPHA_KEY"] = "sk-alpha"
    os.environ["BETA_KEY"] = "sk-beta"
    os.environ["ZETA_KEY"] = "sk-zeta"

    cfg = load_config(config_path=str(tmp_yaml))
    assert cfg.active_model_id == "alpha"


def test_load_config_unloaded_active_falls_back_to_loaded_profile(tmp_yaml: Path):
    """启动时 active 指向未加载 profile 时，应回退到可用 profile 而不是失败。"""
    data = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8")) or {}
    data["llm"]["active"] = "beta"
    data["llm"]["models"]["beta"]["loaded"] = False
    tmp_yaml.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.environ["ALPHA_KEY"] = "sk-alpha"
    os.environ["BETA_KEY"] = "sk-beta"

    cfg = load_config(config_path=str(tmp_yaml))

    assert cfg.active_model_id == "alpha"
    assert cfg.model == "alpha-1"


def test_legacy_cron_tick_seconds_is_ignored_and_warned_once(
    tmp_yaml: Path,
    caplog,
    monkeypatch,
):
    import crew.state.config as config_module

    data = yaml.safe_load(tmp_yaml.read_text(encoding="utf-8")) or {}
    data["cron"] = {"enabled": True, "tick_seconds": 0.01, "max_parallel_jobs": 3}
    tmp_yaml.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    monkeypatch.setattr(config_module, "_LEGACY_CRON_TICK_WARNING_EMITTED", False)

    with caplog.at_level(logging.WARNING, logger="crew.config"):
        first = load_config(config_path=str(tmp_yaml))
        second = load_config(config_path=str(tmp_yaml))

    assert not hasattr(first, "cron_tick_seconds")
    assert second.cron_max_parallel_jobs == 3
    assert caplog.text.count("已忽略废弃配置 cron.tick_seconds") == 1


def test_persist_never_writes_api_key_plaintext(cfg: Config, tmp_yaml: Path):
    """即便 profile 的 api_key 已加载到内存，yaml 中也不应出现明文。"""
    # alpha 已从 env 加载到 api_key="sk-alpha"
    assert cfg.model_profiles["alpha"].api_key == "sk-alpha"
    cfg.persist_model_profiles()

    text = tmp_yaml.read_text(encoding="utf-8")
    assert "sk-alpha" not in text
    assert "api_key:" not in text  # 整体不应有 api_key 字段


def test_persist_atomic_via_tmp(cfg: Config, tmp_yaml: Path):
    """写回过程中不应留下 .tmp 文件（原子替换成功）。"""
    cfg.persist_model_profiles()
    assert not (tmp_yaml.with_suffix(tmp_yaml.suffix + ".tmp")).exists()


def test_persist_rejects_when_config_path_empty():
    """纯构造的 Config（无 yaml 来源）应拒绝写回。"""
    cfg = Config()
    with pytest.raises(RuntimeError, match="config_path"):
        cfg.persist_model_profiles()


def test_serialize_profile_skips_none_optional():
    """max_tokens/context_window 为 None 时不应写入 yaml。"""
    from crew.state.config import ModelProfile

    p = ModelProfile(id="x", max_tokens=None, context_window=None)
    data = _serialize_profile_for_yaml(p)
    assert "max_tokens" not in data
    assert "context_window" not in data
    # 必填字段仍写入
    assert data["model"] == "gpt-4o-mini"
    assert "api_key_env" in data


# ----------------------- write_env_key -----------------------


def test_write_env_key_creates_new_file(tmp_path: Path):
    env_path = tmp_path / ".env"
    write_env_key(env_path, "MY_VAR", "secret123")
    assert env_path.exists()
    assert "MY_VAR=secret123" in env_path.read_text(encoding="utf-8")
    assert os.environ.get("MY_VAR") == "secret123"
    os.environ.pop("MY_VAR", None)


def test_write_env_key_appends_to_existing(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("EXISTING=foo\n", encoding="utf-8")
    write_env_key(env_path, "NEW_VAR", "bar")
    text = env_path.read_text(encoding="utf-8")
    assert "EXISTING=foo" in text
    assert "NEW_VAR=bar" in text


def test_write_env_key_replaces_existing(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("MY_VAR=old\nOTHER=keep\n", encoding="utf-8")
    write_env_key(env_path, "MY_VAR", "new")
    text = env_path.read_text(encoding="utf-8")
    assert "MY_VAR=new" in text
    assert "MY_VAR=old" not in text
    assert "OTHER=keep" in text


def test_write_env_key_skips_commented_lines(tmp_path: Path):
    """注释行 `# X=1` 不应被当作可替换目标。"""
    env_path = tmp_path / ".env"
    env_path.write_text("# MY_VAR=commented\n", encoding="utf-8")
    write_env_key(env_path, "MY_VAR", "real")
    text = env_path.read_text(encoding="utf-8")
    # 注释保留，新增一行
    assert "# MY_VAR=commented" in text
    assert "MY_VAR=real" in text
    os.environ.pop("MY_VAR", None)


def test_remove_env_key_removes_file_line_and_process_env(tmp_path: Path):
    env_path = tmp_path / ".env"
    env_path.write_text("# MY_VAR=commented\nMY_VAR=secret\nOTHER=keep\n", encoding="utf-8")
    os.environ["MY_VAR"] = "secret"

    remove_env_key(env_path, "MY_VAR")

    text = env_path.read_text(encoding="utf-8")
    assert "# MY_VAR=commented" in text
    assert "MY_VAR=secret" not in text
    assert "OTHER=keep" in text
    assert os.environ.get("MY_VAR") is None


def test_resolve_writable_env_path_returns_under_crew_home(monkeypatch, tmp_path):
    """默认写入 crew_home/.env。"""
    home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))
    p = resolve_writable_env_path()
    assert p == home / ".env"


def test_resolve_writable_env_path_owner_scoped(monkeypatch, tmp_path):
    home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))
    p = resolve_writable_env_path("owner:user-a")
    assert p == home / "accounts" / owner_path_segment("owner:user-a") / ".env"


# ----------------------- Config.activate_model: vision 同步 -----------------------


def test_activate_model_syncs_vision_flag(cfg: Config):
    """capabilities 是视觉能力的唯一运行时来源，legacy vision 仅负责兼容读取。"""
    # 加一个显式关闭 vision 的 profile 并激活
    cfg.add_model({
        "id": "textonly",
        "name": "Text Only",
        "api_key_env": "ALPHA_KEY",
        "model": "text-1",
        "vision": False,
    })
    assert cfg.model_profiles["textonly"].vision is False

    profile = cfg.activate_model("textonly")
    assert profile.vision is False
    # 关键：Config 顶层 vision 必须跟随激活模型，而非保持默认 True
    assert cfg.vision is False

    # 切回 vision=True 的模型，顶层标志应同步回升
    cfg.activate_model("alpha")
    assert cfg.vision is True


def test_capabilities_override_conflicting_legacy_vision(cfg: Config):
    profile = cfg.add_model({
        "id": "capability-text-only",
        "model": "text-only",
        "vision": True,
        "capabilities": ["text", "tools"],
    })

    assert profile.vision is False
    assert profile.supports_vision is False
    assert profile.public_dict()["vision"] is False


def test_legacy_vision_migrates_when_capabilities_are_absent(cfg: Config):
    profile = cfg.add_model({
        "id": "legacy-vision",
        "model": "legacy-vision-model",
        "vision": True,
    })

    assert profile.supports_vision is True
    assert "vision" in profile.capabilities
