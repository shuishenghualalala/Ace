"""Config 层 CRUD 单元测试。

覆盖：
- Config.add_model / update_model / remove_model / set_active_model 的语义
- persist_model_profiles 写回 yaml（结构、不写敏感字段、原子替换）
- write_env_key 写回 .env（新增 / 替换 / 创建）
- MCP server 配置事务：add/update/delete_mcp_server（候选值 → 持久化 → 发布）
  的失败语义，及其 CLI 同型入口（crew.cli.integration）
- 模型 profile / evolution 配置事务（候选值 → 持久化 → 发布）：持久化失败时
  内存、raw_config、磁盘一致保留旧值，真实入口（app / gateway misc / CLI
  knowledge）零运行资源调用，owner 维度按 overlay 隔离
- 边界：id 重复、id 不存在、删除最后一个
- 加载后行为：load_config → CRUD → 再 load，验证持久化生效
"""

from __future__ import annotations

import os
import logging
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from crew.cli.app import CliContext, CliError
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
    """空 owner 归一为 local：默认写入 local 账号目录下的 .env（方案 A 布局）。"""
    home = tmp_path / ".crew"
    monkeypatch.setenv("CREW_HOME", str(home))
    p = resolve_writable_env_path("local")
    assert p == home / "accounts" / owner_path_segment("local") / ".env"


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


# ----------------------- MCP 配置事务（候选值 → 持久化 → 发布）-----------------------


