"""渠道配置写回的隔离性与事务性测试。

锁定两个写回契约：
- 隔离性（A-1）：_write_channel_config 在修改目标渠道有效配置前必须与源数据
  脱钩（deepcopy）。YAML anchor 共享嵌套 dict（extra 等）时，删除目标渠道
  字段不得串改兄弟渠道条目或调用方传入的 config_data。
- 事务性（A-2）：persist 失败（tmp 写入失败 / replace 失败）时，内存状态
  （channels / platforms / raw_config）与磁盘必须一致保留旧值，异常正常
  传播；成功时内存与磁盘一致更新。persist_mcp_servers 同契约。
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from crew.state.config import _write_channel_config, load_config

ANCHOR_SHARED_PLATFORM_EXTRA = """\
platforms:
  feishu:
    enabled: true
    extra: &shared_extra
      token: secret-a
      keep: keep-a
  weixin:
    enabled: true
    extra: *shared_extra
"""

ANCHOR_SHARED_CHANNEL_NESTED_EXTRA = """\
channels:
  feishu:
    extra: &shared_nested
      auth:
        secret: s
        keep: k
  weixin:
    extra: *shared_nested
"""


def _write_config_text(tmp_path: Path, text: str) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(text, encoding="utf-8")
    return config_path


def _memory_snapshot(cfg) -> tuple[dict, dict, dict]:
    return deepcopy(cfg.channels), deepcopy(cfg.platforms), deepcopy(cfg.raw_config)


# ----------------------- A-1：配置隔离 -----------------------


def test_anchor_shared_platform_extra_removal_is_isolated_per_channel(tmp_path: Path):
    """platforms 两个渠道经 anchor 共享 extra：删除 A 的字段不得影响 B。"""
    config_path = _write_config_text(tmp_path, ANCHOR_SHARED_PLATFORM_EXTRA)
    cfg = load_config(config_path=config_path)
    assert (
        cfg.raw_config["platforms"]["feishu"]["extra"]
        is cfg.raw_config["platforms"]["weixin"]["extra"]
    )

    cfg.persist_channel_config("feishu", {"_remove_keys": ["token"]})

    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert written["platforms"]["weixin"]["extra"] == {"token": "secret-a", "keep": "keep-a"}
    assert written["features"]["channels"]["feishu"]["extra"] == {"keep": "keep-a"}
    assert "feishu" not in written.get("channels", {})
    reloaded = load_config(config_path=config_path)
    assert reloaded.channel_config("weixin") == {
        "enabled": True,
        "extra": {"token": "secret-a", "keep": "keep-a"},
    }
    assert reloaded.channel_config("feishu") == {
        "enabled": True,
        "extra": {"keep": "keep-a"},
    }


def test_anchor_shared_channel_nested_extra_removal_is_isolated_per_channel(tmp_path: Path):
    """channels 旧节共享嵌套 extra（内层任意深度对象）：删除只影响目标渠道。"""
    config_path = _write_config_text(tmp_path, ANCHOR_SHARED_CHANNEL_NESTED_EXTRA)
    cfg = load_config(config_path=config_path)
    assert (
        cfg.raw_config["channels"]["feishu"]["extra"]
        is cfg.raw_config["channels"]["weixin"]["extra"]
    )

    cfg.persist_channel_config("feishu", {"_remove_keys": ["auth"]})

    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert written["channels"]["weixin"]["extra"] == {"auth": {"secret": "s", "keep": "k"}}
    reloaded = load_config(config_path=config_path)
    assert reloaded.channel_config("weixin") == {"extra": {"auth": {"secret": "s", "keep": "k"}}}
    assert reloaded.channel_config("feishu") == {}


def test_write_channel_config_does_not_mutate_input_or_sibling_entries():
    """纯函数级：config_data 与 data 中其他渠道对象（含共享嵌套）不被串改。"""
    data = yaml.safe_load(ANCHOR_SHARED_PLATFORM_EXTRA)
    payload = {"_remove_keys": ["token"], "note": {"deep": {"k": 1}}}
    payload_before = deepcopy(payload)

    _write_channel_config(data, "feishu", payload)

    assert payload == payload_before
    assert data["platforms"]["weixin"]["extra"] == {"token": "secret-a", "keep": "keep-a"}
    assert data["features"]["channels"]["feishu"]["extra"] == {"keep": "keep-a"}
    assert data["features"]["channels"]["feishu"]["note"] == {"deep": {"k": 1}}


# ----------------------- A-2：配置事务 -----------------------


def test_persist_channel_write_failure_keeps_memory_and_disk_on_old_values(
    tmp_path: Path, monkeypatch
):
    config_path = _write_config_text(
        tmp_path,
        """\
channels:
  feishu:
    token: old
platforms:
  weixin:
    token: wx
