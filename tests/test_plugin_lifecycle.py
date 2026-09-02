"""插件生命周期：权限透传、按插件卸载、用户级偏好与有效状态判定。"""

from __future__ import annotations

import asyncio

import pytest

from crew.core.types import ToolCall, ToolPermissionDecision
from crew.features import FeatureState, RegistrationPhase
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

    assert await plugins.unload_plugin_async("lifecycle_plugin") is True

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
    assert await plugins.unload_plugin_async("lifecycle_plugin") is False
    assert await plugins.unload_plugin_async("nonexistent") is False


async def test_plugin_skill_root_resolves_against_plugin_dir(tmp_path):
    _, plugins = _load(tmp_path)
    roots = plugins.plugin_skill_roots()
    assert len(roots) == 1
    assert roots[0].endswith("lifecycle_plugin/skills")


async def test_failed_install_rolls_back_contributions_and_async_resource(tmp_path):
    plugin_dir = tmp_path / "broken_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: broken-plugin\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "skills").mkdir()
    (plugin_dir / "__init__.py").write_text(
        """
import asyncio

EVENTS = []

async def dispose_resource():
    await asyncio.sleep(0)
    EVENTS.append("resource")

def hook(**kwargs):
    return None

def handler(args):
    return "ok"

def register(ctx):
    ctx.register_disposer(dispose_resource)
    ctx.register_tool(
        name="broken_tool",
        toolset="broken",
        schema={"name": "broken_tool", "parameters": {"type": "object"}},
        handler=handler,
    )
    ctx.register_hook("pre_tool_call", hook)
    ctx.register_command("broken", handler)
    ctx.register_skill_root("skills")
    raise RuntimeError("install failed after contributions")
""".lstrip(),
        encoding="utf-8",
    )
    registry = Registry()
    plugins = PluginManager(registry=registry)

    await plugins.discover_and_load_async([tmp_path], enabled=["broken-plugin"])

    loaded = plugins.get_plugin("broken-plugin")
    assert loaded is not None and not loaded.enabled
    assert "install failed after contributions" in str(loaded.error)
    assert registry.names() == []
    assert plugins._hooks == {}
    assert plugins.plugin_commands == {}
    assert plugins.plugin_skill_roots() == []
    module = __import__("crew_runtime_plugins.broken_plugin", fromlist=["EVENTS"])
    assert module.EVENTS == ["resource"]
    assert loaded.feature_record is not None
    assert loaded.feature_record.scope is not None
    assert loaded.feature_record.scope.state is FeatureState.DISPOSED


async def test_repeated_discovery_replaces_generation_without_duplicates(tmp_path):
    _write_lifecycle_plugin(tmp_path)
    registry = Registry()
    plugins = PluginManager(registry=registry)

    await plugins.discover_and_load_async([tmp_path], enabled=["lifecycle_plugin"])
    first = plugins.get_plugin("lifecycle_plugin")
    assert first is not None and first.feature_record is not None
    first_scope = first.feature_record.scope
    first_module = __import__(
        "crew_runtime_plugins.lifecycle_plugin",
        fromlist=["DISPOSED"],
    )

    await plugins.discover_and_load_async([tmp_path], enabled=["lifecycle_plugin"])

    second = plugins.get_plugin("lifecycle_plugin")
    assert second is not None and second.feature_record is not None
    assert first_scope is not None and first_scope.state is FeatureState.DISPOSED
    assert first_module.DISPOSED == ["sync"]
    assert second.feature_record.generation is not None
    assert second.feature_record.generation.key == "lifecycle_plugin@g2"
    assert registry.names() == ["lifecycle_echo"]
    assert len(plugins._hooks["pre_tool_call"]) == 1
    assert len(plugins.plugin_skill_roots()) == 1


async def test_async_unload_closes_contributions_before_waiting_for_resource(tmp_path):
    plugin_dir = tmp_path / "slow_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: slow-plugin\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        """
import asyncio

STARTED = asyncio.Event()
RELEASE = asyncio.Event()

def handler(args):
    return "ok"

def hook(**kwargs):
    return None

async def dispose_resource():
    STARTED.set()
    await RELEASE.wait()

def register(ctx):
    ctx.register_tool(
        name="slow_tool",
        toolset="slow",
        schema={"name": "slow_tool", "parameters": {"type": "object"}},
        handler=handler,
    )
    ctx.register_hook("pre_tool_call", hook)
    ctx.register_disposer(dispose_resource)
""".lstrip(),
        encoding="utf-8",
    )
    registry = Registry()
    plugins = PluginManager(registry=registry)
    await plugins.discover_and_load_async([tmp_path], enabled=["slow-plugin"])
    loaded = plugins.get_plugin("slow-plugin")
    module = __import__("crew_runtime_plugins.slow_plugin", fromlist=["STARTED"])

    unloading = asyncio.create_task(plugins.unload_plugin_async("slow-plugin"))
    await module.STARTED.wait()

    assert loaded is not None and loaded.enabled
    assert loaded.feature_record is not None
    assert loaded.feature_record.state is FeatureState.STOPPING
    assert not unloading.done()
    assert registry.names() == []
    assert plugins._hooks == {}

    module.RELEASE.set()
    assert await unloading is True
    assert not loaded.enabled
    assert loaded.error is None


async def test_cleanup_failure_is_aggregated_after_other_resources_stop(tmp_path):
    plugin_dir = tmp_path / "cleanup_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: cleanup-plugin\nkind: standalone\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        """
EVENTS = []

def broken():
    EVENTS.append("broken")
    raise RuntimeError("close failed")