def _mcp_config(tmp_path: Path) -> tuple[Config, Path]:
    """带两个 MCP server 的临时 config.yaml。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "mcp_servers": {
                    "fs": {"command": "run-fs"},
                    "other": {"command": "stable"},
                },
                "runtime": {"log_level": "INFO"},
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return load_config(config_path=str(p)), p


def test_add_mcp_server_persists_then_publishes(tmp_path: Path):
    cfg, p = _mcp_config(tmp_path)

    cfg.add_mcp_server("git", {"command": "run-git"})

    assert cfg.mcp_servers == {
        "fs": {"command": "run-fs"},
        "other": {"command": "stable"},
        "git": {"command": "run-git"},
    }
    reloaded = load_config(config_path=str(p))
    assert reloaded.mcp_servers == cfg.mcp_servers
    assert cfg.raw_config["mcp_servers"]["git"] == {"command": "run-git"}


def test_add_mcp_server_duplicate_rejected_without_side_effects(tmp_path: Path):
    cfg, p = _mcp_config(tmp_path)

    with pytest.raises(ValueError, match="已存在"):
        cfg.add_mcp_server("fs", {"command": "run-git"})

    assert cfg.mcp_servers["fs"] == {"command": "run-fs"}
    assert load_config(config_path=str(p)).mcp_servers["fs"] == {"command": "run-fs"}


def test_update_delete_mcp_server_missing_rejected_without_side_effects(tmp_path: Path):
    cfg, p = _mcp_config(tmp_path)

    with pytest.raises(KeyError):
        cfg.update_mcp_server("nope", {"command": "x"})
    with pytest.raises(KeyError):
        cfg.delete_mcp_server("nope")

    reloaded = load_config(config_path=str(p))
    assert reloaded.mcp_servers == {"fs": {"command": "run-fs"}, "other": {"command": "stable"}}


def test_mcp_update_persist_failure_keeps_business_fields_and_disk(tmp_path: Path, monkeypatch):
    """持久化失败：业务字段 cfg.mcp_servers、raw_config、磁盘一致保留旧值。"""
    cfg, p = _mcp_config(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(OSError, match="simulated write failure"):
        cfg.update_mcp_server("fs", {"command": "run-fs-new"})

    assert cfg.mcp_servers["fs"] == {"command": "run-fs"}
    assert cfg.mcp_servers["other"] == {"command": "stable"}
    assert cfg.raw_config["mcp_servers"]["fs"] == {"command": "run-fs"}
    reloaded = load_config(config_path=str(p))
    assert reloaded.mcp_servers["fs"] == {"command": "run-fs"}
    # 失败不留残留 tmp
    assert not p.with_suffix(p.suffix + ".tmp").exists()


def test_mcp_update_failure_then_retry_applies_without_losing_updates(tmp_path: Path, monkeypatch):
    """失败后重试不丢更新：v1 → (失败) → v1 → v2，other server 全程不受影响。"""
    cfg, p = _mcp_config(tmp_path)

    real_safe_dump = yaml.safe_dump
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("simulated write failure")
        return real_safe_dump(*args, **kwargs)

    monkeypatch.setattr(yaml, "safe_dump", flaky)

    with pytest.raises(OSError):
        cfg.update_mcp_server("fs", {"command": "v2"})
    assert cfg.mcp_servers["fs"] == {"command": "run-fs"}

    cfg.update_mcp_server("fs", {"command": "v2"})
    assert cfg.mcp_servers["fs"] == {"command": "v2"}
    assert cfg.mcp_servers["other"] == {"command": "stable"}
    reloaded = load_config(config_path=str(p))
    assert reloaded.mcp_servers["fs"] == {"command": "v2"}
    assert reloaded.mcp_servers["other"] == {"command": "stable"}


def test_mcp_delete_persist_failure_keeps_business_fields_and_disk(tmp_path: Path, monkeypatch):
    cfg, p = _mcp_config(tmp_path)

    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(OSError):
        cfg.delete_mcp_server("fs")

    assert "fs" in cfg.mcp_servers
    assert cfg.raw_config["mcp_servers"]["fs"] == {"command": "run-fs"}
    assert load_config(config_path=str(p)).mcp_servers["fs"] == {"command": "run-fs"}


# ----------------------- MCP 配置事务的 CLI 同型入口 -----------------------


class _SpyMcpManager:
    """记录运行资源调用的假 manager：保存失败路径必须零调用。"""

    def __init__(self, *, remove_raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self._remove_raises = remove_raises

    async def start(self, _registry) -> None:
        self.calls.append("start")

    def register_pending(self, name, _cfg) -> None:
        self.calls.append(f"register_pending:{name}")

    async def add_server(self, name, _cfg) -> bool:
        self.calls.append(f"add_server:{name}")
        return True

    async def reload_one(self, name, _cfg=None) -> bool:
        self.calls.append(f"reload_one:{name}")
        return True

    async def remove_server(self, name) -> bool:
        self.calls.append(f"remove_server:{name}")
        if self._remove_raises is not None:
            raise self._remove_raises
        return True

    def status(self) -> list[dict]:
        return []


def _cli_ctx(cfg: Config, manager) -> CliContext:
    """CLI handler 只消费 config / mcp_manager / registry，最小假 app 即可。"""
    return CliContext(owner="local", _app=SimpleNamespace(config=cfg, mcp_manager=manager, registry=None))


def _cli_args(*argv: str):
    from crew.cli.main import build_parser

    return build_parser().parse_args(list(argv))


def test_cli_update_persist_failure_keeps_state_and_skips_runtime(tmp_path: Path, monkeypatch):
    from crew.cli.integration import _mcp_servers_update

    cfg, p = _mcp_config(tmp_path)
    manager = _SpyMcpManager()
    ctx = _cli_ctx(cfg, manager)
    args = _cli_args("mcp", "servers", "update", "--name", "fs", "--command", "v2")

    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(CliError, match="持久化失败"):
        _run(_mcp_servers_update(args, ctx))

    assert cfg.mcp_servers["fs"] == {"command": "run-fs"}
    assert load_config(config_path=str(p)).mcp_servers["fs"] == {"command": "run-fs"}
    assert manager.calls == []


def test_cli_delete_persist_failure_keeps_state_and_skips_runtime(tmp_path: Path, monkeypatch):
    from crew.cli.integration import _mcp_servers_delete

    cfg, p = _mcp_config(tmp_path)
    manager = _SpyMcpManager()
    ctx = _cli_ctx(cfg, manager)
    args = _cli_args("mcp", "servers", "delete", "--name", "fs")

    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(CliError, match="持久化失败"):
        _run(_mcp_servers_delete(args, ctx))

    assert "fs" in cfg.mcp_servers
    assert load_config(config_path=str(p)).mcp_servers["fs"] == {"command": "run-fs"}
    assert manager.calls == []


def test_cli_delete_saved_but_runtime_failure_reports_honestly(tmp_path: Path):
    from crew.cli.integration import _mcp_servers_delete

    cfg, p = _mcp_config(tmp_path)
    manager = _SpyMcpManager(remove_raises=RuntimeError("worker stop failed"))
    ctx = _cli_ctx(cfg, manager)
    args = _cli_args("mcp", "servers", "delete", "--name", "fs")

    with pytest.raises(CliError, match="配置已保存"):
        _run(_mcp_servers_delete(args, ctx))

    # 配置（磁盘+内存）保持"已删除"的新值，运行资源操作错误如实上抛
    assert "fs" not in cfg.mcp_servers
    assert load_config(config_path=str(p)).mcp_servers == {"other": {"command": "stable"}}
    assert manager.calls == ["start", "remove_server:fs"]


def test_cli_add_persist_failure_keeps_state_and_skips_runtime(tmp_path: Path, monkeypatch):
    from crew.cli.integration import _mcp_servers_add

    cfg, p = _mcp_config(tmp_path)
    manager = _SpyMcpManager()
    ctx = _cli_ctx(cfg, manager)
    args = _cli_args("mcp", "servers", "add", "--name", "git", "--command", "run-git")

    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(CliError, match="持久化失败"):
        _run(_mcp_servers_add(args, ctx))

    assert "git" not in cfg.mcp_servers
    assert load_config(config_path=str(p)).mcp_servers == {
        "fs": {"command": "run-fs"},
        "other": {"command": "stable"},
    }
    assert manager.calls == []


def _run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


# ----------------------- 同型核查转正：模型 profile / evolution 配置事务 -----------------------
#
# S2 审查固化的两处"先改已发布内存、后调 persist"缺陷已修复：模型 profile 写路径
# （Config.add/update/remove_model、set_active_model）与 evolution 写路径
# （Config.set_evolution_config）均迁移到候选值事务（候选值 → 校验 →
# _atomic_write_yaml 持久化 → 磁盘成功后发布内存）。持久化失败时内存业务字段、
# raw_config、磁盘三者一致保留旧值，可直接重试。


def test_model_profile_update_persist_failure_should_keep_memory_old_value(
    cfg: Config, tmp_yaml: Path, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    before = cfg.model_profiles["alpha"].temperature
    before_raw = deepcopy(cfg.raw_config)
    with pytest.raises(OSError, match="simulated write failure"):
        cfg.update_model("alpha", {"temperature": 0.99})
    # 内存业务字段保留旧值（缺陷已修：不再先改内存后持久化）
    assert cfg.model_profiles["alpha"].temperature == before
    assert cfg.raw_config == before_raw
    # 磁盘同样保留旧值，失败不留残留 tmp
    reloaded = load_config(config_path=str(tmp_yaml))
    assert reloaded.model_profiles["alpha"].temperature == before
    assert not tmp_yaml.with_suffix(tmp_yaml.suffix + ".tmp").exists()


def test_evolution_persist_failure_should_keep_memory_old_value(
    cfg: Config, tmp_yaml: Path, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    before = (cfg.evolution_auto_trigger, cfg.evolution_auto_full_cycle, cfg.evolution_visible)
    before_raw = deepcopy(cfg.raw_config)
    with pytest.raises(OSError, match="simulated write failure"):
        cfg.set_evolution_config(auto_trigger=True)
    assert (cfg.evolution_auto_trigger, cfg.evolution_auto_full_cycle, cfg.evolution_visible) == before
    assert cfg.raw_config == before_raw
    reloaded = load_config(config_path=str(tmp_yaml))
    assert "evolution" not in reloaded.raw_config.get("agent", {})
    assert not tmp_yaml.with_suffix(tmp_yaml.suffix + ".tmp").exists()


def test_model_update_failure_then_retry_applies_without_losing_updates(
    cfg: Config, tmp_yaml: Path, monkeypatch
):
    """失败后重试不丢更新：0.5 → (失败) → 0.5 → 0.3。"""
    real_safe_dump = yaml.safe_dump
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("simulated write failure")
        return real_safe_dump(*args, **kwargs)

    monkeypatch.setattr(yaml, "safe_dump", flaky)

    with pytest.raises(OSError):
        cfg.update_model("alpha", {"temperature": 0.2})
    assert cfg.model_profiles["alpha"].temperature == 0.5

    cfg.update_model("alpha", {"temperature": 0.3})
    assert cfg.model_profiles["alpha"].temperature == 0.3
    reloaded = load_config(config_path=str(tmp_yaml))
    assert reloaded.model_profiles["alpha"].temperature == 0.3


# ----------------------- Config.set_active_model（use_model / 激活切换共用事务）-----------------------


def test_set_active_model_persists_active_and_default(cfg: Config, tmp_yaml: Path):
    profile = cfg.set_active_model("beta")

    assert profile.id == "beta"
    assert cfg.active_model_id == "beta"
    assert cfg.default_model_id == "beta"
    # 激活派生字段跟随新激活模型发布
    assert cfg.model == "beta-1"
    reloaded = load_config(config_path=str(tmp_yaml))
    assert reloaded.active_model_id == "beta"
    assert reloaded.default_model_id == "beta"


def test_set_active_model_rejects_unloaded_without_side_effects(cfg: Config, tmp_yaml: Path):
    cfg.model_profiles["beta"].loaded = False
    with pytest.raises(ValueError, match="未加载"):
        cfg.set_active_model("beta")

    assert cfg.active_model_id == "alpha"
    assert load_config(config_path=str(tmp_yaml)).active_model_id == "alpha"


def test_set_active_model_persist_failure_keeps_memory_and_disk(
    cfg: Config, tmp_yaml: Path, monkeypatch
):
    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)
    with pytest.raises(OSError, match="simulated write failure"):
        cfg.set_active_model("beta")

    assert cfg.active_model_id == "alpha"
    assert cfg.model == "alpha-1"
    reloaded = load_config(config_path=str(tmp_yaml))
    assert reloaded.active_model_id == "alpha"
    assert "default" not in (reloaded.raw_config.get("llm") or {})


# ----------------------- 模型 CRUD / evolution 真实入口事务 -----------------------
#
# 从 app（use/add/update/remove_model）、gateway misc（PUT /api/skills/evolution）、
# CLI knowledge（skill evolution）真实调用路径注入写盘失败：内存业务字段、
# raw_config、磁盘三者一致保留旧值，运行资源操作零调用。


def _model_app_config(tmp_path: Path) -> tuple[Config, Path]:
    """带 2 个已加载模型的隔离 config.yaml（app 层真实入口用）。"""
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "active": "alpha",
                    "models": {
                        "alpha": {
                            "name": "Alpha",
                            "api_key_env": "ALPHA_API_KEY",
                            "base_url": "https://alpha.example.com/v1",
                            "model": "alpha-1",
                            "temperature": 0.5,
                        },
                        "beta": {
                            "name": "Beta",
                            "api_key_env": "BETA_API_KEY",
                            "base_url": "https://beta.example.com/v1",
                            "model": "beta-1",
                        },
                    },
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return load_config(config_path=str(p)), p


@pytest.fixture
def model_app(tmp_path: Path, monkeypatch):
    """yaml 化配置 + CrewApp（enable_team=False），env key 注入后清理。"""
    from crew.app import build_app

    monkeypatch.setenv("CREW_HOME", str(tmp_path / ".crew"))
    os.environ["ALPHA_API_KEY"] = "sk-alpha"
    os.environ["BETA_API_KEY"] = "sk-beta"
    cfg, p = _model_app_config(tmp_path)
    cfg.db_path = str(tmp_path / "crew.db")
    cfg.memory_db_path = str(tmp_path / "memory.db")
    app = build_app(config=cfg, enable_team=False)
    yield app, cfg, p
    os.environ.pop("ALPHA_API_KEY", None)
    os.environ.pop("BETA_API_KEY", None)


def _spy_model_runtime_resources(app, monkeypatch) -> list[str]:
    """记录运行资源调用：持久化失败路径必须零调用。"""
    import crew.app as app_module

    calls: list[str] = []
    monkeypatch.setattr(app_module, "build_provider", lambda _cfg: calls.append("build_provider"))
    monkeypatch.setattr(app.agents, "clear", lambda *a, **k: calls.append("agents.clear"))
    monkeypatch.setattr(app.agents, "drop_owner", lambda *a, **k: calls.append("agents.drop_owner"))
    monkeypatch.setattr(
        app, "_invalidate_owner_team_provider", lambda *a, **k: calls.append("invalidate_team_provider")
    )
    monkeypatch.setattr(
        app, "_sync_default_provider_to_features", lambda *a, **k: calls.append("sync_features")
    )
    monkeypatch.setattr(
        app, "_schedule_provider_retirement", lambda *a, **k: calls.append("retire_provider")
    )
    monkeypatch.setattr("crew.app.remove_env_key", lambda *a, **k: calls.append("remove_env_key"))
    monkeypatch.setattr("crew.app.delete_stored_key", lambda *a, **k: calls.append("delete_stored_key"))
    return calls


def _write_boom(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)


def test_app_use_model_persist_failure_keeps_state_and_skips_runtime(
    model_app, monkeypatch
):
    app, cfg, p = model_app
    calls = _spy_model_runtime_resources(app, monkeypatch)
    _write_boom(monkeypatch)

    with pytest.raises(OSError, match="simulated write failure"):
        app.use_model("beta", owner_account_id="")

    assert cfg.active_model_id == "alpha"
    assert cfg.default_model_id == ""
    assert cfg.model == "alpha-1"
    assert cfg.raw_config["llm"]["active"] == "alpha"
    assert load_config(config_path=str(p)).active_model_id == "alpha"
    assert calls == []


def test_app_use_model_failure_then_retry_persists_new_active(model_app, monkeypatch):
    app, cfg, p = model_app
    _spy_model_runtime_resources(app, monkeypatch)
    real_safe_dump = yaml.safe_dump
    state = {"calls": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise OSError("simulated write failure")
        return real_safe_dump(*args, **kwargs)

    monkeypatch.setattr(yaml, "safe_dump", flaky)

    with pytest.raises(OSError):
        app.use_model("beta", owner_account_id="")
    app.use_model("beta", owner_account_id="")

    assert cfg.active_model_id == "beta"
    reloaded = load_config(config_path=str(p))
    assert reloaded.active_model_id == "beta"
    assert reloaded.default_model_id == "beta"


def test_app_add_model_persist_failure_keeps_state_and_skips_runtime(
    model_app, monkeypatch
):
    app, cfg, p = model_app
    calls = _spy_model_runtime_resources(app, monkeypatch)
    _write_boom(monkeypatch)

    with pytest.raises(OSError, match="simulated write failure"):
        app.add_model({"id": "gamma", "model": "g-1"}, owner_account_id="")

    assert "gamma" not in cfg.model_profiles
    assert "gamma" not in cfg.raw_config["llm"]["models"]
    assert "gamma" not in load_config(config_path=str(p)).model_profiles
    assert calls == []


def test_app_update_model_persist_failure_keeps_state_and_skips_runtime(
    model_app, monkeypatch
):
    app, cfg, p = model_app
    calls = _spy_model_runtime_resources(app, monkeypatch)
    _write_boom(monkeypatch)

    with pytest.raises(OSError, match="simulated write failure"):
        app.update_model("alpha", {"temperature": 0.99}, owner_account_id="")

    assert cfg.model_profiles["alpha"].temperature == 0.5
    assert cfg.raw_config["llm"]["models"]["alpha"]["temperature"] == 0.5
    reloaded = load_config(config_path=str(p))
    assert reloaded.model_profiles["alpha"].temperature == 0.5
    assert calls == []


def test_app_remove_model_persist_failure_keeps_state_and_skips_runtime(
    model_app, monkeypatch
):
    app, cfg, p = model_app
    calls = _spy_model_runtime_resources(app, monkeypatch)
    _write_boom(monkeypatch)

    with pytest.raises(OSError, match="simulated write failure"):
        app.remove_model("beta", owner_account_id="")

    assert "beta" in cfg.model_profiles
    assert "beta" in cfg.raw_config["llm"]["models"]
    assert "beta" in load_config(config_path=str(p)).model_profiles
    assert calls == []


def test_app_remove_active_model_persist_failure_keeps_state_and_skips_runtime(
    model_app, monkeypatch
):
    app, cfg, p = model_app
    calls = _spy_model_runtime_resources(app, monkeypatch)
    _write_boom(monkeypatch)

    with pytest.raises(OSError, match="simulated write failure"):
        app.remove_model("alpha", owner_account_id="")

    assert "alpha" in cfg.model_profiles
    assert cfg.active_model_id == "alpha"
    assert load_config(config_path=str(p)).active_model_id == "alpha"
    assert calls == []


def test_app_remove_active_model_switch_is_persisted(model_app):
    """删除激活模型：自动切换必须持久化到磁盘（active + default 同步落盘）。"""
    app, cfg, p = model_app

    result = app.remove_model("alpha", owner_account_id="")

    assert result["switched_to"] == "beta"
    assert cfg.active_model_id == "beta"
    reloaded = load_config(config_path=str(p))
    assert "alpha" not in reloaded.model_profiles
    assert reloaded.active_model_id == "beta"
    assert reloaded.default_model_id == "beta"


def test_owner_model_persist_failure_is_isolated_per_owner(model_app, monkeypatch):
    """owner 维度按 overlay 文件隔离：owner-a 持久化失败不影响 owner-b 与全局层。"""
    from crew.state.config import Config as ConfigCls

    app, cfg, p = model_app
    original = ConfigCls.persist_owner_model_profiles

    def flaky(self, owner, profiles, *, active_model_id=None):
        if owner == "acc-a":
            raise OSError("simulated overlay write failure")
        return original(self, owner, profiles, active_model_id=active_model_id)

    monkeypatch.setattr(ConfigCls, "persist_owner_model_profiles", flaky)

    with pytest.raises(OSError, match="simulated overlay write failure"):
        app.add_model({"id": "own-a", "model": "a-1"}, owner_account_id="acc-a")
    app.add_model({"id": "own-b", "model": "b-1"}, owner_account_id="acc-b")

    # 失败的 owner-a：overlay 不含新模型（可整体重试）
    assert "own-a" not in (cfg.owner_overlay_data("acc-a").get("llm") or {}).get("models", {})
    # 成功的 owner-b：私有模型只落在自己的 overlay
    assert "own-b" in (cfg.owner_overlay_data("acc-b").get("llm") or {}).get("models", {})
    # 全局共享层不受任何 owner 操作影响
    assert "own-a" not in cfg.model_profiles
    assert "own-b" not in cfg.model_profiles
    assert "own-a" not in load_config(config_path=str(p)).model_profiles
    assert "own-b" not in load_config(config_path=str(p)).model_profiles


def test_gateway_evolution_persist_failure_keeps_state_and_disk(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from crew.gateway.auth import AccountContext
    from crew.gateway.routers import misc as misc_router

    cfg, p = _model_app_config(tmp_path)
    crew = SimpleNamespace(config=cfg)

    app = FastAPI()
    app.include_router(misc_router.create_misc_router(crew))
    client = TestClient(app)
    monkeypatch.setattr(
        misc_router, "account_from_request", lambda _request: AccountContext(owner_account_id="A:admin")
    )
    monkeypatch.setattr(misc_router, "require_admin", lambda _account, _cfg: None)
    _write_boom(monkeypatch)

    resp = client.put("/api/skills/evolution", json={"auto_trigger": True})

    assert resp.status_code == 500
    assert resp.json()["error"].startswith("持久化失败")
    assert cfg.evolution_auto_trigger is False
    assert "evolution" not in load_config(config_path=str(p)).raw_config.get("agent", {})
    assert not p.with_suffix(p.suffix + ".tmp").exists()


def test_cli_evolution_persist_failure_keeps_state_and_disk(tmp_path, monkeypatch):
    from crew.cli.knowledge import _skill_evolution

    cfg, p = _model_app_config(tmp_path)
    ctx = CliContext(owner="local", _app=SimpleNamespace(config=cfg))
    args = SimpleNamespace(auto_trigger=True, auto_full_cycle=None, visible=None)
    _write_boom(monkeypatch)

    with pytest.raises(CliError, match="持久化失败"):
        _skill_evolution(args, ctx)

    assert cfg.evolution_auto_trigger is False
    assert "evolution" not in load_config(config_path=str(p)).raw_config.get("agent", {})


def test_cli_evolution_success_persists_and_publishes(tmp_path):
    from crew.cli.knowledge import _skill_evolution

    cfg, p = _model_app_config(tmp_path)
    ctx = CliContext(owner="local", _app=SimpleNamespace(config=cfg))
    args = SimpleNamespace(auto_trigger=True, auto_full_cycle=None, visible=None)

    result = _skill_evolution(args, ctx)

    assert result.data["auto_trigger"] is True
    reloaded = load_config(config_path=str(p))
    assert reloaded.evolution_auto_trigger is True
    assert reloaded.raw_config["agent"]["evolution"] == {
        "auto_trigger": True,
        "auto_full_cycle": False,
        "visible": False,
    }