""",
    )
    cfg = load_config(config_path=config_path)
    before_channels, before_platforms, before_raw = _memory_snapshot(cfg)

    def boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)

    with pytest.raises(OSError, match="simulated write failure"):
        cfg.persist_channel_config("feishu", {"token": "new"})

    assert cfg.channels == before_channels
    assert cfg.platforms == before_platforms
    assert cfg.raw_config == before_raw
    assert load_config(config_path=config_path).channel_config("feishu") == {"token": "old"}


def test_persist_channel_replace_failure_keeps_memory_and_disk_on_old_values(
    tmp_path: Path, monkeypatch
):
    config_path = _write_config_text(
        tmp_path,
        """\
channels:
  feishu:
    token: old
platforms:
  weixin:
    token: wx
""",
    )
    cfg = load_config(config_path=config_path)
    before_channels, before_platforms, before_raw = _memory_snapshot(cfg)

    def boom(self, target):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", boom)

    with pytest.raises(OSError, match="simulated replace failure"):
        cfg.persist_channel_config("feishu", {"token": "new"})

    assert cfg.channels == before_channels
    assert cfg.platforms == before_platforms
    assert cfg.raw_config == before_raw
    assert load_config(config_path=config_path).channel_config("feishu") == {"token": "old"}


def test_persist_channel_success_updates_memory_and_disk_together(tmp_path: Path):
    config_path = _write_config_text(
        tmp_path,
        """\
channels:
  feishu:
    token: old
platforms:
  weixin:
    token: wx
""",
    )
    cfg = load_config(config_path=config_path)

    cfg.persist_channel_config("feishu", {"token": "new"})

    assert cfg.channels["feishu"] == {"token": "new"}
    assert cfg.platforms == {"weixin": {"token": "wx"}}
    assert cfg.raw_config["features"]["channels"]["feishu"] == {"token": "new"}
    reloaded = load_config(config_path=config_path)
    assert reloaded.channel_config("feishu") == {"token": "new"}
    assert reloaded.channel_config("weixin") == {"token": "wx"}


def test_persist_mcp_servers_write_failure_keeps_raw_config_and_disk_on_old_values(
    tmp_path: Path, monkeypatch
):
    config_path = _write_config_text(
        tmp_path,
        """\
mcp_servers:
  fs:
    command: run-fs
runtime:
  log_level: INFO
""",
    )
    cfg = load_config(config_path=config_path)
    cfg.set_mcp_server("git", {"command": "run-git"})
    # 快照取在 persist 之前：set_mcp_server 是运行时字段更新（经加载时的
    # 共享引用可被 raw_config 观察到，属既有行为），本测试锁定的是 persist
    # 自身的事务性——失败时 raw_config 引用与磁盘都不因写回而改变。
    before_raw = deepcopy(cfg.raw_config)

    def boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)

    with pytest.raises(OSError, match="simulated write failure"):
        cfg.persist_mcp_servers()

    assert cfg.raw_config == before_raw
    reloaded = load_config(config_path=config_path)
    assert reloaded.raw_config["mcp_servers"] == {"fs": {"command": "run-fs"}}


def test_persist_model_profiles_write_failure_keeps_raw_config_and_disk_on_old_values(
    tmp_path: Path, monkeypatch
):
    config_path = _write_config_text(
        tmp_path,
        """\
runtime:
  log_level: INFO
""",
    )
    cfg = load_config(config_path=config_path)
    before_raw = deepcopy(cfg.raw_config)

    def boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)

    with pytest.raises(OSError, match="simulated write failure"):
        cfg.persist_model_profiles()

    assert cfg.raw_config == before_raw
    reloaded = load_config(config_path=config_path)
    assert "llm" not in reloaded.raw_config


def test_persist_evolution_config_write_failure_keeps_raw_config_and_disk_on_old_values(
    tmp_path: Path, monkeypatch
):
    config_path = _write_config_text(
        tmp_path,
        """\
runtime:
  log_level: INFO
""",
    )
    cfg = load_config(config_path=config_path)
    before_raw = deepcopy(cfg.raw_config)

    def boom(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(yaml, "safe_dump", boom)

    with pytest.raises(OSError, match="simulated write failure"):
        cfg.persist_evolution_config()

    assert cfg.raw_config == before_raw
    reloaded = load_config(config_path=config_path)
    assert "evolution" not in reloaded.raw_config.get("agent", {})


def test_persist_mcp_servers_success_updates_raw_config_and_disk_together(tmp_path: Path):
    config_path = _write_config_text(
        tmp_path,
        """\
mcp_servers:
  fs:
    command: run-fs
""",
    )
    cfg = load_config(config_path=config_path)
    cfg.set_mcp_server("git", {"command": "run-git"})

    cfg.persist_mcp_servers()

    assert cfg.raw_config["mcp_servers"] == {
        "fs": {"command": "run-fs"},
        "git": {"command": "run-git"},
    }
    reloaded = load_config(config_path=config_path)
    assert reloaded.raw_config["mcp_servers"] == {
        "fs": {"command": "run-fs"},
        "git": {"command": "run-git"},
    }
