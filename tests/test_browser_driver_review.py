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
            "proxy_url": "http://127.0.0.1:4567",
            "download_dir": str(download_dir.resolve()),
            "mutating": True,
        },
        4,
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
        "console",
        ["--clear"],
    )

    assert transports[0]["retry_readonly"] is True
    assert transports[0]["mutating"] is False
    assert transports[1]["retry_readonly"] is False


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
    assert calls[2][1]["timeout_ms"] == 24_000
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
                "expected_epoch": "0123456789abcdef0123456789abcdef",
            },
            {
                "timeout": 7,
                "mutating": True,
                "retry_readonly": False,
                "_allow_unready": False,
            },
        )
    ]


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
