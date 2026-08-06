"""Crew first-party browser tools backed by Electron's bundled Chromium.

The gateway owns owner/session lifecycle and model-facing refs. The Electron
main process owns WebContentsView instances and exposes their public Playwright
Page through the versioned browser protocol.
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