def healthy():
    EVENTS.append("healthy")

def register(ctx):
    ctx.register_disposer(broken)
    ctx.register_disposer(healthy)
    ctx.register_tool(
        name="cleanup_tool",
        toolset="cleanup",
        schema={"name": "cleanup_tool", "parameters": {"type": "object"}},
        handler=lambda args: "ok",
    )
""".lstrip(),
        encoding="utf-8",
    )
    registry = Registry()
    plugins = PluginManager(registry=registry)
    await plugins.discover_and_load_async([tmp_path], enabled=["cleanup-plugin"])

    assert await plugins.unload_plugin_async("cleanup-plugin") is False

    loaded = plugins.get_plugin("cleanup-plugin")
    module = __import__("crew_runtime_plugins.cleanup_plugin", fromlist=["EVENTS"])
    assert module.EVENTS == ["healthy", "broken"]
    assert registry.names() == []
    assert loaded is not None and not loaded.enabled
    assert "resource:broken" in str(loaded.error)
    assert loaded.feature_record is not None
    assert loaded.feature_record.state is FeatureState.FAILED


async def test_registration_phases_are_visible_in_plugin_diagnostics(tmp_path):
    registry, plugins = _load(tmp_path)
    loaded = plugins.get_plugin("lifecycle_plugin")

    assert loaded is not None and loaded.feature_record is not None
    scope = loaded.feature_record.scope
    assert scope is not None
    phases = {token.label: token.phase for token in scope.registrations}
    assert phases["tool:lifecycle_echo"] is RegistrationPhase.CONTRIBUTION
    assert phases["hook:pre_tool_call"] is RegistrationPhase.CONTRIBUTION
    assert phases["resource:dispose"] is RegistrationPhase.RESOURCE


async def test_manifest_services_drive_plugin_activation_order(tmp_path):
    consumer = tmp_path / "a_consumer"
    consumer.mkdir()
    (consumer / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: a-consumer",
                "requires:",
                "  - catalog",
                "optional:",
                "  - enhancer",
            ]
        ),
        encoding="utf-8",
    )
    (consumer / "__init__.py").write_text(
        """
VALUE = None

def register(ctx):
    global VALUE
    VALUE = ctx.resolve_service("catalog")["origin"]
""".lstrip(),
        encoding="utf-8",
    )

    provider = tmp_path / "z_provider"
    provider.mkdir()
    (provider / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: z-provider",
                "provides:",
                "  - catalog",
            ]
        ),
        encoding="utf-8",
    )
    (provider / "__init__.py").write_text(
        """
def register(ctx):
    ctx.register_service("catalog", {"origin": "provider"})
""".lstrip(),
        encoding="utf-8",
    )

    plugins = PluginManager()
    await plugins.discover_and_load_async(
        [tmp_path],
        enabled=["a-consumer", "z-provider"],
    )

    assert plugins.get_plugin("a-consumer").enabled
    assert plugins.get_plugin("z-provider").enabled
    module = __import__("crew_runtime_plugins.a_consumer", fromlist=["VALUE"])
    assert module.VALUE == "provider"
    report = plugins.startup_audit()
    assert report.healthy
    consumer_diagnostic = next(
        item for item in report.features if item.feature_id == "a_consumer"
    )
    assert consumer_diagnostic.missing_optional == ("enhancer",)


async def test_missing_manifest_service_waits_without_running_plugin(tmp_path):
    plugin_dir = tmp_path / "waiting_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: waiting-plugin\nrequires:\n  - security_service\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "CALLED = False\n\ndef register(ctx):\n    global CALLED\n    CALLED = True\n",
        encoding="utf-8",
    )
    plugins = PluginManager()

    await plugins.discover_and_load_async([tmp_path], enabled=["waiting-plugin"])

    loaded = plugins.get_plugin("waiting-plugin")
    assert loaded is not None and not loaded.enabled
    assert loaded.feature_record is not None
    assert loaded.feature_record.state is FeatureState.WAITING
    assert loaded.error == "missing required services: security_service"
    assert plugins.startup_audit().as_dict()["issues"] == ["waiting_plugin"]
    assert "crew_runtime_plugins.waiting_plugin" not in __import__("sys").modules


async def test_late_host_service_retries_waiting_plugin(tmp_path):
    plugin_dir = tmp_path / "late_consumer"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: late-consumer\nrequires:\n  - security_service\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        """
VALUE = None

def register(ctx):
    global VALUE
    VALUE = ctx.resolve_service("security_service")
""".lstrip(),
        encoding="utf-8",
    )
    plugins = PluginManager()
    await plugins.discover_and_load_async([tmp_path], enabled=["late-consumer"])
    service = object()

    plugins.publish_host_services({"security_service": service})
    await plugins.retry_waiting_async()

    loaded = plugins.get_plugin("late-consumer")
    assert loaded is not None and loaded.enabled
    assert loaded.error is None
    module = __import__("crew_runtime_plugins.late_consumer", fromlist=["VALUE"])
    assert module.VALUE is service
    assert plugins.startup_audit().healthy


async def test_plugin_cannot_publish_undeclared_service(tmp_path):
    plugin_dir = tmp_path / "hidden_provider"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.yaml").write_text(
        "name: hidden-provider\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n    ctx.register_service('hidden', object())\n",
        encoding="utf-8",
    )
    plugins = PluginManager()

    await plugins.discover_and_load_async([tmp_path], enabled=["hidden-provider"])

    loaded = plugins.get_plugin("hidden-provider")
    assert loaded is not None and not loaded.enabled
    assert "must declare provided service 'hidden'" in str(loaded.error)


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
