"""features.* 配置命名空间双读测试（命名审计 P2-1 · 渐进第一步）。

锁定 load_config 的通用双读语义：
- 同名键 features 侧获胜，旧节兜底（含嵌套 dict 深合并、list 整体替换）
- 只新节 / 只旧节 / 两者皆无时分别走新值、旧值、代码默认值
- features 节或子节类型非法时容错，旧节继续生效
- runtime.dk_* 平铺键映射到 features.dynamic_kanban、tools.browser 映射到 features.browser
- 库路径（runtime.<feature>_db_path）不进 features.*（core 基础设施，锁定设计决策）
- 顶层 subagent: 节保持死配置语义，example 模板已移除该段（P3-1）
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import yaml

import crew.state.config as config_module
from crew.state.config import _apply_features_namespace, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_config(tmp_path: Path, data: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return config_path


# ----------------------- 合并语义：新节优先、旧节兜底 -----------------------


def test_features_key_wins_over_legacy_section(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "cron": {"enabled": False, "max_parallel_jobs": 1},
            "features": {"cron": {"enabled": True}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.cron_enabled is True
    # features 未出现的键回落旧节
    assert cfg.cron_max_parallel_jobs == 1


def test_features_only_section_applies(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {"features": {"cron": {"enabled": False, "max_parallel_jobs": 5}}},
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.cron_enabled is False
    assert cfg.cron_max_parallel_jobs == 5


def test_legacy_only_sections_keep_existing_behavior(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "cron": {"enabled": False, "max_parallel_jobs": 4},
            "external_agents": {"enabled": False, "security_enabled": True},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.cron_enabled is False
    assert cfg.cron_max_parallel_jobs == 4
    assert cfg.external_agents_enabled is False
    assert cfg.external_security_enabled is True


def test_neither_section_uses_code_defaults(tmp_path: Path):
    config_path = _write_config(tmp_path, {})

    cfg = load_config(config_path=str(config_path))

    assert cfg.cron_enabled is True
    assert cfg.cron_max_parallel_jobs == 2
    assert cfg.external_agents_enabled is True
    assert cfg.wiki_config == {}


def test_nested_dicts_deep_merge_with_legacy_fallback(tmp_path: Path):
    """嵌套 dict 逐层合并：features 覆盖同路径叶子，其余路径回落旧节。"""
    config_path = _write_config(
        tmp_path,
        {
            "wiki": {
                "model": "legacy-model",
                "ingest": {"auto_summarize": True, "auto_apply": False},
            },
            "features": {"wiki": {"ingest": {"auto_apply": True}}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.wiki_config["ingest"]["auto_apply"] is True
    assert cfg.wiki_config["ingest"]["auto_summarize"] is True
    assert cfg.wiki_config["model"] == "legacy-model"


def test_tasks_section_maps_through_including_sub_sections(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "tasks": {"auto_background_after_seconds": 5, "subagent": {"inactivity_timeout_seconds": 11}},
            "features": {"tasks": {"subagent": {"execution_timeout_seconds": 22}}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.tasks_auto_background_after_seconds == 5.0
    assert cfg.tasks_subagent_inactivity_timeout_seconds == 11.0
    assert cfg.tasks_subagent_execution_timeout_seconds == 22.0


def test_features_team_channels_external_agents_roundtrip(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "features": {
                "team": {"members": [{"name": "a", "role": "r"}]},
                "channels": {"feishu": {"token": "t-1"}},
                "external_agents": {"enabled": False},
            },
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.team_config["members"] == [{"name": "a", "role": "r"}]
    assert cfg.channels["feishu"]["token"] == "t-1"
    assert cfg.channel_config("feishu")["token"] == "t-1"
    assert cfg.external_agents_enabled is False


# ----------------------- 平铺键映射：dk_* 与 tools.browser -----------------------


def test_dynamic_kanban_flat_keys_map_from_features(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {"features": {"dynamic_kanban": {"task_timeout_seconds": 42.5, "verification_gate_enabled": False}}},
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.dk_task_timeout_seconds == 42.5
    assert cfg.dk_verification_gate_enabled is False
    assert cfg.raw_config["runtime"]["dk_verification_gate_enabled"] is False


def test_dynamic_kanban_verification_gate_reads_legacy_flat_key(tmp_path: Path):
    """存量缺口修复：runtime.dk_verification_gate_enabled 自 loader 起真正生效。"""
    config_path = _write_config(
        tmp_path,
        {"runtime": {"dk_verification_gate_enabled": False}},
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.dk_verification_gate_enabled is False


def test_dynamic_kanban_features_key_wins_over_runtime_flat_key(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "runtime": {"dk_task_timeout_seconds": 1},
            "features": {"dynamic_kanban": {"task_timeout_seconds": 2}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.dk_task_timeout_seconds == 2.0


def test_dynamic_kanban_runtime_flat_key_still_works_alone(tmp_path: Path):
    config_path = _write_config(tmp_path, {"runtime": {"dk_task_timeout_seconds": 111}})

    cfg = load_config(config_path=str(config_path))

    assert cfg.dk_task_timeout_seconds == 111.0


def test_browser_features_section_maps_to_tools_browser(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "tools": {"browser": {"enabled": False, "idle_timeout_seconds": 99}},
            "features": {"browser": {"enabled": True}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.browser_config["enabled"] is True
    assert cfg.browser_config["idle_timeout_seconds"] == 99


# ----------------------- 容错与边界 -----------------------


def test_non_dict_features_section_is_tolerated(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {"cron": {"enabled": False}, "features": 42},
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.cron_enabled is False


def test_non_dict_feature_subsection_is_skipped_others_still_apply(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "features": {"wiki": "oops", "cron": {"enabled": False}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.wiki_config == {}
    assert cfg.cron_enabled is False


def test_unknown_feature_names_are_ignored(tmp_path: Path):
    base = {"runtime": {"log_level": "DEBUG"}}
    with_features = {
        **base,
        "features": {"sites": {"future_key": 1}, "some_future_feature": {"x": 2}},
    }

    cfg_plain = load_config(config_path=str(_write_config(tmp_path / "a", base)))
    cfg_features = load_config(config_path=str(_write_config(tmp_path / "b", with_features)))

    assert cfg_features.log_level == cfg_plain.log_level == "DEBUG"
    assert cfg_features.cron_db_path == cfg_plain.cron_db_path


def test_feature_db_path_is_not_readable_via_features_namespace(tmp_path: Path):
    """库路径属于 core 装配层（runtime.*），features.<name>.db_path 不生效。

    db 文件由 core 统一注入各 Store，Feature 自身不读自己的库路径；按
    "谁消费谁归属"留在 runtime.*，此处锁定该设计决策。
    """
    base = {"runtime": {"cron_db_path": "custom/cron.db"}}
    with_attempt = {
        **base,
        "features": {"cron": {"db_path": "hijack/cron.db"}},
    }

    cfg_plain = load_config(config_path=str(_write_config(tmp_path / "a", base)))
    cfg_attempt = load_config(config_path=str(_write_config(tmp_path / "b", with_attempt)))

    assert cfg_attempt.cron_db_path == cfg_plain.cron_db_path
    assert "hijack" not in cfg_attempt.cron_db_path


def test_raw_config_reflects_effective_merged_view(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {"cron": {"enabled": True}, "features": {"cron": {"enabled": False}}},
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.raw_config["cron"]["enabled"] is False
    # features 子树保留在 raw_config 中，运行期可观察完整原始配置
    assert cfg.raw_config["features"]["cron"]["enabled"] is False


def test_apply_features_namespace_without_features_returns_same_object():
    data = {"wiki": {"model": "m"}}

    assert _apply_features_namespace(data) is data


def test_channel_persistence_writes_existing_features_namespace_and_removes_fallback(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "channels": {"feishu": {"token": "legacy", "keep": "yes", "extra": {"old": 1}}},
            "features": {
                "channels": {"feishu": {"token": "new", "extra": {"new": 2}, "unknown": "kept"}}
            },
        },
    )
    cfg = load_config(config_path=config_path)
    payload = {"token": "updated", "_remove_keys": ["unknown", "old"]}

    cfg.persist_channel_config("feishu", payload)

    assert payload == {"token": "updated", "_remove_keys": ["unknown", "old"]}
    written = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert written["features"]["channels"]["feishu"] == {
        "token": "updated",
        "keep": "yes",
        "extra": {"new": 2},
    }
    assert "feishu" not in written["channels"]
    assert load_config(config_path=config_path).channel_config("feishu") == {
        "token": "updated",
        "keep": "yes",
        "extra": {"new": 2},
    }


def test_channel_persistence_removes_keys_from_platforms_fallback(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "platforms": {"feishu": {"token": "legacy", "obsolete": True}},
            "channels": {"feishu": {"token": "current"}},
        },
    )
    cfg = load_config(config_path=config_path)

    cfg.persist_channel_config("feishu", {"_remove_keys": ["obsolete"]})

    assert load_config(config_path=config_path).channel_config("feishu") == {"token": "current"}
    assert cfg.channel_config("feishu") == {"token": "current"}


def test_channel_persistence_empty_payload_preserves_effective_view(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "platforms": {"feishu": {"extra": {"hidden": 1}, "token": "platform"}},
            "channels": {"feishu": {"extra": {}, "token": "channel"}},
        },
    )
    cfg = load_config(config_path=config_path)
    before = cfg.channel_config("feishu")

    cfg.persist_channel_config("feishu", {})

    assert load_config(config_path=config_path).channel_config("feishu") == before


def test_owner_channel_overlay_reads_and_writes_features_namespace(tmp_path: Path, monkeypatch):
    overlay_path = tmp_path / "owner" / "config.yaml"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text(
        yaml.safe_dump({"features": {"channels": {"feishu": {"token": "owner-old"}}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "owner_overlay_config_path", lambda _owner: overlay_path)
    cfg = load_config(config_path=_write_config(tmp_path / "global", {}))

    assert cfg.channel_config("feishu", owner_account_id="owner-a") == {"token": "owner-old"}
    cfg.persist_channel_config("feishu", {"token": "owner-new"}, owner_account_id="owner-a")

    written = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    assert written["features"]["channels"]["feishu"]["token"] == "owner-new"
    assert cfg.channel_config("feishu", owner_account_id="owner-a") == {"token": "owner-new"}


def test_owner_channel_overlays_are_isolated_and_do_not_change_global_config(
    tmp_path: Path, monkeypatch
):
    overlays = {
        owner: tmp_path / owner / "config.yaml"
        for owner in ("owner-a", "owner-b")
    }
    for owner, path in overlays.items():
        path.parent.mkdir(parents=True)
        path.write_text(
            yaml.safe_dump(
                {"features": {"channels": {"feishu": {"token": owner}}}}
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(
        config_module,
        "owner_overlay_config_path",
        lambda owner: overlays[str(owner)],
    )
    global_path = _write_config(
        tmp_path / "global",
        {"features": {"channels": {"feishu": {"token": "global"}}}},
    )
    cfg = load_config(config_path=global_path)

    assert cfg.channel_config("feishu", owner_account_id="owner-a") == {"token": "owner-a"}
    assert cfg.channel_config("feishu", owner_account_id="owner-b") == {"token": "owner-b"}
    cfg.persist_channel_config("feishu", {"token": "owner-a-new"}, owner_account_id="owner-a")

    assert cfg.channel_config("feishu", owner_account_id="owner-a") == {"token": "owner-a-new"}
    assert cfg.channel_config("feishu", owner_account_id="owner-b") == {"token": "owner-b"}
    assert cfg.channel_config("feishu") == {"token": "global"}


def test_owner_channel_config_does_not_fallback_to_owner_platforms(
    tmp_path: Path, monkeypatch
):
    overlay_path = tmp_path / "owner" / "config.yaml"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text(
        yaml.safe_dump({"platforms": {"feishu": {"token": "legacy-owner"}}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module, "owner_overlay_config_path", lambda _owner: overlay_path)
    cfg = load_config(config_path=_write_config(tmp_path / "global", {}))

    assert cfg.channel_config("feishu", owner_account_id="owner-a") == {}


def test_channel_persistence_preserves_other_channels_and_does_not_mutate_nested_input(
    tmp_path: Path,
):
    config_path = _write_config(
        tmp_path,
        {
            "features": {
                "channels": {
                    "feishu": {"token": "old", "extra": {"old": 1}},
                    "weixin": {"token": "keep"},
                }
            }
        },
    )
    cfg = load_config(config_path=config_path)
    payload = {"token": "new", "extra": {"nested": {"enabled": True}}}
    original = deepcopy(payload)

    cfg.persist_channel_config("feishu", payload)

    assert payload == original
    reloaded = load_config(config_path=config_path)
    assert reloaded.channel_config("feishu") == {
        "token": "new", "extra": {"nested": {"enabled": True}}
    }
    assert reloaded.channel_config("weixin") == {"token": "keep"}


def test_global_empty_extra_replaces_mixed_legacy_extra_on_reload(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "platforms": {"feishu": {"token": "platform", "extra": {"old": 1}}},
            "channels": {"feishu": {"token": "channel", "extra": {"legacy": 2}}},
            "features": {
                "channels": {"feishu": {"token": "feature", "extra": {"new": 3}}}
            },
        },
    )
    cfg = load_config(config_path=config_path)
    cfg.persist_channel_config("feishu", {"extra": {}})

    assert load_config(config_path=config_path).channel_config("feishu") == {
        "token": "feature", "extra": {}
    }


def test_invalid_features_channel_node_keeps_legacy_channel_compatibility(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {
            "channels": {"feishu": {"token": "legacy"}},
            "features": {"channels": "old-format"},
        },
    )

    cfg = load_config(config_path=config_path)

    assert cfg.channel_config("feishu") == {"token": "legacy"}


def test_channel_persistence_preserves_extra_replacement_and_clear_semantics(tmp_path: Path):
    config_path = _write_config(
        tmp_path,
        {"channels": {"feishu": {"extra": {"old": 1, "keep": 2}, "token": "t"}}},
    )
    cfg = load_config(config_path=config_path)

    cfg.persist_channel_config("feishu", {"extra": {"new": 3}})
    assert load_config(config_path=config_path).channel_config("feishu")["extra"] == {"new": 3}
    cfg.persist_channel_config("feishu", {"_remove_keys": ["extra"]})
    assert "extra" not in load_config(config_path=config_path).channel_config("feishu")


# ----------------------- P3-1：subagent 死配置 -----------------------


def test_top_level_subagent_section_remains_dead(tmp_path: Path):
    """顶层 subagent: 节从未被 loader 读取（P3-1）；子 agent 超时归属 tasks.subagent。"""
    config_path = _write_config(
        tmp_path,
        {
            "subagent": {"max_concurrent": 99, "idle_timeout_seconds": 1},
            "tasks": {"subagent": {"inactivity_timeout_seconds": 30}},
        },
    )

    cfg = load_config(config_path=str(config_path))

    assert cfg.subagent_max_concurrent == 3
    assert cfg.subagent_idle_timeout_seconds == 120.0
    assert cfg.tasks_subagent_inactivity_timeout_seconds == 30.0


def test_example_template_targets_features_namespace():
    """example 给出 features 目标形态、保留旧节、且已移除顶层 subagent 死段。"""
    data = yaml.safe_load(
        (REPO_ROOT / "config" / "config.yaml.example").read_text(encoding="utf-8")
    )

    assert "subagent" not in data
    features = data["features"]
    assert {
        "wiki",
        "cron",
        "team",
        "tasks",
        "external_agents",
        "channels",
        "browser",
        "dynamic_kanban",
    } <= set(features)
    # 迁移期旧节全部保留
    for legacy in ("wiki", "cron", "team", "tasks", "external_agents", "channels"):
        assert legacy in data
