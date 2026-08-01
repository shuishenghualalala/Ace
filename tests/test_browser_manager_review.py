"""Regression tests for the adversarial BrowserManager review findings."""

from __future__ import annotations

import asyncio
import json
import re
import struct
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from crew.browser.driver import BrowserDriver, BrowserDriverError, BrowserOperationCancelled
from crew.browser.manager import BrowserManager, _bounded, _navigation_requires_approval
from crew.browser.types import BrowserConfig
from crew.core.runctx import current_tool_call_id


def _png_header(width: int, height: int) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    )


class FakeProxy:
    def __init__(self, _policy) -> None:
        self.url = "http://127.0.0.1:45678"
        self.closed = False

    async def start(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class ReviewDriver(BrowserDriver):
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.tabs: dict[str, dict[str, str]] = {}
        self.active = ""
        self.snapshot_text = (
            '- button "Harmless e170" [ref=e170]\n'
            '- button "确定购买" [ref=e17]\n'
            '- textbox "Search" [ref=e18]'
        )
        self.title = "Review page"
        self.counter = 0
        self.scroll_x = 0
        self.scroll_y = 0
        self.width = 100
        self.height = 50
        self.dpr = 1
        self.time_origin = 1000
        self.security_digest = "security-1"
        self.element_security: dict[str, str] = {}
        self.element_navigation: dict[str, str] = {}
        self.native_refs_ready = False
        self.dialog_pending: dict[str, object] | None = None
        self.navigate_during_snapshot = False
        self.mutate_during_screenshot = False
        self.current_target_names: dict[str, str] = {}
        self.mutate_on_mouse_move = False
        self.popup_on_click = False
        self.native_tab_counter = 0
        self.popups: dict[str, dict[str, str]] = {}
        self.can_go_back = False
        self.can_go_forward = False

    def available(self) -> bool:
        return True

    def _new_native_tab_id(self) -> str:
        self.native_tab_counter += 1
        return f"t{self.native_tab_counter}"

    def open_popup(
        self,
        url: str = "https://popup.example/",
        *,
        opener_target_id: str = "",
    ) -> str:
        tab_id = self._new_native_tab_id()
        self.popups[tab_id] = {
            "url": url,
            "title": "Unowned popup",
            "targetId": f"target-{tab_id}",
            "openerTargetId": opener_target_id,
        }
        self.active = tab_id
        return tab_id

    def _active_page(self) -> dict[str, str]:
        if self.active in self.tabs:
            return self.tabs[self.active]
        return self.popups[self.active]

    def _snapshot_target_name(self, native_ref: str) -> str:
        if native_ref in self.current_target_names:
            return self.current_target_names[native_ref]
        ref = native_ref.removeprefix("@")
        for line in self.snapshot_text.splitlines():
            if f"[ref={ref}]" not in line:
                continue
            match = re.match(r'^\s*-\s*[\w-]+(?:\s+"((?:\\.|[^"\\])*)")?', line)
            if match is None or match.group(1) is None:
                return ""
            return json.loads(f'"{match.group(1)}"')
        return ""

    def marker(self) -> str:
        return json.dumps(
            {
                "token": "guard-token",
                "counter": self.counter,
                "href": self._active_page()["url"],
                "timeOrigin": self.time_origin,
                "scrollX": self.scroll_x,
                "scrollY": self.scroll_y,
                "width": self.width,
                "height": self.height,
                "dpr": self.dpr,
                "securityDigest": self.security_digest,
                "elementSecurity": self.element_security,
                "elementNavigation": self.element_navigation,
            },
            sort_keys=True,
        )

    async def execute(
        self,
        owner_session: str,
        profile_dir: Path,
        command: str,
        args=(),
        **_kwargs,
    ) -> dict:
        values = tuple(str(item) for item in args)
        self.calls.append((command, values))
        if command == "tab":
            # The Electron Host's process-local ref epoch is cleared by native
            # tab creation/switch/close, including a no-op active-tab switch.
            if values == ("list",):
                rows = [
                    {
                        "tabId": data["tabId"],
                        "label": label,
                        "title": data["title"],
                        "url": data["url"],
                        "type": "page",
                        "active": self.active == label,
                        "targetId": data["targetId"],
                    }
                    for label, data in self.tabs.items()
                ]
                rows.extend(
                    {
                        "tabId": tab_id,
                        "label": None,
                        "title": data["title"],
                        "url": data["url"],
                        "type": "page",
                        "active": self.active == tab_id,
                        "targetId": data["targetId"],
                        "openerTargetId": data.get("openerTargetId", ""),
                    }
                    for tab_id, data in self.popups.items()
                )
                return {"success": True, "data": {"tabs": rows}}

            self.native_refs_ready = False
            if values and values[0] in {"new", "new-user"}:
                label = values[values.index("--label") + 1]
                tab_id = self._new_native_tab_id()
                self.tabs[label] = {
                    "tabId": tab_id,
                    "targetId": f"target-{tab_id}",
                    "url": values[-1],
                    "title": self.title,
                }
                self.active = label
            elif values and values[0] == "close":
                target = values[-1]
                owned_label = next(
                    (
                        label
                        for label, data in self.tabs.items()
                        if label == target or data["tabId"] == target
                    ),
                    "",
                )
                if owned_label:
                    self.tabs.pop(owned_label, None)
                    if self.active == owned_label:
                        self.active = next(iter(self.tabs), next(iter(self.popups), ""))
                elif target in self.popups:
                    self.popups.pop(target, None)
                    if self.active == target:
                        self.active = next(iter(self.tabs), next(iter(self.popups), ""))
            elif values:
                target = values[0]
                self.active = next(
                    (
                        label
                        for label, data in self.tabs.items()
                        if label == target or data["tabId"] == target
                    ),
                    target,
                )
        elif command == "open":
            self._active_page()["url"] = values[0]
        elif command == "preview":
            self._active_page()["url"] = "crew-artifact://preview/index.html"
            return {"success": True, "data": {"url": self._active_page()["url"]}}
        elif command == "get":
            key = values[0]
            if key == "history":
                return {
                    "success": True,
                    "data": {
                        "can_go_back": self.can_go_back,
                        "can_go_forward": self.can_go_forward,
                    },
                }
            if key == "text" and len(values) >= 2:
                return {
                    "success": True,
                    "data": {"text": self._snapshot_target_name(values[1])},
                }
            return {
                "success": True,
                "data": {key: self._active_page().get("title" if key == "title" else key, "")},
            }
        elif command == "snapshot":
            self.native_refs_ready = True
            if self.navigate_during_snapshot:
                self.time_origin += 1
            return {"success": True, "data": {"snapshot": self.snapshot_text}}
        elif command in {"click", "fill", "upload", "download"}:
            if values and values[0].startswith("@e") and not self.native_refs_ready:
                raise BrowserDriverError(f"Unknown ref: {values[0]}")
            if command == "click" and self.popup_on_click:
                self.popup_on_click = False
                self.open_popup()
            if command == "click" and self.dialog_pending is not None:
                return {"success": True, "data": {}}
        elif command == "dialog" and values and values[0] == "status":
            return {
                "success": True,
                "data": self.dialog_pending or {"hasDialog": False},
            }
        elif command == "eval":
            expression = values[0]
            if "performance.timeOrigin" in expression:
                if "MutationObserver" in expression:
                    self.counter = 0
                return {"success": True, "data": {"value": self.marker()}}
            if "elementFromPoint" in expression:
                return {"success": True, "data": {"value": '{"tag":"BUTTON","name":"Continue"}'}}
            return {"success": True, "data": {"value": "[]"}}
        elif command == "screenshot":
            if self.mutate_during_screenshot:
                self.counter += 1
            target = Path(values[-1])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(_png_header(200, 100))
            return {"success": True, "data": {"path": str(target)}}
        elif command == "mouse":
            # Mirror the deterministic Electron Host input surface.
            assert values and values[0] in {"move", "down", "up", "wheel"}
            if values[0] == "move":
                assert len(values) == 3
                int(values[1])
                int(values[2])
                if self.mutate_on_mouse_move:
                    self.counter += 1
        return {"success": True, "data": {}}

    async def close(self, owner_session: str, profile_dir: Path) -> None:
        self.calls.append(("close", (owner_session, str(profile_dir))))

    async def page_images(self, *args, target_id: str, **_kwargs):
        self.calls.append(("page_images", (target_id,)))
        return [{"src": "https://example.com/image.png", "alt": "Example"}]

    async def close_target(self, *args, target_id: str, **_kwargs) -> None:
        self.calls.append(("close_target", (target_id,)))
        for label, data in list(self.tabs.items()):
            if data.get("targetId") == target_id:
                self.tabs.pop(label, None)
                if self.active == label:
                    self.active = next(iter(self.tabs), next(iter(self.popups), ""))
                return
        for tab_id, data in list(self.popups.items()):
            if data.get("targetId") == target_id:
                self.popups.pop(tab_id, None)
                if self.active == tab_id:
                    self.active = next(iter(self.tabs), next(iter(self.popups), ""))
                return


class TransitionReviewDriver(ReviewDriver):
    """Production-shaped page markers for transition stability regressions."""

    def __init__(self) -> None:
        super().__init__()
        self.navigation_epoch = 0
        self.navigation_pending = False
        self.location_consistent = True

    def marker(self) -> str:
        data = json.loads(super().marker())
        page = self._active_page()
        data.update(
            {
                "targetId": page["targetId"],
                "frameId": "main-frame",
                "loaderId": "main-loader",
                "navigationEpoch": self.navigation_epoch,
                "navigationPending": self.navigation_pending,
                "titleDigest": f"title:{page['title']}",
                "locationConsistent": self.location_consistent,
            }
        )
        return json.dumps(data, sort_keys=True)

    async def page_guard(
        self,
        *_args,
        reset: bool,
        include_security: bool = True,
        **_kwargs,
    ) -> str:
        if reset:
            self.counter = 0
        data = json.loads(self.marker())
        if not include_security:
            data.pop("securityDigest", None)
            data.pop("elementSecurity", None)
            data.pop("elementNavigation", None)
        return json.dumps(data, sort_keys=True)


@pytest.fixture
def browser_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crew.browser.manager.get_owner_runtime_home",
        lambda owner: tmp_path / "accounts" / str(owner),
    )
    monkeypatch.setattr("crew.browser.manager.LoopbackPolicyProxy", FakeProxy)
    return tmp_path


