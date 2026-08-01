"""Public browser configuration and state types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


BrowserControlMode = Literal["ai", "human", "paused"]


@dataclass
class BrowserConfig:
    enabled: bool = True
    runtime: str = "electron"
    display: str = "in_app"
    headed: bool = False
    idle_timeout_seconds: int = 600
    command_timeout_seconds: int = 30
    navigation_timeout_seconds: int = 60
    max_tabs_per_session: int = 8
    max_output_chars: int = 30_000
    max_transfer_bytes: int = 104_857_600
    artifact_ttl_hours: int = 24
    allowed_private_hosts: list[str] = field(default_factory=list)
    allowed_private_cidrs: list[str] = field(default_factory=list)
    blocked_hosts: list[str] = field(default_factory=list)
    allow_file_urls: bool = False

    @classmethod
    def from_raw(cls, raw: Any) -> "BrowserConfig":
        if not isinstance(raw, dict):
            return cls()
        defaults = cls()
        values: dict[str, Any] = {}
        for name in defaults.__dataclass_fields__:
            if name in raw:
                values[name] = raw[name]
        for name in (
            "idle_timeout_seconds",
            "command_timeout_seconds",
            "navigation_timeout_seconds",
            "max_tabs_per_session",
            "max_output_chars",
            "max_transfer_bytes",
            "artifact_ttl_hours",
        ):
            if name in values:
                values[name] = max(0, int(values[name]))
        for name in ("allowed_private_hosts", "allowed_private_cidrs", "blocked_hosts"):
            if name in values:
                value = values[name]
                values[name] = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        for name in ("enabled", "headed", "allow_file_urls"):
            if name in values:
                values[name] = bool(values[name])
        return cls(**values)

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BrowserRef:
    generation: int
    native_ref: str

    @classmethod
    def parse(cls, value: str) -> "BrowserRef":
        raw = str(value or "").strip()
        if not raw.startswith("p") or ":" not in raw:
            raise ValueError("元素 ref 必须来自最近一次 browser snapshot，例如 p42:e17")
        page, native = raw.split(":", 1)
        if not page[1:].isdigit() or not native.startswith("e") or not native[1:].isdigit():
            raise ValueError("无效的 browser ref")
        return cls(generation=int(page[1:]), native_ref=f"@{native}")

    def __str__(self) -> str:
        return f"p{self.generation}:{self.native_ref.removeprefix('@')}"


@dataclass
class BrowserPageState:
    owner_hash: str
    session_hash: str
    tab_id: str = ""
    tab_label: str = ""
    url: str = ""
    title: str = ""
    generation: int = 0
    mode: BrowserControlMode = "ai"
    running: bool = False
    last_action: str = ""
    last_error: str = ""
    screenshot_id: str = ""
    viewport_width: int = 0
    viewport_height: int = 0
    can_go_back: bool = False
    can_go_forward: bool = False
    tabs: list[dict[str, str]] = field(default_factory=list)
    console_count: int = 0
    network_count: int = 0
    downloads: list[dict[str, Any]] = field(default_factory=list)

    def public_dict(self) -> dict[str, Any]:
        return asdict(self)
