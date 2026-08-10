"""插件生命周期：权限透传、按插件卸载、用户级偏好与有效状态判定。"""

from __future__ import annotations

import pytest

from crew.core.types import ToolCall, ToolPermissionDecision
from crew.plugins.manager import PluginManager
from crew.state.plugin_preferences import (
    PluginPreferencesStore,
    plugin_effective_enabled,
    plugin_role_allowed,
)
from crew.tools.registry import Registry


def _write_lifecycle_plugin(root):
    plugin_dir = root / "lifecycle_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join([
            "name: lifecycle_plugin",
            "version: 1.0.0",
            "kind: standalone",
            "provides_tools:",
            "  - lifecycle_echo",
            "provides_hooks:",
            "  - pre_tool_call",
        ]),
        encoding="utf-8",
    )
    (plugin_dir / "skills").mkdir()
    (plugin_dir / "__init__.py").write_text(
        """
import json

SCHEMA = {
    "name": "lifecycle_echo",
    "description": "Echo text",
    "parameters": {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    },
}

DISPOSED = []

def handle_echo(args):
    return json.dumps({"text": args["text"]}, ensure_ascii=False)

def resolve(args):
    from crew.core.types import ToolPermissionDecision
    return ToolPermissionDecision(behavior="allow", reason="test")

def approve(token, args):
    return token == "ok-token"

def noop_hook(tool_call):
    return None

def dispose():
    DISPOSED.append("sync")

def register(ctx):
    ctx.register_tool(
        name="lifecycle_echo",
        toolset="lifecycle",
        schema=SCHEMA,
        handler=handle_echo,
        permission_resolver=resolve,
        permission_approver=approve,
        display_name="回声",
        ui_label_template="回声 {text}",
    )
    ctx.register_hook("pre_tool_call", noop_hook)
    ctx.register_disposer(dispose)
    ctx.register_skill_root("skills")
""".lstrip(),
        encoding="utf-8",
    )
    return plugin_dir


def _load(tmp_path):
    _write_lifecycle_plugin(tmp_path)
    registry = Registry()
    plugins = PluginManager(registry=registry, services={"config": object()})
    plugins.discover_and_load([tmp_path], enabled=["lifecycle_plugin"])
    return registry, plugins


async def test_register_tool_passthrough_permission_and_ui(tmp_path):
    registry, plugins = _load(tmp_path)

    loaded = plugins.loaded_plugins[0]
    assert loaded.enabled
    assert loaded.tools_registered == ["lifecycle_echo"]

    decision = await registry.resolve_permission(
        ToolCall("c1", "lifecycle_echo", {"text": "hi"})
    )
    assert isinstance(decision, ToolPermissionDecision)
    assert decision.behavior == "allow"

    ok = await registry.confirm_permission(
        ToolCall("c1", "lifecycle_echo", {"text": "hi"}),
        ToolPermissionDecision(behavior="ask", approval_token="ok-token"),
    )
    assert ok is True
    rejected = await registry.confirm_permission(
        ToolCall("c1", "lifecycle_echo", {"text": "hi"}),
        ToolPermissionDecision(behavior="ask", approval_token="bad-token"),
    )
    assert rejected is False

    meta = registry.ui_meta("lifecycle_echo")
    assert meta["display_name"] == "回声"
    assert meta["ui_label_template"] == "回声 {text}"


async def test_unload_plugin_removes_registrations_and_runs_disposers(tmp_path):
    registry, plugins = _load(tmp_path)
    assert registry.names() == ["lifecycle_echo"]
    assert plugins.plugin_skill_roots() != []

    assert plugins.unload_plugin("lifecycle_plugin") is True

    assert registry.names() == []
    # hook 不再触发：pre_tool_call 列表已空
    assert plugins._hooks.get("pre_tool_call", []) == []
    assert plugins._hook_owners.get("pre_tool_call", []) == []
    # disposer 被调用（插件模块级记录）
    plugin_module = __import__("crew_runtime_plugins.lifecycle_plugin", fromlist=["DISPOSED"])
    assert plugin_module.DISPOSED == ["sync"]
    # skill root 不再出现
    assert plugins.plugin_skill_roots() == []
    # 插件保留在清单中、标记未启用
    loaded = plugins.get_plugin("lifecycle_plugin")
    assert loaded is not None
    assert loaded.enabled is False
    assert loaded.error is None
    # 重复卸载返回 False
    assert plugins.unload_plugin("lifecycle_plugin") is False
    assert plugins.unload_plugin("nonexistent") is False


async def test_plugin_skill_root_resolves_against_plugin_dir(tmp_path):
    _, plugins = _load(tmp_path)
    roots = plugins.plugin_skill_roots()
    assert len(roots) == 1
    assert roots[0].endswith("lifecycle_plugin/skills")


# ---- PluginPreferencesStore ----


def test_preferences_store_roundtrip(tmp_path):
    store = PluginPreferencesStore(str(tmp_path / "prefs.db"))
    try:
        assert store.get_enabled("owner-a", "browser") is None
        store.set_enabled("owner-a", "browser", True)
        assert store.get_enabled("owner-a", "browser") is True
        store.set_enabled("owner-a", "browser", False)
        assert store.get_enabled("owner-a", "browser") is False
        assert store.get_enabled("owner-b", "browser") is None

        store.set_enabled("owner-a", "other", True)
        assert store.list_for_owner("owner-a") == {"browser": False, "other": True}
        assert store.list_for_owner("owner-b") == {}
    finally:
        store.close()


def test_preferences_store_requires_owner_and_key(tmp_path):
    store = PluginPreferencesStore(str(tmp_path / "prefs.db"))
    try:
        with pytest.raises(ValueError):
            store.set_enabled("", "browser", True)
        with pytest.raises(ValueError):
            store.set_enabled("owner-a", "", True)
    finally:
        store.close()


# ---- role_allowed / effective_enabled ----


@pytest.mark.parametrize(
    ("ac", "expected"),
    [
        (None, True),
        ({}, True),
        ({"enabled_plugins": None}, True),
        ({"enabled_plugins": ["*"]}, True),
        ({"enabled_plugins": ["browser"]}, True),
        ({"enabled_plugins": ["other"]}, False),
        ({"enabled_plugins": []}, False),
        ({"disabled_plugins": ["browser"]}, False),
        ({"disabled_plugins": ["*"]}, False),
        ({"enabled_plugins": ["*"], "disabled_plugins": ["browser"]}, False),
        ({"enabled_plugins": ["browser"], "disabled_plugins": ["*"]}, False),
    ],
)
def test_plugin_role_allowed(ac, expected):
    assert plugin_role_allowed(ac, "browser") is expected


@pytest.mark.parametrize(
    ("system_enabled", "role_allowed", "user_enabled", "user_type", "expected"),
    [
        (True, True, True, "internal", True),
        (True, True, False, "internal", False),
        (True, True, None, "internal", True),   # internal 缺省开
        (True, True, None, "external", False),  # external 缺省关（fail-closed）
        (True, True, None, "", False),
        (True, False, True, "internal", False), # role 否决优先
        (False, True, True, "internal", False), # system 否决优先
        (True, True, True, "external", True),   # external 显式 opt-in
    ],
)
def test_plugin_effective_enabled(
    system_enabled, role_allowed, user_enabled, user_type, expected
):
    assert (
        plugin_effective_enabled(
            system_enabled=system_enabled,
            role_allowed=role_allowed,
            user_enabled=user_enabled,
            user_type=user_type,
        )
        is expected
    )