def _approve_click(
    manager: BrowserManager,
    ref: str,
    *,
    owner: str = "owner",
    session: str = "session",
) -> None:
    args = {"ref": ref}
    decision = manager.permission_for("browser_click", args, owner, session)
    if decision is not None:
        assert decision.approval_token
        assert manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            owner,
            session,
        )


async def test_ref_descriptions_are_bound_to_exact_snapshot_lines(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        output = await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]

        assert "Harmless e170" in session.refs["p1:e170"]
        assert "确定购买" in session.refs["p1:e17"]
        assert "Harmless e170" not in session.refs["p1:e17"]
        assert "p1:e170" in output and "p1:e17" in output
        decision = manager.permission_for(
            "browser_click",
            {"ref": "p1:e17"},
            "owner",
            "session",
        )
        assert decision is not None and decision.behavior == "ask"
    finally:
        await manager.aclose()


async def test_ordinary_ref_click_is_automatic_and_enter_fails_closed(
    browser_env,
):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args = {"ref": "p1:e170"}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision is None
        output = await manager.click("owner", "session", "p1:e170")
        assert "p2:e170" in output

        enter_permission = manager.permission_for(
            "browser_press",
            {"key": "Enter"},
            "owner",
            "session",
        )
        assert enter_permission and enter_permission.behavior == "deny"
        with pytest.raises(BrowserDriverError, match="必须绑定"):
            await manager.press("owner", "session", "Enter")
        assert not any(
            command == "press" and values == ("Enter",) for command, values in driver.calls
        )
    finally:
        await manager.aclose()


async def test_safe_link_uses_direct_navigation_without_page_click_handler(
    browser_env,
):
    driver = ReviewDriver()
    driver.snapshot_text = '- link "Next" [ref=e170]'
    driver.element_security = {"link\0next": "link-security-1"}
    driver.element_navigation = {"link\0next": "https://example.com/next"}
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        output = await manager.navigate("owner", "session", "https://example.com")
        assert "p1:e170" in output
        assert (
            manager.permission_for(
                "browser_click",
                {"ref": "p1:e170"},
                "owner",
                "session",
            )
            is None
        )

        driver.calls.clear()
        output = await manager.click("owner", "session", "p1:e170")

        assert ("open", ("https://example.com/next",)) in driver.calls
        assert not any(command == "click" for command, _args in driver.calls)
        assert "p2:e170" in output
    finally:
        await manager.aclose()


async def test_safe_link_url_with_final_action_requires_approval(browser_env):
    driver = ReviewDriver()
    driver.snapshot_text = '- link "View details" [ref=e170]'
    driver.element_security = {"link\0view details": "link-security-1"}
    driver.element_navigation = {"link\0view details": "https://example.com/%2564elete-account"}
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args = {"ref": "p1:e170"}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision and decision.behavior == "ask"
        with pytest.raises(BrowserDriverError, match="一次性审批"):
            await manager.click("owner", "session", "p1:e170")
        assert manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            "owner",
            "session",
        )

        await manager.click("owner", "session", "p1:e170")

        assert (
            "open",
            ("https://example.com/%2564elete-account",),
        ) in driver.calls
        assert not any(command == "click" for command, _args in driver.calls)
    finally:
        await manager.aclose()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/place_order",
        "https://example.com/api?action=save_changes",
        "https://example.com/signIn",
        "https://example.com/%2570urchase",
        "https://example.com/%25252573ubmit",
        "https://example.com/?next=%25252564elete",
        "https://app.example/#/delete-account",
        "https://app.example/#/placeOrder",
        "https://app.example/#/settings?action=save_changes",
        "https://example.com/logout",
        "https://example.com/sign_out",
        "https://example.com/oauth/revoke",
        "https://example.com/account/deactivate",
        "https://delete-account.example/",
        "https://placeOrder.example/",
        "https://checkout.example/",
    ],
)
def test_navigation_risk_normalizes_common_route_encodings(url: str):
    assert _navigation_requires_approval("View details", url)


