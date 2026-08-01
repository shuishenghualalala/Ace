"""Crew first-party browser tools backed by Electron's bundled Chromium.

The gateway owns policy, approvals and model-facing refs.  The authenticated
Electron main process owns sandboxed WebContentsView instances and never exposes
its debugger handle to the trusted UI renderer or to model tools.
"""

from crew.browser.driver import BrowserDriver
from crew.browser.electron_driver import ElectronBrowserDriver
from crew.browser.manager import BrowserManager
from crew.browser.types import BrowserConfig, BrowserPageState, BrowserRef

__all__ = [
    "ElectronBrowserDriver",
    "BrowserConfig",
    "BrowserDriver",
    "BrowserManager",
    "BrowserPageState",
    "BrowserRef",
]
