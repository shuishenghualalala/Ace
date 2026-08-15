"""Public browser configuration and state types."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


BrowserControlMode = Literal["ai", "human", "paused"]


@dataclass
class BrowserConfig:
    # ── 已移除的配置项（不要再加回来，除非同时接上执行路径） ────────────
    #
    # `allowed_private_cidrs` / `allow_file_urls` 曾经存在但没有执行路径，已移除。
    # `allowed_private_hosts` 只在强制 pinning proxy 中消费：精确主机匹配后，
    # proxy 为单次连接签发 owner/request 绑定的短期 grant。它不是 CIDR 通配，
    # metadata 地址即使列出也仍会被统一 outbound policy 拒绝。
    #
    # `max_tabs_per_session` 同样移除：标签页护栏在 Electron 宿主里
    # （browser-host.ts 的 MAX_TABS_PER_SESSION），而 Python 配置**到不了宿主**
    # ——没有这条通道。真正的保护在宿主那边；在这里留一个到不了执行点的数字，
    # 就是又一个骗人的旋钮。
    #
    # 一个不生效的安全开关比没有这个开关更危险：它让人停止追问。所以这里
    # 选择删掉而不是留着，删掉之后旧配置里的这些键会被 `from_raw` 直接忽略，
    # 与现状（不生效）行为一致，但不再宣称任何能力。
    enabled: bool = True
    runtime: str = "electron"
    display: str = "in_app"
    headed: bool = False
    idle_timeout_seconds: int = 600
    command_timeout_seconds: int = 30
    navigation_timeout_seconds: int = 60
    # 单次输出的期望规模。**不是硬截断**：真实内网页面的完整快照经常超过它，
    # 按它截断会让模型基于半张页面下结论。只有远超它（见 manager 的护栏倍数）
    # 才按行截断并如实报出。
    max_output_chars: int = 30_000
    # 单次传输的失控护栏（下载/上传/网络体读取）。
    max_transfer_bytes: int = 104_857_600
    artifact_ttl_hours: int = 24
    blocked_hosts: list[str] = field(default_factory=list)
    allowed_private_hosts: list[str] = field(default_factory=list)

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
            "max_output_chars",
            "max_transfer_bytes",
            "artifact_ttl_hours",
        ):
            if name in values:
                values[name] = max(0, int(values[name]))
        for name in ("blocked_hosts", "allowed_private_hosts"):
            if name in values:
                value = values[name]
                values[name] = [str(item).strip() for item in value if str(item).strip()] if isinstance(value, list) else []
        for name in ("enabled", "headed"):
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
        if (
            not page[1:].isdigit()
            or len(native) < 2
            or native[0] not in {"e", "s"}
            or not native[1:].isdigit()
        ):
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


# ── 录制工作流的能力词表（唯一权威） ─────────────────────────────────────
#
# 这份顺序是**四方共享的契约**：编译器产出 plan、workflow_store 校验并签名
# artifact、replay_tool 要求 capabilities 精确等于 plan、validate_generated_skill
# 校验 SKILL.md 里的声明顺序。
#
# 放在 crew/browser 而不是 plugins/browser：按仓库既定原则，Crew 定规范、
# 插件适配 Crew。让 crew/agent/skills.py 去 import plugins/ 是层次倒置。
#
# **不要在任何地方维护副本。** 曾经 skills.py 抄了一份，然后新增
# assert_state / handle_overlay 时只改了权威表——后果是任何含状态断言或遮挡
# 处理的工作流在安装时 100% 校验失败，而两侧的测试恰好都没用到这两项，
# 全绿也暴露不了。顺序漂移这种 bug 只能靠"只有一份"来根治。
WORKFLOW_CAPABILITY_ORDER_V2: tuple[str, ...] = (
    "navigate",
    "dialog",
    "click",
    "dblclick",
    "drag",
    "press",
    "fill_form",
    "fill",
    "select",
    "check",
    "upload",
    "scroll",
    "assert_state",
    "handle_overlay",
    "snapshot_full",
    "takeover",
)

WORKFLOW_CAPABILITY_ORDER_V3: tuple[str, ...] = (
    "open_page",
    "close_page",
    "navigate",
    "activate_page",
    "resize",
    "hover",
    "click",
    "dblclick",
    "drag",
    "drop",
    "pointer_gesture",
    "press",
    "fill",
    "select",
    "check",
    "upload",
    "scroll",
    "wait_page",
    "wait_navigation",
    "wait_page_closed",
    "wait_download",
    "wait_dialog",
    "assert_state",
    "handle_overlay",
    "snapshot_full",
    "takeover",
    # 原子 effect 也是可执行行为，不是建议性元数据。
    "popup",
    "navigation_effect",
    "download",
    "dialog",
    "page_closed",
)