async def test_high_risk_initial_navigate_is_bound_to_url_only_approval(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    args = {"url": "https://example.com/delete-account"}
    try:
        decision = manager.permission_for("browser_navigate", args, "owner", "session")
        assert decision and decision.behavior == "ask"
        assert manager.confirm_approval(
            decision.approval_token,
            "browser_navigate",
            args,
            "owner",
            "session",
        )

        await manager.navigate("owner", "session", args["url"])

        assert any(
            command == "tab" and values[-1] == args["url"] for command, values in driver.calls
        )
    finally:
        await manager.aclose()


async def test_high_risk_new_tab_requires_page_bound_approval(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    args = {"action": "new", "url": "https://example.com/place_order"}
    try:
        await manager.navigate("owner", "session", "https://example.com")
        with pytest.raises(BrowserDriverError, match="一次性审批"):
            await manager.tabs("owner", "session", **args)

        decision = manager.permission_for("browser_tabs", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_tabs",
            args,
            "owner",
            "session",
        )
        await manager.tabs("owner", "session", **args)

        assert any(
            command == "tab" and values[-1] == args["url"] for command, values in driver.calls
        )
    finally:
        await manager.aclose()


async def test_new_tab_no_longer_depends_on_a_gateway_pixel_stream(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    args = {"action": "new", "url": "https://example.com/place_order"}
    try:
        await manager.navigate("owner", "session", "https://example.com")
        assert not hasattr(manager, "_streams")
        decision = manager.permission_for("browser_tabs", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_tabs",
            args,
            "owner",
            "session",
        )
        await manager.tabs("owner", "session", **args)

        assert any(
            command == "tab" and values[:1] == ("new",) and values[-1] == args["url"]
            for command, values in driver.calls
        )
    finally:
        await manager.aclose()


async def test_high_risk_navigation_after_last_tab_close_uses_empty_generation(
    browser_env,
):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]
        await manager.tabs(
            "owner",
            "session",
            "close",
            tab_id=session.active_label,
        )
        assert not session.tabs and session.generation > 0

        args = {"url": "https://example.com/delete-account"}
        decision = manager.permission_for("browser_navigate", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_navigate",
            args,
            "owner",
            "session",
        )
        await manager.navigate("owner", "session", args["url"])
        assert manager.state("owner", "session")["url"] == args["url"]
    finally:
        await manager.aclose()


async def test_tab_close_uses_target_id_not_reused_native_tab_id(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session-a", "https://example.com/a")
        await manager.navigate("owner", "session-b", "https://example.com/b")
        owner = manager._owners["owner"]
        session_a = owner.sessions["session-a"]
        session_b = owner.sessions["session-b"]
        tab_a = session_a.tabs[session_a.active_label]
        tab_b = session_b.tabs[session_b.active_label]

        # A disappeared and B's native tN was reused. Closing A must address
        # A's immutable targetId and must never close B by stale tN.
        driver.tabs.pop(tab_a.label)
        driver.tabs[tab_b.label]["tabId"] = tab_a.native_id
        driver.active = tab_b.label

        await manager.tabs(
            "owner",
            "session-a",
            "close",
            tab_id=tab_a.id,
        )

        assert tab_b.label in driver.tabs
        assert (
            "close_target",
            (tab_a.target_id,),
        ) in driver.calls
        assert not any(
            command == "tab" and values[:1] == ("close",) for command, values in driver.calls
        )
    finally:
        await manager.aclose()


async def test_close_session_fail_stops_account_instead_of_forgetting_live_tab(
    browser_env,
):
    class FailingCloseDriver(ReviewDriver):
        async def close_target(self, *args, target_id: str, **kwargs) -> None:
            self.calls.append(("close_target", (target_id,)))
            raise BrowserDriverError("close failed", uncertain=True)

        async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
            self.calls.append(("interrupt", (owner_session,)))
            self.tabs.clear()
            self.popups.clear()
            self.active = ""

    driver = FailingCloseDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")

        await manager.close_session("owner", "session")

        owner = manager._owners["owner"]
        assert "session" not in owner.sessions
        assert not driver.tabs
        assert owner.actions_blocked and not owner.running
        assert any(command == "interrupt" for command, _args in driver.calls)
    finally:
        await manager.aclose()


async def test_close_session_retains_tombstone_when_fail_stop_is_unconfirmed(
    browser_env,
):
    class UnstoppableDriver(ReviewDriver):
        async def close_target(self, *args, target_id: str, **kwargs) -> None:
            raise BrowserDriverError("close failed", uncertain=True)

        async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
            raise BrowserDriverError("stop failed", stop_unconfirmed=True)

    driver = UnstoppableDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")

        with pytest.raises(BrowserDriverError, match="保留所有权记录"):
            await manager.close_session("owner", "session")

        owner = manager._owners["owner"]
        assert "session" in owner.sessions
        assert owner.actions_blocked and owner.stop_unconfirmed
        assert driver.tabs
    finally:
        await manager.aclose()


async def test_close_session_revokes_pending_and_granted_approvals(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        click_args = {"ref": "p1:e17"}
        granted = manager.permission_for("browser_click", click_args, "owner", "session")
        assert granted and manager.confirm_approval(
            granted.approval_token,
            "browser_click",
            click_args,
            "owner",
            "session",
        )
        pending = manager.permission_for(
            "browser_upload",
            {"ref": "p1:e18", "paths": ["/tmp/file"]},
            "owner",
            "session",
        )
        assert pending

        await manager.close_session("owner", "session")

        assert not any(
            approval.owner == "owner" and approval.session_id == "session"
            for approval in manager._pending_approvals.values()
        )
        assert not any(
            approval.owner == "owner" and approval.session_id == "session"
            for approval in manager._granted_approvals.values()
        )
    finally:
        await manager.aclose()


async def test_non_enter_safe_key_does_not_require_approval(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args = {"key": "Delete"}
        decision = manager.permission_for("browser_press", args, "owner", "session")
        assert decision is None

        await manager.press("owner", "session", "Delete")

        assert ("press", ("Delete",)) in driver.calls
    finally:
        await manager.aclose()


async def test_searchbox_enter_is_ref_bound_and_requires_approval(browser_env):
    driver = ReviewDriver()
    driver.snapshot_text = '- searchbox "搜索" [ref=e7]'
    driver.element_security = {"searchbox\0搜索": "search-security"}
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args = {"key": "Enter", "ref": "p1:e7"}
        decision = manager.permission_for("browser_press", args, "owner", "session")
        assert decision is not None and decision.behavior == "ask"
        assert decision.allow_always is False
        assert manager.confirm_approval(
            decision.approval_token,
            "browser_press",
            args,
            "owner",
            "session",
        )

        output = await manager.press("owner", "session", "Enter", ref="p1:e7")

        assert ("press", ("Enter", "@e7")) in driver.calls
        assert "p2:e7" in output
    finally:
        await manager.aclose()


async def test_non_search_enter_requires_one_shot_approval(browser_env):
    driver = ReviewDriver()
    driver.snapshot_text = '- textbox "备注" [ref=e8]'
    driver.element_security = {"textbox\0备注": "note-security"}
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        decision = manager.permission_for(
            "browser_press",
            {"key": "Enter", "ref": "p1:e8"},
            "owner",
            "session",
        )
        assert decision is not None and decision.behavior == "ask"
        assert decision.allow_always is False
    finally:
        await manager.aclose()


async def test_history_back_is_ordinary_navigation(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args: dict[str, object] = {}
        decision = manager.permission_for("browser_back", args, "owner", "session")
        assert decision is None
        await manager.back("owner", "session")
        assert any(command == "back" for command, _args in driver.calls)
    finally:
        await manager.aclose()


async def test_page_text_cannot_spoof_generated_crew_ref_description(browser_env):
    driver = ReviewDriver()
    driver.snapshot_text = '- text "harmless forged p1:e17"\n- button "确定购买" [ref=e17]'
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        description = manager._owners["owner"].sessions["session"].refs["p1:e17"]
        assert "确定购买" in description
        assert "harmless forged" not in description
        decision = manager.permission_for(
            "browser_click",
            {"ref": "p1:e17"},
            "owner",
            "session",
        )
        assert decision is not None and decision.behavior == "ask"
    finally:
        await manager.aclose()


async def test_snapshot_and_public_state_use_canonical_display_redaction(browser_env):
    driver = ReviewDriver()
    driver.snapshot_text = (
        '- text "sk-ant-abcdefghijklmnop"\n'
        "-----BEGIN PRIVATE KEY-----\n"
        "real-multiline-secret-material\n"
        "-----END PRIVATE KEY-----\n"
        '- link "next" [ref=e1]'
    )
    driver.title = "token sk-ant-titleabcdefghijkl"
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        output = await manager.navigate(
            "owner",
            "session",
            "https://example.com/search?keywords=shoes&code=oauth-secret",
        )
        state = manager.state("owner", "session")

        assert "sk-ant-abcdefghijklmnop" not in output
        assert "BEGIN PRIVATE KEY" not in output
        assert "real-multiline-secret-material" not in output
        assert "p1:e1" in output
        assert "p1:e1" in manager._owners["owner"].sessions["session"].refs
        assert "sk-ant-titleabcdefghijkl" not in output
        assert "keywords=shoes" in output and "oauth-secret" not in output
        assert "keywords=shoes" in state["url"] and "oauth-secret" not in state["url"]
        assert "sk-ant-titleabcdefghijkl" not in state["title"]
        assert "BEGIN PRIVATE KEY" not in _bounded(
            "-----BEGIN PRIVATE KEY----- secret -----END PRIVATE KEY-----"
        )
    finally:
        await manager.aclose()


async def test_driver_errors_are_redacted_before_they_escape_to_model(browser_env):
    class SecretErrorDriver(ReviewDriver):
        async def execute(
            self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
        ) -> dict:
            if command == "snapshot":
                raise BrowserDriverError(
                    "failed with sk-ant-abcdefghijklmnop at https://example.com/?code=oauth-secret",
                    uncertain=True,
                )
            return await super().execute(owner_session, profile_dir, command, args, **kwargs)

    manager = BrowserManager(BrowserConfig(), SecretErrorDriver())
    try:
        with pytest.raises(BrowserDriverError) as captured:
            await manager.navigate("owner", "session", "https://example.com")
        message = str(captured.value)
        assert "sk-ant-abcdefghijklmnop" not in message
        assert "oauth-secret" not in message
        assert captured.value.uncertain
    finally:
        await manager.aclose()


async def test_live_mutations_scroll_and_viewport_change_keep_refs(browser_env):
    # 视口变化（挂载/调整面板、拖窗口）不再使 ref 失效：ref 是活的原生句柄，
    # 元素级完整性由每次动作的签名重查与点击后 hit-test 兜底；坐标点击仍由
    # 更严格的 screenshot marker 约束（见 test_browser_use.py 视觉坐标用例）。
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.counter = 20
        driver.scroll_y = 300

        _approve_click(manager, "p1:e170")
        output = await manager.click("owner", "session", "p1:e170")
        assert "p2:e170" in output

        driver.width = 101
        driver.height = 60
        driver.dpr = 2
        _approve_click(manager, "p2:e170")
        output = await manager.click("owner", "session", "p2:e170")
        assert "p3:e170" in output
    finally:
        await manager.aclose()


async def test_document_identity_change_still_invalidates_refs(browser_env):
    # 文档身份变化（导航/重载，timeOrigin 变化）仍必须作废旧 ref。
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        _approve_click(manager, "p1:e170")
        output = await manager.click("owner", "session", "p1:e170")
        assert "p2:e170" in output

        driver.time_origin += 1
        _approve_click(manager, "p2:e170")
        with pytest.raises(BrowserDriverError, match="页面已在审批"):
            await manager.click("owner", "session", "p2:e170")
    finally:
        await manager.aclose()


async def test_click_rejects_a_target_renamed_to_high_risk_after_snapshot(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.current_target_names["@e170"] = "确定购买"
        driver.counter += 1

        with pytest.raises(BrowserDriverError, match="目标元素.*发生变化"):
            await manager.click("owner", "session", "p1:e170")

        assert not any(command == "click" for command, _ in driver.calls)
        assert not manager._owners["owner"].sessions["session"].refs
    finally:
        await manager.aclose()


async def test_fill_revalidates_target_after_unrelated_dom_mutation(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.counter += 1
        args = {"ref": "p1:e18", "text": "private model text"}
        decision = manager.permission_for("browser_type", args, "owner", "session")
        assert decision is None

        output = await manager.fill("owner", "session", args["ref"], args["text"])

        assert "p2:e18" in output
        assert any(command == "fill" for command, _ in driver.calls)
    finally:
        await manager.aclose()


async def test_ref_action_rejects_changed_exact_element_fingerprint(browser_env):
    driver = ReviewDriver()
    driver.element_security = {"button\0harmless e170": "fingerprint-1"}
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]
        assert session.ref_security["p1:e170"] == "fingerprint-1"

        driver.element_security["button\0harmless e170"] = "fingerprint-2"
        driver.counter += 1
        with pytest.raises(BrowserDriverError, match="目标元素.*发生变化"):
            await manager.click("owner", "session", "p1:e170")

        assert not any(command == "click" for command, _args in driver.calls)
    finally:
        await manager.aclose()


async def test_old_screenshot_is_rejected_and_dpr_coordinates_use_css_pixels(browser_env):
    driver = ReviewDriver()
    driver.dpr = 2
    manager = BrowserManager(BrowserConfig(), driver)
    call_id = current_tool_call_id.set("coordinate-review")
    try:
        await manager.navigate("owner", "session", "https://example.com")
        vision = await manager.vision("owner", "session", "click target")
        screenshot_id = json.loads(vision.content.splitlines()[1])["screenshot_id"]
        args = {"screenshot_id": screenshot_id, "x": 100, "y": 50}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            "owner",
            "session",
        )

        await manager.coordinate_click("owner", "session", screenshot_id, 100, 50)
        mouse_calls = [values for command, values in driver.calls if command == "mouse"]
        assert mouse_calls[-3:] == [
            ("move", "50", "25"),
            ("down", "left"),
            ("up", "left"),
        ]
        assert any(
            command == "eval" and "elementFromPoint(50,25)" in values[0]
            for command, values in driver.calls
        )

        vision = await manager.vision("owner", "session", "stale target")
        stale_id = json.loads(vision.content.splitlines()[1])["screenshot_id"]
        stale_args = {"screenshot_id": stale_id, "x": 10, "y": 10}
        decision = manager.permission_for("browser_click", stale_args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            stale_args,
            "owner",
            "session",
        )
        driver.counter += 1
        driver.scroll_y += 1
        with pytest.raises(BrowserDriverError, match="滚动位置"):
            await manager.coordinate_click("owner", "session", stale_id, 10, 10)
    finally:
        current_tool_call_id.reset(call_id)
        await manager.aclose()


async def test_atomic_coordinate_click_avoids_eval_and_three_step_mouse_path(browser_env):
    class AtomicDriver(ReviewDriver):
        atomic_calls: list[tuple[str, int, int, str]]
        host_epoch = "fedcba9876543210fedcba9876543210"

        def __init__(self) -> None:
            super().__init__()
            self.atomic_calls = []

        async def execute(self, *args, **kwargs) -> dict:
            result = await super().execute(*args, **kwargs)
            if len(args) >= 3 and args[2] == "screenshot":
                result["data"]["host_epoch"] = self.host_epoch
            return result

        async def coordinate_click_atomic(
            self,
            *_args,
            target_id: str,
            x: int,
            y: int,
            expected_epoch: str = "",
            **_kwargs,
        ) -> dict:
            self.atomic_calls.append((target_id, x, y, expected_epoch))
            return {"clicked": True, "target": {"tag": "BUTTON", "role": "button"}}

        async def page_guard(self, *_args, **_kwargs) -> str:
            # Mirror the production Electron driver so the assertion below
            # covers the complete Host-native path, including post-click
            # observation, without entering the alternative-driver eval shim.
            return self.marker()

    driver = AtomicDriver()
    driver.dpr = 2
    manager = BrowserManager(BrowserConfig(), driver)
    call_id = current_tool_call_id.set("atomic-coordinate-review")
    try:
        await manager.navigate("owner", "session", "https://example.com")
        vision = await manager.vision("owner", "session", "click target")
        screenshot_id = json.loads(vision.content.splitlines()[1])["screenshot_id"]
        args = {"screenshot_id": screenshot_id, "x": 100, "y": 50}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            "owner",
            "session",
        )
        calls_before = len(driver.calls)

        await manager.coordinate_click("owner", "session", screenshot_id, 100, 50)

        target_id = manager._owners["owner"].sessions["session"].tabs[
            manager._owners["owner"].sessions["session"].active_label
        ].target_id
        assert driver.atomic_calls == [(target_id, 50, 25, driver.host_epoch)]
        assert not any(
            command in {"eval", "mouse"} for command, _args in driver.calls[calls_before:]
        )
    finally:
        current_tool_call_id.reset(call_id)
        await manager.aclose()


async def test_vision_rejects_malformed_host_epoch(browser_env):
    class InvalidEpochDriver(ReviewDriver):
        async def execute(self, *args, **kwargs) -> dict:
            result = await super().execute(*args, **kwargs)
            if len(args) >= 3 and args[2] == "screenshot":
                result["data"]["host_epoch"] = "attacker-controlled"
            return result

    manager = BrowserManager(BrowserConfig(), InvalidEpochDriver())
    try:
        await manager.navigate("owner", "session", "https://example.com")
        with pytest.raises(BrowserDriverError, match="无效的视觉截图 epoch"):
            await manager.vision("owner", "session", "inspect")
        assert manager._owners["owner"].sessions["session"].screenshot_id == ""
    finally:
        await manager.aclose()


async def test_page_dom_annotation_is_disabled(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.calls.clear()
        with pytest.raises(BrowserDriverError, match="修改不可信页面 DOM"):
            await manager.vision("owner", "session", "annotated", annotate=True)
        assert not any(command == "screenshot" for command, _args in driver.calls)
    finally:
        await manager.aclose()


async def test_get_images_uses_driver_cdp_reader_not_page_eval(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.calls.clear()

        output = await manager.get_images("owner", "session")

        assert "https://example.com/image.png" in output
        assert any(command == "page_images" for command, _args in driver.calls)
        assert not any(
            command == "eval" and "document.images" in " ".join(args)
            for command, args in driver.calls
        )
    finally:
        await manager.aclose()


async def test_direct_driver_lifecycle_error_blocks_followup_browser_actions(
    browser_env,
):
    class UnknownStopDriver(ReviewDriver):
        async def page_images(self, *args, **kwargs):
            raise BrowserDriverError(
                "browser stop unknown",
                uncertain=True,
                stop_unconfirmed=True,
            )

    driver = UnknownStopDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")

        with pytest.raises(BrowserDriverError, match="stop unknown"):
            await manager.get_images("owner", "session")

        owner = manager._owners["owner"]
        assert owner.actions_blocked and owner.stop_unconfirmed
        assert not owner.sessions["session"].tabs
        with pytest.raises(BrowserDriverError, match="已停止"):
            await manager.snapshot("owner", "session")
    finally:
        await manager.aclose()


async def test_coordinate_click_rechecks_guard_after_mouse_move(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("coordinate-move-race")
    try:
        await manager.navigate("owner", "session", "https://example.com")
        vision = await manager.vision("owner", "session", "target")
        screenshot_id = json.loads(vision.content.splitlines()[1])["screenshot_id"]
        args = {"screenshot_id": screenshot_id, "x": 10, "y": 10}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            "owner",
            "session",
        )
        driver.mutate_on_mouse_move = True

        with pytest.raises(BrowserDriverError, match="鼠标移动后发生变化"):
            await manager.coordinate_click("owner", "session", screenshot_id, 10, 10)

        mouse_calls = [values for command, values in driver.calls if command == "mouse"]
        assert mouse_calls[-1][0] == "move"
        assert not any(values[0] == "down" for values in mouse_calls)
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


class InterruptibleDriver(ReviewDriver):
    def __init__(self) -> None:
        super().__init__()
        self.press_started = asyncio.Event()
        self.release_press = asyncio.Event()
        self.interrupt_called = asyncio.Event()
        self.interrupted = False
        self.execute_after_interrupt: list[str] = []

    async def execute(
        self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
    ) -> dict:
        if self.interrupted:
            self.execute_after_interrupt.append(command)
        if command == "press":
            self.calls.append((command, tuple(str(item) for item in args)))
            self.press_started.set()
            await self.release_press.wait()
            return {"success": True, "data": {}}
        return await super().execute(owner_session, profile_dir, command, args, **kwargs)

    async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
        self.interrupted = True
        self.interrupt_called.set()
        self.release_press.set()
        await asyncio.sleep(0)
        await self.close(owner_session, profile_dir)


async def test_stop_atomically_blocks_sibling_and_new_session_restart(browser_env):
    driver = InterruptibleDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session-a", "https://example.com/a")
        await manager.navigate("owner", "session-b", "https://example.com/b")
        await manager.snapshot("owner", "session-a")
        press_args = {"key": "A"}
        decision = manager.permission_for("browser_press", press_args, "owner", "session-a")
        assert decision is None
        press_task = asyncio.create_task(manager.press("owner", "session-a", "A"))
        await driver.press_started.wait()

        stop_task = asyncio.create_task(manager.takeover("owner", "session-a", "stop"))
        await driver.interrupt_called.wait()
        new_task = asyncio.create_task(
            manager.navigate("owner", "session-new", "https://example.com/new")
        )

        with pytest.raises(BrowserDriverError, match="已停止"):
            await press_task
        await stop_task
        with pytest.raises(BrowserDriverError, match="已停止"):
            await new_task

        assert not driver.execute_after_interrupt
        assert all(
            manager.state("owner", session_id)["mode"] == "paused"
            for session_id in ("session-a", "session-b", "session-new")
        )
        with pytest.raises(BrowserDriverError, match="已停止"):
            await manager.navigate("owner", "another-new", "https://example.com")
    finally:
        await manager.aclose()


async def test_cross_session_switch_rejects_old_native_ref_until_snapshot(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session-a", "https://example.com/a")
        await manager.navigate("owner", "session-b", "https://example.com/b")

        with pytest.raises(BrowserDriverError, match="标签页切换"):
            await manager.click("owner", "session-a", "p1:e170")
        assert not any(command == "click" for command, _ in driver.calls)

        output = await manager.snapshot("owner", "session-a")
        session = manager._owners["owner"].sessions["session-a"]
        ref = next(value for value in session.refs if value.endswith(":e170"))
        assert ref in output
        _approve_click(manager, ref, owner="owner", session="session-a")
        await manager.click("owner", "session-a", ref)
        assert any(command == "click" for command, _ in driver.calls)
    finally:
        await manager.aclose()


async def test_native_active_tab_overrides_stale_owner_selection(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session-a", "https://example.com/a")
        await manager.navigate("owner", "session-b", "https://example.com/b")
        await manager.snapshot("owner", "session-a")
        owner = manager._owners["owner"]
        session_a = owner.sessions["session-a"]
        label_a = session_a.active_label
        label_b = owner.sessions["session-b"].active_label
        ref = next(value for value in session_a.refs if value.endswith(":e170"))

        # Simulate an out-of-band target activation. The manager cache still
        # claims A, while an unchecked process-local Host would execute on B.
        assert owner.selected_label == label_a
        driver.active = label_b
        clicks_before = sum(command == "click" for command, _ in driver.calls)

        with pytest.raises(BrowserDriverError, match="标签页切换"):
            await manager.click("owner", "session-a", ref)

        assert driver.active == label_a
        assert sum(command == "click" for command, _ in driver.calls) == clicks_before
        assert not session_a.refs
        assert not owner.native_ref_session
    finally:
        await manager.aclose()


async def test_reused_native_tab_id_cannot_replace_immutable_target_id(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]
        tab = session.tabs[session.active_label]
        cached_native_id = tab.native_id
        cached_target_id = tab.target_id
        assert cached_native_id and cached_target_id

        # Simulate a Browser Host tab-epoch reset. Its process-local tN and
        # persisted Crew label are reused, but Chromium has created a new
        # target. Neither value may overwrite the audited identity.
        native = driver.tabs[tab.label]
        assert native["tabId"] == cached_native_id
        native["targetId"] = "target-after-host-epoch-reset"

        with pytest.raises(BrowserDriverError, match="targetId 已变化"):
            await manager.snapshot("owner", "session")

        assert tab.native_id == cached_native_id
        assert tab.target_id == cached_target_id
        assert not session.refs
        assert not session.page_marker
    finally:
        await manager.aclose()


async def test_unowned_active_popup_is_closed_before_any_ref_action(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        label = session.active_label
        ref = next(value for value in session.refs if value.endswith(":e170"))
        popup_id = driver.open_popup()
        clicks_before = sum(command == "click" for command, _ in driver.calls)

        # Popup activation does not update Crew's cached selected_label.
        assert owner.selected_label == label
        with pytest.raises(BrowserDriverError, match="标签页切换"):
            await manager.click("owner", "session", ref)

        assert popup_id not in driver.popups
        assert ("close_target", (f"target-{popup_id}",)) in driver.calls
        assert driver.active == label
        assert sum(command == "click" for command, _ in driver.calls) == clicks_before
        assert not session.refs

        # A fresh observation repopulates the native ref cache; subsequent
        # actions remain bound to the owned tab.
        await manager.snapshot("owner", "session")
        fresh_ref = next(value for value in session.refs if value.endswith(":e170"))
        _approve_click(manager, fresh_ref)
        await manager.click("owner", "session", fresh_ref)
        assert sum(command == "click" for command, _ in driver.calls) == clicks_before + 1
    finally:
        await manager.aclose()


async def test_popup_opened_by_click_is_closed_before_post_action_snapshot(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]
        ref = next(value for value in session.refs if value.endswith(":e170"))
        driver.popup_on_click = True

        _approve_click(manager, ref)
        output = await manager.click("owner", "session", ref)

        assert "p2:e170" in output
        assert not driver.popups
        click_index = next(
            index for index, (command, _args) in enumerate(driver.calls) if command == "click"
        )
        close_index = next(
            index
            for index, (command, args) in enumerate(driver.calls)
            if index > click_index and command == "close_target"
        )
        snapshot_index = next(
            index
            for index, (command, _args) in enumerate(driver.calls)
            if index > click_index and command == "snapshot"
        )
        assert click_index < close_index < snapshot_index
    finally:
        await manager.aclose()


async def test_human_popup_adoption_uses_exact_native_target_id(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        main_target_id = session.tabs[session.active_label].target_id
        unrelated_popup = driver.open_popup(
            "https://login.example/callback",
            opener_target_id="target-from-another-session",
        )
        approved_popup = driver.open_popup(
            "https://login.example/callback",
            opener_target_id=main_target_id,
        )
        driver.popups[approved_popup]["targetId"] = "target-approved-popup"
        driver.popups[unrelated_popup]["title"] = "Sign in"

        async with owner.lock:
            selected, switched = await manager._select(owner, session)

        assert approved_popup in session.tabs
        assert session.tabs[approved_popup].target_id == "target-approved-popup"
        assert unrelated_popup not in session.tabs
        assert unrelated_popup not in driver.popups
        assert session.active_label == approved_popup
        assert driver.active == approved_popup
        assert selected.target_id == "target-approved-popup"
        assert switched is True
    finally:
        await manager.aclose()


async def test_takeover_sets_driver_mode(browser_env):
    class ModeDriver(ReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.mode_calls: list[tuple[str, str]] = []

        async def set_mode(
            self,
            _owner_session: str,
            _profile_dir: Path,
            *,
            target_id: str,
            mode: str,
        ) -> None:
            self.mode_calls.append((target_id, mode))

    driver = ModeDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]
        target_id = session.tabs[session.active_label].target_id

        await manager.takeover("owner", "session", "takeover")
        await manager.user_control("owner", "session", "return")
        await manager.takeover("owner", "session", "pause")
        await manager.user_control("owner", "session", "return")

        assert driver.mode_calls == [
            (target_id, "human"),
            (target_id, "ai"),
            (target_id, "paused"),
            (target_id, "ai"),
        ]
        assert session.mode == "ai"
    finally:
        await manager.aclose()


async def test_model_cannot_revoke_or_disrupt_user_control(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")

        await manager.takeover("owner", "session", "takeover")
        for action in ("return", "takeover", "pause", "stop"):
            with pytest.raises(BrowserDriverError, match="模型不能"):
                await manager.takeover("owner", "session", action)
        assert manager.state("owner", "session")["mode"] == "human"
        assert not any(command == "close" for command, _args in driver.calls)

        await manager.user_control("owner", "session", "return")
        await manager.takeover("owner", "session", "pause")
        for action in ("return", "takeover", "pause", "stop"):
            with pytest.raises(BrowserDriverError, match="模型不能"):
                await manager.takeover("owner", "session", action)
        assert manager.state("owner", "session")["mode"] == "paused"

        await manager.user_control("owner", "session", "return")
        assert manager.state("owner", "session")["mode"] == "ai"
    finally:
        await manager.aclose()


async def test_model_stop_loses_race_to_trusted_takeover(browser_env):
    class BlockingModeDriver(ReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.human_mode_started = asyncio.Event()
            self.release_human_mode = asyncio.Event()

        async def set_mode(
            self,
            _owner_session: str,
            _profile_dir: Path,
            *,
            target_id: str,
            mode: str,
        ) -> None:
            del target_id
            if mode == "human":
                self.human_mode_started.set()
                await self.release_human_mode.wait()

    driver = BlockingModeDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        takeover_task = asyncio.create_task(
            manager.user_control("owner", "session", "takeover")
        )
        await driver.human_mode_started.wait()

        stop_task = asyncio.create_task(manager.takeover("owner", "session", "stop"))
        await asyncio.sleep(0)
        assert not stop_task.done()

        driver.release_human_mode.set()
        await takeover_task
        with pytest.raises(BrowserDriverError, match="模型不能"):
            await stop_task
        assert manager.state("owner", "session")["mode"] == "human"
        assert not any(command == "close" for command, _args in driver.calls)
    finally:
        driver.release_human_mode.set()
        await manager.aclose()


async def test_user_can_open_blank_browser_preview_workspace_html_and_close_tab(
    browser_env,
):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    workspace = browser_env / "workspace"
    workspace.mkdir()
    page = workspace / "index.html"
    page.write_text("<title>Preview</title>", encoding="utf-8")
    try:
        opened = await manager.open_for_user("owner", "session")
        assert opened["mode"] == "human"
        assert opened["url"] == "about:blank"
        assert opened["can_go_back"] is False
        assert opened["can_go_forward"] is False
        assert ("tab", ("new-user", "--label", opened["tab_label"], "about:blank")) in driver.calls

        driver.can_go_back = True
        driver.can_go_forward = True
        refreshed = await manager.human_command("owner", "session", "reload")
        assert refreshed["can_go_back"] is True
        assert refreshed["can_go_forward"] is True

        previewed = await manager.open_for_user(
            "owner",
            "session",
            artifact_path=str(page),
            artifact_root=str(workspace),
        )
        assert previewed["mode"] == "human"
        assert previewed["url"].startswith("crew-artifact://")

        closed = await manager.human_command("owner", "session", "close_tab")
        assert closed["tab_id"] == ""
        assert closed["tabs"] == []
    finally:
        await manager.aclose()


async def test_user_artifact_preview_rejects_symlink_escape(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    workspace = browser_env / "workspace"
    workspace.mkdir()
    outside = browser_env / "secret.html"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "index.html"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink is unavailable")
    try:
        with pytest.raises(BrowserDriverError, match="当前工作区"):
            await manager.open_for_user(
                "owner",
                "session",
                artifact_path=str(link),
                artifact_root=str(workspace),
            )
    finally:
        await manager.aclose()


async def test_pending_confirm_stops_automatic_snapshot(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.dialog_pending = {
            "hasDialog": True,
            "type": "confirm",
            "message": "确定提交？",
        }
        _approve_click(manager, "p1:e170")
        output = await manager.click("owner", "session", "p1:e170")
        assert "dialog_pending" in output
        assert "dialog_status" in output
        assert not driver.calls[-1][0] == "snapshot"
    finally:
        await manager.aclose()


async def test_snapshot_and_vision_reject_capture_window_changes(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.navigate_during_snapshot = True
        with pytest.raises(BrowserDriverError, match="采集期间"):
            await manager.snapshot("owner", "session")
        driver.navigate_during_snapshot = False
        await manager.snapshot("owner", "session")

        driver.mutate_during_screenshot = True
        with pytest.raises(BrowserDriverError, match="视觉截图采集期间"):
            await manager.vision("owner", "session", "check")
    finally:
        await manager.aclose()


async def test_snapshot_waits_for_delayed_baidu_like_pushstate_before_publishing(
    browser_env,
):
    class DelayedPushStateDriver(TransitionReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.snapshot_observations: list[tuple[str, str, str]] = []
            self.transition_scheduled = False

        async def execute(
            self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
        ) -> dict:
            result = await super().execute(owner_session, profile_dir, command, args, **kwargs)
            if command == "click" and not self.transition_scheduled:
                self.transition_scheduled = True
                page = self._active_page()
                loop = asyncio.get_running_loop()

                def render_results_before_history_update() -> None:
                    page["title"] = "云南旅游视频_百度搜索"
                    self.snapshot_text = '- link "云南旅游视频 - 视频大全" [ref=e29]'
                    self.security_digest = "results-security"
                    # Client rendering can precede pushState without a
                    # did-start-navigation pending phase. The changed title
                    # epoch plus quiet window must still prevent publication.
                    self.navigation_epoch += 1

                def commit_delayed_pushstate() -> None:
                    page["url"] = "https://example.com/s?wd=云南旅游视频"
                    self.navigation_pending = False
                    self.navigation_epoch += 1

                # Real Chrome/Baidu tracing observed the navigation event about
                # 57 ms after Enter returned; use 60 ms to guard that shape.
                loop.call_later(0.01, render_results_before_history_update)
                loop.call_later(0.06, commit_delayed_pushstate)
            if command == "snapshot":
                page = self._active_page()
                self.snapshot_observations.append(
                    (page["url"], page["title"], self.snapshot_text)
                )
            return result

    driver = DelayedPushStateDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com/")

        output = await manager.click("owner", "session", "p1:e170")

        assert driver.snapshot_observations[-1] == (
            "https://example.com/s?wd=云南旅游视频",
            "云南旅游视频_百度搜索",
            '- link "云南旅游视频 - 视频大全" [ref=e29]',
        )
        assert not any(
            url == "https://example.com/" and title == "云南旅游视频_百度搜索"
            for url, title, _snapshot in driver.snapshot_observations
        )
        assert "https://example.com/s?wd=" in output
        assert "云南旅游视频_百度搜索" in output
    finally:
        await manager.aclose()


@pytest.mark.parametrize("race_stage", ["after_ax_capture", "before_publish"])
async def test_snapshot_rejects_navigation_start_after_quiet_gate(
    browser_env,
    race_stage: str,
):
    class PostGateNavigationDriver(TransitionReviewDriver):
        armed = False

        def begin_navigation(self) -> None:
            # Deliberately leave href/loader/title/security unchanged.  Before
            # the capture-specific transition check this shape passed the
            # security-surface comparison and published a snapshot even though
            # the Host had already reported a main-frame navigation start.
            self.navigation_epoch += 1
            self.navigation_pending = True

        async def execute(
            self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
        ) -> dict:
            result = await super().execute(
                owner_session, profile_dir, command, args, **kwargs
            )
            if self.armed and race_stage == "after_ax_capture" and command == "snapshot":
                self.armed = False
                self.begin_navigation()
            return result

        async def deny_downloads(
            self, owner_session: str, profile_dir: Path, **_kwargs
        ) -> None:
            if self.armed and race_stage == "before_publish":
                self.armed = False
                self.begin_navigation()

    driver = PostGateNavigationDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com/")
        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        if race_stage == "before_publish":
            # Force the publication-stage download fence to execute again so
            # the deterministic driver can inject the second race window.
            owner.downloads_locked = False
        driver.armed = True

        expected = "采集期间" if race_stage == "after_ax_capture" else "发布前"
        with pytest.raises(BrowserDriverError, match=expected):
            await manager.snapshot("owner", "session")

        assert not session.refs
        assert session.page_marker == ""
        assert owner.native_ref_session == ""
        assert owner.native_ref_generation == 0
    finally:
        await manager.aclose()


@pytest.mark.parametrize("failure_mode", ["pending", "url_mismatch", "epoch_churn"])
async def test_snapshot_rejects_a_transition_that_never_becomes_quiet(
    browser_env,
    monkeypatch,
    failure_mode: str,
):
    class ChurningTransitionDriver(TransitionReviewDriver):
        churning = False

        async def page_guard(self, *args, **kwargs) -> str:
            if self.churning:
                self.navigation_epoch += 1
            return await super().page_guard(*args, **kwargs)

    monkeypatch.setattr("crew.browser.manager._PAGE_TRANSITION_QUIET_SECONDS", 0.03)
    monkeypatch.setattr("crew.browser.manager._PAGE_TRANSITION_MAX_SECONDS", 0.09)
    monkeypatch.setattr("crew.browser.manager._PAGE_TRANSITION_POLL_SECONDS", 0.01)
    driver = ChurningTransitionDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com/")
        baseline_snapshots = sum(command == "snapshot" for command, _args in driver.calls)
        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        driver.navigation_pending = failure_mode == "pending"
        driver.location_consistent = failure_mode != "url_mismatch"
        driver.churning = failure_mode == "epoch_churn"

        with pytest.raises(BrowserDriverError, match="过渡状态.*未发布 snapshot"):
            await manager.snapshot("owner", "session")

        assert sum(command == "snapshot" for command, _args in driver.calls) == baseline_snapshots
        assert not session.refs
        assert session.page_marker == ""
        assert owner.native_ref_session == ""
        assert owner.native_ref_generation == 0
    finally:
        await manager.aclose()


async def test_snapshot_tolerates_a_churning_title(browser_env, monkeypatch):
    """标题跳动（倒计时、未读数 (3) 收件箱、播放进度，或恶意 setInterval 改 title）不是
    导航，绝不能让稳定门永远 settle 不了。href/epoch 稳定时 snapshot 必须成功发布 refs。

    回归 H2：titleDigest 曾在 _page_transition_signature 里，一行
    setInterval(()=>document.title=Math.random()) 就能让页面对 agent 永久不可观察。
    """
    class ChurningTitleDriver(TransitionReviewDriver):
        title_tick = 0

        def marker(self) -> str:
            data = json.loads(super().marker())
            # 每次读取换一个 titleDigest；epoch/href/pending/location 全稳定。
            data["titleDigest"] = f"title:churn:{self.title_tick}"
            return json.dumps(data, sort_keys=True)

        async def page_guard(self, *args, **kwargs) -> str:
            self.title_tick += 1
            return await super().page_guard(*args, **kwargs)

    monkeypatch.setattr("crew.browser.manager._PAGE_TRANSITION_QUIET_SECONDS", 0.03)
    monkeypatch.setattr("crew.browser.manager._PAGE_TRANSITION_MAX_SECONDS", 0.3)
    monkeypatch.setattr("crew.browser.manager._PAGE_TRANSITION_POLL_SECONDS", 0.01)
    driver = ChurningTitleDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com/")
        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        # 标题一直在跳，显式 snapshot 仍应成功——不因过渡状态被拒。
        snapshot = await manager.snapshot("owner", "session")
        assert "page_generation: p" in snapshot
        assert session.refs
        assert session.page_marker != ""
    finally:
        await manager.aclose()


async def test_uncertain_mutation_scopes_failure_to_its_own_session(browser_env):
    """一次 mutation 超时（结果未知、浏览器还活着）只作废本会话的观察，
    绝不能把整个账号 fence 掉、清空所有会话的标签页。

    回归：超时曾被标成 stop_unconfirmed（语义是「无法确认浏览器已关闭」），
    于是 _apply_driver_lifecycle_failure 把所有 session 置 paused、清空全部 tab、
    actions_blocked——用户点一次搜索超时，之后每个动作都报「账号浏览器已停止」。
    """

    class TimeoutOnClickDriver(ReviewDriver):
        arm = False

        async def execute(
            self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
        ) -> dict:
            if command == "click" and self.arm:
                # 纯超时：动作已发出、结果未知，但 socket 仍在、浏览器没死。
                raise BrowserDriverError("桌面浏览器操作超时", uncertain=True)
            return await super().execute(owner_session, profile_dir, command, args, **kwargs)

    driver = TimeoutOnClickDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session-a", "https://example.com/a")
        await manager.navigate("owner", "session-b", "https://example.com/b")
        owner = manager._owners["owner"]
        session_b = owner.sessions["session-b"]
        b_tabs_before = dict(session_b.tabs)
        assert b_tabs_before

        # 先让 session-a 重新拿回原生 ref 归属（session-b 的导航会抢走它），
        # 否则 click 会在 _select_checked 就以「旧 ref 已失效」告终，根本走不到驱动。
        await manager.snapshot("owner", "session-a")
        ref = next(iter(owner.sessions["session-a"].refs))

        driver.arm = True
        with pytest.raises(BrowserDriverError, match="超时"):
            await manager.click("owner", "session-a", ref)

        # 本会话：观察作废，模型必须重新 snapshot。
        assert not owner.sessions["session-a"].refs
        # 兄弟会话：完全不受牵连——标签页还在、模式没被改、账号没被 fence。
        assert session_b.tabs == b_tabs_before
        assert session_b.mode == "ai"
        assert owner.actions_blocked is False
        assert owner.stop_unconfirmed is False
    finally:
        await manager.aclose()


async def test_post_action_observation_failure_is_uncertain(browser_env):
    class ObservationFailureDriver(ReviewDriver):
        fail_after_click = False

        async def execute(
            self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
        ) -> dict:
            if command == "snapshot" and self.fail_after_click:
                raise BrowserDriverError("snapshot unavailable")
            return await super().execute(owner_session, profile_dir, command, args, **kwargs)

    driver = ObservationFailureDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.fail_after_click = True
        _approve_click(manager, "p1:e170")
        with pytest.raises(BrowserDriverError, match="结果未知") as captured:
            await manager.click("owner", "session", "p1:e170")
        assert captured.value.uncertain
        assert sum(command == "click" for command, _ in driver.calls) == 1
    finally:
        await manager.aclose()


async def test_stop_does_not_claim_success_when_runtime_close_is_unconfirmed(browser_env):
    class UnstoppableDriver(ReviewDriver):
        async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
            raise BrowserDriverError("interrupt failed")

        async def close(self, owner_session: str, profile_dir: Path) -> bool:
            return False

    manager = BrowserManager(BrowserConfig(), UnstoppableDriver())
    try:
        await manager.navigate("owner", "session", "https://example.com")
        with pytest.raises(BrowserDriverError, match="无法确认 Chromium 已停止"):
            await manager.takeover("owner", "session", "stop")
        state = manager.state("owner", "session")
        assert state["mode"] == "paused"
        assert state["running"] is True
        with pytest.raises(BrowserDriverError, match="停止未能确认"):
            await manager.user_control("owner", "session", "return")
    finally:
        await manager.aclose()


class BlockingCloseDriver(ReviewDriver):
    def __init__(self) -> None:
        super().__init__()
        self.block_close = False
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self, owner_session: str, profile_dir: Path) -> None:
        self.calls.append(("close", (owner_session, str(profile_dir))))
        if self.block_close:
            self.close_started.set()
            await self.release_close.wait()


async def test_idle_retire_is_a_tombstone_until_runtime_profile_close_finishes(browser_env):
    driver = BlockingCloseDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        original = manager._owners["owner"]
        original.last_activity = 0
        original.retiring = True
        driver.block_close = True
        retirement = asyncio.create_task(manager._retire_owner_if_idle(original, time.monotonic()))
        await driver.close_started.wait()

        replacement_task = asyncio.create_task(manager._owner("owner"))
        await asyncio.sleep(0)
        assert not replacement_task.done()

        driver.release_close.set()
        await retirement
        replacement = await replacement_task
        assert replacement is not original
        assert manager._owners["owner"] is replacement
    finally:
        driver.block_close = False
        driver.release_close.set()
        await manager.aclose()


async def test_slow_owner_initialization_does_not_hold_global_owner_lock(browser_env, monkeypatch):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    started = threading.Event()
    release = threading.Event()

    def slow_cleanup(path: Path) -> None:
        if "owner-a" in str(path):
            started.set()
            release.wait(timeout=5)

    monkeypatch.setattr(manager, "_cleanup_expired_artifacts", slow_cleanup)
    owner_a_task = asyncio.create_task(manager._owner("owner-a"))
    try:
        assert await asyncio.to_thread(started.wait, 1)
        owner_b = await asyncio.wait_for(manager._owner("owner-b"), timeout=1)
        assert owner_b.owner == "owner-b"
    finally:
        release.set()
        await owner_a_task
        await manager.aclose()


async def test_failed_owner_initialization_cannot_return_orphan(browser_env, monkeypatch):
    class FlakyProxy(FakeProxy):
        attempts = 0
        first_started = asyncio.Event()
        release_first = asyncio.Event()

        async def start(self) -> None:
            type(self).attempts += 1
            if type(self).attempts == 1:
                type(self).first_started.set()
                await type(self).release_first.wait()
                raise RuntimeError("first init fails")

    monkeypatch.setattr("crew.browser.manager.LoopbackPolicyProxy", FlakyProxy)
    manager = BrowserManager(BrowserConfig(), ReviewDriver())
    first = asyncio.create_task(manager._owner("owner"))
    await FlakyProxy.first_started.wait()
    waiter = asyncio.create_task(manager._owner("owner"))
    FlakyProxy.release_first.set()
    with pytest.raises(RuntimeError, match="first init fails"):
        await first
    replacement = await waiter
    try:
        assert replacement.initialized
        assert not replacement.closing
        assert manager._owners["owner"] is replacement
        assert FlakyProxy.attempts == 2
    finally:
        await manager.aclose()


async def test_cancelled_owner_initialization_cannot_return_orphan(browser_env, monkeypatch):
    class CancelledProxy(FakeProxy):
        attempts = 0
        first_started = asyncio.Event()
        first_closed = asyncio.Event()

        async def start(self) -> None:
            type(self).attempts += 1
            if type(self).attempts == 1:
                type(self).first_started.set()
                await asyncio.Event().wait()

        async def aclose(self) -> None:
            type(self).first_closed.set()

    monkeypatch.setattr("crew.browser.manager.LoopbackPolicyProxy", CancelledProxy)
    manager = BrowserManager(BrowserConfig(), ReviewDriver())
    first = asyncio.create_task(manager._owner("owner"))
    await CancelledProxy.first_started.wait()
    waiter = asyncio.create_task(manager._owner("owner"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    replacement = await waiter
    try:
        assert CancelledProxy.first_closed.is_set()
        assert replacement.initialized and not replacement.closing
        assert manager._owners["owner"] is replacement
        assert CancelledProxy.attempts == 2
    finally:
        await manager.aclose()


async def test_expired_granted_approvals_are_pruned(browser_env):
    manager = BrowserManager(BrowserConfig(), ReviewDriver())
    token = current_tool_call_id.set("approval-prune")
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args = {"ref": "p1:e17"}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            "owner",
            "session",
        )
        key, approval = next(iter(manager._granted_approvals.items()))
        manager._granted_approvals[key] = replace(approval, expires_at=time.monotonic() - 1)

        manager._prune_approvals()
        assert not manager._granted_approvals
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_high_risk_approval_fails_closed_after_security_surface_mutation(browser_env):
    manager = BrowserManager(BrowserConfig(), ReviewDriver())
    token = current_tool_call_id.set("approval-mutation")
    try:
        await manager.navigate("owner", "session", "https://example.com")
        args = {"ref": "p1:e17"}
        decision = manager.permission_for("browser_click", args, "owner", "session")
        assert decision and manager.confirm_approval(
            decision.approval_token,
            "browser_click",
            args,
            "owner",
            "session",
        )
        driver = manager.driver
        assert isinstance(driver, ReviewDriver)
        driver.security_digest = "security-2"

        with pytest.raises(BrowserDriverError, match="页面/目标已在审批后变化"):
            await manager.click("owner", "session", "p1:e17")
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_return_cannot_clear_account_gate_during_stop(browser_env):
    class HoldingInterruptDriver(ReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.interrupt_started = asyncio.Event()
            self.release_interrupt = asyncio.Event()

        async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
            self.interrupt_started.set()
            await self.release_interrupt.wait()
            await self.close(owner_session, profile_dir)

    driver = HoldingInterruptDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        stop_task = asyncio.create_task(manager.takeover("owner", "session", "stop"))
        await driver.interrupt_started.wait()

        with pytest.raises(BrowserDriverError, match="正在停止"):
            await manager.user_control("owner", "session", "return")
        with pytest.raises(BrowserDriverError, match="已停止"):
            await manager.navigate("owner", "new", "https://example.com/new")

        driver.release_interrupt.set()
        await stop_task
        assert manager._owners["owner"].actions_blocked
        assert not manager._owners["owner"].stopping
    finally:
        driver.release_interrupt.set()
        await manager.aclose()


async def test_startup_prepares_driver_in_background(browser_env):
    class PreparingDriver(ReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.prepare_started = asyncio.Event()
            self.release_prepare = asyncio.Event()

        async def prepare(self) -> bool:
            self.prepare_started.set()
            await self.release_prepare.wait()
            return True

    driver = PreparingDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    await manager.startup()
    try:
        await driver.prepare_started.wait()
        assert manager._prepare_task is not None and not manager._prepare_task.done()
    finally:
        driver.release_prepare.set()
        await manager.aclose()


async def test_takeover_clears_debug_buffers_at_both_privacy_boundaries(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        await manager.takeover("owner", "session", "takeover")
        await manager.user_control("owner", "session", "return")

        assert (
            sum(command == "console" and values == ("--clear",) for command, values in driver.calls)
            == 2
        )
        assert (
            sum(
                command == "network" and values == ("requests", "--clear")
                for command, values in driver.calls
            )
            == 2
        )
    finally:
        await manager.aclose()


class DownloadReviewDriver(ReviewDriver):
    def __init__(self) -> None:
        super().__init__()
        self.block_deny = False
        self.deny_started = asyncio.Event()
        self.release_deny = asyncio.Event()
        self.deny_completed = False

    async def execute(
        self, owner_session: str, profile_dir: Path, command: str, args=(), **kwargs
    ) -> dict:
        result = await super().execute(owner_session, profile_dir, command, args, **kwargs)
        if command == "download":
            target = Path(str(args[1]))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"approved download")
        return result

    async def deny_downloads(self, owner_session: str, profile_dir: Path, **_kwargs) -> None:
        if self.block_deny:
            self.deny_started.set()
            await self.release_deny.wait()
            self.deny_completed = True


async def _approve_download(
    manager: BrowserManager,
    *,
    filename: str,
) -> None:
    args = {"ref": "p1:e170", "filename": filename}
    decision = manager.permission_for("browser_download", args, "owner", "session")
    assert decision and manager.confirm_approval(
        decision.approval_token,
        "browser_download",
        args,
        "owner",
        "session",
    )


async def test_download_cancellation_waits_for_deny_restoration(browser_env):
    driver = DownloadReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("download-cancel")
    workdir = browser_env / "workspace"
    workdir.mkdir()
    try:
        await manager.navigate("owner", "session", "https://example.com")
        await _approve_download(manager, filename="report.bin")
        driver.block_deny = True
        task = asyncio.create_task(
            manager.download(
                "owner",
                "session",
                "p1:e170",
                "report.bin",
                workdir=str(workdir),
            )
        )
        await driver.deny_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        driver.release_deny.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert driver.deny_completed
    finally:
        current_tool_call_id.reset(token)
        driver.release_deny.set()
        await manager.aclose()


async def test_download_rejects_preexisting_workspace_symlink(browser_env):
    driver = DownloadReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("download-symlink")
    workdir = browser_env / "workspace"
    outside = browser_env / "outside"
    (workdir / "downloads").mkdir(parents=True)
    outside.mkdir()
    (workdir / "downloads" / "browser").symlink_to(outside, target_is_directory=True)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        await _approve_download(manager, filename="report.bin")
        with pytest.raises(BrowserDriverError, match="符号链接") as captured:
            await manager.download(
                "owner",
                "session",
                "p1:e170",
                "report.bin",
                workdir=str(workdir),
            )
        assert captured.value.uncertain
        assert not (outside / "report.bin").exists()
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_download_tolerates_unrelated_mutation_when_target_is_unchanged(browser_env):
    class MutatingTargetDriver(DownloadReviewDriver):
        async def execute(
            self,
            owner_session: str,
            profile_dir: Path,
            command: str,
            args=(),
            **kwargs,
        ) -> dict:
            result = await super().execute(
                owner_session,
                profile_dir,
                command,
                args,
                **kwargs,
            )
            if command == "get" and tuple(args[:1]) == ("box",):
                self.counter += 1
            return result

    driver = MutatingTargetDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("download-final-marker")
    workdir = browser_env / "workspace"
    workdir.mkdir()
    try:
        await manager.navigate("owner", "session", "https://example.com")
        await _approve_download(manager, filename="report.bin")
        result = await manager.download(
            "owner",
            "session",
            "p1:e170",
            "report.bin",
            workdir=str(workdir),
        )
        assert "report.bin" in result
        assert any(command == "download" for command, _ in driver.calls)
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_download_does_not_restart_after_driver_fail_stop(browser_env):
    class FailStoppedDownloadDriver(DownloadReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.deny_calls = 0

        async def download_bounded(
            self,
            owner_session: str,
            profile_dir: Path,
            native_ref: str,
            target: Path,
            **_kwargs,
        ) -> dict:
            raise BrowserDriverError(
                "download observation lost",
                uncertain=True,
                browser_stopped=True,
            )

        async def deny_downloads(
            self,
            owner_session: str,
            profile_dir: Path,
            **_kwargs,
        ) -> None:
            self.deny_calls += 1

    driver = FailStoppedDownloadDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("download-fail-stop")
    workdir = browser_env / "workspace"
    workdir.mkdir()
    try:
        await manager.navigate("owner", "session", "https://example.com")
        await _approve_download(manager, filename="report.bin")
        deny_before = driver.deny_calls
        with pytest.raises(BrowserDriverError, match="download observation lost") as captured:
            await manager.download(
                "owner",
                "session",
                "p1:e170",
                "report.bin",
                workdir=str(workdir),
            )
        assert captured.value.browser_stopped
        assert driver.deny_calls == deny_before
        state = manager.state("owner", "session")
        assert state["running"] is False
        assert state["mode"] == "paused"
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_cancelled_fail_stopped_download_does_not_recreate_host_for_deny(browser_env):
    class CancelledDownloadDriver(DownloadReviewDriver):
        def __init__(self) -> None:
            super().__init__()
            self.deny_calls = 0

        async def download_bounded(
            self,
            owner_session: str,
            profile_dir: Path,
            native_ref: str,
            target: Path,
            **_kwargs,
        ) -> dict:
            raise BrowserOperationCancelled(
                "download observation lost",
                uncertain=True,
                browser_stopped=True,
            )

        async def deny_downloads(
            self,
            owner_session: str,
            profile_dir: Path,
            **_kwargs,
        ) -> None:
            self.deny_calls += 1

    driver = CancelledDownloadDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("download-cancelled-fail-stop")
    workdir = browser_env / "workspace"
    workdir.mkdir()
    try:
        await manager.navigate("owner", "session", "https://example.com")
        await _approve_download(manager, filename="report.bin")
        deny_before = driver.deny_calls

        with pytest.raises(asyncio.CancelledError):
            await manager.download(
                "owner",
                "session",
                "p1:e170",
                "report.bin",
                workdir=str(workdir),
            )

        assert driver.deny_calls == deny_before
        owner = manager._owners["owner"]
        assert owner.actions_blocked is True
        assert owner.running is False
    finally:
        current_tool_call_id.reset(token)
        await manager.aclose()


async def test_cancelled_remote_failure_updates_lifecycle_and_still_cancels_turn(browser_env):
    class CancelAfterNavigateDriver(ReviewDriver):
        cancel_next = False

        async def execute(
            self,
            owner_session: str,
            profile_dir: Path,
            command: str,
            args=(),
            **kwargs,
        ):
            if self.cancel_next:
                raise BrowserOperationCancelled(
                    "账号浏览器已停止",
                    browser_stopped=True,
                )
            return await super().execute(
                owner_session,
                profile_dir,
                command,
                args,
                **kwargs,
            )

    driver = CancelAfterNavigateDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.cancel_next = True

        with pytest.raises(asyncio.CancelledError):
            await manager.snapshot("owner", "session")

        state = manager.state("owner", "session")
        assert state["running"] is False
        assert state["mode"] == "paused"
        assert manager._owners["owner"].actions_blocked is True
    finally:
        await manager.aclose()


@pytest.mark.parametrize(
    "cancel_method",
    ["deny_downloads", "page_guard", "close_target", "set_mode", "coordinate_click"],
)
async def test_direct_driver_mutation_cancellation_applies_fail_stop_lifecycle(
    browser_env,
    cancel_method: str,
):
    class DirectCancelDriver(ReviewDriver):
        cancel_method = ""

        @staticmethod
        def cancelled() -> BrowserOperationCancelled:
            return BrowserOperationCancelled(
                "Host mutation state unknown",
                uncertain=True,
                stop_unconfirmed=True,
            )

        async def deny_downloads(self, *_args, **_kwargs) -> None:
            if self.cancel_method == "deny_downloads":
                raise self.cancelled()

        async def page_guard(self, *_args, **_kwargs) -> str | None:
            if self.cancel_method == "page_guard":
                raise self.cancelled()
            return None

        async def close_target(self, *args, target_id: str, **kwargs) -> None:
            if self.cancel_method == "close_target":
                raise self.cancelled()
            await super().close_target(*args, target_id=target_id, **kwargs)

        async def set_mode(self, *_args, **_kwargs) -> None:
            if self.cancel_method == "set_mode":
                raise self.cancelled()

        async def coordinate_click_atomic(self, *_args, **_kwargs):
            if self.cancel_method == "coordinate_click":
                raise self.cancelled()
            return None

    driver = DirectCancelDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    token = current_tool_call_id.set("direct-cancel")
    try:
        await manager.navigate("owner", "session", "https://example.com")
        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        driver.cancel_method = cancel_method

        with pytest.raises(asyncio.CancelledError):
            if cancel_method == "deny_downloads":
                owner.downloads_locked = False
                await manager._lock_downloads(owner)
            elif cancel_method == "page_guard":
                await manager._page_guard(owner, session, reset=True, workdir="")
            elif cancel_method == "close_target":
                await manager._close_tab_target(owner, session, manager._active_tab(session))
            elif cancel_method == "set_mode":
                await manager._set_driver_mode(owner, session, "paused")
            else:
                session.screenshot_id = "shot"
                session.screenshot_generation = session.generation
                session.screenshot_coordinates_allowed = True
                session.screenshot_marker = session.page_marker
                session.viewport_width = 100
                session.viewport_height = 50
                session.screenshot_css_width = 100
                session.screenshot_css_height = 50
                session.screenshot_host_epoch = "a" * 32
                args = {"screenshot_id": "shot", "x": 1, "y": 1}
                decision = manager.permission_for(
                    "browser_click", args, "owner", "session"
                )
                assert decision and manager.confirm_approval(
                    decision.approval_token,
                    "browser_click",
                    args,
                    "owner",
                    "session",
                )
                await manager.coordinate_click(
                    "owner", "session", "shot", 1, 1
                )

        assert owner.actions_blocked is True
        assert owner.stop_unconfirmed is True
        assert owner.running is True
        assert manager.state("owner", "session")["mode"] == "paused"
    finally:
        current_tool_call_id.reset(token)
        driver.cancel_method = ""
        await manager.aclose()


async def test_emergency_stop_preserves_cancelled_interrupt_lifecycle(browser_env):
    class CancelInterruptDriver(ReviewDriver):
        cancel_interrupt = False

        async def interrupt(self, owner_session: str, profile_dir: Path) -> None:
            if self.cancel_interrupt:
                raise BrowserOperationCancelled(
                    "Host close state unknown",
                    uncertain=True,
                    stop_unconfirmed=True,
                )
            await super().interrupt(owner_session, profile_dir)

    driver = CancelInterruptDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        driver.cancel_interrupt = True

        with pytest.raises(asyncio.CancelledError):
            await manager.takeover("owner", "session", "stop")

        owner = manager._owners["owner"]
        assert owner.actions_blocked is True
        assert owner.stop_unconfirmed is True
        assert owner.running is True
        assert owner.stopping is False
        assert manager.state("owner", "session")["mode"] == "paused"
    finally:
        driver.cancel_interrupt = False
        await manager.aclose()


async def test_deterministic_first_navigation_failure_rolls_back_placeholder(browser_env):
    class FailsOnceDriver(ReviewDriver):
        failed = False

        async def execute(
            self,
            owner_session: str,
            profile_dir: Path,
            command: str,
            args=(),
            **kwargs,
        ) -> dict:
            if command == "tab" and tuple(args[:1]) == ("new",) and not self.failed:
                self.failed = True
                raise BrowserDriverError("Host 尚未连接")
            return await super().execute(
                owner_session,
                profile_dir,
                command,
                args,
                **kwargs,
            )

    driver = FailsOnceDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        with pytest.raises(BrowserDriverError, match="尚未连接"):
            await manager.navigate("owner", "session", "https://example.com/first")

        owner = manager._owners["owner"]
        session = owner.sessions["session"]
        assert session.tabs == {}
        assert session.active_label == ""
        assert owner.actions_blocked is False

        output = await manager.navigate("owner", "session", "https://example.com/retry")
        assert "p1:e170" in output
        assert len(session.tabs) == 1
    finally:
        await manager.aclose()


async def test_stop_return_without_tabs_allows_clean_restart(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com/first")
        await manager.takeover("owner", "session", "stop")
        assert manager.state("owner", "session")["tabs"] == []

        returned = await manager.user_control("owner", "session", "return")
        assert '"mode": "ai"' in returned
        assert manager._owners["owner"].actions_blocked is False

        output = await manager.navigate("owner", "session", "https://example.com/restarted")
        assert "p" in output and ":e170" in output
        assert manager.state("owner", "session")["running"] is True
    finally:
        await manager.aclose()


async def test_clear_owner_data_orders_session_clear_before_close_and_artifacts(browser_env):
    class ClearDriver(ReviewDriver):
        lifecycle: list[str]

        def __init__(self) -> None:
            super().__init__()
            self.lifecycle = []

        async def clear_owner_data(self, *_args, **_kwargs) -> bool:
            self.lifecycle.append("clear")
            return True

        async def close(self, owner_session: str, profile_dir: Path) -> bool:
            self.lifecycle.append("close")
            await super().close(owner_session, profile_dir)
            return True

    driver = ClearDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        browser_root = browser_env / "accounts" / "owner" / "browser"
        profile = browser_root / "profile"
        artifacts = browser_root / "artifacts"
        profile.mkdir(parents=True, exist_ok=True)
        artifacts.mkdir(parents=True, exist_ok=True)
        (profile / "Preferences").write_text("session-owned", encoding="utf-8")
        (artifacts / "old.png").write_bytes(b"old")

        result = await manager.clear_owner_data("owner")

        assert result["cleared"] is True
        assert driver.lifecycle[:2] == ["clear", "close"]
        assert (profile / "Preferences").is_file()
        assert not artifacts.exists()
        assert "owner" not in manager._owners
    finally:
        await manager.aclose()


async def test_cold_clear_starts_mandatory_temporary_policy_proxy(browser_env):
    class ColdClearDriver(ReviewDriver):
        proxy_urls: list[str]

        def __init__(self) -> None:
            super().__init__()
            self.proxy_urls = []

        async def clear_owner_data(self, *_args, proxy_url: str = "", **_kwargs) -> bool:
            self.proxy_urls.append(proxy_url)
            return True

        async def close(self, owner_session: str, profile_dir: Path) -> bool:
            await super().close(owner_session, profile_dir)
            return True

    driver = ColdClearDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        result = await manager.clear_owner_data("never-started-owner")

        assert result["cleared"] is True
        assert driver.proxy_urls == ["http://127.0.0.1:45678"]
        assert "never-started-owner" not in manager._owners
    finally:
        await manager.aclose()


async def test_host_registration_reset_clears_owner_epoch_and_proxy(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    try:
        await manager.navigate("owner", "session", "https://example.com")
        owner = manager._owners["owner"]
        proxy = owner.proxy
        decision = manager.permission_for(
            "browser_click",
            {"ref": "p1:e17"},
            "owner",
            "session",
        )
        assert decision is not None
        assert manager._pending_approvals

        await manager.reset_host_registration("owner")

        assert "owner" not in manager._owners
        assert proxy is not None and proxy.closed is True
        assert not any(value.owner == "owner" for value in manager._pending_approvals.values())
        assert not any(value.owner == "owner" for value in manager._granted_approvals.values())
    finally:
        await manager.aclose()


async def test_host_debug_event_requires_exact_non_human_target_and_is_redacted(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    stream = None
    try:
        await manager.navigate("owner", "session", "https://example.com")
        session = manager._owners["owner"].sessions["session"]
        target_id = session.tabs[session.active_label].target_id
        stream = manager.subscribe("owner", "session")
        await anext(stream)

        assert await manager.publish_host_debug(
            "owner",
            "session",
            target_id,
            "console",
            {"text": "token=top-secret", "token": "raw-secret"},
        )
        event = await anext(stream)
        assert event["type"] == "debug"
        assert "top-secret" not in json.dumps(event)
        assert "raw-secret" not in json.dumps(event)

        session.mode = "human"
        assert not await manager.publish_host_debug(
            "owner",
            "session",
            target_id,
            "console",
            {"text": "typed-password"},
        )
        assert manager.session_for_target("owner", "foreign-target") is None
    finally:
        if stream is not None:
            await stream.aclose()
        await manager.aclose()


async def test_idle_retire_publishes_confirmed_paused_empty_state(browser_env):
    driver = ReviewDriver()
    manager = BrowserManager(BrowserConfig(), driver)
    stream = None
    try:
        await manager.navigate("owner", "session", "https://example.com")
        owner = manager._owners["owner"]
        stream = manager.subscribe("owner", "session")
        await anext(stream)

        owner.retiring = True
        owner.last_activity = 0
        await manager._retire_owner_if_idle(owner, time.monotonic())

        clear = await anext(stream)
        state = await anext(stream)
        assert clear["type"] == "debug_clear"
        assert state["state"]["mode"] == "paused"
        assert state["state"]["tabs"] == []
        assert state["state"]["running"] is False
    finally:
        if stream is not None:
            await stream.aclose()
        await manager.aclose()
