"""BrowserConfig 解析与 Config.browser_config 原始透传测试。

核心契约（阶段 6K 起）：core 的 Config 只保存 config.yaml 的 tools.browser
原始 dict，BrowserConfig 的解析与默认值由 browser 侧 `from_raw` 负责；
`from_raw({})` 必须与默认构造逐字段等价——这是"配置缺省"与"解析缺省"
永不分叉的锁。
"""

from __future__ import annotations

from pathlib import Path

import yaml

from crew.browser.types import BrowserConfig
from crew.state.config import Config, load_config


def test_from_raw_empty_equals_default_construction():
    assert BrowserConfig.from_raw({}) == BrowserConfig()


def test_from_raw_none_and_non_dict_fall_back_to_defaults():
    assert BrowserConfig.from_raw(None) == BrowserConfig()
    assert BrowserConfig.from_raw("junk") == BrowserConfig()


def test_default_config_carries_empty_raw_section():
    cfg = Config()
    assert cfg.browser_config == {}
    assert BrowserConfig.from_raw(cfg.browser_config) == BrowserConfig()


def _write_config(tmp_path: Path, data: dict) -> str:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(config_path)


def test_load_config_passes_tools_browser_through_verbatim(tmp_path: Path):
    raw = {
        "enabled": False,
        "headed": True,
        "idle_timeout_seconds": "120",  # 字符串数字，交由 from_raw 收敛
        "command_timeout_seconds": -5,  # 负数，交由 from_raw 钳到 0
        "blocked_hosts": ["  evil.example ", "", None],
        "governance_mode": "bogus",  # 非法档位回落默认
        "max_tabs_per_session": 9,  # 已废除的键：原样保存，from_raw 忽略
    }
    config_path = _write_config(tmp_path, {"tools": {"browser": raw}})

    cfg = load_config(config_path=config_path)
    assert cfg.browser_config == raw
    assert BrowserConfig.from_raw(cfg.browser_config) == BrowserConfig.from_raw(raw)


def test_load_config_normalizes_missing_or_invalid_browser_section(tmp_path: Path):
    assert load_config(
        config_path=_write_config(tmp_path, {})
    ).browser_config == {}

    assert load_config(
        config_path=_write_config(tmp_path, {"tools": {"browser": None}})
    ).browser_config == {}

    assert load_config(
        config_path=_write_config(tmp_path, {"tools": {"browser": "nope"}})
    ).browser_config == {}

    assert load_config(
        config_path=_write_config(tmp_path, {"tools": "nope"})
    ).browser_config == {}


def test_from_raw_reproduces_legacy_parse_semantics():
    parsed = BrowserConfig.from_raw({
        "enabled": "yes",
        "headed": 1,
        "idle_timeout_seconds": "120",
        "command_timeout_seconds": -5,
        "queue_timeout_seconds": -1,
        "blocked_hosts": ["  evil.example ", ""],
        "governance_mode": "read_only",
    })
    assert parsed.enabled is True
    assert parsed.headed is True
    assert parsed.idle_timeout_seconds == 120
    assert parsed.command_timeout_seconds == 0
    assert parsed.queue_timeout_seconds == 0.0
    assert parsed.blocked_hosts == ["evil.example"]
    assert parsed.governance_mode == "read_only"
