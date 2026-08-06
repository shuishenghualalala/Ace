"""Focused contract tests for Crew's Electron-backed browser driver."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import pytest

from crew.app import AgentManager, build_app
from crew.browser.driver import BrowserDriver, BrowserDriverError, BrowserOperationCancelled
from crew.browser.electron_bridge import ElectronBridgeCancelled, ElectronBridgeError
from crew.browser.electron_driver import ElectronBrowserDriver, runtime_doctor
from crew.browser.types import BrowserConfig
from crew.core.errors import ToolError
from crew.state.config import Config, ModelProfile


class _ContractDriver(BrowserDriver):
    def __init__(self, *, available: bool = True) -> None:
        self.is_available = available
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def execute(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args: Sequence[str] = (),
        *,
        timeout: float | None = None,
        proxy_url: str = "",
        download_dir: Path | None = None,
        mutating: bool = False,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "execute",
                (owner_session, profile_dir, command, tuple(args)),
                {
                    "timeout": timeout,
                    "proxy_url": proxy_url,
                    "download_dir": download_dir,
                    "mutating": mutating,
                },
            )
        )
        return {"success": True, "data": {"command": command}}

    async def close(self, owner_session: str, profile_dir: Path) -> bool:
        self.calls.append(("close", (owner_session, profile_dir), {}))
        return True

    def available(self) -> bool:
        return self.is_available


def test_browser_driver_error_preserves_lifecycle_flags() -> None:
    error = BrowserDriverError(
        "动作结果未知",
        uncertain=True,
        browser_stopped=True,
        stop_unconfirmed=True,
    )

    assert str(error) == "动作结果未知"
    assert error.uncertain is True
    assert error.browser_stopped is True
    assert error.stop_unconfirmed is True
    assert isinstance(error, ToolError)


@pytest.mark.parametrize("code", ["dialog_pending", "file_chooser_pending"])
def test_electron_driver_classifies_modal_pending_as_recoverable_state(
    code: str,
) -> None:
    with pytest.raises(BrowserDriverError) as raised:
        ElectronBrowserDriver._raise(
            ElectronBridgeError("modal pending", code=code)
        )

    assert raised.value.code == code
    assert raised.value.uncertain is False
    assert raised.value.next_state == {
        "status": "blocked",
        "recoverable": True,
        "reason": code,
        "retry_original_action": False,
    }


@pytest.mark.asyncio
async def test_electron_driver_preserves_cancellation_after_remote_lifecycle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(*_args: Any, **_kwargs: Any) -> Any:
        raise ElectronBridgeCancelled(
            ElectronBridgeError("账号浏览器已停止", browser_stopped=True)
        )

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())

    with pytest.raises(BrowserOperationCancelled, match="已停止") as captured:
        await driver.execute(
            "crew_0123456789ab",
            tmp_path / "profile",
            "click",
            ["@e1"],
            mutating=True,
        )
    assert captured.value.browser_stopped is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "args", "expected_mutating"),
    [
        ("snapshot", (), False),
        ("get", ("url",), False),
        ("tab", ("list",), False),
        ("tab", ("t2",), True),
        ("console", ("--clear",), True),
        ("network", ("requests", "--clear"), True),
        ("screenshot", ("/tmp/shot.png",), True),
    ],
)
async def test_electron_driver_derives_mutation_from_strict_readonly_whitelist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    args: tuple[str, ...],
    expected_mutating: bool,
) -> None:
    captured: dict[str, Any] = {}

    async def request(*_args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        captured["params"] = _args[2]
        return {"success": True, "data": {}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())

    await driver.execute(
        "crew_0123456789ab",
        tmp_path / "profile",
        command,
        args,
        mutating=False,
    )

    assert captured["mutating"] is expected_mutating
    assert captured["params"]["mutating"] is expected_mutating
    assert captured["retry_readonly"] is (not expected_mutating)


@pytest.mark.asyncio
async def test_electron_driver_binds_targeted_execute_to_exact_target_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def request(*args: Any, **kwargs: Any) -> Any:
        captured["params"] = args[2]
        captured.update(kwargs)
        return {"success": True, "data": {}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())

    await driver.execute_targeted(
        "crew_0123456789ab",
        tmp_path / "profile",
        "click",
        ["@e1"],
        target_id="target-exact",
        timeout=12.345,
        mutating=True,
    )

    assert captured["params"]["target_id"] == "target-exact"
    assert captured["params"]["command"] == "click"
    assert captured["params"]["args"] == ["@e1"]
    assert captured["params"]["command_timeout_ms"] == 12_345


@pytest.mark.asyncio
async def test_electron_driver_sends_form_values_only_in_typed_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def request(*args: Any, **kwargs: Any) -> Any:
        captured["method"] = args[1]
        captured["params"] = args[2]
        captured.update(kwargs)
        return {"success": True, "data": {"completed_count": 1}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())
    fields = [
        {
            "type": "textbox",
            "ref": "@e1",
            "value": "private-form-value",
        }
    ]

    await driver.fill_form(
        "crew_0123456789ab",
        tmp_path / "profile",
        fields,
        target_id="target-1",
        timeout=3,
    )

    assert captured["method"] == "execute"
    assert captured["params"]["command"] == "fill_form"
    assert captured["params"]["args"] == []
    assert captured["params"]["fields"] == fields
    assert captured["params"]["command_timeout_ms"] == 3_000
    assert "private-form-value" not in repr(captured["params"]["args"])
    assert captured["mutating"] is True
    assert captured["retry_readonly"] is False


@pytest.mark.asyncio
async def test_electron_driver_sends_atomic_upload_as_typed_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def request(*args: Any, **kwargs: Any) -> Any:
        captured["method"] = args[1]
        captured["params"] = args[2]
        captured.update(kwargs)
        return {"success": True, "data": {"via": "chooser", "uploaded": 256}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())
    files = [f"/tmp/upload-{index}.txt" for index in range(256)]

    await driver.upload_with_trigger(
        "crew_0123456789ab",
        tmp_path / "profile",
        target_id="target-1",
        trigger_selector="#choose-files",
        input_selector="input[type=file]",
        files=files,
        timeout=40,
    )

    assert captured["method"] == "execute"
    assert captured["params"]["command"] == "upload_with_trigger"
    assert captured["params"]["args"] == []
    assert captured["params"]["trigger_selector"] == "#choose-files"
    assert captured["params"]["input_selector"] == "input[type=file]"
    assert captured["params"]["files"] == files
    assert captured["params"]["command_timeout_ms"] == 40_000
    assert captured["mutating"] is True
    assert captured["retry_readonly"] is False


@pytest.mark.asyncio
async def test_driver_defaults_remain_safe_for_test_and_alternative_drivers(tmp_path: Path) -> None:
    driver = _ContractDriver()
    profile = tmp_path / "profile"

    assert await driver.prepare() is True
    assert await driver.page_guard(
        "owner",
        profile,
        target_id="target-1",
        state_key="key",
        state_token="token",
        reset=False,
        timeout=1,
    ) is None
    assert await driver.deny_downloads("owner", profile) is None
    assert await driver.set_mode(
        "owner", profile, target_id="target-1", mode="human"
    ) is None

    await driver.interrupt("owner", profile)
    assert driver.calls[-1] == ("close", ("owner", profile), {})

    await driver.execute_targeted(
        "owner",
        profile,
        "click",
        ["@e1"],
        target_id="ignored-by-compatible-driver",
    )
    assert driver.calls[-1][0] == "execute"
    assert driver.calls[-1][1][2:] == ("click", ("@e1",))


@pytest.mark.asyncio
async def test_electron_page_guard_forwards_lightweight_security_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def request(*args: Any, **kwargs: Any) -> Any:
        captured["method"] = args[1]
        captured["params"] = args[2]
        captured.update(kwargs)
        return "marker"

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())

    marker = await driver.page_guard(
        "crew_0123456789ab",
        tmp_path / "profile",
        target_id="target-1",
        state_key="__crew_guard_state",
        state_token="guard-token",
        reset=False,
        timeout=1,
        include_security=False,
    )

    assert marker == "marker"
    assert captured["method"] == "page_guard"
    assert captured["params"]["include_security"] is False
    assert captured["params"]["command_timeout_ms"] == 1_000
    assert captured["mutating"] is False
    assert captured["retry_readonly"] is True


@pytest.mark.asyncio
async def test_default_bounded_download_uses_mutating_execute_contract(tmp_path: Path) -> None:
    driver = _ContractDriver()
    profile = tmp_path / "profile"
    download_dir = tmp_path / "downloads"
    target = download_dir / "report.pdf"

    result = await driver.download_bounded(
        "owner",
        profile,
        "@e17",
        target,
        target_id="target-1",
        max_bytes=1024,
        timeout=5,
        proxy_url="http://127.0.0.1:4567",
        download_dir=download_dir,
    )

    assert result == {"success": True, "data": {"command": "download"}}
    assert driver.calls == [
        (
            "execute",
            ("owner", profile, "download", ("@e17", str(target))),
            {
                "timeout": 5,
                "proxy_url": "http://127.0.0.1:4567",
                "download_dir": download_dir,
                "mutating": True,
            },
        )
    ]


@pytest.mark.asyncio
async def test_optional_exact_target_operations_fail_closed_by_default(tmp_path: Path) -> None:
    driver = _ContractDriver()
    profile = tmp_path / "profile"

    with pytest.raises(BrowserDriverError, match="图片元数据"):
        await driver.page_images("owner", profile, target_id="target-1", timeout=1)
    with pytest.raises(BrowserDriverError, match="targetId"):
        await driver.close_target("owner", profile, target_id="target-1", timeout=1)


def test_default_availability_error_tracks_driver_state() -> None:
    assert _ContractDriver(available=True).availability_error() == ""
    assert "不可用" in _ContractDriver(available=False).availability_error()


@pytest.mark.asyncio
async def test_electron_driver_maps_execute_and_control_mode_to_host_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any], float, dict[str, Any]]] = []

    async def request(
        owner_session: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        **transport: Any,
    ) -> Any:
        calls.append((owner_session, method, params, timeout, transport))
        return {"success": True, "data": {}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr("crew.browser.electron_driver.time.time", lambda: 1_000.0)
    driver = ElectronBrowserDriver(BrowserConfig(command_timeout_seconds=17))
    profile = tmp_path / "profile"
    download_dir = tmp_path / "downloads"

    result = await driver.execute(
        "crew_0123456789ab",
        profile,
        "click",
        ["@e17"],
        timeout=4,
        proxy_url="http://127.0.0.1:4567",
        download_dir=download_dir,
        mutating=True,
    )
    await driver.set_mode(
        "crew_0123456789ab",
        profile,
        target_id="target-1",
        mode="human",
    )

    assert result["success"] is True
    assert calls[0] == (
        "crew_0123456789ab",
        "execute",
        {
            "profile_dir": str(profile.resolve()),
            "command": "click",
            "args": ["@e17"],
            "command_timeout_ms": 4_000,
            "command_deadline_ms": 1_004_000,
            "proxy_url": "http://127.0.0.1:4567",
            "download_dir": str(download_dir.resolve()),
            "mutating": True,
        },
        6,
        {
            "mutating": True,
            "retry_readonly": False,
            "_allow_unready": False,
        },
    )
    assert calls[1] == (
        "crew_0123456789ab",
        "set_mode",
        {
            "profile_dir": str(profile.resolve()),
            "target_id": "target-1",
            "mode": "human",
        },
        17,
        {
            "mutating": True,
            "retry_readonly": False,
            "_allow_unready": False,
        },
    )


@pytest.mark.asyncio
async def test_electron_driver_uses_independent_atomic_replay_rpcs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict[str, Any], float, dict[str, Any]]] = []

    async def request(
        owner_session: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
        **transport: Any,
    ) -> Any:
        calls.append((owner_session, method, params, timeout, transport))
        if method == "capabilities":
            return {
                "recordingEventSchemas": [10, 11],
                "replayArtifactSchemas": [
                    "crew.browser.replay.v2",
                    "crew.browser.replay.v3",
                ],
                "atomicReplayEffects": True,
            }
        return {
            "matchedEffects": [],
            "pageBindings": [],
            "downloads": [],
            "activePageGuid": "",
            "closedPageGuids": [],
        }

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig(command_timeout_seconds=17))
    profile = tmp_path / "profile"
    download_dir = tmp_path / "downloads"
    transaction = {
        "schemaVersion": 1,
        "transactionId": 1,
        "source": {"pageGuid": "p0"},
        "knownPages": [],
        "action": {"name": "openPage", "url": "https://example.test/"},
        "expectedEffects": [],
        "timeoutMs": 4_000,
    }

    capabilities = await driver.capabilities(
        "crew_0123456789ab",
        profile,
        timeout=4,
    )
    result = await driver.execute_transaction(
        "crew_0123456789ab",
        profile,
        transaction,
        timeout=4,
        proxy_url="http://127.0.0.1:4567",
        download_dir=download_dir,
    )

    assert capabilities["atomicReplayEffects"] is True
    assert result["matchedEffects"] == []
    assert calls == [
        (
            "crew_0123456789ab",
            "capabilities",
            {},
            6,
            {
                "mutating": False,
                "retry_readonly": True,
                "_allow_unready": False,
            },
        ),
        (
            "crew_0123456789ab",
            "execute_transaction",
            {
                "profile_dir": str(profile.resolve()),
                **transaction,
                "proxy_url": "http://127.0.0.1:4567",
                "download_dir": str(download_dir.resolve()),
            },
            6,
            {
                "mutating": True,
                "retry_readonly": False,
                "_allow_unready": False,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_electron_driver_marks_only_provable_reads_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[dict[str, Any]] = []

    async def request(*_args: Any, **kwargs: Any) -> Any:
        transports.append(kwargs)
        return {"success": True, "data": {}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())
    await driver.execute("crew_0123456789ab", tmp_path / "profile", "snapshot")
    await driver.execute(
        "crew_0123456789ab",
        tmp_path / "profile",
        "find",
        ["--text", "Search"],
    )
    await driver.execute(
        "crew_0123456789ab",
        tmp_path / "profile",
        "console",
        ["--clear"],
    )

    assert transports[0]["retry_readonly"] is True
    assert transports[0]["mutating"] is False
    assert transports[1]["retry_readonly"] is True
    assert transports[1]["mutating"] is False
    assert transports[2]["retry_readonly"] is False


@pytest.mark.asyncio
async def test_electron_driver_clears_inactive_owner_and_bounds_download_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def request(
        _owner: str,
        method: str,
        params: dict[str, Any],
        **transport: Any,
    ) -> Any:
        calls.append((method, params, transport))
        if method == "clear_owner_data":
            return {"cleared": True}
        if method == "download":
            return {"path": params["target"]}
        return {"success": True, "data": {"tabs": []}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr("crew.browser.electron_driver.time.time", lambda: 1_000.0)
    driver = ElectronBrowserDriver(BrowserConfig())
    profile = tmp_path / "profile"
    quarantine = tmp_path / "download-quarantine"

    assert await driver.clear_owner_data(
        "crew_0123456789ab",
        profile,
        timeout=8,
        download_dir=quarantine,
    ) is True
    await driver.download_bounded(
        "crew_0123456789ab",
        profile,
        "@e1",
        tmp_path / "approved-downloads" / "id" / "report.bin",
        target_id="target-1",
        max_bytes=1024,
        timeout=25,
        download_dir=quarantine,
    )

    assert [call[0] for call in calls[:2]] == ["execute", "clear_owner_data"]
    assert calls[0][1]["command"] == "tab" and calls[0][1]["args"] == ["list"]
    assert calls[1][2]["mutating"] is True
    assert calls[2][1]["timeout_ms"] == 25_000
    assert calls[2][1]["command_timeout_ms"] == 25_000
    assert calls[2][1]["command_deadline_ms"] == 1_025_000
    assert calls[2][2]["timeout"] == 27
    assert calls[2][2]["mutating"] is True


def test_runtime_doctor_is_scoped_to_requested_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.connected",
        lambda key=None: key == "crew_0123456789ab",
    )
    assert runtime_doctor(BrowserConfig(), "crew_0123456789ab")["ok"] is True
    assert runtime_doctor(BrowserConfig(), "crew_ffffffffffff")["ok"] is False


@pytest.mark.asyncio
async def test_electron_driver_maps_atomic_coordinate_click_to_one_mutating_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def request(
        _owner: str,
        method: str,
        params: dict[str, Any],
        **transport: Any,
    ) -> Any:
        calls.append((method, params, transport))
        return {"clicked": True, "target": {"tag": "BUTTON", "role": "button"}}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr("crew.browser.electron_driver.time.time", lambda: 1_000.0)
    driver = ElectronBrowserDriver(BrowserConfig())
    profile = tmp_path / "profile"

    result = await driver.coordinate_click_atomic(
        "crew_0123456789ab",
        profile,
        target_id="target-1",
        x=50,
        y=25,
        timeout=7,
        proxy_url="http://127.0.0.1:4567",
        expected_epoch="0123456789abcdef0123456789abcdef",
    )

    assert result and result["clicked"] is True
    assert calls == [
        (
            "coordinate_click",
            {
                "profile_dir": str(profile.resolve()),
                "target_id": "target-1",
                "x": 50,
                "y": 25,
                "proxy_url": "http://127.0.0.1:4567",
                "download_dir": "",
                "expected_epoch": "0123456789abcdef0123456789abcdef",
                "command_timeout_ms": 7_000,
                "command_deadline_ms": 1_007_000,
            },
            {
                "timeout": 9,
                "mutating": True,
                "retry_readonly": False,
                "_allow_unready": False,
            },
        )
    ]


@pytest.mark.asyncio
async def test_electron_driver_bounds_recording_control_with_absolute_deadline_and_grace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def request(*args: Any, **kwargs: Any) -> Any:
        captured["method"] = args[1]
        captured["params"] = args[2]
        captured.update(kwargs)
        return {"recording": True}

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    monkeypatch.setattr("crew.browser.electron_driver.time.time", lambda: 2_000.0)
    driver = ElectronBrowserDriver(BrowserConfig(command_timeout_seconds=17))

    await driver.set_recording(
        "crew_0123456789ab",
        tmp_path / "profile",
        target_id="target-1",
        action="start",
        recording_id="aabbccddeeff0011",
    )

    assert captured["method"] == "set_recording"
    assert captured["params"]["command_timeout_ms"] == 17_000
    assert captured["params"]["command_deadline_ms"] == 2_017_000
    assert captured["timeout"] == 19
    assert captured["mutating"] is True


@pytest.mark.asyncio
async def test_electron_driver_preserves_bridge_failure_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def request(*_args: Any, **_kwargs: Any) -> Any:
        raise ElectronBridgeError(
            "动作结果未知",
            uncertain=True,
            browser_stopped=True,
            stop_unconfirmed=True,
        )

    monkeypatch.setattr(
        "crew.browser.electron_driver.electron_browser_bridge.request",
        request,
    )
    driver = ElectronBrowserDriver(BrowserConfig())

    with pytest.raises(BrowserDriverError, match="结果未知") as captured:
        await driver.execute(
            "crew_0123456789ab",
            tmp_path / "profile",
            "click",
            ["@e17"],
            mutating=True,
        )
    assert captured.value.uncertain is True
    assert captured.value.browser_stopped is True
    assert captured.value.stop_unconfirmed is True


def test_partial_model_updates_preserve_capabilities() -> None:
    cfg = Config()
    cfg.model_profiles = {
        "vision": ModelProfile(
            id="vision",
            model="vision-model",
            capabilities=["text", "tools", "vision"],
        )
    }
    cfg.active_model_id = "vision"

    cfg.update_model("vision", {"temperature": 0.2})

    assert cfg.model_profiles["vision"].capabilities == ["text", "tools", "vision"]


def test_model_update_cache_invalidation_can_be_scoped_to_owner() -> None:
    manager = AgentManager(lambda _config, owner_account_id="": object())
    manager.get("session-a", owner_account_id="owner-a")
    owner_b = manager.get("session-b", owner_account_id="owner-b")

    manager.drop_owner("owner-a")

    assert manager.peek("session-a", owner_account_id="owner-a") is None
    assert manager.peek("session-b", owner_account_id="owner-b") is owner_b


def test_owner_profile_falls_back_to_effective_global_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = ModelProfile(id="text", model="text-only", capabilities=["text", "tools"])
    cfg = Config(active_model_id="text", model_profiles={"text": profile})
    monkeypatch.setattr(cfg, "owner_model_profiles", lambda _owner=None: {})

    assert cfg.owner_active_model_profile("owner") is profile


def test_text_only_model_does_not_receive_browser_vision(tmp_path: Path) -> None:
    profile = ModelProfile(
        id="text",
        api_key="test-key",
        model="text-only",
        builtin=True,
        capabilities=["text", "tools"],
    )
    cfg = Config(
        db_path=str(tmp_path / "crew.db"),
        memory_db_path=str(tmp_path / "memory.db"),
        active_model_id="text",
        model_profiles={"text": profile},
    )
    cfg.activate_model("text")
    app = build_app(config=cfg, enable_team=False)
    app.browser_manager.driver.available = lambda: True

    agent = app._make_agent({}, owner_account_id="owner")

    # 单一 browser_use 工具保留在工具集中；vision action 由工具的
    # permission_resolver 按模型能力拒绝（见 plugins/browser/tool.py）。
    assert "browser_use" in (agent.tool_filter or [])
    assert "browser_vision" not in (agent.tool_filter or [])
