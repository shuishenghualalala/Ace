"""Account-scoped browser lifecycle, tab isolation and Crew ref protocol."""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import inspect
import json
import math
import os
import re
import shutil
import stat
import struct
import time
import uuid
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

from crew.browser.driver import BrowserDriver, BrowserDriverError, BrowserOperationCancelled
from crew.browser.electron_driver import ElectronBrowserDriver
from crew.browser.security import BrowserNetworkPolicy, LoopbackPolicyProxy, path_is_within
from crew.browser.tab_reading import PAGE_TEXT_LIMIT, PAGE_TEXT_SCRIPT, parse_page_text_result
from crew.browser.types import BATCH_STEP_TOOLS, BrowserConfig, BrowserPageState, BrowserRef
from crew.core.observability import span
from crew.core.types import MediaPart, ToolOutput, ToolPermissionDecision
from crew.state.home import get_owner_runtime_home
from crew.state.logging import get_logger

log = get_logger("browser.manager")


def _observed_browser(operation: str):
    """Record browser operations while keeping the driver boundary black-box safe."""
    def decorate(function):
        @functools.wraps(function)
        async def wrapped(self, *args, **kwargs):
            owner = kwargs.get("owner_id") or (args[0] if args else "")
            with span(
                "browser.operation",
                kind="client",
                module="browser",
                component="manager",
                operation=operation,
                attributes={
                    "owner_scope": str(owner or ""),
                    "coverage_status": "black_box",
                    "driver_boundary": "external",
                },
            ):
                return await function(self, *args, **kwargs)
        return wrapped
    return decorate

# Snapshot 的原生 ref 只在 ariaSnapshot 节点键里有结构意义。Accessible name
# 也可能包含同形的 ``[ref=e17]`` 文本，因此必须按行解析结构位置，不能全局替换。
_SNAPSHOT_REF_TOKEN = re.compile(r"(?<!\S)\[ref=(@?e[1-9]\d*)\](?=$|\s)")
# 截断硬切兜底用：尾部一段还没闭合的 [ref=pN:eM 片段。留着它等于把一个残缺却仍然
# 合法的 ref（p42:e17 被切成 p42:e1）交给模型，会造成静默误点击。
_REF_TAIL_PATTERN = re.compile(r"\[ref=p?\d*:?e?\d*$")
_INVALID_KEY_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
# Product-level action caps used to reject otherwise valid Playwright inputs
# before Chromium saw them. The download RPC is encoded as a signed 32-bit byte
# budget, so configured limits are clamped to this transport ceiling.
_WIRE_MAX_TRANSFER_BYTES = 2_147_483_647
_CLICK_BUTTONS = frozenset({"left", "right", "middle"})
_CLICK_MODIFIERS = frozenset(
    {"Alt", "Control", "ControlOrMeta", "Meta", "Shift"}
)
_DEBUG_SECRET_ASSIGNMENT = re.compile(
    r"\b(access[_-]?token|api[_-]?key|auth(?:orization)?|client[_-]?secret|cookie|"
    r"credential|id[_-]?token|password|refresh[_-]?token|secret|session|signature|token)"
    r"(\s*[=:]\s*)([\"']?)[^\s,;&\"']+\3",
    re.IGNORECASE,
)
_DEBUG_SECRET_KEY = re.compile(
    r"^(?:access[_-]?token|api[_-]?key|auth(?:orization)?|client[_-]?secret|cookie|"
    r"credential|id[_-]?token|password|private[_-]?key|refresh[_-]?token|secret|"
    r"session|signature|token)$",
    re.IGNORECASE,
)

# Optional diagnostic/test override.  Production has no hidden 30-second cap;
# the configured navigation timeout is authoritative.
_PAGE_TRANSITION_MAX_SECONDS: float | None = None
_PAGE_TRANSITION_POLL_SECONDS = 0.04

# browser_use 在开始执行时取得能力代次，并把它作为 task-local lease 带到
# BrowserManager。这样 revoke 即使发生在 handler 的入口校验之后、owner 冷启动
# 之前，_owner/_run 仍会在真正创建实例或发送 RPC 前拒绝旧动作。直接调用
# BrowserManager 的兼容代码不设置 lease，保持原 API 兼容。
_EXPECTED_CAPABILITY: ContextVar[tuple[str, int] | None] = ContextVar(
    "browser_expected_capability", default=None
)
_POST_OBSERVATION_DEFERRED: ContextVar[bool] = ContextVar(
    "browser_post_observation_deferred", default=False
)


def _hash(value: str, size: int = 12) -> str:
    return hashlib.sha256(str(value or "anonymous").encode("utf-8")).hexdigest()[:size]


def _data(result: dict[str, Any]) -> Any:
    value = result.get("data", result)
    return value


def _text(result: dict[str, Any]) -> str:
    value = _data(result)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("snapshot", "text", "result", "value", "output", "url", "title"):
            item = value.get(key)
            if isinstance(item, str):
                return item
    return json.dumps(value, ensure_ascii=False)


def _escape_tag_markers(text: str) -> str:
    """只转义能构成标签的 < >，保留 & 原样。

    专供 url 使用：snapshot 正文里没有 URL，头部这一个是模型全程唯一能看到并复用的
    地址（翻页、二次导航都靠它）。走全量转义会把 query 分隔符 & 变成 &amp;，模型照抄
    就得到 amp;pn 这种参数。伪造包装标签必须有 < >，把这两个挡住即可，& 无害。
    """
    return text.replace("<", "&lt;").replace(">", "&gt;")


# 不可信内容的包装标签。**每一条送给模型的页面派生文本都必须落在其中一个里面。**
#
# 分标签而不是共用一个，是为了让模型能区分证据的来源：快照是页面结构，
# 控制台是页面自己打的日志，网络是线上真实报文。三者的可信度一样低，
# 但排障时的含义完全不同。
_UNTRUSTED_TAGS = {
    "console": "untrusted_browser_console",
    "network": "untrusted_browser_network",
    "content": "untrusted_browser_content",
}


def _escape_wrapper_markers(text: str) -> str:
    """转义会伪造 Crew 固定包装标签的字符。

    所有写进 <untrusted_browser_*> 边界的页面派生文本——正文、title、url——都必须先
    过这里。漏掉任何一处，页面就能用字面 </untrusted_browser_content> 逃出隔离区，
    并伪造 tool.py 的 <browser_action_result> 信封谎报动作成功。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# 输出的**失控护栏**倍数。
#
# `max_output_chars`（默认 30k）现在是"期望规模"而不是硬截断：真实内网页面的
# 完整快照经常超过它，按它截断会让模型只看到半张页面然后基于半张页面下结论
# ——那比输出长更伤成功率。所以正常超出不截。
#
# 但完全不设上限也不行：一个几十 MB 的页面会把整个上下文窗口吃光，模型连
# 自己的指令都读不到。护栏定在期望值的 20 倍，正常页面永远碰不到。
_OUTPUT_GUARD_MULTIPLIER = 20
# 护栏的绝对下限：不论调用方传什么，低于这个量都不截断。
_OUTPUT_GUARD_FLOOR = 600_000
# `snapshot(full=true)` is an explicit request for a larger observation. Keep
# an absolute ceiling so a pathological document still cannot consume the
# whole model context.
_FULL_SNAPSHOT_GUARD = 4_000_000

# 批量模式（browser_use batch）中间步骤跳过后置观察时的占位结果文本。
# 批量调用方只用它拼每步一行的简报；最终观察由末步或显式 snapshot 提供。
DEFERRED_OBSERVATION_NOTE = "已执行（批量中间步骤，跳过中间观察）"
DEFERRED_SINGLE_OBSERVATION_NOTE = "已执行（按请求跳过后置观察；如需页面状态请调用 snapshot）"


def _truncate_snapshot_at_line(
    text: str,
    limit: int,
    *,
    full: bool = False,
) -> tuple[str, str]:
    """只在远超期望规模时按行截断，并如实报出截断原因。

    截断必须**按行**：快照是一行一个元素，从中间切断会产出一个残缺的
    `- button "确..." [ref=` 行，模型可能把它当成一个真实可点的元素。
    """
    # 护栏取**绝对下限**，不能被调用方传进来的小值拖下来。
    #
    # 旧的 `limit` 是"期望规模"，历史调用点会传各种值（甚至 0）。按 limit × 倍数
    # 直接算，limit=0 时护栏退化成 0，正常快照全被截断——实测踩过。
    guard = (
        _FULL_SNAPSHOT_GUARD
        if full
        else max(_OUTPUT_GUARD_FLOOR, max(0, int(limit)) * _OUTPUT_GUARD_MULTIPLIER)
    )
    if len(text) <= guard:
        return text, ""
    cut = text.rfind("\n", 0, guard)
    if cut <= 0:
        cut = guard
    return (
        text[:cut],
        f"输出超过 {guard} 字符护栏，已按行截断；这一页的内容没有看全",
    )


def _bounded(value: Any, *, kind: str = "content", limit: int = 30_000) -> str:
    """把页面派生文本放进一个页面无法伪造的边界里。

    ## 只转义闭合标记本身，不做全量转义

    早先是把正文里的 `& < >` 全部转义再套标签。边界牢固，但**代价落在正文上**：
    JSON 响应体里的 `<` 变成 `&lt;`、query 里的 `&` 变成 `&amp;`，模型读到的是
    一份被改花的文档，照抄出来的 URL 带着 `amp;`。这直接伤成功率。

    实际需要挡住的只有一件事：正文里出现字面的结束标记，把后续内容顶到边界外面
    去冒充 Crew 自己的信封。那就**只转义那个字面串**——不含它的正文（绝大多数
    情况）一个字节都不动。

    三个标签名全部转义，不只当前这一个：在 `content` 里塞
    `</untrusted_browser_console>` 同样能制造歧义。
    """
    del limit
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    for tag_name in _UNTRUSTED_TAGS.values():
        text = text.replace(f"</{tag_name}>", f"&lt;/{tag_name}&gt;")
    tag = _UNTRUSTED_TAGS.get(kind, "untrusted_browser_content")
    return f"<{tag}>\n{text}\n</{tag}>"


def _public_url(value: str) -> str:
    return str(value or "")


def _safe_public_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _safe_public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_public_value(item) for item in value]
    if isinstance(value, tuple):
        return [_safe_public_value(item) for item in value]
    return value


@dataclass
class _Tab:
    id: str
    label: str
    native_id: str = ""
    target_id: str = ""
    opener_target_id: str = ""
    popup_ordinal: int = 0
    native_labeled: bool = True
    url: str = ""
    title: str = ""
    guard_property: str = field(default_factory=lambda: f"__crew_guard_{uuid.uuid4().hex}")
    guard_token: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class _Session:
    session_id: str
    owner: str
    tabs: dict[str, _Tab] = field(default_factory=dict)
    active_label: str = ""
    counter: int = 0
    generation: int = 0
    refs: dict[str, str] = field(default_factory=dict)
    # 显式旧技能的只读能力档。
    # native ref -> 动作类别（目前只有 "submit"）。由宿主显式下发，不从渲染
    # 文本解析。现在是**诊断信息**：不再据此拒绝任何点击，但知道"这个 ref 是
    # 提交控件"对失败归因很有用（点完就跳走 vs 点了没反应）。
    ref_actions: dict[str, str] = field(default_factory=dict)
    # 连续失败计数与最后一次成功的动作，仅作为诊断证据。它不会触发固定次数
    # 熔断：恢复策略由调用方结合稳定错误码和当前页面状态决定。
    consecutive_failures: int = 0
    last_success: str = ""
    mode: str = "ai"
    last_action: str = ""
    last_error: str = ""
    # 批量执行（browser_use batch）的中间步骤置 True：跳过后置 snapshot。
    # 每次 snapshot 都会换 generation、重铸 ref——中间观察会让后续预规划步骤的
    # ref 全部失效，所以批量模式只在末步观察一次。由 set_observation_deferred 维护。
    defer_post_observation: bool = False
    screenshot_id: str = ""
    screenshot_host_epoch: str = ""
    screenshot_generation: int = 0
    screenshot_path: str = ""
    screenshot_marker: str = ""
    screenshot_css_width: float = 0
    screenshot_css_height: float = 0
    screenshot_dpr: float = 1
    screenshot_coordinates_allowed: bool = False
    viewport_width: int = 0
    viewport_height: int = 0
    can_go_back: bool = False
    can_go_forward: bool = False
    page_marker: str = ""
    downloads: list[dict[str, Any]] = field(default_factory=list)


class _OwnerOperationLock(asyncio.Lock):
    """Owner lock with bounded queue waiting and low-cost runtime metrics."""

    def __init__(self) -> None:
        super().__init__()
        self.queue_timeout_seconds = 30.0
        self.queue_depth = 0
        self.last_queue_wait_ms = 0.0
        self.last_operation_ms = 0.0
        self.queue_timeouts = 0
        self._acquired_at = 0.0

    async def acquire(self) -> bool:
        queued = self.locked()
        started = time.monotonic()
        if queued:
            self.queue_depth += 1
        try:
            timeout = float(self.queue_timeout_seconds or 0)
            if timeout > 0:
                acquired = await asyncio.wait_for(super().acquire(), timeout)
            else:
                acquired = await super().acquire()
        except asyncio.TimeoutError:
            if queued:
                self.queue_depth = max(0, self.queue_depth - 1)
            self.queue_timeouts += 1
            raise BrowserDriverError(
                f"浏览器动作排队超过 {timeout:g} 秒，请稍后重试",
                code="browser_queue_timeout",
            ) from None
        except BaseException:
            if queued:
                self.queue_depth = max(0, self.queue_depth - 1)
            raise
        if queued:
            self.queue_depth = max(0, self.queue_depth - 1)
        self.last_queue_wait_ms = max(0.0, (time.monotonic() - started) * 1000)
        self._acquired_at = time.monotonic()
        return acquired

    def release(self) -> None:
        if self._acquired_at:
            self.last_operation_ms = max(
                0.0, (time.monotonic() - self._acquired_at) * 1000
            )
            self._acquired_at = 0.0
        super().release()


@dataclass
class _Owner:
    owner: str
    runtime_key: str
    profile_dir: Path
    lock: "_OwnerOperationLock" = field(default_factory=lambda: _OwnerOperationLock())
    control_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    stop_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    sessions: dict[str, _Session] = field(default_factory=dict)
    proxy: LoopbackPolicyProxy | None = None
    last_activity: float = field(default_factory=time.monotonic)
    running: bool = False
    selected_label: str = ""
    native_ref_session: str = ""
    native_ref_generation: int = 0
    initialized: bool = False
    retiring: bool = False
    closing: bool = False
    stopping: bool = False
    stop_unconfirmed: bool = False
    actions_blocked: bool = False
    downloads_locked: bool = False
    closed_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class _ApprovalGrant:
    """一次 ask 审批签发的待确认授权（一次性、限时有效）。

    digest 绑定「工具名 + 参数」，generation/ref 绑定审批那一刻的页面观察；
    任一漂移都说明审批到的已经不是要执行的东西，授权作废（防 TOCTOU）。
    """

    owner: str
    session_id: str
    digest: str
    generation: int
    ref: str
    expires_at: float


# 一次性审批令牌有效期：审批卡挂着期间页面随时可能变，超过即要求重新观察。
_APPROVAL_TOKEN_TTL_SECONDS = 120.0

# 写交互动作：confirm_writes 档升级为 ask，read_only 档直接 deny。
# keydown/keyup/mouse_* 是构不成完整语义的低层原语，不在此列（Enter 按下
# 由敏感分类单独兜住）。
_GOVERNANCE_WRITES = frozenset(
    {
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_select",
        "browser_check",
        "browser_drag",
        "browser_fill_form",
        "browser_drop",
    }
)

# 页面内执行代码：ask 时禁止「本次对话允许」复用，每次都必须单独确认。
_GOVERNANCE_NO_ALLOW_ALWAYS = frozenset({"browser_evaluate", "browser_run_code_unsafe"})


class BrowserManager:
    def __init__(self, config: BrowserConfig, driver: BrowserDriver | None = None) -> None:
        self.config = config
        self.driver = driver or ElectronBrowserDriver(config)
        self.policy = BrowserNetworkPolicy(config)
        self._owners: dict[str, _Owner] = {}
        # (owner, session_id) -> 已登记的只读租约。独立于 _Session 存在，
        # 因为策略要在 session 被创建之前就能登记。
        self._owners_lock = asyncio.Lock()
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = {}
        self._idle_task: asyncio.Task | None = None
        self._prepare_task: asyncio.Task | None = None
        # 能力代次：用户关闭/重开 Browser 能力时单调递增。旧代次的
        # ref、截图与标签页句柄一律不可复用（见 revoke_owner）。
        self._capability_generations: dict[str, int] = {}
        # 一次性审批令牌：token -> 授权记录。发 ask 时签发，confirm_approval
        # 弹出并校验页面代次/ref 未变；插入时惰性清理过期项，表不会无界增长。
        self._approval_tokens: dict[str, _ApprovalGrant] = {}
        self._closed = False

    def available(self) -> bool:
        return bool(self.config.enabled and self.driver.available())

    def _configure_owner_lock(self, owner: _Owner) -> None:
        owner.lock.queue_timeout_seconds = max(
            0.0,
            float(getattr(self.config, "queue_timeout_seconds", 30.0) or 0.0),
        )

    def _transfer_limit_bytes(self) -> int:
        configured = int(getattr(self.config, "max_transfer_bytes", 0) or 0)
        if configured <= 0:
            return _WIRE_MAX_TRANSFER_BYTES
        return min(configured, _WIRE_MAX_TRANSFER_BYTES)

    async def startup(self) -> None:
        if self._idle_task is None and self.config.enabled:
            self._idle_task = asyncio.create_task(self._idle_loop())
        if (
            self._prepare_task is None
            and self.config.enabled
            and callable(getattr(self.driver, "prepare", None))
        ):
            # Let the selected driver perform any lightweight readiness work in
            # the background without delaying gateway startup.
            self._prepare_task = asyncio.create_task(self._prepare_driver())

    async def _prepare_driver(self) -> None:
        prepare = getattr(self.driver, "prepare", None)
        if not callable(prepare):
            return
        try:
            if inspect.iscoroutinefunction(prepare):
                await prepare()
            else:
                result = await asyncio.to_thread(prepare)
                if inspect.isawaitable(result):
                    await result
        except asyncio.CancelledError:
            raise
        except Exception:
            # Availability remains fail-closed in the driver.  Preparation is
            # an optimization and must not crash application startup.
            log.warning("browser host background preparation failed", exc_info=True)

    async def aclose(self) -> None:
        self._closed = True
        if self._prepare_task is not None:
            self._prepare_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._prepare_task
            self._prepare_task = None
        if self._idle_task is not None:
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None
        async with self._owners_lock:
            owners = list(self._owners.values())
            self._owners.clear()
            for owner in owners:
                owner.closing = True
                owner.actions_blocked = True
        for owner in owners:
            try:
                await self._close_owner(owner)
            except Exception:
                log.error("browser owner shutdown could not be confirmed", exc_info=True)
            finally:
                owner.closed_event.set()

    def capability_generation(self, owner_account_id: str) -> int:
        """该 owner 当前的 Browser 能力代次（未撤销过为 0，单调递增）。"""
        return self._capability_generations.get(owner_account_id, 0)

    async def set_observation_deferred(
        self, owner_account_id: str, session_id: str, deferred: bool
    ) -> None:
        """批量执行开关：中间步骤跳过后置观察（见 _Session.defer_post_observation）。

        批量调用方负责 try/finally 复位；会话级标志，不影响其它会话。
        """
        owner = await self._owner(owner_account_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            session.defer_post_observation = deferred

    @contextmanager
    def defer_post_observation(self) -> Iterator[None]:
        """Skip one action's automatic snapshot without changing session state.

        Batch already uses the session flag because all of its steps share one
        observation boundary.  Individual calls use this task-local switch so
        concurrent sessions and callers cannot inherit a performance choice.
        """
        token = _POST_OBSERVATION_DEFERRED.set(True)
        try:
            yield
        finally:
            _POST_OBSERVATION_DEFERRED.reset(token)

    def _bump_capability_generation(self, owner_account_id: str) -> int:
        new_value = self._capability_generations.get(owner_account_id, 0) + 1
        self._capability_generations[owner_account_id] = new_value
        return new_value

    def renew_capability(self, owner_account_id: str) -> int:
        """重新启用能力时递增代次，并失效旧页面观察句柄。"""
        owner_id = owner_account_id
        owner = self._owners.get(owner_id)
        if owner is not None:
            self._clear_native_selection(owner)
            for session in owner.sessions.values():
                self._invalidate_observation(session)
        return self._bump_capability_generation(owner_id)

    def capability_runtime_state(self, owner_account_id: str) -> dict[str, bool]:
        """Return a synchronous fail-closed view for capability enable gates."""
        owner = self._owners.get(owner_account_id)
        closing = bool(owner is not None and owner.closing)
        actions_blocked = bool(owner is not None and owner.actions_blocked)
        stop_unconfirmed = bool(owner is not None and owner.stop_unconfirmed)
        return {
            "ready": not (closing or actions_blocked or stop_unconfirmed),
            "closing": closing,
            "actions_blocked": actions_blocked,
            "stop_unconfirmed": stop_unconfirmed,
        }

    @contextmanager
    def capability_lease(
        self, owner_account_id: str, expected_generation: int
    ) -> Iterator[None]:
        """把一次 browser_use 调用绑定到不可跨 revoke 的能力代次。"""
        token = _EXPECTED_CAPABILITY.set((owner_account_id, int(expected_generation)))
        try:
            yield
        finally:
            _EXPECTED_CAPABILITY.reset(token)

    def _ensure_leased_capability_current(self, owner_account_id: str) -> None:
        lease = _EXPECTED_CAPABILITY.get()
        if lease is None or lease[0] != owner_account_id:
            return
        self.ensure_capability_current(lease[0], lease[1])

    async def revoke_owner(self, owner_account_id: str) -> None:
        """立即撤销某 owner 的 Browser 能力（用户关闭插件开关）。

        顺序：递增 capability generation → 置 closing/actions_blocked →
        关闭其标签页与 Host owner（磁盘 Profile/Cookie 保留）。fail-stop：任何一步
        失败该 owner 保持 blocked 且不回收到可用集合，绝不静默恢复。其它 owner 不受影响。
        """
        if not owner_account_id:
            raise BrowserDriverError("browser revoke_owner 缺少账号上下文")
        owner_id = owner_account_id
        owner: _Owner | None = None
        generation = self.capability_generation(owner_id)

        async def fence_owner() -> None:
            nonlocal generation, owner
            async with self._owners_lock:
                generation = self._bump_capability_generation(owner_id)
                owner = self._owners.get(owner_id)
                if owner is not None:
                    owner.closing = True
                    owner.actions_blocked = True

        # Cancellation while waiting for _owners_lock must not skip the fence:
        # the preference is already disabled and an in-flight action may still
        # hold an older capability generation.
        cancelled = await self._complete_critical(fence_owner())
        log.info("browser capability revoked for owner=%s generation=%d", owner_id, generation)
        if owner is None:
            if cancelled:
                raise asyncio.CancelledError
            return

        async def close_and_retire() -> None:
            await self._close_owner(owner)
            async with self._owners_lock:
                if self._owners.get(owner_id) is owner:
                    self._owners.pop(owner_id, None)
            owner.stop_unconfirmed = False
            if not owner.closed_event.is_set():
                owner.closed_event.set()

        close_task = asyncio.create_task(close_and_retire())
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                # Disabling the capability has already fenced the owner. Finish
                # the profile/Host cleanup before allowing request cancellation
                # to escape, otherwise _owner() can wait forever on its tombstone.
                cancelled = True
            except Exception:
                break
        close_error: BaseException | None = None
        try:
            close_task.result()
        except asyncio.CancelledError as exc:
            cancelled = True
            close_error = exc
        except Exception as exc:
            close_error = exc
        if close_error is not None:
            # fail-stop：关闭无法确认时保留 closing/actions_blocked 墓碑，绝不允许
            # 同一 Profile 创建替代实例。_owner 对新代次也会明确报错而不是无限等待。
            owner.stop_unconfirmed = True
            # 先立墓碑再唤醒：close_and_retire 只在成功路径 set() 事件，异常路径必须
            # 在这里补 set()，否则在关闭窗口内已 await closed_event 的 _owner() 等待者
            # 永远醒不过来。stop_unconfirmed 已置位，被唤醒的等待者重新拿锁会看到墓碑
            # 并抛「关闭状态无法确认」，而不是重新初始化一个已被撤销的 owner。
            owner.closed_event.set()
            log.error(
                "browser revoke could not confirm owner shutdown",
                exc_info=(type(close_error), close_error, close_error.__traceback__),
            )
            if cancelled:
                raise asyncio.CancelledError
            return
        if cancelled:
            raise asyncio.CancelledError

    def ensure_capability_current(self, owner_account_id: str, expected_generation: int) -> None:
        """执行入口校验：动作携带的代次必须与当前一致，否则视为能力已撤销。"""
        if self.capability_generation(owner_account_id) != expected_generation:
            raise BrowserDriverError(
                "BROWSER_CAPABILITY_DISABLED: 浏览器能力已被撤销或代次已过期"
            )

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(min(60, max(5, self.config.idle_timeout_seconds // 2)))
            cutoff = time.monotonic() - max(1, self.config.idle_timeout_seconds)
            async with self._owners_lock:
                owners = []
                for owner in self._owners.values():
                    if (
                        owner.last_activity < cutoff
                        and not owner.lock.locked()
                        and not owner.retiring
                        and not owner.closing
                    ):
                        owner.retiring = True
                        owners.append(owner)
            for owner in owners:
                await self._retire_owner_if_idle(owner, cutoff)

    async def _retire_owner_if_idle(self, owner: _Owner, cutoff: float) -> None:
        """Close an idle owner without racing a replacement on its profile."""
        async with owner.lock:
            async with self._owners_lock:
                if self._owners.get(owner.owner) is not owner:
                    return
                if not owner.retiring or owner.last_activity >= cutoff:
                    owner.retiring = False
                    return
                # Keep the tombstone in _owners while the Host owner/Profile are
                # being closed.  _owner() waits for closed_event, so a new
                # instance cannot start against the same runtime/profile.
                owner.closing = True
                owner.actions_blocked = True
            try:
                await self._close_owner_locked(owner)
            except Exception:
                # Abort retirement and retain the same owner/profile binding.
                # Removing the tombstone after an unconfirmed close would let
                # a replacement race a still-live Host owner on that Profile.
                async with self._owners_lock:
                    if self._owners.get(owner.owner) is owner:
                        owner.closing = False
                        owner.actions_blocked = False
                        owner.retiring = False
                        owner.last_activity = time.monotonic()
                        # 释放在 closing 窗口内停到 closed_event 上的等待者：这条中止
                        # 路径以前直接 return，等待者永久挂死（无超时）。唤醒后它们重新
                        # 拿锁看到 closing=False，正常继续。
                        # 唤醒后必须换一个全新 Event：owner 对象在中止路径上会被继续复用，
                        # 而 asyncio.Event 一旦 set 就保持置位，下一轮 closing 窗口的等待
                        # 者会立刻空跑（这正是 clear_owner_data 活锁的另一半成因）。
                        owner.closed_event.set()
                        owner.closed_event = asyncio.Event()
                log.warning("browser idle retirement aborted because close failed", exc_info=True)
                return
            retired_states: list[tuple[str, dict[str, Any]]] = []
            for session in owner.sessions.values():
                session.mode = "paused"
                session.tabs.clear()
                session.active_label = ""
                self._invalidate_observation(session)
                retired_states.append(
                    (session.session_id, self._page_state(owner, session).public_dict())
                )
            async with self._owners_lock:
                if self._owners.get(owner.owner) is owner:
                    self._owners.pop(owner.owner, None)
            owner.closed_event.set()
            for session_id, state in retired_states:
                await self._publish(
                    owner.owner,
                    session_id,
                    {"type": "debug_clear"},
                )
                await self._publish(
                    owner.owner,
                    session_id,
                    {"type": "state", "state": state},
                )

    async def _close_owner(self, owner: _Owner) -> None:
        interrupted = False
        try:
            await self.driver.interrupt(owner.runtime_key, owner.profile_dir)
            interrupted = True
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, None, exc)
            raise
        except Exception:
            log.warning("browser owner interrupt failed during shutdown", exc_info=True)
        async with owner.lock:
            await self._close_owner_locked(owner, close_driver=not interrupted)

    async def _close_owner_locked(self, owner: _Owner, *, close_driver: bool = True) -> None:
        for session in list(owner.sessions.values()):
            await self._close_session_stream(owner, session)
        if close_driver and (owner.initialized or owner.running):
            try:
                closed = await self.driver.close(owner.runtime_key, owner.profile_dir)
            except BrowserOperationCancelled as exc:
                await self._apply_driver_lifecycle_failure(owner, None, exc)
                raise
            if closed is False:
                raise BrowserDriverError("无法确认账号 Chromium 已关闭")
        if owner.proxy is not None:
            await owner.proxy.aclose()
            owner.proxy = None
        owner.running = False
        owner.selected_label = ""
        owner.native_ref_session = ""
        owner.native_ref_generation = 0
        owner.stop_unconfirmed = False
        owner.downloads_locked = False
        owner.initialized = False

    async def _close_session_stream(self, owner: _Owner, session: _Session) -> None:
        """Compatibility no-op after presentation moved into Electron.

        Lifecycle paths still call this helper as an observation boundary, but
        presentation now belongs entirely to Electron WebContentsView.
        """
        return None

    async def _owner(self, owner_account_id: str) -> _Owner:
        owner_id = str(owner_account_id or "").strip()
        if not owner_id:
            raise BrowserDriverError("browser 工具缺少账号上下文")
        while True:
            wait_for_close: asyncio.Event | None = None
            async with self._owners_lock:
                if self._closed:
                    raise BrowserDriverError("浏览器管理器已关闭")
                self._ensure_leased_capability_current(owner_id)
                current = self._owners.get(owner_id)
                if current is not None and current.closing:
                    if current.stop_unconfirmed:
                        raise BrowserDriverError(
                            "账号浏览器关闭状态无法确认；为保护 Profile 已保持锁定，请重启应用后再试"
                        )
                    wait_for_close = current.closed_event
                else:
                    if current is None:
                        home = get_owner_runtime_home(owner_id)
                        current = _Owner(
                            owner=owner_id,
                            runtime_key=f"crew_{_hash(owner_id)}",
                            profile_dir=home / "browser" / "profile",
                        )
                        self._configure_owner_lock(current)
                        self._owners[owner_id] = current
                    # Claiming an owner cancels a not-yet-started idle retire
                    # before the caller waits on its per-account lock.
                    current.last_activity = time.monotonic()
                    current.retiring = False
            if wait_for_close is not None:
                await wait_for_close.wait()
                continue
            if current.initialized:
                return current

            try:
                async with current.lock:
                    async with self._owners_lock:
                        self._ensure_leased_capability_current(owner_id)
                        stale = current.closing or self._owners.get(owner_id) is not current
                    if stale:
                        continue
                    try:
                        if not current.initialized:
                            home = get_owner_runtime_home(owner_id)
                            await asyncio.to_thread(
                                self._cleanup_expired_artifacts,
                                home / "browser" / "artifacts",
                            )
                            await self._start_owner_proxy(current)
                    except BaseException:
                        # Publish the tombstone before releasing current.lock.
                        # A waiter can therefore never reinitialize and return
                        # an owner that has already been removed from registry.
                        async with self._owners_lock:
                            if self._owners.get(owner_id) is current:
                                self._owners.pop(owner_id, None)
                            current.closing = True
                            current.actions_blocked = True
                            current.closed_event.set()
                        raise
                return current
            except Exception:
                async with self._owners_lock:
                    if self._owners.get(owner_id) is current:
                        self._owners.pop(owner_id, None)
                    current.closing = True
                    current.closed_event.set()
                raise

    async def _start_owner_proxy(self, owner: _Owner) -> None:
        """Create the authenticated DNS-pinning boundary before browser use."""
        if not self.driver.requires_policy_proxy():
            owner.initialized = True
            return
        if owner.proxy is None:
            proxy = LoopbackPolicyProxy(self.policy)
            try:
                await proxy.start()
            except BaseException:
                await proxy.aclose()
                raise
            owner.proxy = proxy
        owner.initialized = True

    def _cleanup_expired_artifacts(self, root: Path) -> None:
        if not root.is_dir():
            return
        cutoff = time.time() - max(0, self.config.artifact_ttl_hours) * 3600
        for path in root.rglob("*"):
            try:
                if path.is_file() and path.stat().st_mtime <= cutoff:
                    path.unlink()
            except OSError:
                continue

    def _session(self, owner: _Owner, session_id: str) -> _Session:
        sid = str(session_id or "").strip()
        if not sid:
            raise BrowserDriverError("browser 工具缺少会话上下文")
        value = owner.sessions.get(sid)
        if value is None:
            value = _Session(
                session_id=sid,
                owner=owner.owner,
                mode="paused" if (owner.stopping or owner.actions_blocked) else "ai",
            )
            owner.sessions[sid] = value
        return value

    def _download_dir(self, session: _Session, workdir: str = "") -> Path:
        if workdir:
            return Path(workdir).expanduser().resolve() / "downloads" / "browser"
        return (
            get_owner_runtime_home(session.owner)
            / "task_workspaces"
            / _hash(session.session_id)
            / "downloads"
            / "browser"
        )

    def _prepare_download_dir(self, session: _Session, workdir: str = "") -> Path:
        """Create the task download directory without following preset links."""
        if workdir:
            base = Path(workdir).expanduser().resolve()
            if not base.is_dir():
                raise BrowserDriverError("当前任务工作区不存在，无法保存下载")
        else:
            base = (
                get_owner_runtime_home(session.owner)
                / "task_workspaces"
                / _hash(session.session_id)
            ).resolve()
            base.mkdir(parents=True, exist_ok=True, mode=0o700)

        current = base
        for component in ("downloads", "browser"):
            candidate = current / component
            if candidate.is_symlink():
                raise BrowserDriverError("下载目录包含符号链接；拒绝写入工作区边界之外")
            candidate.mkdir(exist_ok=True, mode=0o700)
            if candidate.is_symlink() or candidate.resolve() != candidate.absolute():
                raise BrowserDriverError("下载目录解析到工作区边界之外；拒绝写入")
            current = candidate
        if not path_is_within(current, [base]):
            raise BrowserDriverError("下载目录不能离开当前任务工作区")
        return current

    @staticmethod
    def _download_quarantine(owner: _Owner) -> Path:
        return owner.profile_dir.parent / "download-quarantine"

    def _artifact_dir(self, session: _Session) -> Path:
        path = (
            get_owner_runtime_home(session.owner)
            / "browser"
            / "artifacts"
            / _hash(session.session_id)
        )
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        with suppress(OSError):
            path.chmod(0o700)
        return path

    async def _apply_driver_lifecycle_failure(
        self,
        owner: _Owner,
        session: _Session | None,
        exc: BrowserDriverError | BrowserOperationCancelled,
    ) -> None:
        """Apply fail-stop metadata without choosing error vs cancellation semantics.

        分三级，不能混为一谈：

        - browser_stopped / stop_unconfirmed：宿主真的没了，或关停无法确认（Profile
          必须保持锁定，否则替代实例会和仍存活的 Host owner 抢同一个 Profile）。
          这两种才 fence 整个 owner。
        - uncertain：某次 mutation 已发出但结果未知，浏览器本身还活着。只作废**该会话**
          的观察。把它升格成账号级 fence，会因为一次点击超时就把所有会话置 paused、
          清空全部标签页——用户看到的「账号浏览器已停止」正是这么来的，而且此后每个
          动作都失败，只能重启。
        """
        if exc.browser_stopped or exc.stop_unconfirmed:
            owner.running = not exc.browser_stopped
            owner.actions_blocked = True
            owner.stop_unconfirmed = exc.stop_unconfirmed
            self._clear_native_selection(owner)
            for value in owner.sessions.values():
                await self._close_session_stream(owner, value)
                value.mode = "paused"
                value.tabs.clear()
                value.active_label = ""
                self._invalidate_observation(value)
            return
        if (exc.uncertain or getattr(exc, "partial", False)) and session is not None:
            # 结果未知：这一会话的旧 ref 不能再用，必须重新观察。但其它会话的标签页
            # 与本次动作无关，不受牵连。复合动作即使当前失败是确定的，只要已有
            # 前序步骤成功，旧观察同样不再代表页面。
            self._invalidate_observation(session)
            if owner.native_ref_session == session.session_id:
                owner.native_ref_session = ""
                owner.native_ref_generation = 0

    @staticmethod
    def _recoverable_next_state(code: str) -> dict[str, Any] | None:
        """Describe a Host modal as a blocked, explicitly resumable state."""
        if code == "dialog_pending":
            return {
                "status": "blocked",
                "recoverable": True,
                "reason": "dialog_pending",
                "next": [
                    {
                        "tool": "browser_use",
                        "arguments": {"action": "dialog_status"},
                    },
                    {
                        "tool": "browser_use",
                        "arguments": {"action": "dialog_accept"},
                        "optional_arguments": ["text"],
                    },
                    {
                        "tool": "browser_use",
                        "arguments": {"action": "dialog_dismiss"},
                    },
                ],
                "retry_original_action": False,
            }
        if code == "file_chooser_pending":
            return {
                "status": "blocked",
                "recoverable": True,
                "reason": "file_chooser_pending",
                "next": [
                    {
                        "tool": "browser_use",
                        "arguments": {"action": "upload"},
                        "required_arguments": ["paths"],
                        "instruction": "omit ref to resolve the pending chooser",
                    },
                    {
                        "tool": "browser_use",
                        "arguments": {"action": "upload", "paths": []},
                        "instruction": "cancel the pending chooser",
                    },
                ],
                "retry_original_action": False,
            }
        return None

    @classmethod
    def _recoverable_error(
        cls,
        message: str,
        *,
        code: str,
        uncertain: bool = False,
        browser_stopped: bool = False,
        stop_unconfirmed: bool = False,
        phase: str = "",
        partial: bool = False,
        completed_count: int = 0,
        next_state: dict[str, Any] | None = None,
    ) -> BrowserDriverError:
        state = next_state or cls._recoverable_next_state(code)
        rendered = str(message)
        if state is not None:
            rendered += (
                "\n<browser_next_state>\n"
                + json.dumps(state, ensure_ascii=False, separators=(",", ":"))
                + "\n</browser_next_state>"
            )
        error = BrowserDriverError(
            rendered,
            uncertain=uncertain,
            browser_stopped=browser_stopped,
            stop_unconfirmed=stop_unconfirmed,
            code=code,
            phase=phase,
            partial=partial,
            completed_count=completed_count,
        )
        if state is not None:
            # BrowserDriverError predates structured recovery metadata.  Keep
            # its stable constructor compatible while exposing the state to
            # direct Python callers and tests.
            error.next_state = state  # type: ignore[attr-defined]
        return error

    async def _raise_driver_error(
        self,
        owner: _Owner,
        session: _Session,
        exc: BrowserDriverError,
    ) -> None:
        """Promote driver lifecycle failures consistently across all paths."""
        await self._apply_driver_lifecycle_failure(owner, session, exc)
        safe_error = str(exc)
        code = str(getattr(exc, "code", "") or "")
        canonical_next_state = self._recoverable_next_state(code)
        next_state = canonical_next_state or getattr(exc, "next_state", None)
        if not isinstance(next_state, dict):
            next_state = None
        session.last_error = safe_error
        event: dict[str, Any] = {
            "type": "error",
            "error": safe_error,
            **({"code": code} if code else {}),
        }
        if next_state is not None:
            event["recoverable"] = True
            event["next_state"] = next_state
        await self._publish(
            owner.owner,
            session.session_id,
            event,
        )
        raise self._recoverable_error(
            safe_error,
            uncertain=exc.uncertain,
            browser_stopped=exc.browser_stopped,
            stop_unconfirmed=exc.stop_unconfirmed,
            # code 必须透传：工具层据此把「ref 失效」这类可恢复失败补上最新观察，
            # 丢了 code 就退化成模型无法据以行动的死错误。
            code=code,
            # Host 的 mutation 阶段/部分执行语义同样是稳定契约。Manager
            # 重新包装脱敏错误文案时不能把它们悄悄抹掉。
            phase=getattr(exc, "phase", ""),
            partial=bool(getattr(exc, "partial", False)),
            completed_count=getattr(exc, "completed_count", 0),
            next_state=next_state,
        ) from None

    async def _run(
        self,
        owner: _Owner,
        session: _Session,
        command: str,
        args: list[str] | tuple[str, ...] = (),
        *,
        navigation: bool = False,
        mutating: bool = False,
        workdir: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_leased_capability_current(owner.owner)
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；请先交还 AI 后再执行浏览器动作")
        owner.last_activity = time.monotonic()
        timeout = (
            max(0.001, float(timeout_seconds))
            if timeout_seconds is not None
            else (
                self.config.navigation_timeout_seconds
                if navigation
                else self.config.command_timeout_seconds
            )
        )
        try:
            proxy_url = owner.proxy.url if owner.proxy else ""
            quarantine = self._download_quarantine(owner)
            if command == "download" and len(args) == 2:
                result = await self.driver.download_bounded(
                    owner.runtime_key,
                    owner.profile_dir,
                    str(args[0]),
                    Path(str(args[1])),
                    target_id=self._active_tab(session).target_id,
                    max_bytes=self._transfer_limit_bytes(),
                    timeout=timeout,
                    proxy_url=proxy_url,
                    download_dir=quarantine,
                )
            else:
                active_target = ""
                if session.active_label:
                    active = session.tabs.get(session.active_label)
                    if active is not None:
                        active_target = active.target_id
                execute = (
                    self.driver.execute_targeted
                    if active_target
                    else self.driver.execute
                )
                execute_kwargs: dict[str, Any] = {
                    "timeout": timeout,
                    "proxy_url": proxy_url,
                    # Every ordinary Playwright/Electron action may trigger a
                    # browser download (including attachment navigation,
                    # popups and page-authored timers).  The Host retains this
                    # task directory on the logical tab and makes the native
                    # will-download event the sole setSavePath authority.
                    "download_dir": self._prepare_download_dir(session, workdir),
                    "mutating": mutating,
                }
                if active_target:
                    execute_kwargs["target_id"] = active_target
                result = await execute(
                    owner.runtime_key,
                    owner.profile_dir,
                    command,
                    args,
                    **execute_kwargs,
                )
                await self._ingest_automatic_downloads(owner, session, result)
            owner.running = True
            session.last_error = ""
            return result
        except BrowserDriverError as exc:
            await self._raise_driver_error(owner, session, exc)
            raise AssertionError("unreachable")
        except BrowserOperationCancelled as exc:
            # Preserve Host lifecycle safety, then let user cancellation end
            # the turn instead of returning a retryable-looking tool error.
            await self._apply_driver_lifecycle_failure(owner, session, exc)
            raise

    async def _run_fill_form(
        self,
        owner: _Owner,
        session: _Session,
        fields: list[dict[str, Any]],
        *,
        workdir: str = "",
    ) -> dict[str, Any]:
        """Send a typed form payload without serializing private values into argv."""
        self._ensure_leased_capability_current(owner.owner)
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；请先交还 AI 后再执行浏览器动作")
        owner.last_activity = time.monotonic()
        # One RPC contains sequential Playwright actions.  Budget one configured
        # action timeout per field plus transport headroom; do not clip large,
        # valid forms to a product-defined five-minute ceiling.
        per_field_timeout = max(
            0.001,
            float(self.config.command_timeout_seconds),
        )
        batch_timeout = per_field_timeout * len(fields) + 5.0
        try:
            download_root = self._prepare_download_dir(session, workdir)
            result = await self.driver.fill_form(
                owner.runtime_key,
                owner.profile_dir,
                fields,
                target_id=self._active_tab(session).target_id,
                timeout=batch_timeout,
                proxy_url=owner.proxy.url if owner.proxy else "",
                download_dir=download_root,
            )
            await self._ingest_automatic_downloads(owner, session, result)
            owner.running = True
            session.last_error = ""
            return result
        except BrowserDriverError as exc:
            await self._raise_driver_error(owner, session, exc)
            raise AssertionError("unreachable")
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, session, exc)
            raise

    async def _run_upload_with_trigger(
        self,
        owner: _Owner,
        session: _Session,
        *,
        trigger_selector: str,
        input_selector: str,
        files: list[str],
        workdir: str = "",
    ) -> dict[str, Any]:
        """Send one typed trigger→chooser/input upload transaction to Host."""
        self._ensure_leased_capability_current(owner.owner)
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；请先交还 AI 后再执行浏览器动作")
        owner.last_activity = time.monotonic()
        # The Host owns the exact Playwright action/chooser transaction.  Use
        # caller configuration directly instead of a hidden 40..120s window.
        timeout = max(
            0.001,
            float(self.config.command_timeout_seconds),
            float(self.config.navigation_timeout_seconds),
        )
        staged_files, upload_stage = await asyncio.to_thread(
            self._stage_approved_uploads,
            owner,
            files,
        )
        try:
            download_root = self._prepare_download_dir(session, workdir)
            result = await self.driver.upload_with_trigger(
                owner.runtime_key,
                owner.profile_dir,
                target_id=self._active_tab(session).target_id,
                trigger_selector=trigger_selector,
                input_selector=input_selector,
                files=staged_files,
                timeout=timeout,
                proxy_url=owner.proxy.url if owner.proxy else "",
                download_dir=download_root,
            )
            await self._ingest_automatic_downloads(owner, session, result)
            owner.running = True
            session.last_error = ""
            return result
        except BrowserDriverError as exc:
            await self._raise_driver_error(owner, session, exc)
            raise AssertionError("unreachable")
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, session, exc)
            raise
        finally:
            await asyncio.to_thread(
                self._cleanup_approved_upload_stage,
                upload_stage,
                owner.profile_dir.parent / "approved-uploads",
            )

    def _new_tab(self, session: _Session) -> _Tab:
        session.counter += 1
        # Labels cross the Python/Host trust boundary and are used to recover
        # exact session ownership after native tab changes. Keep 128 bits of
        # the session digest; the shorter public session_hash remains separate.
        label = f"s{_hash(session.session_id, 32)}-{session.counter}"
        tab = _Tab(id=label, label=label)
        session.tabs[label] = tab
        session.active_label = label
        return tab

    @staticmethod
    def _rollback_new_tab(session: _Session, tab: _Tab, previous_active: str) -> None:
        """Remove a local placeholder after a provably unsent/failed tab create."""
        if session.tabs.get(tab.id) is not tab:
            return
        session.tabs.pop(tab.id, None)
        session.active_label = (
            previous_active if previous_active in session.tabs else next(iter(session.tabs), "")
        )

    async def _close_tab_target(
        self,
        owner: _Owner,
        session: _Session,
        tab: _Tab,
    ) -> None:
        if not tab.target_id:
            raise BrowserDriverError("标签页缺少不可复用的 CDP targetId，已拒绝按原生 tabId 关闭")
        try:
            await self.driver.close_target(
                owner.runtime_key,
                owner.profile_dir,
                target_id=tab.target_id,
                timeout=self.config.command_timeout_seconds,
                proxy_url=owner.proxy.url if owner.proxy else "",
                download_dir=self._download_quarantine(owner),
            )
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, session, exc)
            raise
        except BrowserDriverError as exc:
            await self._raise_driver_error(owner, session, exc)
            raise AssertionError("unreachable")

    @staticmethod
    def _native_tab_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate the Electron host's compatibility ``tab list`` shape.

        The native response is ``{tabs: [{tabId, label, ..., active}]}``.
        Treat malformed or ambiguous state as unsafe: selecting the wrong
        native target could operate on a popup or another Crew session.
        """
        data = _data(result)
        raw_tabs = data.get("tabs") if isinstance(data, dict) else None
        if not isinstance(raw_tabs, list):
            raise BrowserDriverError("浏览器返回了无效的标签页状态")

        rows: list[dict[str, Any]] = []
        tab_ids: set[str] = set()
        for item in raw_tabs:
            if not isinstance(item, dict):
                raise BrowserDriverError("浏览器返回了无效的标签页状态")
            tab_id = item.get("tabId")
            label = item.get("label")
            active = item.get("active")
            target_id = item.get("targetId")
            session_hash = item.get("sessionHash")
            opener_target_id = item.get("openerTargetId")
            popup_ordinal = item.get("popupOrdinal", 0)
            popup_ordinal_base = item.get("popupOrdinalBase", 0)
            if (
                not isinstance(tab_id, str)
                or re.fullmatch(r"t[1-9]\d*", tab_id) is None
                or tab_id in tab_ids
                or (label is not None and not isinstance(label, str))
                or (target_id is not None and not isinstance(target_id, str))
                or (
                    session_hash is not None
                    and (
                        not isinstance(session_hash, str)
                        or (
                            session_hash
                            and re.fullmatch(r"[0-9a-f]{32}", session_hash) is None
                        )
                    )
                )
                or (opener_target_id is not None and not isinstance(opener_target_id, str))
                or isinstance(popup_ordinal, bool)
                or not isinstance(popup_ordinal, int)
                or popup_ordinal < 0
                or isinstance(popup_ordinal_base, bool)
                or not isinstance(popup_ordinal_base, int)
                or popup_ordinal_base < 0
                or not isinstance(active, bool)
            ):
                raise BrowserDriverError("浏览器返回了无效的标签页状态")
            tab_ids.add(tab_id)
            rows.append(
                {
                    "tabId": tab_id,
                    "label": label,
                    "active": active,
                    "url": str(item.get("url") or ""),
                    "title": str(item.get("title") or ""),
                    "targetId": str(target_id or ""),
                    "sessionHash": str(session_hash or ""),
                    "openerTargetId": str(opener_target_id or ""),
                    "popupOrdinal": popup_ordinal,
                    "popupOrdinalBase": popup_ordinal_base,
                }
            )
        return rows

    @staticmethod
    def _clear_native_selection(owner: _Owner) -> None:
        owner.selected_label = ""
        owner.native_ref_session = ""
        owner.native_ref_generation = 0

    def _reject_ambiguous_native_selection(self, owner: _Owner, session: _Session) -> None:
        self._clear_native_selection(owner)
        if session.refs or session.page_marker or session.screenshot_id:
            self._invalidate_observation(session)

    async def _sync_topology(
        self,
        owner: _Owner,
        session: _Session,
        *,
        allow_empty: bool,
    ) -> tuple[_Tab | None, bool]:
        """Reconcile one Crew session from the Host's authoritative page graph.

        The Host owns page lifetime and opener topology. Python owns only the
        stable mapping from a Crew session to immutable Chromium ``targetId``s.
        Reconciliation therefore has a strict order:

        1. refresh every still-live local target;
        2. adopt all exact-session/opener descendants to a fixed point;
        3. prune local targets no longer reported by the Host;
        4. restore the selected page without touching unknown/foreign pages.

        Adopting before pruning is essential. A payment/OAuth opener can close
        in the same task that creates its child; the child still carries either
        the inherited ``sessionHash`` or the cached immutable opener target.
        """
        original_active = session.tabs.get(session.active_label)
        original_active_id = original_active.id if original_active is not None else ""
        original_active_target = (
            original_active.target_id if original_active is not None else ""
        )
        selection_changed = False
        expected_session_hash = _hash(session.session_id, 32)
        loop = asyncio.get_running_loop()
        topology_deadline = loop.time() + max(
            0.001,
            float(self.config.navigation_timeout_seconds),
        )

        while loop.time() < topology_deadline:
            try:
                rows = self._native_tab_rows(
                    await self._run(
                        owner,
                        session,
                        "tab",
                        ["list"],
                        timeout_seconds=max(0.001, topology_deadline - loop.time()),
                    )
                )
            except BrowserDriverError:
                self._reject_ambiguous_native_selection(owner, session)
                raise

            active_rows = [row for row in rows if row["active"]]
            if len(active_rows) > 1:
                self._reject_ambiguous_native_selection(owner, session)
                raise BrowserDriverError("浏览器返回了多个活动标签页")

            rows_by_target: dict[str, dict[str, Any]] = {}
            rows_by_label: dict[str, list[dict[str, Any]]] = {}
            for row in rows:
                target_id = str(row.get("targetId") or "")
                if target_id:
                    if target_id in rows_by_target:
                        self._reject_ambiguous_native_selection(owner, session)
                        raise BrowserDriverError("浏览器返回了重复的 targetId")
                    rows_by_target[target_id] = row
                label = str(row.get("label") or "")
                if label:
                    rows_by_label.setdefault(label, []).append(row)

            foreign_targets = {
                tab.target_id
                for candidate in owner.sessions.values()
                if candidate is not session
                for tab in candidate.tabs.values()
                if tab.target_id
            }
            foreign_labels = {
                tab.label
                for candidate in owner.sessions.values()
                if candidate is not session
                for tab in candidate.tabs.values()
                if tab.native_labeled and tab.label
            }
            known_session_targets = {
                tab.target_id for tab in session.tabs.values() if tab.target_id
            }
            live_tabs_by_target: dict[str, _Tab] = {}
            adopted_any = False
            adopted_targets: set[str] = set()

            def refresh(tab: _Tab, row: dict[str, Any]) -> None:
                target_id = str(row.get("targetId") or "")
                row_session_hash = str(row.get("sessionHash") or "")
                if not target_id:
                    raise BrowserDriverError("Crew 标签页缺少不可复用的 targetId")
                if row_session_hash and row_session_hash != expected_session_hash:
                    raise BrowserDriverError("Crew 标签页的 Host sessionHash 已变化")
                if tab.target_id and tab.target_id != target_id:
                    raise BrowserDriverError(
                        "当前标签页 targetId 已变化；拒绝使用可能复用的原生 tabId"
                    )
                tab.target_id = target_id
                tab.native_id = str(row["tabId"])
                tab.opener_target_id = str(row.get("openerTargetId") or "")
                tab.popup_ordinal = int(row.get("popupOrdinal") or 0)
                tab.url = str(row.get("url") or "")
                tab.title = str(row.get("title") or "")
                existing_live = live_tabs_by_target.get(target_id)
                if existing_live is not None and existing_live is not tab:
                    raise BrowserDriverError(
                        "同一 targetId 被登记到多个 Crew 标签页"
                    )
                live_tabs_by_target[target_id] = tab
                known_session_targets.add(target_id)

            # Match immutable targets first. Labeled roots without a captured
            # target are the sole bootstrap exception. A reused label with a
            # different target is an integrity failure, never a replacement.
            try:
                for tab in list(session.tabs.values()):
                    row = rows_by_target.get(tab.target_id) if tab.target_id else None
                    label_rows = rows_by_label.get(tab.label, []) if tab.native_labeled else []
                    if len(label_rows) > 1:
                        raise BrowserDriverError("浏览器返回了重复的 Crew 标签页 label")
                    if row is None and label_rows:
                        candidate = label_rows[0]
                        candidate_target = str(candidate.get("targetId") or "")
                        if tab.target_id and candidate_target != tab.target_id:
                            raise BrowserDriverError(
                                "当前标签页 targetId 已变化；拒绝使用可能复用的原生 tabId"
                            )
                        row = candidate
                    if row is not None:
                        refresh(tab, row)
            except BrowserDriverError:
                self._reject_ambiguous_native_selection(owner, session)
                raise

            # Exact sessionHash makes popup ownership survive opener closure.
            # Older/alternative drivers may omit it, so retain an opener-chain
            # compatibility path seeded with *all cached targets* before prune.
            pending = [
                row
                for row in rows
                if str(row.get("targetId") or "") not in live_tabs_by_target
                and str(row.get("targetId") or "") not in foreign_targets
                and str(row.get("label") or "") not in foreign_labels
            ]
            while pending:
                progressed = False
                for row in list(pending):
                    target_id = str(row.get("targetId") or "")
                    row_session_hash = str(row.get("sessionHash") or "")
                    opener_target_id = str(row.get("openerTargetId") or "")
                    if (
                        not target_id
                        or (
                            row_session_hash != expected_session_hash
                            and not (
                                not row_session_hash
                                and opener_target_id in known_session_targets
                            )
                        )
                    ):
                        continue

                    native_id = str(row.get("tabId") or "")
                    native_label = str(row.get("label") or "")
                    is_labeled = bool(
                        native_label
                        and re.fullmatch(
                            rf"s{re.escape(expected_session_hash)}-[1-9]\d*",
                            native_label,
                        )
                    )
                    local_id = native_label if is_labeled else native_id
                    existing = session.tabs.get(local_id)
                    if existing is not None and existing.target_id != target_id:
                        # A process-local tN may be reused. Keep the public id
                        # stable by falling back to the immutable identity.
                        local_id = target_id
                    if local_id in session.tabs:
                        existing = session.tabs[local_id]
                        if existing.target_id != target_id:
                            raise BrowserDriverError("无法唯一登记浏览器 targetId")
                        tab = existing
                    else:
                        tab = _Tab(
                            id=local_id,
                            label=native_label if is_labeled else native_id,
                            native_labeled=is_labeled,
                        )
                        session.tabs[tab.id] = tab
                    refresh(tab, row)
                    adopted_any = True
                    adopted_targets.add(target_id)
                    pending.remove(row)
                    progressed = True
                if not progressed:
                    break

            # Only now is it safe to remove local pages absent from Host state:
            # their target ids were still needed to prove newly created children.
            stale_active = session.tabs.get(session.active_label)
            for local_id, tab in list(session.tabs.items()):
                if not tab.target_id or live_tabs_by_target.get(tab.target_id) is not tab:
                    session.tabs.pop(local_id, None)
                    if owner.selected_label == tab.label:
                        self._clear_native_selection(owner)

            active_row = active_rows[0] if active_rows else None
            active_tab = (
                live_tabs_by_target.get(str(active_row.get("targetId") or ""))
                if active_row is not None
                else None
            )
            current = session.tabs.get(session.active_label)
            if (
                active_tab is not None
                and active_tab.target_id in adopted_targets
            ):
                # A newly discovered active descendant is the direct result of
                # the preceding browser action (window.open/target=_blank).
                # Follow it exactly once. Already-known sibling tabs do not
                # override an explicit Python tabs(select) request.
                desired = active_tab
            elif current is not None:
                # An unknown or another session's page may be active. Preserve
                # it, but restore this session's existing selection.
                desired = current
            elif active_tab is not None:
                desired = active_tab
            else:
                desired = None
                if stale_active is not None and stale_active.opener_target_id:
                    desired = live_tabs_by_target.get(stale_active.opener_target_id)
                if desired is None:
                    # Follow Host tab order for a deterministic browser-like
                    # fallback after an active ephemeral popup closes.
                    for row in rows:
                        desired = live_tabs_by_target.get(str(row.get("targetId") or ""))
                        if desired is not None:
                            break

            if desired is None:
                session.active_label = ""
                self._clear_native_selection(owner)
                if allow_empty:
                    return None, bool(original_active_target)
                raise BrowserDriverError(
                    "当前会话没有浏览器标签页，请先调用 browser_navigate"
                )

            session.active_label = desired.id
            if original_active_target:
                selection_changed = (
                    selection_changed
                    or desired.target_id != original_active_target
                )
            elif original_active_id and desired.id != original_active_id:
                selection_changed = True

            if active_tab is desired:
                owner.selected_label = desired.label
                if adopted_any:
                    # Popup creation can itself synchronously create another
                    # popup. Confirm one more authoritative graph and keep
                    # reconciling until no new descendants arrive.
                    continue
                return desired, selection_changed

            # Select by immutable targetId, never by the reusable process-local
            # tN. Unknown pages are deliberately left open.
            try:
                await self._run(
                    owner,
                    session,
                    "tab",
                    [desired.target_id],
                    timeout_seconds=max(0.001, topology_deadline - loop.time()),
                )
            except BrowserDriverError as exc:
                if exc.code == "ambiguous_tab" and loop.time() < topology_deadline:
                    selection_changed = True
                    self._clear_native_selection(owner)
                    continue
                self._reject_ambiguous_native_selection(owner, session)
                raise
            selection_changed = True
            self._clear_native_selection(owner)

        self._reject_ambiguous_native_selection(owner, session)
        raise BrowserDriverError(
            "浏览器标签页在配置的导航超时内持续变化；请重新观察页面",
            code="page_topology_timeout",
        )

    async def _select(self, owner: _Owner, session: _Session) -> tuple[_Tab, bool]:
        tab, changed = await self._sync_topology(owner, session, allow_empty=False)
        assert tab is not None
        return tab, changed

    # 失败分类学。分类决定的是**该不该改技能**——把环境问题（没登录、通道
    # 不可用）当成技能缺陷去改代码，是这类系统最典型也最昂贵的错误。
    # 浏览器错误分类规则集。
    _FAILURE_CLASSES: tuple[tuple[str, str, str], ...] = (
        (
            "user_state",
            "人工接管|接管|暂停|未登录|登录|会话已失效",
            "用户正在接管浏览器、或页面要求重新登录。这不是技能缺陷，"
            "请让用户处理后再继续，不要改技能。",
        ),
        (
            "environment",
            "浏览器已停止|通道|连接|超时|不可用|未启用",
            "浏览器运行时或通道不可用。这不是技能缺陷，重试或让用户重启浏览器。",
        ),
        (
            "capability",
            "只读|不能|未开放|拒绝",
            "动作被能力档拒绝。技能是只读的，写操作必须由用户本人完成——"
            "不要绕过，把页面内容汇报给用户并交还浏览器。",
        ),
        (
            "stale_observation",
            "ref 已失效|页面已变化|重新观察|发生变化",
            "观察已过期。重新 snapshot 再继续，这是正常的页面变动，不是缺陷。",
        ),
    )

    def note_action_outcome(
        self, owner_id: str, session_id: str, action: str, *, ok: bool
    ) -> None:
        """记录一次动作的结果，用于诊断，不实施固定次数熔断。"""
        owner = self._owners.get(str(owner_id or ""))
        session = owner.sessions.get(str(session_id or "")) if owner else None
        if session is None:
            return
        if ok:
            session.consecutive_failures = 0
            session.last_success = str(action or "")
        else:
            session.consecutive_failures += 1

    # 宿主的稳定错误码 -> 分类。**码优先于文本**：码是契约，文本会改。
    _FAILURE_CODE_CLASSES: dict[str, str] = {
        "control_mode_blocked": "user_state",
        "browser_stopped": "environment",
        "tab_stopped": "environment",
        "debugger_unavailable": "environment",
        "profile_owner_mismatch": "environment",
        "stale_ref": "stale_observation",
        "stale_ref_security": "stale_observation",
        "page_changed": "stale_observation",
        "hit_test_failed": "stale_observation",
    }

    def failure_evidence(
        self, owner_id: str, session_id: str, action: str, message: str
    ) -> dict[str, Any]:
        """给模型的失败证据包。

        workflow-use 的 ErrorContext 字段设计得很好，但它只 logger.error 给人看。
        这里把它喂给模型——那正是它差的临门一脚。
        """
        owner = self._owners.get(str(owner_id or ""))
        session = owner.sessions.get(str(session_id or "")) if owner else None
        text = str(message or "")
        failure_class = "unknown"
        guidance = "无法归类。把已经读到的内容和失败现象报告给用户，不要反复重试。"
        # 调用方传进来的可能是宿主的稳定错误码，也可能是错误文本。码优先——
        # 它是契约，而文本随时会改（改文案不该悄悄改变分类）。
        by_code = self._FAILURE_CODE_CLASSES.get(text.strip())
        for name, pattern, advice in self._FAILURE_CLASSES:
            if name == by_code or (by_code is None and re.search(pattern, text)):
                failure_class, guidance = name, advice
                break
        consecutive = session.consecutive_failures if session else 0
        return {
            "failure_class": failure_class,
            "guidance": guidance,
            "consecutive_failures": consecutive,
            "last_success": session.last_success if session else "",
            "action": str(action or ""),
        }
    @staticmethod
    def _clear_ref_state(session: _Session) -> None:
        session.refs.clear()
        # 动作类别与 ref 表同生共死。留着上一页的标记，只读档就会拿旧页面的
        # "这个 ref 不是提交按钮"去放行新页面上同名的提交按钮。
        session.ref_actions.clear()

    @staticmethod
    def _invalidate_observation(session: _Session, *, bump_generation: bool = True) -> None:
        if bump_generation:
            session.generation += 1
        BrowserManager._clear_ref_state(session)
        BrowserManager._clear_screenshot(session)
        session.page_marker = ""

    async def _select_checked(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str = "",
    ) -> _Tab:
        """Select the owned tab and enforce the public ref generation.

        Cross-document invalidation is owned by BrowserHost/Playwright: the
        Host clears its exact ref table synchronously at navigation start, and
        every action resolves/counts the strict Locator immediately before
        dispatch.  Re-reading a Python ``page_guard`` here duplicated that
        document check, added a renderer round trip to every operation, and
        rejected legitimate dynamic pages.  Python retains only the boundaries
        it actually owns: session/tab topology and the public ``pN`` namespace.
        """
        del workdir
        had_observation = bool(session.refs or session.page_marker or session.screenshot_id)
        tab, switched = await self._select(owner, session)
        if switched and had_observation:
            # Native refs belong to the selected tab. Never send a previously
            # issued @e ref after another Crew session has been active; require
            # a fresh snapshot instead.
            self._invalidate_observation(session)
            raise BrowserDriverError(
                "标签页切换已使旧 ref/截图失效，请调用 browser_use 的 snapshot action"
            )
        if session.refs and (
            owner.native_ref_session != session.session_id
            or owner.native_ref_generation != session.generation
        ):
            self._invalidate_observation(session)
            raise BrowserDriverError(
                "原生 ref 缓存已变化；旧 ref 已失效，请调用 browser_use 的 snapshot action"
            )
        return tab

    @staticmethod
    def _marker_data(marker: str) -> dict[str, Any] | None:
        try:
            value: Any = json.loads(str(marker or ""))
            if isinstance(value, str):
                value = json.loads(value)
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def _same_page_marker(cls, current: str, observed: str) -> bool:
        """Compare document identity while tolerating live DOM churn and viewport changes.

        视口（width/height/dpr）不再作为 ref 失效条件：ref 是活的原生 DOM 句柄，
        与视口无关；元素级完整性由每次动作的签名重查与点击后 hit-test 兜底。
        真正依赖视口的是坐标点击，由更严格的 _same_screenshot_marker 单独约束。
        挂载/调整浏览器面板、最大化、拖窗口因此不会再误伤进行中的自动化。
        """
        left = cls._marker_data(current)
        right = cls._marker_data(observed)
        if left is None or right is None:
            return current == observed
        fields = (
            "token",
            "targetId",
            "frameId",
            "loaderId",
            "href",
            "timeOrigin",
        )
        return all(left.get(field) == right.get(field) for field in fields)

    @classmethod
    def _same_screenshot_marker(cls, current: str, observed: str) -> bool:
        """Bind coordinate screenshots to the same document, scroll and viewport.

        Live pages routinely update clocks, badges, carousels and virtualized
        rows between screenshot and click. A global MutationObserver counter or
        ref-security digest would make coordinate fallback unusable on exactly
        those pages. The production Host additionally checks its one-shot epoch
        and the current document identity before dispatch.
        """
        left = cls._marker_data(current)
        right = cls._marker_data(observed)
        if left is None or right is None:
            return current == observed
        fields = (
            "token",
            "targetId",
            "frameId",
            "loaderId",
            "href",
            "timeOrigin",
            "scrollX",
            "scrollY",
            "width",
            "height",
            "dpr",
        )
        return all(left.get(field) == right.get(field) for field in fields)

    @staticmethod
    def _snapshot_structural_ref(
        line: str,
        allowed_native_refs: set[str],
    ) -> tuple[int, int, str] | None:
        """Return the sole Host-authorized ref token in one aria node key.

        Accessible names are JSON double-quoted and page text may contain an
        arbitrary ``[ref=eN]`` string. Only an unquoted token in the YAML node
        key, before the ``: value`` separator, can be structural.
        """
        prefix = re.match(r"^\s*-\s+", line)
        if prefix is None:
            return None
        start = prefix.end()
        quoted = False
        escaped = False
        key_end = len(line)
        for index in range(start, len(line)):
            char = line[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
                continue
            if char == '"':
                quoted = True
                continue
            if char == ":" and (index + 1 == len(line) or line[index + 1].isspace()):
                key_end = index
                break

        candidates: list[tuple[int, int, str]] = []
        for match in _SNAPSHOT_REF_TOKEN.finditer(line, start, key_end):
            # Ignore tokens inside the accessible-name JSON string.
            in_name = False
            escaped = False
            for char in line[start : match.start()]:
                if in_name:
                    if escaped:
                        escaped = False
                    elif char == "\\":
                        escaped = True
                    elif char == '"':
                        in_name = False
                elif char == '"':
                    in_name = True
            if in_name:
                continue
            raw_native = match.group(1)
            native = raw_native if raw_native.startswith("@") else f"@{raw_native}"
            candidates.append((match.start(), match.end(), native))
        if len(candidates) != 1 or candidates[0][2] not in allowed_native_refs:
            return None
        return candidates[0]

    @classmethod
    def _page_transition_signature(cls, marker: str) -> tuple[Any, ...] | None:
        """Return only fields that describe a main-page transition.

        Mutation counters are deliberately absent: dynamic pages may mutate
        forever, while transition checks only need document/navigation identity
        and readiness.

        titleDigest is deliberately NOT here: a title change is not a page
        transition. Pages with a churning title (countdowns, "(3) 收件箱",
        players, or a hostile setInterval(()=>document.title=...)) would
        otherwise reset the quiet gate forever and make themselves permanently
        un-observable. Real same-document navigation still shows up through
        href + navigationEpoch (the Host bumps the epoch on did-navigate-in-page,
        not on page-title-updated), so dropping the title loses no transition.
        """
        data = cls._marker_data(marker)
        if data is None or "navigationEpoch" not in data:
            return None
        fields = (
            "token",
            "targetId",
            "frameId",
            "loaderId",
            "href",
            "timeOrigin",
            "navigationEpoch",
            "navigationPending",
            "locationConsistent",
        )
        return tuple(data.get(field) for field in fields)

    @classmethod
    def _page_transition_ready(cls, marker: str) -> bool:
        data = cls._marker_data(marker)
        epoch = data.get("navigationEpoch") if data is not None else None
        return bool(
            data is not None
            and isinstance(epoch, int)
            and not isinstance(epoch, bool)
            and epoch >= 0
            and data.get("navigationPending") is False
            and data.get("locationConsistent") is True
            and isinstance(data.get("token"), str)
            and bool(data.get("token"))
            and isinstance(data.get("targetId"), str)
            and bool(data.get("targetId"))
            and isinstance(data.get("frameId"), str)
            and bool(data.get("frameId"))
            and isinstance(data.get("href"), str)
            and bool(data.get("href"))
            and isinstance(data.get("titleDigest"), str)
            and bool(data.get("titleDigest"))
        )

    @staticmethod
    def _clear_screenshot(session: _Session) -> None:
        session.screenshot_id = ""
        session.screenshot_host_epoch = ""
        session.screenshot_generation = 0
        session.screenshot_path = ""
        session.screenshot_marker = ""
        session.screenshot_css_width = 0
        session.screenshot_css_height = 0
        session.screenshot_dpr = 1
        session.screenshot_coordinates_allowed = False

    @staticmethod
    def _guard_expression(tab: _Tab, *, reset: bool) -> str:
        key = json.dumps(tab.guard_property)
        token = json.dumps(tab.guard_token)
        state = (
            "{token:s.token,counter:s.counter,href:location.href,timeOrigin:performance.timeOrigin,"
            "scrollX:window.scrollX,scrollY:window.scrollY,width:window.innerWidth,height:window.innerHeight,"
            "dpr:window.devicePixelRatio}"
        )
        if not reset:
            return f"(()=>{{const s=globalThis[{key}]||{{token:'',counter:-1}};return JSON.stringify({state})}})()"
        return (
            "(()=>{"
            f"const k={key};const old=globalThis[k];if(old&&old.observer)old.observer.disconnect();"
            f"const s={{token:{token},counter:0,observer:null}};"
            "s.observer=new MutationObserver(()=>{s.counter+=1});"
            "s.observer.observe(document,{subtree:true,childList:true,attributes:true,characterData:true});"
            "Object.defineProperty(globalThis,k,{value:s,configurable:true});"
            f"return JSON.stringify({state})"
            "})()"
        )

    async def _page_guard(
        self,
        owner: _Owner,
        session: _Session,
        *,
        reset: bool,
        include_security: bool = False,
        timeout_seconds: float | None = None,
        workdir: str,
    ) -> str:
        tab = self._active_tab(session)
        owner.last_activity = time.monotonic()
        try:
            guard_kwargs: dict[str, Any] = {
                "target_id": tab.target_id,
                "state_key": tab.guard_property,
                "state_token": tab.guard_token,
                "reset": reset,
                "timeout": (
                    max(0.001, float(timeout_seconds))
                    if timeout_seconds is not None
                    else self.config.command_timeout_seconds
                ),
                "proxy_url": owner.proxy.url if owner.proxy else "",
                "download_dir": self._download_quarantine(owner),
            }
            # Keep the optional optimization source-compatible with external
            # BrowserDriver subclasses that implemented the older exact
            # page_guard signature. Only the bundled Electron driver consumes
            # the lightweight/full-security distinction.
            if isinstance(self.driver, ElectronBrowserDriver):
                guard_kwargs["include_security"] = include_security
            marker = await self.driver.page_guard(
                owner.runtime_key,
                owner.profile_dir,
                **guard_kwargs,
            )
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, session, exc)
            raise
        except BrowserDriverError as exc:
            await self._raise_driver_error(owner, session, exc)
            raise AssertionError("unreachable")
        if marker is not None:
            owner.running = True
            session.last_error = ""
            return marker

        # Compatibility path for deterministic test/alternative drivers. The
        # production Electron driver returns an isolated-world marker instead.
        return _text(
            await self._run(
                owner,
                session,
                "eval",
                [self._guard_expression(tab, reset=reset)],
                workdir=workdir,
                timeout_seconds=timeout_seconds,
            )
        )

    async def _observe_after_mutation(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str,
    ) -> str:
        if session.defer_post_observation or _POST_OBSERVATION_DEFERRED.get():
            # 批量中间步骤：不重新 snapshot（换代会让后续预规划 ref 全部失效）。
            # 仍做一次 _select 对齐弹窗/活动标签页；坐标截图随页面变化失效照旧。
            # 对话框等待等异常会推迟到下一个动作或末步观察时暴露，不会丢。
            await self._select(owner, session)
            self._clear_screenshot(session)
            return (
                DEFERRED_OBSERVATION_NOTE
                if session.defer_post_observation
                else DEFERRED_SINGLE_OBSERVATION_NOTE
            )
        try:
            # Navigation/click/input events can synchronously open and activate
            # an unlabeled popup. Reconcile against native tab state before any
            # post-action dialog check, metadata read, or snapshot.
            await self._select(owner, session)
            return await self._snapshot_locked(owner, session, full=False, workdir=workdir)
        except BrowserDriverError as exc:
            if exc.uncertain or getattr(exc, "code", "") in {
                "dialog_pending",
                "file_chooser_pending",
            }:
                raise
            raise BrowserDriverError(
                f"动作已发送，但后置页面观察失败：{exc}；结果未知，请重新观察，Crew 不会自动重复该动作",
                uncertain=True,
                # 原始 code 必须带上：失败归因表（_FAILURE_CODE_CLASSES）是**按
                # code 优先**分类的，丢了它这一整类失败都会退化成 unknown，
                # 模型拿到的建议就变成"无法归类"——而底层其实已经说清楚了原因。
                code=getattr(exc, "code", "") or "post_action_observation_failed",
                phase=getattr(exc, "phase", ""),
                partial=bool(getattr(exc, "partial", False)),
            ) from None

    @staticmethod
    def _require_ai(
        owner: _Owner,
        session: _Session,
    ) -> None:
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；请先交还 AI 后再执行浏览器动作")
        if session.mode == "human":
            raise BrowserDriverError("用户正在接管浏览器，AI 动作已暂停")
        if session.mode == "paused":
            raise BrowserDriverError("浏览器动作已暂停")

    @_observed_browser("browser.navigate")
    async def navigate(self, owner_id: str, session_id: str, url: str, *, workdir: str = "") -> str:
        safe_url = self.policy.validate_navigation_url(url)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            if not session.tabs:
                previous_active = session.active_label
                tab = self._new_tab(session)
                try:
                    await self._run(
                        owner,
                        session,
                        "tab",
                        ["new", "--label", tab.label, safe_url],
                        navigation=True,
                        mutating=True,
                        workdir=workdir,
                    )
                except BrowserDriverError as exc:
                    if not (
                        exc.uncertain or exc.browser_stopped or exc.stop_unconfirmed
                    ):
                        self._rollback_new_tab(session, tab, previous_active)
                    raise
                owner.selected_label = tab.label
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
            else:
                tab, _ = await self._select(owner, session)
                await self._run(
                    owner,
                    session,
                    "open",
                    [safe_url],
                    navigation=True,
                    mutating=True,
                    workdir=workdir,
                )
            tab.url = safe_url
            session.last_action = f"导航到 {_public_url(safe_url)}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    @_observed_browser("browser.snapshot")
    async def snapshot(
        self, owner_id: str, session_id: str, *, full: bool = False, workdir: str = ""
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            return await self._snapshot_locked(owner, session, full=full, workdir=workdir)

    @staticmethod
    def _validated_find_query(
        text: Any,
        regex: Any,
    ) -> tuple[str, str]:
        if text is not None and not isinstance(text, str):
            raise BrowserDriverError(
                'browser_find 的 "text" 必须是字符串',
                code="invalid_find_query",
            )
        if regex is not None and not isinstance(regex, str):
            raise BrowserDriverError(
                'browser_find 的 "regex" 必须是字符串',
                code="invalid_find_query",
            )
        has_text = isinstance(text, str) and bool(text)
        has_regex = isinstance(regex, str) and bool(regex)
        if has_text == has_regex:
            message = (
                'browser_find 只能提供 "text" 或 "regex" 其中一个'
                if has_text
                else 'browser_find 必须提供 "text" 或 "regex"'
            )
            raise BrowserDriverError(message, code="invalid_find_query")
        return (
            ("--regex", str(regex))
            if has_regex
            else ("--text", str(text))
        )

    @_observed_browser("browser.find")
    async def find(
        self,
        owner_id: str,
        session_id: str,
        *,
        text: Any = None,
        regex: Any = None,
        workdir: str = "",
    ) -> str:
        flag, query = self._validated_find_query(text, regex)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            return await self._snapshot_locked(
                owner,
                session,
                full=False,
                workdir=workdir,
                command="find",
                command_args=[flag, query],
            )

    async def _snapshot_locked(
        self,
        owner: _Owner,
        session: _Session,
        *,
        full: bool,
        workdir: str,
        require_capture: bool = False,
        command: str = "snapshot",
        command_args: list[str] | None = None,
    ) -> str:
        """Publish one Host snapshot as a new public document generation.

        The Host replaces its Playwright ref table during capture.  If parsing
        or transport fails afterward, Python must invalidate the matching
        public ``pN`` namespace instead of retaining refs that the Host no
        longer knows.  This is generation consistency, not a page-security
        scan, and requires no renderer round trip.
        """
        starting_generation = session.generation
        try:
            return await self._snapshot_transaction_locked(
                owner,
                session,
                full=full,
                workdir=workdir,
                require_capture=require_capture,
                command=command,
                command_args=command_args,
            )
        except BaseException:
            # Never let a failed Host capture reuse the same public pN
            # namespace on the next successful snapshot.
            if session.generation == starting_generation:
                session.generation += 1
            self._clear_ref_state(session)
            self._clear_screenshot(session)
            session.page_marker = ""
            owner.native_ref_session = ""
            owner.native_ref_generation = 0
            raise

    async def _snapshot_transaction_locked(
        self,
        owner: _Owner,
        session: _Session,
        *,
        full: bool,
        workdir: str,
        require_capture: bool = False,
        command: str = "snapshot",
        command_args: list[str] | None = None,
    ) -> str:
        del require_capture
        # BrowserHost wraps the command in its session-modal race and
        # Playwright captures the accessibility tree and ref table in one
        # operation.  A separate dialog poll plus pre/post/final page_guard
        # sequence was redundant and could never make a later snapshot atomic;
        # it only added renderer round trips and quiet-period latency.
        args = (
            list(command_args)
            if command_args is not None
            else ["--compact"]
            if not full
            else []
        )
        result = await self._run(owner, session, command, args, workdir=workdir)
        result_value = _data(result)
        result_data = result_value if isinstance(result_value, dict) else None
        raw = _text(result)
        generation = session.generation + 1

        # The public generation needs only Playwright's native @eN token.
        # Crew-specific fingerprint/role/action maps are intentionally absent
        # from the functional wire contract.
        allowed_native_refs = {
            (
                match.group(1)
                if match.group(1).startswith("@")
                else f"@{match.group(1)}"
            )
            for match in _SNAPSHOT_REF_TOKEN.finditer(raw)
        }
        structural_by_line: dict[int, tuple[int, int, str]] = {}
        native_counts: dict[str, int] = {}
        raw_lines = raw.splitlines()
        for line_index, raw_line in enumerate(raw_lines):
            structural = self._snapshot_structural_ref(raw_line, allowed_native_refs)
            if structural is None:
                continue
            structural_by_line[line_index] = structural
            native = structural[2]
            native_counts[native] = native_counts.get(native, 0) + 1
        if any(count != 1 for count in native_counts.values()):
            self._invalidate_observation(session)
            owner.native_ref_session = ""
            owner.native_ref_generation = 0
            raise BrowserDriverError("宿主 snapshot 含重复的原生 ref")

        ref_nonce = uuid.uuid4().hex
        placeholder_refs: dict[str, tuple[str, str]] = {}
        controlled_lines: list[str] = []
        for line_index, raw_line in enumerate(raw_lines):
            structural = structural_by_line.get(line_index)
            controlled = raw_line
            if structural is not None:
                token_start, token_end, native = structural
                number = native.removeprefix("@e")
                crew_ref = f"p{generation}:e{number}"
                placeholder = f"__CREW_REF_{ref_nonce}_{number}__"
                placeholder_refs[placeholder] = (crew_ref, native)
                controlled = (
                    raw_line[:token_start] + placeholder + raw_line[token_end:]
                )
            # Everything not explicitly replaced by a Host-authorized placeholder
            # remains page text, including accessible names that mimic ref syntax.
            controlled_lines.append(controlled.replace("[ref=", "[page-ref="))

        complete_snapshot = "\n".join(controlled_lines)
        placeholder_pattern = re.compile(rf"__CREW_REF_{re.escape(ref_nonce)}_(\d+)__")
        output_lines: list[str] = []
        pending_refs: dict[str, str] = {}
        for safe_line in complete_snapshot.splitlines():
            found = list(placeholder_pattern.finditer(safe_line))
            rendered = placeholder_pattern.sub(
                lambda match: f"[ref=p{generation}:e{match.group(1)}]",
                safe_line,
            )
            output_lines.append(rendered)
            for match in found:
                placeholder = match.group(0)
                crew_ref, native = placeholder_refs[placeholder]
                pending_refs[crew_ref] = f"{native}\n{rendered.strip()}"
        bounded = "\n".join(output_lines)
        if result_data is not None:
            tab = self._active_tab(session)
            if isinstance(result_data.get("url"), str):
                tab.url = str(result_data.get("url") or tab.url)
            if isinstance(result_data.get("title"), str):
                tab.title = str(result_data.get("title") or tab.title)
            if type(result_data.get("can_go_back")) is bool:
                session.can_go_back = result_data["can_go_back"]
            if type(result_data.get("can_go_forward")) is bool:
                session.can_go_forward = result_data["can_go_forward"]

        # Commit one new public generation only after the atomic Host snapshot
        # has been parsed completely.  Failed captures still invalidate the
        # candidate in _snapshot_locked.
        session.generation = generation
        session.refs = pending_refs
        # 提交类控件的标记与这一代 ref 表同时提交。
        #
        # 键是宿主的 native ref（`@eN`），与 session.refs 的值同一命名空间；
        # 只保留本代 ref 表里真实存在的那些，防止宿主多给的键留在会话里。
        session.ref_actions = {}
        if isinstance(result_data, dict):
            raw_actions = result_data.get("ref_actions")
            if isinstance(raw_actions, dict):
                # session.refs 的值是 "native\n渲染行" 复合串（见 _native_ref），
                # 不是裸 native ref。直接拿整串比对会全部落空——实测过。
                live_natives = {
                    str(stored).split("\n", 1)[0] for stored in pending_refs.values()
                }
                session.ref_actions = {
                    str(native): str(kind)
                    for native, kind in raw_actions.items()
                    if isinstance(native, str)
                    and isinstance(kind, str)
                    and native in live_natives
                }
        self._clear_screenshot(session)
        session.page_marker = ""
        owner.native_ref_session = session.session_id
        owner.native_ref_generation = generation
        await self._publish(
            owner.owner,
            session.session_id,
            {"type": "state", "state": self._page_state(owner, session).public_dict()},
        )
        # title 与 url 均取自页面（title 见 _snapshot_locked 里 tab.title=页面标题），
        # 是不可信数据，必须与正文走同一转义，否则页面可在 title 里塞
        # </untrusted_browser_content> 逃出隔离区并伪造 <browser_action_result> 信封。
        title = _escape_wrapper_markers(self._active_tab(session).title)
        url = _escape_tag_markers(_public_url(self._active_tab(session).url))
        boundary_safe, truncation = _truncate_snapshot_at_line(
            _escape_wrapper_markers(bounded),
            self.config.max_output_chars,
            full=full,
        )
        # 截断说明走 Crew 独占的头部位置（与 page_generation 同级），不混进正文——
        # 混进正文页面就能用元素名伪造同样的句子来制造「什么都没看全」的假象。
        # 宿主侧的截断（AX 节点上限、ref 上限、文本上限）与 Crew 侧的输出上限
        # 是两回事，两者都要如实报出去。宿主静默截断时模型会以为「这一页就这么
        # 多内容」，从而基于半张页面下结论。
        host_truncation = ""
        if isinstance(result_data, dict):
            host_truncation = str(result_data.get("truncated") or "")
        reasons = [item for item in (host_truncation, truncation) if item]
        truncated_line = f"truncated: {'; '.join(reasons)}\n" if reasons else ""
        return (
            "<untrusted_browser_content>\n"
            f"page_generation: p{generation}\nurl: {url}\n"
            f"title: {title}\n{truncated_line}{boundary_safe}\n"
            "</untrusted_browser_content>"
        )

    async def _lock_downloads(self, owner: _Owner) -> None:
        # Native page downloads are ordinary browser behavior. The explicit
        # download RPC only chooses a save path for its matching item; there is
        # no global deny policy to restore between actions.
        owner.downloads_locked = True

    @staticmethod
    async def _complete_critical(awaitable: Any) -> bool:
        """Finish a security cleanup even if the caller is cancelled.

        Returns whether cancellation was deferred.  Callers must restore their
        fail-closed state first and then re-raise ``CancelledError``.
        """
        task = asyncio.create_task(awaitable)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        # Propagate the cleanup's own error after it has definitely stopped.
        task.result()
        return cancelled

    async def _clear_human_buffers(self, owner: _Owner, session: _Session) -> None:
        """把浏览器交给用户之前，清掉模型可读的调试缓冲。

        **只服务 `_open_for_user_with_control` 这一条路径。** takeover/return
        刻意不清——那两处会让模型丢掉接管前收集的调试历史，而排障时正需要它
        跨越一次接管继续可用（见 test_takeover_does_not_clear_browser_debug_history）。
        「用户点面板『在浏览器中打开』」不是排障场景，是把浏览器整个交出去，
        清掉是对的。

        这个方法曾被整段删掉而调用点还留着，触发路径是「AI 用过浏览器 → 用户
        点『在浏览器中打开』」，直接 AttributeError → 路由只捕获
        BrowserDriverError/ValueError → 500。既有两个用例恰好都绕开了这条分支
        （一个无标签页、一个已是 human 模式）。
        """
        await self._select(owner, session)
        await self._run(owner, session, "console", ["--clear"])
        await self._run(owner, session, "network", ["requests", "--clear"])

    async def _set_driver_mode(self, owner: _Owner, session: _Session, mode: str) -> None:
        """Synchronize control mode with an Electron-owned browser surface."""
        set_mode = getattr(self.driver, "set_mode", None)
        if not callable(set_mode):
            return
        tab = session.tabs.get(session.active_label)
        if tab is None or not tab.target_id:
            if mode == "paused":
                return
            raise BrowserDriverError("当前标签页缺少不可伪造的 targetId，无法切换浏览器控制权")
        try:
            result = set_mode(
                owner.runtime_key,
                owner.profile_dir,
                target_id=tab.target_id,
                mode=mode,
            )
            if inspect.isawaitable(result):
                await result
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, session, exc)
            raise
        except BrowserDriverError as exc:
            await self._raise_driver_error(owner, session, exc)
            raise AssertionError("unreachable")

    async def _refresh_metadata(self, owner: _Owner, session: _Session, *, workdir: str) -> None:
        tab = self._active_tab(session)
        for field_name, args in (("url", ["url"]), ("title", ["title"])):
            try:
                value = (
                    _text(await self._run(owner, session, "get", args, workdir=workdir))
                    .strip()
                    .strip('"')
                )
                setattr(tab, field_name, value)
            except BrowserDriverError:
                pass
        try:
            history = _data(
                await self._run(owner, session, "get", ["history"], workdir=workdir)
            )
            session.can_go_back = history.get("can_go_back") is True
            session.can_go_forward = history.get("can_go_forward") is True
        except BrowserDriverError:
            session.can_go_back = False
            session.can_go_forward = False

    @staticmethod
    def _active_tab(session: _Session) -> _Tab:
        tab = session.tabs.get(session.active_label)
        if tab is None:
            raise BrowserDriverError("当前会话没有活动标签页")
        return tab

    @staticmethod
    def _public_tab(tab: _Tab) -> dict[str, str]:
        return {
            "id": tab.id,
            "label": tab.label,
            "url": _public_url(tab.url),
            "title": tab.title,
        }

    @staticmethod
    def _native_ref(session: _Session, value: str) -> str:
        parsed = BrowserRef.parse(value)
        if parsed.generation != session.generation:
            raise BrowserDriverError(
                "元素 ref 已失效；页面已变化，请调用 browser_use 的 snapshot action"
            )
        stored = session.refs.get(str(parsed))
        if not stored:
            raise BrowserDriverError("元素 ref 不属于当前页面或当前会话")
        return stored.split("\n", 1)[0]

    async def _action(
        self,
        owner_id: str,
        session_id: str,
        command: str,
        args: list[str],
        description: str,
        *,
        workdir: str = "",
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            await self._run(owner, session, command, args, mutating=True, workdir=workdir)
            session.last_action = description
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    @staticmethod
    def _validated_click_options(
        button: Any,
        click_count: Any,
        modifiers: Any,
        delay_ms: Any,
    ) -> tuple[str, int, list[str], int]:
        if button not in _CLICK_BUTTONS:
            raise BrowserDriverError("click button 必须是 left/right/middle")
        if type(click_count) is not int or click_count < 1:
            raise BrowserDriverError("click_count 必须是正整数")
        if type(delay_ms) is not int or delay_ms < 0:
            raise BrowserDriverError("delay_ms 必须是非负整数")
        if (
            not isinstance(modifiers, list)
            or len(modifiers) > len(_CLICK_MODIFIERS)
            or len(set(modifiers)) != len(modifiers)
            or any(
                not isinstance(modifier, str)
                or modifier not in _CLICK_MODIFIERS
                for modifier in modifiers
            )
        ):
            raise BrowserDriverError("modifiers 包含无效或重复的修饰键")
        return str(button), click_count, list(modifiers), delay_ms

    @staticmethod
    def _validated_finite_number(value: Any, field_name: str) -> int | float:
        """Validate a JSON number without Python's bool-as-int coercion.

        Playwright's public mouse/viewport tools accept arbitrary finite
        coordinates and deltas, including fractional and negative values.  Do
        not add viewport bounds or product-level magnitude caps here; Chromium
        and Playwright own the operation-specific constraints.
        """
        if type(value) not in {int, float}:
            raise BrowserDriverError(f"{field_name} 必须是有限数字")
        try:
            finite = math.isfinite(float(value))
        except (OverflowError, ValueError):
            finite = False
        if not finite:
            raise BrowserDriverError(f"{field_name} 必须是有限数字")
        return value

    @staticmethod
    def _wire_number(value: int | float) -> str:
        # Preserve integer spellings where the caller supplied an integer.
        # This keeps the argv protocol exact without narrowing fractional
        # coordinates accepted by Playwright.
        return str(value)

    @staticmethod
    def _validated_mouse_button(button: Any) -> str:
        if not isinstance(button, str) or button not in _CLICK_BUTTONS:
            raise BrowserDriverError("mouse button 必须是 left/right/middle")
        return button

    @classmethod
    def _validated_mouse_click_options(
        cls,
        button: Any,
        click_count: Any,
        delay_ms: Any,
    ) -> tuple[str, int, int | float]:
        checked_button = cls._validated_mouse_button(button)
        if type(click_count) is not int or click_count < 1:
            raise BrowserDriverError("mouse click_count 必须是正整数")
        checked_delay = cls._validated_finite_number(
            delay_ms,
            "mouse delay_ms",
        )
        if checked_delay < 0:
            raise BrowserDriverError("mouse delay_ms 必须是非负有限数字")
        return checked_button, click_count, checked_delay

    @staticmethod
    def _validated_key(key: Any) -> str:
        if (
            not isinstance(key, str)
            or not key
            or _INVALID_KEY_CHARACTERS.search(key)
        ):
            raise BrowserDriverError("key 必须是非空且不含控制字符的字符串")
        return key

    @staticmethod
    def _validated_wait(
        time_seconds: Any,
        text: Any,
        text_gone: Any,
    ) -> tuple[float, str, str]:
        if (
            type(time_seconds) not in {int, float}
            or isinstance(time_seconds, bool)
            or not math.isfinite(float(time_seconds))
            or float(time_seconds) < 0
        ):
            raise BrowserDriverError("time_seconds 必须是非负有限数字")
        if not isinstance(text, str) or not isinstance(text_gone, str):
            raise BrowserDriverError("等待文本必须是字符串")
        seconds = float(time_seconds)
        if seconds == 0 and not text and not text_gone:
            raise BrowserDriverError(
                "wait 至少需要 time_seconds、text 或 text_gone 之一"
            )
        return seconds, text, text_gone

    @_observed_browser("browser.click")
    async def click(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        *,
        button: str = "left",
        click_count: int = 1,
        modifiers: list[str] | None = None,
        delay_ms: int = 0,
        workdir: str = "",
    ) -> str:
        checked_button, checked_count, checked_modifiers, checked_delay = (
            self._validated_click_options(
                button,
                click_count,
                [] if modifiers is None else modifiers,
                delay_ms,
            )
        )
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            native = self._native_ref(session, ref)
            description = (
                f"点击 {ref}"
                if checked_button == "left" and checked_count == 1
                else f"{checked_button} 点击 {ref} ×{checked_count}"
            )
            # Preserve the historical one-argument command for default clicks;
            # optional flags are additive so alternative drivers remain
            # compatible until they opt into the richer Playwright surface.
            click_args = [native]
            if checked_button != "left":
                click_args.extend(["--button", checked_button])
            if checked_count != 1:
                click_args.extend(["--click-count", str(checked_count)])
            if checked_delay:
                click_args.extend(["--delay-ms", str(checked_delay)])
            for modifier in checked_modifiers:
                click_args.extend(["--modifier", modifier])
            await self._run(
                owner,
                session,
                "click",
                click_args,
                mutating=True,
                workdir=workdir,
            )
            session.last_action = description
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def _mouse_action(
        self,
        owner_id: str,
        session_id: str,
        subaction: str,
        args: list[str],
        description: str,
        *,
        include_snapshot: bool,
        workdir: str,
    ) -> str:
        """Dispatch one official Playwright ``page.mouse`` operation."""
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            # Coordinate mouse input is intentionally independent of a prior
            # snapshot/screenshot epoch. Select only the owned page; do not pay
            # for or gate on a page-guard round trip.
            await self._select(owner, session)
            await self._run(
                owner,
                session,
                "mouse",
                [subaction, *args],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = description
            if include_snapshot:
                return await self._observe_after_mutation(
                    owner,
                    session,
                    workdir=workdir,
                )

            # Match Playwright's lean response surface: do not force a fresh
            # accessibility snapshot or discard its live locators.  A later
            # exact-ref action is still re-resolved by Playwright.  Screenshot
            # coordinates, unlike locators, are pixel/viewport-bound and must
            # never survive any direct mouse input.
            self._clear_screenshot(session)
            return description

    async def mouse_move(
        self,
        owner_id: str,
        session_id: str,
        x: int | float,
        y: int | float,
        *,
        workdir: str = "",
    ) -> str:
        checked_x = self._validated_finite_number(x, "mouse x")
        checked_y = self._validated_finite_number(y, "mouse y")
        return await self._mouse_action(
            owner_id,
            session_id,
            "move",
            [self._wire_number(checked_x), self._wire_number(checked_y)],
            f"鼠标已移动到 ({checked_x}, {checked_y})",
            include_snapshot=False,
            workdir=workdir,
        )

    async def mouse_down(
        self,
        owner_id: str,
        session_id: str,
        button: str = "left",
        *,
        workdir: str = "",
    ) -> str:
        checked_button = self._validated_mouse_button(button)
        return await self._mouse_action(
            owner_id,
            session_id,
            "down",
            [checked_button],
            f"鼠标 {checked_button} 键已按下",
            include_snapshot=False,
            workdir=workdir,
        )

    async def mouse_up(
        self,
        owner_id: str,
        session_id: str,
        button: str = "left",
        *,
        workdir: str = "",
    ) -> str:
        checked_button = self._validated_mouse_button(button)
        return await self._mouse_action(
            owner_id,
            session_id,
            "up",
            [checked_button],
            f"鼠标 {checked_button} 键已释放",
            include_snapshot=False,
            workdir=workdir,
        )

    async def mouse_wheel(
        self,
        owner_id: str,
        session_id: str,
        delta_x: int | float = 0,
        delta_y: int | float = 0,
        *,
        workdir: str = "",
    ) -> str:
        checked_x = self._validated_finite_number(delta_x, "mouse delta_x")
        checked_y = self._validated_finite_number(delta_y, "mouse delta_y")
        return await self._mouse_action(
            owner_id,
            session_id,
            "wheel",
            [self._wire_number(checked_x), self._wire_number(checked_y)],
            f"鼠标滚轮已滚动 ({checked_x}, {checked_y})",
            include_snapshot=False,
            workdir=workdir,
        )

    async def mouse_click(
        self,
        owner_id: str,
        session_id: str,
        x: int | float,
        y: int | float,
        *,
        button: str = "left",
        click_count: int = 1,
        delay_ms: int | float = 0,
        workdir: str = "",
    ) -> str:
        checked_x = self._validated_finite_number(x, "mouse x")
        checked_y = self._validated_finite_number(y, "mouse y")
        checked_button, checked_count, checked_delay = (
            self._validated_mouse_click_options(
                button,
                click_count,
                delay_ms,
            )
        )
        return await self._mouse_action(
            owner_id,
            session_id,
            "click",
            [
                self._wire_number(checked_x),
                self._wire_number(checked_y),
                checked_button,
                str(checked_count),
                self._wire_number(checked_delay),
            ],
            f"鼠标在 ({checked_x}, {checked_y}) 点击",
            include_snapshot=True,
            workdir=workdir,
        )

    async def mouse_drag(
        self,
        owner_id: str,
        session_id: str,
        start_x: int | float,
        start_y: int | float,
        end_x: int | float,
        end_y: int | float,
        *,
        workdir: str = "",
    ) -> str:
        checked_start_x = self._validated_finite_number(start_x, "mouse start_x")
        checked_start_y = self._validated_finite_number(start_y, "mouse start_y")
        checked_end_x = self._validated_finite_number(end_x, "mouse end_x")
        checked_end_y = self._validated_finite_number(end_y, "mouse end_y")
        return await self._mouse_action(
            owner_id,
            session_id,
            "drag",
            [
                self._wire_number(checked_start_x),
                self._wire_number(checked_start_y),
                self._wire_number(checked_end_x),
                self._wire_number(checked_end_y),
            ],
            (
                f"鼠标已从 ({checked_start_x}, {checked_start_y}) "
                f"拖动到 ({checked_end_x}, {checked_end_y})"
            ),
            include_snapshot=True,
            workdir=workdir,
        )

    async def resize(
        self,
        owner_id: str,
        session_id: str,
        width: int | float,
        height: int | float,
        *,
        workdir: str = "",
    ) -> str:
        checked_width = self._validated_finite_number(width, "resize width")
        checked_height = self._validated_finite_number(height, "resize height")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            await self._run(
                owner,
                session,
                "resize",
                [
                    self._wire_number(checked_width),
                    self._wire_number(checked_height),
                ],
                mutating=True,
                workdir=workdir,
            )
            description = (
                f"浏览器视口已调整为 {checked_width} × {checked_height}"
            )
            session.last_action = description
            # Resizing does not intrinsically invalidate Playwright locators,
            # but it always invalidates pixel coordinates captured earlier.
            self._clear_screenshot(session)
            return description

    async def fill(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        text: str,
        *,
        submit: bool = False,
        slowly: bool = False,
        workdir: str = "",
    ) -> str:
        if not isinstance(text, str):
            raise BrowserDriverError("type text 必须是字符串")
        if type(submit) is not bool or type(slowly) is not bool:
            raise BrowserDriverError("type submit/slowly 必须是 boolean")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            # 先按公共 generation 校验并选中本会话拥有的标签页，再解析 ref。
            # 不做这一步，跨会话遗留的 @eN 可能解析到**另一个会话**同序号的元素
            # ——症状是内容填进了错误的窗口，而且不报错。
            await self._select_checked(owner, session, workdir=workdir)
            native = self._native_ref(session, ref)
            fill_args = [native, text]
            if slowly:
                fill_args.append("--slowly")
            if submit:
                fill_args.append("--submit")
            await self._run(
                owner, session, "fill", fill_args, mutating=True, workdir=workdir
            )
            mode = "逐字输入" if slowly else "填写"
            session.last_action = f"{mode}并提交 {ref}" if submit else f"{mode} {ref}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    @staticmethod
    def _validated_fill_form_fields(fields: Any) -> list[dict[str, Any]]:
        """Normalize the public typed contract without widening coercions."""
        if not isinstance(fields, list):
            raise BrowserDriverError("fill_form fields 必须是数组")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(fields):
            if not isinstance(raw, dict):
                raise BrowserDriverError(f"fill_form 第 {index + 1} 项必须是 object")
            field_type = raw.get("type")
            ref = raw.get("ref")
            if (
                field_type not in {
                    "textbox",
                    "combobox",
                    "checkbox",
                    "radio",
                    "slider",
                }
                or not isinstance(ref, str)
                or not ref
            ):
                raise BrowserDriverError(f"fill_form 第 {index + 1} 项的 type/ref 无效")
            if field_type in {"textbox", "slider"}:
                if (
                    set(raw) != {"type", "ref", "value"}
                    or not isinstance(raw.get("value"), str)
                    or (field_type == "slider" and not raw["value"])
                ):
                    raise BrowserDriverError(
                        f"fill_form 第 {index + 1} 项 {field_type} value "
                        "必须是字符串"
                    )
                normalized.append(
                    {"type": field_type, "ref": ref, "value": raw["value"]}
                )
            elif field_type == "combobox":
                if (
                    set(raw) != {"type", "ref", "value", "select_by"}
                    or not isinstance(raw.get("value"), str)
                    or raw.get("select_by") not in {"label", "value"}
                ):
                    raise BrowserDriverError(
                        f"fill_form 第 {index + 1} 项 combobox 必须显式提供"
                        " select_by=label|value 和字符串 value"
                    )
                normalized.append(
                    {
                        "type": "combobox",
                        "ref": ref,
                        "value": raw["value"],
                        "select_by": raw["select_by"],
                    }
                )
            else:
                if (
                    set(raw) != {"type", "ref", "value"}
                    or type(raw.get("value")) is not bool
                ):
                    raise BrowserDriverError(
                        f"fill_form 第 {index + 1} 项 {field_type} value 必须是 boolean"
                    )
                normalized.append(
                    {"type": field_type, "ref": ref, "value": raw["value"]}
                )
        return normalized

    async def fill_form(
        self,
        owner_id: str,
        session_id: str,
        fields: list[dict[str, Any]],
        *,
        workdir: str = "",
    ) -> str:
        """Execute one typed, ordered form batch and never submit the form."""
        checked_fields = self._validated_fill_form_fields(fields)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)

            wire_fields: list[dict[str, Any]] = []
            for field in checked_fields:
                ref = str(field["ref"])
                try:
                    native = self._native_ref(session, ref)
                except ValueError as exc:
                    raise BrowserDriverError(str(exc)) from None
                wire = {key: value for key, value in field.items() if key != "ref"}
                wire["ref"] = native
                wire_fields.append(wire)

            await self._run_fill_form(
                owner,
                session,
                wire_fields,
                workdir=workdir,
            )
            session.last_action = f"批量填写 {len(checked_fields)} 项"
            try:
                # Return the real guarded final snapshot.  Page-owned content
                # may legitimately render a formatted business result; only
                # Crew's UI/history/trace argument surfaces hide runtime values.
                return await self._observe_after_mutation(
                    owner,
                    session,
                    workdir=workdir,
                )
            except BrowserDriverError as exc:
                if getattr(exc, "code", "") in {
                    "dialog_pending",
                    "file_chooser_pending",
                }:
                    exc.partial = True
                    exc.completed_count = len(checked_fields)
                    raise
                raise BrowserDriverError(
                    "批量表单字段已全部填写，但最终页面观察失败；"
                    "结果未知，请重新观察且不要自动重复填写",
                    uncertain=True,
                    code=getattr(exc, "code", ""),
                    phase=getattr(exc, "phase", ""),
                    partial=True,
                    completed_count=len(checked_fields),
                ) from None

    @staticmethod
    def _validated_select_values(values: Any) -> list[str]:
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) for value in values)
        ):
            raise BrowserDriverError("select values 必须是字符串数组")
        return list(values)

    async def _locator_ref_action(
        self,
        owner_id: str,
        session_id: str,
        *,
        ref: str,
        action: str,
        command: str,
        command_tail: list[str],
        description: str,
        workdir: str,
    ) -> str:
        """Run one ref through the driver's exact Playwright Locator."""
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            native = self._native_ref(session, ref)
            await self._run(
                owner,
                session,
                command,
                [native, *command_tail],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = description
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def drag(
        self,
        owner_id: str,
        session_id: str,
        start_ref: str,
        end_ref: str,
        *,
        workdir: str = "",
    ) -> str:
        if not start_ref or not end_ref:
            raise BrowserDriverError("drag 必须同时提供 start_ref 和 end_ref")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            # Resolve both Crew refs under the same owner lock and snapshot
            # generation before Host strict-counts both exact locators.
            start_native = self._native_ref(session, start_ref)
            end_native = self._native_ref(session, end_ref)
            await self._run(
                owner,
                session,
                "drag",
                [start_native, end_native],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = f"拖动 {start_ref} 到 {end_ref}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def select(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        values: list[str],
        *,
        workdir: str = "",
    ) -> str:
        checked_values = self._validated_select_values(values)
        return await self._locator_ref_action(
            owner_id,
            session_id,
            ref=ref,
            action="select",
            command="select",
            command_tail=checked_values,
            description=f"选择 {ref}",
            workdir=workdir,
        )

    async def check(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        checked: bool,
        *,
        workdir: str = "",
    ) -> str:
        if type(checked) is not bool:
            raise BrowserDriverError("check checked 必须是 boolean")
        return await self._locator_ref_action(
            owner_id,
            session_id,
            ref=ref,
            action="check",
            command="check",
            command_tail=["true" if checked else "false"],
            description=f"{'选中' if checked else '取消选中'} {ref}",
            workdir=workdir,
        )

    async def hover(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        *,
        workdir: str = "",
    ) -> str:
        return await self._locator_ref_action(
            owner_id,
            session_id,
            ref=ref,
            action="hover",
            command="hover",
            command_tail=[],
            description=f"悬停 {ref}",
            workdir=workdir,
        )

    async def scroll(
        self, owner_id: str, session_id: str, direction: str, pixels: int, *, workdir: str = ""
    ) -> str:
        if direction not in {"up", "down", "left", "right"}:
            raise BrowserDriverError("scroll direction 无效")
        if type(pixels) is not int or pixels <= 0:
            raise BrowserDriverError("scroll pixels 必须是正整数")
        return await self._action(
            owner_id,
            session_id,
            "scroll",
            [direction, str(pixels)],
            f"向{direction}滚动",
            workdir=workdir,
        )

    async def _history_action(
        self,
        owner_id: str,
        session_id: str,
        command: str,
        description: str,
        *,
        workdir: str,
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            await self._run(
                owner,
                session,
                command,
                [],
                navigation=True,
                mutating=True,
                workdir=workdir,
            )
            session.last_action = description
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def back(self, owner_id: str, session_id: str, *, workdir: str = "") -> str:
        return await self._history_action(
            owner_id,
            session_id,
            "back",
            "后退",
            workdir=workdir,
        )

    async def forward(self, owner_id: str, session_id: str, *, workdir: str = "") -> str:
        return await self._history_action(
            owner_id,
            session_id,
            "forward",
            "前进",
            workdir=workdir,
        )

    async def reload(self, owner_id: str, session_id: str, *, workdir: str = "") -> str:
        return await self._history_action(
            owner_id,
            session_id,
            "reload",
            "重新加载",
            workdir=workdir,
        )

    async def press(
        self,
        owner_id: str,
        session_id: str,
        key: str,
        *,
        ref: str = "",
        workdir: str = "",
    ) -> str:
        checked_key = self._validated_key(key)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            # 空 ref 表示页面级按键（焦点在哪就发给哪），不解析元素。
            native = self._native_ref(session, ref) if ref else ""
            await self._run(
                owner,
                session,
                "press",
                [checked_key, native] if native else [checked_key],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = (
                f"在 {ref} 按键 {checked_key}" if ref else f"按键 {checked_key}"
            )
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def _keyboard_transition(
        self,
        owner_id: str,
        session_id: str,
        command: str,
        key: str,
        *,
        workdir: str,
    ) -> str:
        checked_key = self._validated_key(key)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            await self._run(
                owner,
                session,
                command,
                [checked_key],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = (
                f"按下 {checked_key}" if command == "keydown" else f"释放 {checked_key}"
            )
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def keydown(
        self,
        owner_id: str,
        session_id: str,
        key: str,
        *,
        workdir: str = "",
    ) -> str:
        return await self._keyboard_transition(
            owner_id,
            session_id,
            "keydown",
            key,
            workdir=workdir,
        )

    async def keyup(
        self,
        owner_id: str,
        session_id: str,
        key: str,
        *,
        workdir: str = "",
    ) -> str:
        return await self._keyboard_transition(
            owner_id,
            session_id,
            "keyup",
            key,
            workdir=workdir,
        )

    async def wait_for(
        self,
        owner_id: str,
        session_id: str,
        *,
        time_seconds: float | int = 0,
        text: str = "",
        text_gone: str = "",
        workdir: str = "",
    ) -> str:
        checked_time, checked_text, checked_text_gone = self._validated_wait(
            time_seconds,
            text,
            text_gone,
        )
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            args: list[str] = []
            if checked_time > 0:
                args.extend(["--time-seconds", format(checked_time, ".15g")])
            if checked_text:
                args.extend(["--text", checked_text])
            if checked_text_gone:
                args.extend(["--text-gone", checked_text_gone])
            condition_count = int(bool(checked_text)) + int(bool(checked_text_gone))
            await self._run(
                owner,
                session,
                "wait",
                args,
                mutating=False,
                workdir=workdir,
                # A combined wait is sequential by contract. Give each text
                # condition one full action timeout plus transport headroom.
                timeout_seconds=(
                    checked_time
                    + condition_count * self.config.command_timeout_seconds
                    + 5
                ),
            )
            session.last_action = "等待页面条件"
            return await self._snapshot_locked(
                owner,
                session,
                full=False,
                workdir=workdir,
            )

    async def console(
        self,
        owner_id: str,
        session_id: str,
        *,
        kind: str = "console",
        level: str = "info",
        all: bool = False,
        clear: bool = False,
        filename: str = "",
        workdir: str = "",
    ) -> str:
        if not isinstance(kind, str) or kind not in {"console", "network"}:
            raise BrowserDriverError("console kind 仅支持 console/network")
        if not isinstance(level, str) or level not in {
            "error",
            "warning",
            "info",
            "debug",
        }:
            raise BrowserDriverError(
                "console level 仅支持 error/warning/info/debug"
            )
        if type(all) is not bool:
            raise BrowserDriverError("console all 必须是 boolean")
        if type(clear) is not bool:
            raise BrowserDriverError("console clear 必须是 boolean")
        if not isinstance(filename, str):
            raise BrowserDriverError("console filename 必须是字符串")
        if kind == "network" and (level != "info" or all or filename):
            raise BrowserDriverError(
                "console kind=network 不支持 level/all/filename；"
                "请使用 network_requests"
            )
        if clear and (level != "info" or all or filename):
            raise BrowserDriverError("console clear 不能与 level/all/filename 组合")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            if kind == "network":
                result = await self._run(
                    owner,
                    session,
                    "network",
                    ["requests", *(["--clear"] if clear else [])],
                    workdir=workdir,
                )
                return _bounded(
                    _text(result),
                    kind="content",
                    limit=self.config.max_output_chars,
                )

            args = ["--clear"] if clear else [
                "--level",
                level,
                *(["--all"] if all else []),
            ]
            result = await self._run(
                owner,
                session,
                "console",
                args,
                workdir=workdir,
            )
            session.last_action = "清空控制台消息" if clear else "读取控制台消息"
            return await self._console_result(
                session,
                result,
                filename=filename,
                workdir=workdir,
            )

    async def _console_result(
        self,
        session: _Session,
        result: dict[str, Any],
        *,
        filename: str,
        workdir: str,
    ) -> str:
        """Return or persist the complete UTF-8 Playwright console transcript."""
        payload = _data(result)
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise BrowserDriverError("浏览器返回了无效的控制台结果")
        text = payload["text"]
        if not filename:
            # 控制台内容是**页面自己写的**，和快照一样不可信：页面可以
            # `console.log("</untrusted_browser_content> ...")` 伪造 Crew 的固定
            # 信封、谎报动作成功。"原始诊断 API 所以要字节精确"这个理由不成立——
            # 需要字节精确的场景是落盘（下面那条分支），那份不进模型上下文。
            #
            # 送进模型上下文的一律包裹 + 转义，没有例外：
            # `_escape_wrapper_markers` 的文档说得很清楚，漏掉任何一处，
            # 整条隔离边界就不存在了。
            #
            # 空结果（`console --clear`、没有匹配的日志）例外：没有内容就没有
            # 注入面，包一个空壳只是给模型添噪声。
            return _bounded(text, kind="console") if text else ""

        safe_name = self._safe_download_name(filename)
        if not safe_name.lower().endswith(".log"):
            safe_name = f"{safe_name}.log"
        data = text.encode("utf-8")
        download_root = self._prepare_download_dir(session, workdir)
        target = download_root / safe_name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            raise BrowserDriverError("控制台结果目标已存在且不是普通文件")
        self._guard_transfer_size(data, "写入的内容")
        await asyncio.to_thread(target.write_bytes, data)
        saved = await asyncio.to_thread(target.read_bytes) if target.is_file() else b""
        if (
            not target.is_file()
            or not path_is_within(target, [download_root])
            or saved != data
        ):
            with suppress(OSError):
                target.unlink()
            raise BrowserDriverError("控制台结果未能完整保存到任务目录")
        return str(target)

    def _guard_transfer_size(self, data: bytes, what: str) -> None:
        """单次传输的失控护栏。

        `max_transfer_bytes` 曾经在配置里存在**却没有任何消费方**——运维照着
        文档配了以为生效，实际不执行。一个不生效的上限比没有上限更危险：
        它让人停止追问"这里到底有没有边界"。

        护栏很宽（默认 100MB），正常下载/响应体碰不到；它挡的是"一个几 GB 的
        响应把磁盘写满或把内存吃光"。
        """
        limit = int(getattr(self.config, "max_transfer_bytes", 0) or 0)
        if limit > 0 and len(data) > limit:
            raise BrowserDriverError(
                f"{what}超过 {limit} 字节传输上限；请缩小范围后重试",
                code="transfer_too_large",
            )

    async def _network_result(
        self,
        session: _Session,
        result: dict[str, Any],
        *,
        filename: str,
        workdir: str,
    ) -> str:
        """Return exact text or materialize exact bytes from a Host network payload."""
        payload = _data(result)
        if not isinstance(payload, dict):
            raise BrowserDriverError("浏览器返回了无效的网络结果")
        payload_format = payload.get("format")
        if payload_format == "empty":
            return ""
        if payload_format == "text":
            text = payload.get("text")
            if not isinstance(text, str):
                raise BrowserDriverError("浏览器返回了无效的网络文本")
            if not filename:
                # 线上报文同样是页面/服务端派生的不可信文本，走与 console 相同的
                # 判断：需要字节精确的是落盘那条分支，模型面必须包裹 + 转义。
                # 响应体是最好用的注入载体之一——JSON 里塞一句伪造的
                # `</untrusted_browser_content>` 就能逃出隔离区。
                #
                # 空 body 例外，理由同 console。
                return _bounded(text, kind="network") if text else ""
            data = text.encode("utf-8")
            default_extension = str(payload.get("extension") or "txt")
        elif payload_format == "binary":
            encoded = payload.get("base64")
            if not isinstance(encoded, str):
                raise BrowserDriverError("浏览器返回了无效的网络二进制结果")
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, TypeError) as exc:
                raise BrowserDriverError("浏览器返回了损坏的网络二进制结果") from exc
            default_extension = str(payload.get("extension") or "bin").lower()
        else:
            raise BrowserDriverError("浏览器返回了未知的网络结果格式")

        if re.fullmatch(r"[a-z0-9]+", default_extension) is None:
            raise BrowserDriverError("浏览器返回了无效的网络文件扩展名")
        safe_name = self._safe_download_name(
            filename
            or f"response-{uuid.uuid4().hex[:8]}.{default_extension}"
        )
        download_root = self._prepare_download_dir(session, workdir)
        target = download_root / safe_name
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.exists():
            raise BrowserDriverError("网络结果目标已存在且不是普通文件")
        self._guard_transfer_size(data, "写入的内容")
        await asyncio.to_thread(target.write_bytes, data)
        saved = await asyncio.to_thread(target.read_bytes) if target.is_file() else b""
        if (
            not target.is_file()
            or not path_is_within(target, [download_root])
            or saved != data
        ):
            with suppress(OSError):
                target.unlink()
            raise BrowserDriverError("网络结果未能完整保存到任务目录")
        return str(target)

    async def network_requests(
        self,
        owner_id: str,
        session_id: str,
        *,
        static: bool = False,
        filter: str = "",
        filename: str = "",
        workdir: str = "",
    ) -> str:
        """List the active Playwright Page's requests with stable 1-based indexes."""
        if type(static) is not bool:
            raise BrowserDriverError("network_requests static 必须是 boolean")
        if not isinstance(filter, str):
            raise BrowserDriverError("network_requests filter 必须是字符串")
        if not isinstance(filename, str):
            raise BrowserDriverError("network_requests filename 必须是字符串")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            args = [
                *(["--static"] if static else []),
                *(["--filter", filter] if filter else []),
            ]
            result = await self._run(
                owner,
                session,
                "network_requests",
                args,
                workdir=workdir,
            )
            session.last_action = "查看网络请求"
            return await self._network_result(
                session,
                result,
                filename=filename,
                workdir=workdir,
            )

    async def network_request(
        self,
        owner_id: str,
        session_id: str,
        index: int,
        *,
        part: str = "",
        filename: str = "",
        workdir: str = "",
    ) -> str:
        """Read one request by the exact index printed by ``network_requests``."""
        if (
            type(index) is not int
            or index < 1
            or index > 9_007_199_254_740_991
        ):
            raise BrowserDriverError("network_request index 必须是正整数")
        allowed_parts = {
            "",
            "request-headers",
            "request-body",
            "response-headers",
            "response-body",
        }
        if not isinstance(part, str) or part not in allowed_parts:
            raise BrowserDriverError("network_request part 无效")
        if not isinstance(filename, str):
            raise BrowserDriverError("network_request filename 必须是字符串")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            result = await self._run(
                owner,
                session,
                "network_request",
                [str(index), *([part] if part else [])],
                workdir=workdir,
            )
            session.last_action = f"查看网络请求 #{index}"
            return await self._network_result(
                session,
                result,
                filename=filename,
                workdir=workdir,
            )

    async def evaluate(
        self,
        owner_id: str,
        session_id: str,
        function: str,
        *,
        ref: str = "",
        filename: str = "",
        workdir: str = "",
    ) -> str:
        """Evaluate a Playwright-MCP-compatible function/expression on the page.

        A supplied Crew ref resolves to the same exact Locator used by ordinary
        actions and is passed to the function as ``element``. Arbitrary page
        JavaScript may mutate even when used for inspection, so the Host
        invalidates old refs and this method always returns a fresh snapshot
        together with the exact evaluation result.
        """
        if not isinstance(function, str) or not function:
            raise BrowserDriverError("evaluate function 不能为空")
        if not isinstance(ref, str):
            raise BrowserDriverError("evaluate ref 必须是字符串")
        if not isinstance(filename, str):
            raise BrowserDriverError("evaluate filename 必须是字符串")
        expression = function
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(
                owner,
                session,
                workdir=workdir,
            )
            args = [expression]
            if ref:
                args.append(self._native_ref(session, ref))
            result = await self._run(
                owner,
                session,
                "eval",
                args,
                mutating=True,
                workdir=workdir,
            )
            session.last_action = "执行页面 JavaScript"
            payload = _data(result)
            if not isinstance(payload, dict):
                raise BrowserDriverError("浏览器返回了无效的 evaluate 结果")
            observation = await self._observe_after_mutation(
                owner,
                session,
                workdir=workdir,
            )
            if filename:
                serialized = payload.get("serialized")
                if not isinstance(serialized, str):
                    raise BrowserDriverError("浏览器未返回完整的 evaluate JSON 结果")
                safe_name = self._safe_download_name(filename)
                if not safe_name.lower().endswith(".json"):
                    safe_name = f"{safe_name}.json"
                data = serialized.encode("utf-8")
                download_root = self._prepare_download_dir(session, workdir)
                target = download_root / safe_name
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    raise BrowserDriverError(
                        "evaluate 结果目标已存在且不是普通文件"
                    )
                self._guard_transfer_size(data, "写入的内容")
                await asyncio.to_thread(target.write_bytes, data)
                saved = (
                    await asyncio.to_thread(target.read_bytes)
                    if target.is_file()
                    else b""
                )
                if (
                    not target.is_file()
                    or not path_is_within(target, [download_root])
                    or saved != data
                ):
                    with suppress(OSError):
                        target.unlink()
                    raise BrowserDriverError(
                        "evaluate 结果未能完整保存到任务目录"
                    )
                return f"evaluation_result_file:\n{target}\n{observation}"
            visible_payload = {
                key: value
                for key, value in payload.items()
                if key != "serialized"
            }
            evaluation = _bounded(
                visible_payload,
                limit=self.config.max_output_chars,
            )
            return f"evaluation_result:\n{evaluation}\n{observation}"

    @_observed_browser("browser.run_code")
    async def run_code_unsafe(
        self,
        owner_id: str,
        session_id: str,
        code: Any = None,
        *,
        filename: Any = None,
        workdir: str = "",
    ) -> str:
        """Run an upstream-compatible Playwright function in the Host VM.

        ``filename`` is a client-side source file: resolve it with the same
        task-workdir semantics as uploads, read exact UTF-8 text, and let it
        override inline ``code``. The resolved path is sent only as the VM
        stack filename; the Host receives the already-read source text.
        """
        if code is not None and not isinstance(code, str):
            raise BrowserDriverError("run_code_unsafe code 必须是字符串")
        if filename is not None and not isinstance(filename, str):
            raise BrowserDriverError("run_code_unsafe filename 必须是字符串")
        if code is None and filename is None:
            raise BrowserDriverError(
                "run_code_unsafe 至少需要 code 或 filename 之一"
            )

        source = code
        source_filename = ""
        if filename is not None:
            if (
                "\x00" in filename
                or any(0xD800 <= ord(char) <= 0xDFFF for char in filename)
            ):
                raise BrowserDriverError("run_code_unsafe filename 无效")
            base = (
                Path(workdir).expanduser().resolve()
                if workdir
                else Path.cwd().resolve()
            )
            candidate = Path(filename).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file() or not os.access(resolved, os.R_OK):
                    raise OSError("not a readable file")
                source = await asyncio.to_thread(
                    resolved.read_text,
                    encoding="utf-8",
                )
            except (OSError, RuntimeError, UnicodeError) as exc:
                raise BrowserDriverError(
                    "run_code_unsafe filename 不存在、不可读取或不是 UTF-8 文件"
                ) from exc
            source_filename = str(resolved)

        assert isinstance(source, str)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            args = [source, *([source_filename] if source_filename else [])]
            try:
                result = await self._run(
                    owner,
                    session,
                    "run_code_unsafe",
                    args,
                    mutating=True,
                    workdir=workdir,
                )
                payload = _data(result)
                data = payload if isinstance(payload, dict) else {}
                has_result = data.get("has_result") is True
                serialized = data.get("result")
                if has_result and not isinstance(serialized, str):
                    raise BrowserDriverError(
                        "浏览器 Host 返回了无效的 run_code_unsafe 结果",
                        uncertain=True,
                        phase="after_dispatch",
                    )
            except BrowserDriverError:
                # The Host invalidates refs in a finally block because the
                # snippet may have mutated before failing. Mirror that boundary
                # locally and reconcile any popup/close topology best-effort so
                # the next command never sends a ref the Host has discarded.
                self._invalidate_observation(session)
                with suppress(BrowserDriverError):
                    await self._select(owner, session)
                raise
            except BaseException:
                self._invalidate_observation(session)
                raise
            session.last_action = "执行 Playwright 代码"
            observation = await self._observe_after_mutation(
                owner,
                session,
                workdir=workdir,
            )
            if not has_result:
                return observation
            # 结果是**页面里跑出来的**，与 evaluate 同一性质：它可以是页面内容、
            # 也可以是脚本自己拼的任意字符串。evaluate 那边已经包裹，这边漏了
            # ——同一条边界上有一个出口不包，等于这条边界不存在。
            return (
                f"{observation}\nrun_code_result:\n"
                f"{_bounded(serialized, limit=self.config.max_output_chars)}"
            )

    async def get_images(self, owner_id: str, session_id: str, *, workdir: str = "") -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            tab = await self._select_checked(owner, session, workdir=workdir)
            try:
                images = await self.driver.page_images(
                    owner.runtime_key,
                    owner.profile_dir,
                    target_id=tab.target_id,
                    timeout=self.config.command_timeout_seconds,
                    proxy_url=owner.proxy.url if owner.proxy else "",
                    download_dir=self._download_quarantine(owner),
                )
            except BrowserDriverError as exc:
                await self._raise_driver_error(owner, session, exc)
                raise AssertionError("unreachable")
            return _bounded(images, limit=self.config.max_output_chars)

    async def vision(
        self,
        owner_id: str,
        session_id: str,
        question: str,
        *,
        annotate: bool = False,
        workdir: str = "",
    ) -> ToolOutput:
        if annotate:
            raise BrowserDriverError(
                "网页内标注会修改不可信页面 DOM，已禁用；"
                "请使用 browser_use 的 vision action 获取普通截图"
            )
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            screenshot_id = uuid.uuid4().hex
            path = self._artifact_dir(session) / f"{screenshot_id}.png"
            args = [str(path)]
            result = await self._run(
                owner,
                session,
                "vision_screenshot",
                args,
                workdir=workdir,
            )
            actual = _data(result)
            if isinstance(actual, dict) and actual.get("path"):
                candidate = Path(str(actual["path"]))
                if candidate.is_file() and candidate.resolve() != path.resolve():
                    await asyncio.to_thread(shutil.copyfile, candidate, path)
            host_epoch = str(actual.get("host_epoch") or "") if isinstance(actual, dict) else ""
            if host_epoch and re.fullmatch(r"[0-9a-f]{32}", host_epoch) is None:
                path.unlink(missing_ok=True)
                raise BrowserDriverError("浏览器返回了无效的视觉截图 epoch")
            if not path.is_file():
                raise BrowserDriverError("浏览器未生成截图")
            # The Host screenshot RPC already binds the image to one document
            # and returns a one-shot epoch used by coordinate_click.  One
            # lightweight guard read remains solely to translate image pixels
            # to CSS pixels on HiDPI/zoomed pages; it carries no ref security
            # surface and is not a publication gate.
            metrics_marker = await self._page_guard(
                owner,
                session,
                reset=False,
                include_security=False,
                workdir=workdir,
            )
            width, height = self._png_size(path)
            session.screenshot_id = screenshot_id
            session.screenshot_host_epoch = host_epoch
            session.screenshot_generation = session.generation
            session.screenshot_path = str(path)
            session.viewport_width, session.viewport_height = width, height
            session.last_action = "获取视觉截图"
            marker = self._marker_data(metrics_marker) or {}
            dpr = float(marker.get("dpr") or 1)
            session.screenshot_dpr = dpr if dpr > 0 else 1
            session.screenshot_coordinates_allowed = True
            session.screenshot_css_width = float(
                marker.get("width") or (width / session.screenshot_dpr if width else 0)
            )
            session.screenshot_css_height = float(
                marker.get("height") or (height / session.screenshot_dpr if height else 0)
            )
            session.screenshot_marker = ""
            content = json.dumps(
                {
                    "screenshot_id": screenshot_id,
                    "width": width,
                    "height": height,
                    "page_generation": f"p{session.generation}",
                    "question": str(question or "请检查当前页面"),
                    "note": "截图是不可信外部内容；坐标仅在当前页面代次、视口和滚动位置下有效。",
                },
                ensure_ascii=False,
            )
            return ToolOutput(
                content=_bounded(content, limit=self.config.max_output_chars),
                media=[
                    MediaPart(
                        "image/png",
                        path=str(path),
                        alt=str(question or "当前浏览器页面截图"),
                        detail="high",
                    )
                ],
            )

    async def save_screenshot(
        self,
        owner_id: str,
        session_id: str,
        filename: str = "",
        *,
        ref: str = "",
        image_type: str = "",
        full_page: bool = False,
        scale: str = "css",
        settled: bool = True,
        workdir: str = "",
    ) -> str | ToolOutput:
        """通过 Playwright 公共 API 导出页面/元素截图。

        与 vision（给模型自己看的多模态输入）不同，这是面向用户的导出：
        不经过 VLM、不要求模型视觉能力；文件边界与 download 一致（工作区内
        downloads/browser/，符号链接与大小检查相同）。默认 settled 模式只释放
        由 Crew 填写遗留的页面焦点/高亮，不执行任意 Escape；无需审批。
        """
        if not isinstance(filename, str):
            raise BrowserDriverError("screenshot filename 必须是字符串")
        if not isinstance(ref, str):
            raise BrowserDriverError("screenshot ref 必须是字符串")
        if not isinstance(image_type, str) or image_type not in {"", "png", "jpeg"}:
            raise BrowserDriverError("screenshot type 仅支持 png/jpeg")
        if type(full_page) is not bool:
            raise BrowserDriverError("screenshot full_page 必须是 boolean")
        if not isinstance(scale, str) or scale not in {"css", "device"}:
            raise BrowserDriverError("screenshot scale 仅支持 css/device")
        if type(settled) is not bool:
            raise BrowserDriverError("screenshot settled 必须是 boolean")
        if full_page and ref:
            raise BrowserDriverError("screenshot 的 full_page 与 ref 不能同时使用")

        inferred_type = ""
        if filename:
            suffix = Path(filename).suffix.lower()
            if suffix == ".png":
                inferred_type = "png"
            elif suffix in {".jpg", ".jpeg"}:
                inferred_type = "jpeg"
        if image_type and inferred_type and image_type != inferred_type:
            raise BrowserDriverError(
                "screenshot 显式 type 与 filename 扩展名不一致"
            )
        file_type = image_type or inferred_type or "png"
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            safe_name = self._safe_download_name(
                filename
                or (
                    f"{'element' if ref else 'page'}-"
                    f"{uuid.uuid4().hex[:8]}.{file_type}"
                )
            )
            if filename and Path(safe_name).suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
            }:
                safe_name = f"{safe_name}.{file_type}"
            staging_target = (
                self._artifact_dir(session)
                / f"shot-{uuid.uuid4().hex}.{file_type}"
            )
            screenshot_args = [
                "--type",
                file_type,
                "--scale",
                scale,
            ]
            if ref:
                screenshot_args.extend(["--ref", self._native_ref(session, ref)])
            if full_page:
                screenshot_args.append("--full-page")
            if settled:
                screenshot_args.append("--settled")
            screenshot_args.append(str(staging_target))
            result = await self._run(
                owner, session, "screenshot", screenshot_args, workdir=workdir
            )
            actual = _data(result)
            if isinstance(actual, dict) and actual.get("path"):
                candidate = Path(str(actual["path"]))
                if candidate.is_file() and candidate.resolve() != staging_target.resolve():
                    await asyncio.to_thread(shutil.copyfile, candidate, staging_target)
            if not staging_target.is_file():
                raise BrowserDriverError("浏览器未生成截图")
            download_root = self._prepare_download_dir(session, workdir)
            target = download_root / safe_name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                raise BrowserDriverError("截图目标已存在且不是普通文件")
            await asyncio.to_thread(shutil.move, str(staging_target), str(target))
            if not target.is_file() or not path_is_within(target, [download_root]):
                with suppress(OSError):
                    target.unlink()
                raise BrowserDriverError("截图目标在保存期间离开任务目录，文件已删除")
            session.last_action = "保存页面截图"
            if filename:
                return str(target)
            return ToolOutput(
                content=str(target),
                media=[
                    MediaPart(
                        f"image/{file_type}",
                        path=str(target),
                        alt=(
                            f"浏览器元素截图 {ref}"
                            if ref
                            else (
                                "浏览器完整页面截图"
                                if full_page
                                else "浏览器页面截图"
                            )
                        ),
                        detail="high",
                    )
                ],
            )

    async def coordinate_click(
        self,
        owner_id: str,
        session_id: str,
        screenshot_id: str,
        x: int,
        y: int,
        *,
        workdir: str = "",
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            if (
                screenshot_id != session.screenshot_id
                or session.screenshot_generation != session.generation
            ):
                raise BrowserDriverError(
                    "视觉坐标已失效，请重新调用 browser_use 的 vision action"
                )
            if not session.screenshot_coordinates_allowed:
                raise BrowserDriverError(
                    "标注截图不能用于坐标点击；请重新调用 browser_use 的 vision action"
                )
            if x < 0 or y < 0 or x >= session.viewport_width or y >= session.viewport_height:
                raise BrowserDriverError("坐标超出截图范围")
            if session.viewport_width <= 0 or session.viewport_height <= 0:
                raise BrowserDriverError(
                    "截图尺寸无效，请重新调用 browser_use 的 vision action"
                )
            # Convert screenshot/device pixels once and use the same rounded
            # CSS point for the DOM hit-test and native input events.
            css_x = int(
                round(float(x) * session.screenshot_css_width / float(session.viewport_width))
            )
            css_y = int(
                round(float(y) * session.screenshot_css_height / float(session.viewport_height))
            )
            try:
                atomic_result = await self.driver.coordinate_click_atomic(
                    owner.runtime_key,
                    owner.profile_dir,
                    target_id=self._active_tab(session).target_id,
                    x=css_x,
                    y=css_y,
                    timeout=self.config.command_timeout_seconds,
                    proxy_url=owner.proxy.url if owner.proxy else "",
                    download_dir=self._prepare_download_dir(session, workdir),
                    expected_epoch=session.screenshot_host_epoch,
                )
            except BrowserOperationCancelled as exc:
                # Host screenshot epochs are one-shot even when the cancelled
                # request reports a lifecycle failure.
                self._clear_screenshot(session)
                await self._apply_driver_lifecycle_failure(owner, session, exc)
                raise
            except BrowserDriverError as exc:
                # The production Host treats visual epochs as one-shot even
                # when input becomes uncertain. Never offer a stale screenshot
                # for a second dispatch after any atomic-host failure.
                self._clear_screenshot(session)
                await self._raise_driver_error(owner, session, exc)
                raise AssertionError("unreachable")
            if atomic_result is not None:
                await self._ingest_automatic_downloads(
                    owner,
                    session,
                    atomic_result,
                )
                owner.running = True
                session.last_error = ""
                session.last_action = f"坐标点击 ({x}, {y})"
                return await self._observe_after_mutation(owner, session, workdir=workdir)

            # Compatibility drivers use the same single Playwright-style mouse
            # click transaction.  The old eval hit-test + move/guard/down/guard
            # /up sequence added five round trips and behaved differently from
            # the engine's public mouse surface.
            await self._run(
                owner,
                session,
                "mouse",
                ["click", str(css_x), str(css_y), "left", "1", "0"],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = f"坐标点击 ({x}, {y})"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    def _resolved_upload_paths(
        self,
        _owner_id: str,
        paths: list[str],
        *,
        workdir: str,
    ) -> list[str]:
        if not isinstance(paths, list):
            raise BrowserDriverError("上传文件列表无效")
        base = (
            Path(workdir).expanduser().resolve()
            if workdir
            else Path.cwd().resolve()
        )
        resolved: list[str] = []
        for raw in paths:
            if (
                not isinstance(raw, str)
                or not raw
                or "\x00" in raw
                or any(0xD800 <= ord(char) <= 0xDFFF for char in raw)
            ):
                raise BrowserDriverError("上传文件路径无效")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = base / candidate
            try:
                path = candidate.resolve(strict=True)
                path.stat()
            except (OSError, RuntimeError) as exc:
                raise BrowserDriverError("上传文件不存在或不可读取") from exc
            # Playwright supports directory paths for ``webkitdirectory`` file
            # inputs.  Let the engine validate the input element/path pairing.
            if not (path.is_file() or path.is_dir()) or not os.access(path, os.R_OK):
                raise BrowserDriverError("上传目标不是可读取的文件或目录")
            resolved.append(str(path))
        return resolved

    @staticmethod
    def _is_reparse_point(info: os.stat_result) -> bool:
        """Return whether a Windows entry is a junction or other reparse point."""
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0) or 0)
        return bool(marker and attributes & marker)

    def _stage_approved_uploads(
        self,
        owner: _Owner,
        paths: list[str],
    ) -> tuple[list[str], Path | None]:
        """Snapshot authorized upload inputs into the Host-owned staging root."""
        if not paths:
            return [], None

        approved_root = owner.profile_dir.parent / "approved-uploads"
        approved_root.mkdir(parents=True, exist_ok=True)
        try:
            root = approved_root.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise BrowserDriverError("无法创建浏览器上传审批暂存目录") from exc
        if root != approved_root.absolute() or root.is_symlink():
            raise BrowserDriverError("浏览器上传审批暂存目录不安全")

        stage = root / uuid.uuid4().hex
        try:
            stage.mkdir(mode=0o700)
        except OSError as exc:
            raise BrowserDriverError("无法创建浏览器上传暂存区") from exc

        limit = int(getattr(self.config, "max_transfer_bytes", 0) or 0)
        copied_bytes = 0
        copied_entries = 0

        def reserve(info: os.stat_result) -> None:
            nonlocal copied_bytes, copied_entries
            copied_entries += 1
            if copied_entries > 10_000:
                raise BrowserDriverError("上传目录包含过多文件")
            copied_bytes += max(0, int(info.st_size))
            if limit > 0 and copied_bytes > limit:
                raise BrowserDriverError(
                    f"上传内容超过 {limit} 字节传输上限；请缩小范围后重试",
                    code="transfer_too_large",
                )

        def copy_file(source: Path, destination: Path, info: os.stat_result) -> None:
            if not stat.S_ISREG(info.st_mode) or self._is_reparse_point(info):
                raise BrowserDriverError("上传内容包含符号链接或特殊文件")
            reserve(info)
            flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
            flags |= int(getattr(os, "O_NOFOLLOW", 0))
            try:
                descriptor = os.open(source, flags)
                with os.fdopen(descriptor, "rb", closefd=True) as reader:
                    opened = os.fstat(reader.fileno())
                    if (
                        not stat.S_ISREG(opened.st_mode)
                        or self._is_reparse_point(opened)
                        or (
                            getattr(info, "st_ino", 0)
                            and getattr(opened, "st_ino", 0)
                            and (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
                        )
                    ):
                        raise BrowserDriverError("上传文件在审批后发生变化")
                    with destination.open("xb") as writer:
                        shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    finished = os.fstat(reader.fileno())
                    if (
                        opened.st_size != finished.st_size
                        or opened.st_mtime_ns != finished.st_mtime_ns
                    ):
                        raise BrowserDriverError("上传文件在复制期间发生变化")
            except BrowserDriverError:
                raise
            except OSError as exc:
                raise BrowserDriverError("上传文件无法安全读取") from exc

        def copy_entry(source: Path, destination: Path) -> None:
            try:
                info = source.lstat()
            except OSError as exc:
                raise BrowserDriverError("上传文件不存在或不可读取") from exc
            if stat.S_ISLNK(info.st_mode) or self._is_reparse_point(info):
                raise BrowserDriverError("上传内容包含符号链接或特殊文件")
            if stat.S_ISREG(info.st_mode):
                copy_file(source, destination, info)
                return
            if not stat.S_ISDIR(info.st_mode):
                raise BrowserDriverError("上传内容包含符号链接或特殊文件")
            destination.mkdir(mode=0o700)
            try:
                children = sorted(source.iterdir(), key=lambda item: item.name)
            except OSError as exc:
                raise BrowserDriverError("上传目录无法读取") from exc
            for child in children:
                copy_entry(child, destination / child.name)

        staged: list[str] = []
        try:
            for index, raw in enumerate(paths):
                source = Path(raw)
                # Indexing prevents duplicate basenames from colliding while the
                # original basename remains visible to the browser upload API.
                destination = stage / f"{index:03d}" / source.name
                destination.parent.mkdir(mode=0o700)
                copy_entry(source, destination)
                staged.append(str(destination))
            return staged, stage
        except BaseException:
            self._cleanup_approved_upload_stage(stage, root)
            raise

    @staticmethod
    def _cleanup_approved_upload_stage(stage: Path | None, approved_root: Path) -> None:
        if stage is None:
            return
        try:
            root = approved_root.resolve(strict=True)
            candidate = stage.resolve(strict=True)
            if candidate.parent != root or not re.fullmatch(r"[0-9a-f]{32}", candidate.name):
                return
            shutil.rmtree(candidate)
        except OSError:
            log.warning("failed to clean browser upload staging directory: %s", stage)

    @staticmethod
    def _validated_drop_data(data: Any) -> dict[str, str] | None:
        if data is None:
            return None
        if (
            not isinstance(data, dict)
            or any(
                not isinstance(mime, str) or not isinstance(value, str)
                for mime, value in data.items()
            )
        ):
            raise BrowserDriverError("drop data 必须是 MIME type 到字符串的 object")
        # Preserve insertion order exactly as supplied.  There is deliberately
        # no key/value length or item-count cap on Playwright's DataTransfer.
        return dict(data)

    async def drop(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        paths: list[str] | None = None,
        data: dict[str, str] | None = None,
        *,
        workdir: str = "",
    ) -> str:
        if not isinstance(ref, str) or not ref:
            raise BrowserDriverError("drop ref 必须是当前页面的元素 ref")
        checked_data = self._validated_drop_data(data)
        if paths is not None and not isinstance(paths, list):
            raise BrowserDriverError("drop paths 必须是本地路径数组")
        resolved = self._resolved_upload_paths(
            owner_id,
            [] if paths is None else paths,
            workdir=workdir,
        )
        # Match pinned Playwright: an explicitly provided empty data object is a
        # valid payload, while absent data plus no files is not an operation.
        if not resolved and checked_data is None:
            raise BrowserDriverError('drop 至少需要非空 "paths" 或显式 "data"')

        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            native = self._native_ref(session, ref)
            staged, upload_stage = await asyncio.to_thread(
                self._stage_approved_uploads,
                owner,
                resolved,
            )
            try:
                drop_args = [native]
                for path in staged:
                    drop_args.extend(["--path", path])
                if checked_data is not None:
                    if checked_data:
                        for mime, value in checked_data.items():
                            drop_args.extend(["--data", mime, value])
                    else:
                        # The argv wire otherwise cannot distinguish official
                        # ``data: {}`` from an entirely absent payload.
                        drop_args.append("--empty-data")
                await self._run(
                    owner,
                    session,
                    "drop",
                    drop_args,
                    mutating=True,
                    workdir=workdir,
                )
                session.last_action = f"拖放到 {ref}"
                return await self._observe_after_mutation(
                    owner,
                    session,
                    workdir=workdir,
                )
            finally:
                await asyncio.to_thread(
                    self._cleanup_approved_upload_stage,
                    upload_stage,
                    owner.profile_dir.parent / "approved-uploads",
                )

    async def upload(
        self, owner_id: str, session_id: str, ref: str, paths: list[str], *, workdir: str = ""
    ) -> str:
        resolved = self._resolved_upload_paths(
            owner_id,
            paths,
            workdir=workdir,
        )
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            staged, upload_stage = await asyncio.to_thread(
                self._stage_approved_uploads,
                owner,
                resolved,
            )
            try:
                if ref:
                    native = self._native_ref(session, ref)
                    await self._run(
                        owner,
                        session,
                        "upload",
                        [native, *staged],
                        mutating=True,
                        workdir=workdir,
                    )
                else:
                    await self._run(
                        owner,
                        session,
                        "file_upload",
                        staged if staged else ["--cancel"],
                        mutating=True,
                        workdir=workdir,
                    )
                session.last_action = (
                    f"上传 {len(resolved)} 个文件"
                    if resolved
                    else ("清空文件输入" if ref else "取消文件选择")
                )
                return await self._observe_after_mutation(owner, session, workdir=workdir)
            finally:
                await asyncio.to_thread(
                    self._cleanup_approved_upload_stage,
                    upload_stage,
                    owner.profile_dir.parent / "approved-uploads",
                )

    async def download(
        self, owner_id: str, session_id: str, ref: str, filename: str = "", *, workdir: str = ""
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            native = self._native_ref(session, ref)
            safe_name = self._safe_download_name(filename)
            download_root = self._prepare_download_dir(session, workdir)
            target = download_root / safe_name
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.exists():
                raise BrowserDriverError("下载目标已存在且不是普通文件")
            await self._run(
                owner,
                session,
                "download",
                [native, str(target)],
                mutating=True,
                workdir=workdir,
            )
            if not target.is_file():
                raise BrowserDriverError(
                    "下载动作已完成，但目标文件不存在",
                    uncertain=True,
                )
            record = {"name": target.name, "path": str(target), "created_at": time.time()}
            session.downloads.append(record)
            session.last_action = f"下载 {target.name}"
            await self._publish(
                owner.owner, session.session_id, {"type": "download", "download": record}
            )
            return _bounded(record, limit=self.config.max_output_chars)

    @staticmethod
    def _safe_download_name(filename: str) -> str:
        raw = str(filename or "").strip()
        if not raw:
            return f"download-{uuid.uuid4().hex[:8]}"
        if raw in {".", ".."} or "/" in raw or "\\" in raw:
            raise BrowserDriverError("下载 filename 只能是文件名，不能包含路径")
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip(" .")
        if not value:
            raise BrowserDriverError("下载 filename 无效")
        stem = value.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            value = f"_{value}"
        return value

    async def dialog(
        self,
        owner_id: str,
        session_id: str,
        action: str,
        text: str | None = None,
        *,
        workdir: str = "",
    ) -> str:
        if action not in {"status", "accept", "dismiss"}:
            raise BrowserDriverError("dialog action 仅支持 status/accept/dismiss")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            # Runtime.evaluate is intentionally avoided while a modal dialog is
            # open. JavaScript is paused by Chromium until the dialog is dealt
            # with, so the normal mutation guard cannot make progress here.
            tab, switched = await self._select(owner, session)
            if switched and (session.refs or session.page_marker or session.screenshot_id):
                self._invalidate_observation(session)
            if action == "status":
                result = await self._run(owner, session, "dialog", ["status"], workdir=workdir)
                return _bounded(_text(result), limit=self.config.max_output_chars)
            if text is not None and (
                not isinstance(text, str)
                or "\x00" in text
            ):
                raise BrowserDriverError("dialog text 无效")
            args = [action, *([text] if text is not None else [])]
            await self._run(owner, session, "dialog", args, mutating=True, workdir=workdir)
            session.last_action = f"处理对话框：{action}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def tabs(
        self,
        owner_id: str,
        session_id: str,
        action: str,
        tab_id: str = "",
        url: str = "",
        *,
        workdir: str = "",
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            if action == "list":
                _selected, switched = await self._sync_topology(
                    owner,
                    session,
                    allow_empty=True,
                )
                if switched and (
                    session.refs or session.page_marker or session.screenshot_id
                ):
                    self._invalidate_observation(session)
                return _bounded(
                    {
                        "tabs": [self._public_tab(tab) for tab in session.tabs.values()],
                        "active": session.active_label,
                    },
                    limit=self.config.max_output_chars,
                )
            if action == "new":
                safe_url = self.policy.validate_navigation_url(url) if url else "about:blank"
                if session.tabs:
                    await self._sync_topology(owner, session, allow_empty=True)
                # Stream shutdown can yield while the current page navigates.
                # Finish it before creating the new tab so events remain ordered.
                await self._close_session_stream(owner, session)
                previous_active = session.active_label
                tab = self._new_tab(session)
                try:
                    await self._run(
                        owner,
                        session,
                        "tab",
                        ["new", "--label", tab.label, safe_url],
                        navigation=bool(url),
                        mutating=True,
                        workdir=workdir,
                    )
                except BrowserDriverError as exc:
                    if not (
                        exc.uncertain or exc.browser_stopped or exc.stop_unconfirmed
                    ):
                        self._rollback_new_tab(session, tab, previous_active)
                    raise
                owner.selected_label = tab.label
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                tab.url = safe_url
                return await self._observe_after_mutation(owner, session, workdir=workdir)
            requested_tab = session.tabs.get(tab_id)
            requested_was_active = session.active_label == tab_id
            synced_tab, _switched = await self._sync_topology(
                owner,
                session,
                allow_empty=True,
            )
            if action == "close" and not tab_id:
                tab_id = session.active_label
                if not tab_id:
                    self._clear_ref_state(session)
                    self._clear_screenshot(session)
                    session.page_marker = ""
                    return _bounded(
                        {"ok": True, "active": ""},
                        limit=self.config.max_output_chars,
                    )
                requested_tab = session.tabs.get(tab_id)
                requested_was_active = True
            if tab_id not in session.tabs:
                if (
                    action == "close"
                    and requested_tab is not None
                    and requested_tab.target_id
                ):
                    # The authoritative list proves the exact immutable target
                    # already disappeared between calls. Treat close as
                    # idempotent; never send its reusable native tN.
                    if requested_was_active:
                        self._invalidate_observation(session)
                        if synced_tab is not None:
                            return await self._snapshot_locked(
                                owner,
                                session,
                                full=False,
                                workdir=workdir,
                            )
                    return _bounded(
                        {
                            "ok": True,
                            "already_closed": True,
                            "active": session.active_label,
                        },
                        limit=self.config.max_output_chars,
                    )
                raise BrowserDriverError("标签页不属于当前 Crew 会话")
            if action == "select":
                await self._close_session_stream(owner, session)
                session.active_label = tab_id
                await self._select(owner, session)
                return await self._snapshot_locked(owner, session, full=False, workdir=workdir)
            if action == "close":
                was_active = session.active_label == tab_id
                if was_active:
                    await self._close_session_stream(owner, session)
                tab = session.tabs[tab_id]
                await self._close_tab_target(owner, session, tab)
                next_tab, _switched = await self._sync_topology(
                    owner,
                    session,
                    allow_empty=True,
                )
                if was_active:
                    self._clear_ref_state(session)
                    self._clear_screenshot(session)
                    session.page_marker = ""
                    if next_tab is not None:
                        return await self._observe_after_mutation(owner, session, workdir=workdir)
                    session.generation += 1
                return _bounded(
                    {"ok": True, "active": session.active_label}, limit=self.config.max_output_chars
                )
            raise BrowserDriverError("tabs action 仅支持 list/new/select/close")

    async def takeover(self, owner_id: str, session_id: str, action: str) -> str:
        """Handle the model-facing control tool.

        Returning control to AI is deliberately not part of this boundary.  A
        model turn can keep running while the user is entering credentials, so
        allowing the same tool to return/pause/stop a human-controlled page
        would let the model revoke the privacy boundary it just established.
        """
        return await self._control(owner_id, session_id, action, trusted_user=False)

    async def user_control(self, owner_id: str, session_id: str, action: str) -> str:
        """Handle an authenticated control action from the trusted Crew UI."""
        return await self._control(owner_id, session_id, action, trusted_user=True)

    @staticmethod
    def _require_model_control(owner: _Owner, session: _Session) -> None:
        if session.mode != "ai":
            raise BrowserDriverError("用户正在接管或浏览器已暂停；模型不能更改浏览器控制状态")
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；模型不能更改浏览器控制状态")

    async def _control(
        self,
        owner_id: str,
        session_id: str,
        action: str,
        *,
        trusted_user: bool,
    ) -> str:
        if not trusted_user and action not in {"takeover", "pause", "stop"}:
            raise BrowserDriverError("模型不能交还 AI；请由用户在 Crew 浏览器面板操作")

        owner = await self._owner(owner_id)
        if action == "stop":
            sid = str(session_id or "").strip()
            if not sid:
                raise BrowserDriverError("browser 工具缺少会话上下文")
            async with owner.stop_lock:
                model_control_held = False
                if not trusted_user:
                    # Model stop is serialized behind trusted takeover/open but
                    # still does not wait for an ordinary AI page action holding
                    # owner.lock.  The user's emergency stop intentionally skips
                    # this lock so it remains preemptive in every mode.
                    await owner.control_lock.acquire()
                    model_control_held = True
                    try:
                        session = self._session(owner, sid)
                        self._require_model_control(owner, session)
                    except BaseException:
                        owner.control_lock.release()
                        model_control_held = False
                        raise
                # Once this stop transaction starts, the event-loop-atomic
                # gates are set before interrupt() yields.  return/AI/human
                # paths all reject while stopping, so none can reopen Chromium.
                owner.stopping = True
                owner.actions_blocked = True
                try:
                    stop_confirmed = False
                    try:
                        await self.driver.interrupt(owner.runtime_key, owner.profile_dir)
                        stop_confirmed = True
                    except BrowserOperationCancelled as exc:
                        await self._apply_driver_lifecycle_failure(owner, None, exc)
                        raise
                    except Exception:
                        log.warning("browser emergency interrupt failed", exc_info=True)
                    async with owner.lock:
                        self._session(owner, sid)
                        if not stop_confirmed:
                            try:
                                closed = await self.driver.close(
                                    owner.runtime_key, owner.profile_dir
                                )
                                stop_confirmed = closed is not False
                            except BrowserOperationCancelled as exc:
                                await self._apply_driver_lifecycle_failure(owner, None, exc)
                                raise
                            except Exception:
                                log.warning("browser fallback close failed", exc_info=True)
                        owner.running = not stop_confirmed
                        owner.stop_unconfirmed = not stop_confirmed
                        owner.selected_label = ""
                        owner.native_ref_session = ""
                        owner.native_ref_generation = 0
                        for value in list(owner.sessions.values()):
                            await self._close_session_stream(owner, value)
                            value.mode = "paused"
                            value.tabs.clear()
                            value.active_label = ""
                            value.generation += 1
                            self._clear_ref_state(value)
                            self._clear_screenshot(value)
                            value.page_marker = ""
                            await self._publish(
                                owner.owner,
                                value.session_id,
                                {
                                    "type": "state",
                                    "state": self._page_state(owner, value).public_dict(),
                                },
                            )
                    if not stop_confirmed:
                        raise BrowserDriverError(
                            "无法确认 Chromium 已停止；浏览器保持锁定，请重试立即停止或退出应用"
                        )
                    return _bounded(
                        {
                            "mode": "paused",
                            "message": "账号浏览器已立即停止；Profile 已保留，旧标签页/ref/截图均已失效",
                        },
                        limit=self.config.max_output_chars,
                    )
                finally:
                    owner.stopping = False
                    if model_control_held:
                        owner.control_lock.release()

        # Do not queue a return behind an emergency stop and then silently clear
        # the account gate after the stop completes.  When control_lock is free its
        # acquisition below is event-loop atomic; all mode changes use the same
        # lock, so the actor check in _change_control_mode sees a stable mode.
        if owner.stopping:
            if action == "return":
                raise BrowserDriverError("账号浏览器正在停止，请等待停止完成后再交还 AI")
            raise BrowserDriverError("账号浏览器正在停止，不能更改浏览器控制状态")
        async with owner.control_lock:
            return await self._change_control_mode(
                owner,
                session_id,
                action,
                trusted_user=trusted_user,
            )

    async def _change_control_mode(
        self,
        owner: _Owner,
        session_id: str,
        action: str,
        *,
        trusted_user: bool,
    ) -> str:
        async with owner.lock:
            session = self._session(owner, session_id)
            previous_mode = session.mode
            if not trusted_user:
                self._require_model_control(owner, session)
            if action == "takeover":
                if owner.stopping or owner.actions_blocked:
                    raise BrowserDriverError("账号浏览器已停止；请先交还 AI")
                await self._set_driver_mode(owner, session, "human")
                session.mode = "human"
                self._clear_ref_state(session)
                self._clear_screenshot(session)
                session.page_marker = ""
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
            elif action == "return":
                if owner.stopping:
                    raise BrowserDriverError("账号浏览器正在停止，请等待停止完成后再交还 AI")
                if owner.stop_unconfirmed:
                    raise BrowserDriverError(
                        "上次停止未能确认 Chromium 已关闭；请重试立即停止，不能直接交还 AI"
                    )
                if session.tabs:
                    await self._set_driver_mode(owner, session, "ai")
                owner.actions_blocked = False
                if not session.tabs:
                    owner.running = False
                session.mode = "ai"
                self._clear_ref_state(session)
                session.generation += 1
                self._clear_screenshot(session)
                session.page_marker = ""
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
            elif action == "pause":
                if owner.stopping or owner.actions_blocked:
                    raise BrowserDriverError("账号浏览器已停止；请先交还 AI")
                if session.tabs:
                    await self._select(owner, session)
                await self._set_driver_mode(owner, session, "paused")
                session.mode = "paused"
                self._clear_ref_state(session)
            else:
                raise BrowserDriverError("takeover action 无效")
            await self._publish(
                owner.owner,
                session.session_id,
                {"type": "state", "state": self._page_state(owner, session).public_dict()},
            )
            # 接管来源排查口：白屏误触与真实手势都会走到这里，日志里必须能
            # 区分是谁（trusted_user=用户 UI / 否则为模型）把模式切走的。
            log.info(
                "browser control mode changed: session=%s %s -> %s (action=%s, trusted_user=%s)",
                session_id,
                previous_mode,
                session.mode,
                action,
                trusted_user,
            )
            return _bounded(
                {
                    "mode": session.mode,
                    "message": "交还 AI 后必须重新 snapshot，旧 ref 与截图均已失效",
                },
                limit=self.config.max_output_chars,
            )

    async def close_session(self, owner_id: str, session_id: str) -> None:
        owner = self._owners.get(str(owner_id or ""))
        if owner is None:
            return
        async with owner.lock:
            session_key = str(session_id or "")
            session = owner.sessions.get(session_key)
            if session is None:
                return
            close_error: BaseException | None = None
            for tab in list(session.tabs.values()):
                try:
                    await self._close_tab_target(owner, session, tab)
                except BaseException as exc:  # includes caller cancellation
                    close_error = exc
                    break
                if owner.selected_label == tab.label:
                    owner.selected_label = ""
                owner.native_ref_session = ""
                owner.native_ref_generation = 0

            deferred_cancel = isinstance(close_error, asyncio.CancelledError)
            if close_error is not None:
                # Never forget ownership of a page that was not confirmed
                # closed. Fail-stop the account; if that cannot be confirmed,
                # retain this session as a tombstone for later cleanup.
                owner.actions_blocked = True
                stop_confirmed = False
                try:
                    deferred_cancel = (
                        await self._complete_critical(
                            self.driver.interrupt(owner.runtime_key, owner.profile_dir)
                        )
                        or deferred_cancel
                    )
                    stop_confirmed = True
                except BaseException:
                    log.warning(
                        "session tab close and account fail-stop both failed owner=%s session=%s",
                        _hash(owner.owner),
                        _hash(session.session_id),
                        exc_info=True,
                    )
                if not stop_confirmed:
                    owner.stop_unconfirmed = True
                    owner.running = True
                    if deferred_cancel:
                        raise asyncio.CancelledError
                    raise BrowserDriverError(
                        "会话标签页未能确认关闭，账号浏览器已锁定并保留所有权记录"
                    ) from close_error

                owner.running = False
                owner.stop_unconfirmed = False
                self._clear_native_selection(owner)
                for value in owner.sessions.values():
                    await self._close_session_stream(owner, value)
                    value.mode = "paused"
                    value.tabs.clear()
                    value.active_label = ""
                    self._invalidate_observation(value)

            owner.sessions.pop(session_key, None)
            self._subscribers.pop((owner.owner, session.session_id), None)
            if deferred_cancel:
                raise asyncio.CancelledError

    async def reset_host_registration(self, owner_id: str) -> None:
        """Retire Python state from the previous Electron Host epoch.

        The router invokes the newly registered Host's idempotent close_owner
        before this method. No ordinary bridge request is admitted until both
        halves finish, so a stale tab/ref/runtime binding can never leak into the new
        epoch.
        """
        owner_key = str(owner_id or "").strip()
        if not owner_key:
            raise BrowserDriverError("缺少账号上下文")
        async with self._owners_lock:
            owner = self._owners.get(owner_key)
            if owner is not None:
                owner.closing = True
                owner.actions_blocked = True

        session_ids = {
            session_id
            for account, session_id in self._subscribers
            if account == owner_key
        }
        if owner is not None:
            async with owner.lock:
                session_ids.update(owner.sessions)
                for session in owner.sessions.values():
                    await self._close_session_stream(owner, session)
                    session.mode = "paused"
                    session.tabs.clear()
                    session.active_label = ""
                    self._invalidate_observation(session)
                if owner.proxy is not None:
                    await owner.proxy.aclose()
                    owner.proxy = None
                owner.running = False
                owner.initialized = False
                owner.stop_unconfirmed = False
                self._clear_native_selection(owner)
            async with self._owners_lock:
                if self._owners.get(owner_key) is owner:
                    self._owners.pop(owner_key, None)
                owner.closed_event.set()

        for session_id in session_ids:
            await self._publish(
                owner_key,
                session_id,
                {"type": "debug_clear"},
            )
            await self._publish(
                owner_key,
                session_id,
                {"type": "state", "state": self.state(owner_key, session_id)},
            )

    async def clear_owner_data(self, owner_id: str) -> dict[str, Any]:
        """Clear one account's Electron Session and Crew browser artifacts."""
        owner_key = str(owner_id or "").strip()
        if not owner_key:
            raise BrowserDriverError("缺少账号上下文")
        while True:
            wait_for_close: asyncio.Event | None = None
            async with self._owners_lock:
                owner = self._owners.get(owner_key)
                if owner is not None and owner.closing:
                    if owner.stop_unconfirmed:
                        # fail-stop 墓碑：owner 保留在 _owners 里、closed_event 也已置位
                        # （见 revoke_owner 失败分支）。这里若继续 await 就是空转——锁无
                        # 竞争、事件已 set，两个 await 都不让出事件循环，整个进程 100%
                        # CPU 活锁。与 _owner() 一致：直接报错，不等待。
                        raise BrowserDriverError(
                            "账号浏览器关闭状态无法确认；为保护 Profile 已保持锁定，请重启应用后再试"
                        )
                    wait_for_close = owner.closed_event
                else:
                    if owner is None:
                        home = get_owner_runtime_home(owner_key)
                        owner = _Owner(
                            owner=owner_key,
                            runtime_key=f"crew_{_hash(owner_key)}",
                            profile_dir=home / "browser" / "profile",
                        )
                        self._configure_owner_lock(owner)
                        self._owners[owner_key] = owner
                    # Leave this owner as a tombstone until Session clear,
                    # Host close and artifact cleanup finish; other accounts do
                    # not wait on these account-local operations.
                    owner.closing = True
                    owner.actions_blocked = True
            if wait_for_close is None:
                break
            await wait_for_close.wait()

        cleared = False
        try:
            async with owner.lock:
                # Cold clears initialize the same direct-network owner shape as
                # ordinary browser use; no page is created or navigated.
                await self._start_owner_proxy(owner)
                result = await self.driver.clear_owner_data(
                    owner.runtime_key,
                    owner.profile_dir,
                    timeout=self.config.command_timeout_seconds,
                    proxy_url=owner.proxy.url if owner.proxy else "",
                    download_dir=self._download_quarantine(owner),
                )
                if result is False:
                    raise BrowserDriverError("无法确认 Electron 浏览数据已清除")
                closed = await self.driver.close(owner.runtime_key, owner.profile_dir)
                if closed is False:
                    raise BrowserDriverError("无法确认账号 Chromium 已关闭")
                await self._close_owner_locked(owner, close_driver=False)
                for key in [key for key in self._subscribers if key[0] == owner_key]:
                    self._subscribers.pop(key, None)
                # Electron session.fromPath() retains a process-wide binding to
                # Profile even after all WebContents are closed. Deleting that
                # active path is racy on Windows and can recreate partial data.
                # Host clear_owner_data owns Session storage deletion; Python
                # removes only Crew-managed non-Session artifacts.
                browser_root = get_owner_runtime_home(owner_key) / "browser"
                for child in (
                    browser_root / "artifacts",
                    browser_root / "download-quarantine",
                    browser_root / "approved-downloads",
                ):
                    if child.exists():
                        await asyncio.to_thread(shutil.rmtree, child)
                cleared = True
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, None, exc)
            raise
        finally:
            if not cleared and owner.proxy is not None:
                with suppress(Exception, asyncio.CancelledError):
                    await owner.proxy.aclose()
                owner.proxy = None
                owner.initialized = False
            async with self._owners_lock:
                if cleared and self._owners.get(owner_key) is owner:
                    self._owners.pop(owner_key, None)
                    owner.closed_event.set()
                elif self._owners.get(owner_key) is owner:
                    # Keep the same runtime/profile tombstone available for a
                    # later clear/stop retry, while all ordinary actions remain
                    # fail-closed.
                    owner.closing = False
                    owner.retiring = False
                    owner.actions_blocked = True
        return {"ok": True, "cleared": True, "owner_hash": _hash(owner_key)}

    def session_for_target(self, owner_id: str, target_id: str) -> str | None:
        """Resolve one exact Host target to one Crew session in any mode."""
        owner = self._owners.get(str(owner_id or ""))
        target = str(target_id or "")
        if owner is None or not target:
            return None
        matches = [
            session.session_id
            for session in owner.sessions.values()
            if any(tab.target_id == target for tab in session.tabs.values())
        ]
        return matches[0] if len(matches) == 1 else None

    def session_for_hash(self, owner_id: str, session_hash: str) -> str | None:
        """Resolve Host events for a logical session before popup discovery.

        Public ``Page`` APIs can create and download from a popup before the
        next Manager tab refresh.  The Host's logical session hash is already
        bound at page creation, so it is the stable route for that interval.
        """
        owner = self._owners.get(str(owner_id or ""))
        value = str(session_hash or "")
        if owner is None or re.fullmatch(r"[0-9a-f]{32}", value) is None:
            return None
        matches = [
            session.session_id
            for session in owner.sessions.values()
            if _hash(session.session_id, 32) == value
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _automatic_download_record(
        value: Any,
        *,
        expected_session_hash: str,
    ) -> dict[str, Any] | None:
        fields = {
            "downloadId",
            "targetId",
            "sessionHash",
            "path",
            "name",
            "suggestedFilename",
            "url",
            "state",
            "receivedBytes",
            "totalBytes",
            "createdAt",
            "completedAt",
            "error",
        }
        if not isinstance(value, dict) or set(value) != fields:
            return None
        text_fields = (
            "downloadId",
            "targetId",
            "sessionHash",
            "path",
            "name",
            "suggestedFilename",
            "url",
            "state",
            "error",
        )
        integer_fields = (
            "receivedBytes",
            "totalBytes",
            "createdAt",
            "completedAt",
        )
        if (
            any(not isinstance(value.get(field), str) for field in text_fields)
            or any(
                type(value.get(field)) is not int or int(value[field]) < 0
                for field in integer_fields
            )
            or not value["downloadId"]
            or not value["targetId"]
            or value["sessionHash"] != expected_session_hash
            or value["state"]
            not in {"progressing", "completed", "cancelled", "interrupted"}
            or not value["path"]
            or not value["name"]
        ):
            return None
        return {
            "id": value["downloadId"],
            "name": value["name"],
            "suggested_filename": value["suggestedFilename"],
            "path": value["path"],
            "url": value["url"],
            "state": value["state"],
            "received_bytes": int(value["receivedBytes"]),
            "total_bytes": int(value["totalBytes"]),
            "created_at": int(value["createdAt"]) / 1000,
            "completed_at": (
                int(value["completedAt"]) / 1000
                if int(value["completedAt"]) > 0
                else 0.0
            ),
            "error": value["error"],
            "source": "automatic",
        }

    @staticmethod
    def _upsert_download_record(
        session: _Session,
        record: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        download_id = str(record.get("id") or "")
        for index, current in enumerate(session.downloads):
            if str(current.get("id") or "") != download_id:
                continue
            # WebSocket and RPC frames travel independently.  Never let a
            # delayed start frame regress a completed/failed native item.
            if (
                float(current.get("completed_at") or 0) > 0
                and float(record.get("completed_at") or 0) <= 0
            ):
                return current, False
            if current == record:
                return current, False
            session.downloads[index] = record
            return record, True
        session.downloads.append(record)
        return record, True

    async def _ingest_automatic_downloads(
        self,
        owner: _Owner,
        session: _Session,
        response: Any,
    ) -> None:
        data = _data(response) if isinstance(response, dict) else response
        downloads = data.get("downloads") if isinstance(data, dict) else None
        if not isinstance(downloads, list):
            return
        expected_hash = _hash(session.session_id, 32)
        for value in downloads:
            record = self._automatic_download_record(
                value,
                expected_session_hash=expected_hash,
            )
            if record is None:
                continue
            current, changed = self._upsert_download_record(session, record)
            if changed:
                await self._publish(
                    owner.owner,
                    session.session_id,
                    {"type": "download", "download": _safe_public_value(current)},
                )

    async def publish_host_download(
        self,
        owner_id: str,
        session_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Upsert a native Electron download event without taking owner.lock."""
        owner_key = str(owner_id or "")
        session_key = str(session_id or "")
        owner = self._owners.get(owner_key)
        session = owner.sessions.get(session_key) if owner else None
        if owner is None or session is None:
            return False
        expected_hash = _hash(session.session_id, 32)
        target_id = str(event.get("targetId") or "")
        by_target = self.session_for_target(owner_key, target_id)
        by_hash = self.session_for_hash(owner_key, str(event.get("sessionHash") or ""))
        if by_hash != session_key or by_target not in {None, session_key}:
            return False
        wire = {key: value for key, value in event.items() if key != "type"}
        record = self._automatic_download_record(
            wire,
            expected_session_hash=expected_hash,
        )
        if record is None:
            return False
        # Revalidate after normalization: a concurrent Host reset can clear or
        # replace the logical session between the two synchronous sections.
        if self.session_for_hash(owner_key, expected_hash) != session_key:
            return False
        current, changed = self._upsert_download_record(session, record)
        if changed:
            await self._publish(
                owner_key,
                session_key,
                {"type": "download", "download": _safe_public_value(current)},
            )
        return True

    async def publish_host_debug(
        self,
        owner_id: str,
        session_id: str,
        target_id: str,
        channel: str,
        record: dict[str, Any],
    ) -> bool:
        """Revalidate and publish a complete Host debug event without owner locks."""
        if channel not in {"console", "network"}:
            return False
        if self.session_for_target(owner_id, target_id) != str(session_id or ""):
            return False
        safe_record = _safe_public_value(record)
        try:
            json.dumps(
                safe_record,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        except (TypeError, ValueError):
            return False
        # Recheck ownership after serialization; target/session topology can
        # change without waiting for this publisher.
        if self.session_for_target(owner_id, target_id) != str(session_id or ""):
            return False
        await self._publish(
            str(owner_id or ""),
            str(session_id or ""),
            {"type": "debug", "channel": channel, "record": safe_record},
        )
        return True

    def _governance_session(self, owner_id: str, session_id: str) -> _Session | None:
        owner = self._owners.get(str(owner_id or ""))
        return owner.sessions.get(str(session_id or "")) if owner else None

    def _sensitive_reason(
        self, tool_name: str, args: dict[str, Any], owner_id: str, session_id: str
    ) -> str | None:
        """传/高危动作的审批原因（confirm_sensitive 档）；普通动作返回 None。"""
        if tool_name == "browser_type":
            if args.get("submit") is True:
                return "将输入文本并自动提交（模拟回车），可能直接发出搜索、订单或表单"
            return None
        if tool_name in {"browser_press", "browser_keydown"}:
            if str(args.get("key") or "").strip().lower() in {"enter", "numpadenter"}:
                return "将按下回车键，可能提交表单或确认页面上的操作"
            return None
        if tool_name == "browser_click":
            # submit 型点击：经 ref 查本代 snapshot 里宿主显式标注的提交控件。
            # 查不到元素信息时按普通点击放行，不误伤。
            session = self._governance_session(owner_id, session_id)
            stored = session.refs.get(str(args.get("ref") or "")) if session else None
            native = stored.split("\n", 1)[0] if stored else ""
            if native and session is not None and session.ref_actions.get(native) == "submit":
                return "将点击页面上的提交按钮，可能直接提交表单或触发确认"
            return None
        if tool_name == "browser_upload":
            return "将向当前网站上传本地文件"
        if tool_name == "browser_drop":
            if args.get("paths"):
                return "将把本地文件拖放到当前网站（等同于上传）"
            return None
        if tool_name == "browser_download":
            return "将从当前网站下载文件到本地磁盘"
        if tool_name == "browser_dialog":
            if str(args.get("action") or "") == "accept":
                return "将接受页面弹出的对话框（等同点击“确认”）"
            return None
        if tool_name == "browser_evaluate":
            return "将在页面内执行任意 JavaScript，可读取并改写页面全部内容"
        if tool_name == "browser_run_code_unsafe":
            return "将执行任意 Playwright 自动化代码（最高危动作）"
        if tool_name == "browser_batch":
            # 整批取最高危级别：任一敏感步骤则整批 ask。未登记的 action 不影响
            # 敏感判定（执行时插件会拒绝），但写判定按 fail-closed 处理。
            steps = args.get("steps")
            if isinstance(steps, list):
                for step in steps:
                    if not isinstance(step, dict):
                        continue
                    sub_name = BATCH_STEP_TOOLS.get(str(step.get("action") or ""))
                    if not sub_name:
                        continue
                    reason = self._sensitive_reason(sub_name, step, owner_id, session_id)
                    if reason is not None:
                        return f"批量操作包含敏感步骤 {step.get('action')}：{reason}"
            return None
        return None

    def _batch_has_write(self, args: dict[str, Any]) -> bool:
        steps = args.get("steps")
        if not isinstance(steps, list):
            return False
        for step in steps:
            if not isinstance(step, dict):
                continue
            sub_name = BATCH_STEP_TOOLS.get(str(step.get("action") or ""))
            # 未登记的 action 按写操作保守处理（fail-closed）：它本会被插件执行侧
            # 拒绝，治理侧绝不能静默放行（read_only 档的底线）。
            if sub_name is None or sub_name in _GOVERNANCE_WRITES:
                return True
        return False

    @staticmethod
    def _approval_digest(tool_name: str, args: dict[str, Any]) -> str:
        try:
            payload = json.dumps(
                [tool_name, args], ensure_ascii=False, sort_keys=True, default=str
            )
        except (TypeError, ValueError):
            payload = repr([tool_name, args])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _issue_approval_token(
        self, tool_name: str, args: dict[str, Any], owner_id: str, session_id: str
    ) -> str:
        now = time.monotonic()
        for token, grant in list(self._approval_tokens.items()):
            if grant.expires_at <= now:
                del self._approval_tokens[token]
        session = self._governance_session(owner_id, session_id)
        token = uuid.uuid4().hex
        self._approval_tokens[token] = _ApprovalGrant(
            owner=str(owner_id or ""),
            session_id=str(session_id or ""),
            digest=self._approval_digest(tool_name, args),
            generation=session.generation if session else -1,
            ref=str(args.get("ref") or ""),
            expires_at=now + _APPROVAL_TOKEN_TTL_SECONDS,
        )
        return token

    def _governance_gate(
        self, tool_name: str, args: dict[str, Any], owner_id: str, session_id: str
    ) -> ToolPermissionDecision | None:
        """动作治理：各分支参数/ref 校验通过后，按 governance_mode 决定放行/审批/拒绝。"""
        mode = self.config.governance_mode or "confirm_sensitive"
        if mode == "off":
            return None
        sensitive = self._sensitive_reason(tool_name, args, owner_id, session_id)
        if tool_name == "browser_batch":
            is_write = self._batch_has_write(args)
        else:
            is_write = tool_name in _GOVERNANCE_WRITES
        if mode == "read_only":
            if sensitive is not None or is_write:
                return ToolPermissionDecision(
                    "deny",
                    "浏览器当前为只读模式（governance_mode=read_only），"
                    "写交互与高危动作被拒绝",
                )
            return None
        if sensitive is None and not (mode == "confirm_writes" and is_write):
            return None
        reason = sensitive or (
            "批量操作包含写交互步骤，可能改变页面状态"
            if tool_name == "browser_batch"
            else f"将执行写交互 {tool_name.removeprefix('browser_')}，可能改变页面状态"
        )
        # 无交互环境（子 agent 等无 push_fn）下 ask 会被 fail-closed 成拒绝。
        reason += "（子任务等无法弹审批的环境会被直接拒绝，可改用 takeover 交还用户操作）"
        return ToolPermissionDecision(
            "ask",
            reason,
            allow_always=tool_name not in _GOVERNANCE_NO_ALLOW_ALWAYS,
            approval_token=self._issue_approval_token(tool_name, args, owner_id, session_id),
        )

    def permission_for(
        self, tool_name: str, args: dict[str, Any], owner_id: str, session_id: str
    ) -> ToolPermissionDecision | None:
        if tool_name == "browser_fill_form":
            try:
                fields = self._validated_fill_form_fields(args.get("fields"))
            except BrowserDriverError as exc:
                return ToolPermissionDecision("deny", str(exc))
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            if session is None:
                return ToolPermissionDecision(
                    "deny", "批量表单要求当前页面的最新 snapshot"
                )
            for field in fields:
                ref = str(field["ref"])
                try:
                    parsed = BrowserRef.parse(ref)
                except ValueError:
                    return ToolPermissionDecision(
                        "deny", "批量表单的 ref 无效或不属于当前页面"
                    )
                if (
                    parsed.generation != session.generation
                    or ref not in session.refs
                ):
                    return ToolPermissionDecision(
                        "deny",
                        "批量表单目标不属于同一最新 generation",
                    )
            # fill_form never submits. Strict schema, latest-generation refs
            # and Host-side per-field exact-Locator checks are sufficient for
            # ordinary form completion; confirm_sensitive 档不额外审批，
            # confirm_writes/read_only 档由末尾治理门统一处理。
        elif tool_name in {
            "browser_select",
            "browser_check",
            "browser_hover",
            "browser_drop",
        }:
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            ref = str(args.get("ref") or "")
            if session is None or ref not in session.refs:
                return ToolPermissionDecision(
                    "deny", "表单目标的 ref 不属于当前页面或已失效"
                )
            if tool_name == "browser_select":
                try:
                    self._validated_select_values(args.get("values"))
                except BrowserDriverError as exc:
                    return ToolPermissionDecision("deny", str(exc))
            elif tool_name == "browser_check" and type(args.get("checked")) is not bool:
                return ToolPermissionDecision("deny", "check checked 必须是 boolean")
        # browser_upload / browser_download / browser_dialog 无参数级校验
        # （路径、大小、ref 在真正执行路径中重新验证），直接落到末尾治理门。
        elif tool_name == "browser_click" and args.get("screenshot_id"):
            pass  # 截图坐标点击无 ref，跳过 ref 校验；治理在末尾统一判定
        elif tool_name == "browser_click":
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            ref = str(args.get("ref") or "")
            if session is None or ref not in session.refs:
                return ToolPermissionDecision(
                    "deny", "click 的 ref 不属于当前页面或已失效"
                )
            try:
                self._validated_click_options(
                    args.get("button", "left"),
                    args.get("click_count", 1),
                    args.get("modifiers", []),
                    args.get("delay_ms", 0),
                )
            except BrowserDriverError as exc:
                return ToolPermissionDecision("deny", str(exc))
            # Element clicks always dispatch the real Playwright Locator action.
            # There is no href-direct-open substitution; 提交型点击由治理层
            # 按需加一次性审批。
        elif tool_name == "browser_drag":
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            start_ref = str(args.get("start_ref") or "")
            end_ref = str(args.get("end_ref") or "")
            if (
                session is None
                or start_ref not in session.refs
                or end_ref not in session.refs
            ):
                return ToolPermissionDecision(
                    "deny", "drag 的 start_ref/end_ref 不属于当前页面或已失效"
                )
        elif tool_name == "browser_type":
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            ref = str(args.get("ref") or "")
            text = args.get("text")
            if session is None or ref not in session.refs:
                return ToolPermissionDecision(
                    "deny", "type 的 ref 不属于当前页面或已失效"
                )
            if (
                not isinstance(text, str)
                or type(args.get("submit", False)) is not bool
                or type(args.get("slowly", False)) is not bool
            ):
                return ToolPermissionDecision("deny", "type 参数无效")
        elif tool_name in {"browser_press", "browser_keydown", "browser_keyup"}:
            key = args.get("key")
            ref = str(args.get("ref") or "")
            try:
                self._validated_key(key)
            except BrowserDriverError as exc:
                return ToolPermissionDecision("deny", str(exc))
            if tool_name != "browser_press" and ref:
                return ToolPermissionDecision(
                    "deny", f"{tool_name.removeprefix('browser_')} 不接受 ref"
                )
            if ref:
                owner = self._owners.get(owner_id)
                session = owner.sessions.get(session_id) if owner else None
                if session is None or ref not in session.refs:
                    return ToolPermissionDecision(
                        "deny", "press 的 ref 不属于当前页面或已失效"
                    )
        elif tool_name == "browser_wait":
            try:
                self._validated_wait(
                    args.get("time_seconds", 0),
                    args.get("text", ""),
                    args.get("text_gone", ""),
                )
            except BrowserDriverError as exc:
                return ToolPermissionDecision("deny", str(exc))
        # 所有工具统一过治理门：上面的分支只做参数/ref 校验，治理不再是各分支
        # 自觉调用的可选项（新增分支忘了 return 也不会绕过档位）。wait/navigate/
        # tabs:new 等无写无敏感语义的动作经门判定后照常放行。
        return self._governance_gate(tool_name, args, owner_id, session_id)

    @staticmethod
    def _tool_call_id() -> str:
        from crew.core.runctx import current_tool_call_id

        return str(current_tool_call_id.get() or "")

    def confirm_approval(
        self,
        token: str,
        tool_name: str,
        args: dict[str, Any],
        owner_id: str,
        session_id: str,
    ) -> bool:
        """确认一次性审批令牌：无论成败都弹出，杜绝重放。"""
        grant = self._approval_tokens.pop(str(token or ""), None)
        if grant is None:
            return False
        if (
            grant.expires_at <= time.monotonic()
            or grant.owner != str(owner_id or "")
            or grant.session_id != str(session_id or "")
            or grant.digest != self._approval_digest(tool_name, args)
        ):
            return False
        # 防 TOCTOU：审批之后页面换代或目标 ref 失效，授权自动作废。
        session = self._governance_session(owner_id, session_id)
        current_generation = session.generation if session else -1
        if current_generation != grant.generation:
            return False
        if grant.ref and (session is None or grant.ref not in session.refs):
            return False
        return True

    def state(self, owner_id: str, session_id: str) -> dict[str, Any]:
        owner = self._owners.get(str(owner_id or ""))
        session = owner.sessions.get(str(session_id or "")) if owner else None
        if owner is None or session is None:
            state = BrowserPageState(_hash(owner_id), _hash(session_id))
            if self.config.enabled and not self.available():
                reason = getattr(self.driver, "availability_error", None)
                state.last_error = (
                    str(reason() or "桌面内置浏览器不可用")
                    if callable(reason)
                    else "桌面内置浏览器不可用"
                )
            return state.public_dict()
        return self._page_state(owner, session).public_dict()

    async def read_tab_content(
        self,
        owner_id: str,
        session_id: str,
        tab_id: str,
        *,
        max_chars: int = PAGE_TEXT_LIMIT,
    ) -> dict[str, str]:
        """读取指定标签页的 {title, url, text}（只读 eval）；任何失败抛 BrowserDriverError。

        只读观察：不加 owner.lock（Host 按 owner 串行执行 RPC，读不与其他动作互相
        破坏），不触碰 session 观察状态。与 state() 一致：只 peek 已存在的 owner，
        不为一次只读访问创建 owner / 启动 policy proxy。
        """
        limit = max(1, min(int(max_chars), PAGE_TEXT_LIMIT))
        owner = self._owners.get(str(owner_id or ""))
        if owner is None:
            raise BrowserDriverError("当前账号没有浏览器会话")
        session = owner.sessions.get(str(session_id or ""))
        if session is None:
            raise BrowserDriverError("当前会话没有浏览器标签页")
        tab = session.tabs.get(str(tab_id or ""))
        if tab is None:
            raise BrowserDriverError("标签页不存在或已关闭")
        if session.mode != "ai":
            # Host 同样拒绝（control_mode_blocked）；这里提前给出面向用户的明确原因。
            raise BrowserDriverError("人工接管或暂停期间不可读取标签页内容；请先交还 AI")
        if not tab.target_id:
            raise BrowserDriverError("标签页尚未就绪，缺少不可伪造的 targetId")
        result = await self.driver.execute_targeted(
            owner.runtime_key,
            owner.profile_dir,
            "eval",
            [PAGE_TEXT_SCRIPT],
            target_id=tab.target_id,
            timeout=float(self.config.command_timeout_seconds),
            proxy_url=owner.proxy.url if owner.proxy else "",
            mutating=False,
        )
        return parse_page_text_result(result, limit)

    def _page_state(self, owner: _Owner, session: _Session) -> BrowserPageState:
        tab = session.tabs.get(session.active_label) or _Tab("", "")
        return BrowserPageState(
            owner_hash=_hash(owner.owner),
            session_hash=_hash(session.session_id),
            tab_id=tab.id,
            tab_label=tab.label,
            url=_public_url(tab.url),
            title=tab.title,
            generation=session.generation,
            mode=session.mode,  # type: ignore[arg-type]
            running=owner.running,
            last_action=session.last_action,
            last_error=session.last_error,
            screenshot_id=session.screenshot_id,
            viewport_width=session.viewport_width,
            viewport_height=session.viewport_height,
            can_go_back=session.can_go_back,
            can_go_forward=session.can_go_forward,
            queue_depth=owner.lock.queue_depth,
            last_queue_wait_ms=round(owner.lock.last_queue_wait_ms, 2),
            last_operation_ms=round(owner.lock.last_operation_ms, 2),
            queue_timeouts=owner.lock.queue_timeouts,
            tabs=[self._public_tab(value) for value in session.tabs.values()],
            downloads=_safe_public_value(list(session.downloads)),
        )

    async def subscribe(self, owner_id: str, session_id: str) -> AsyncIterator[dict[str, Any]]:
        key = (str(owner_id or ""), str(session_id or ""))
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=4)
        self._subscribers.setdefault(key, set()).add(queue)
        await queue.put({"type": "state", "state": self.state(*key)})
        try:
            while True:
                yield await queue.get()
        finally:
            subscribers = self._subscribers.get(key)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(key, None)

    async def human_command(
        self, owner_id: str, session_id: str, action: str, value: str = ""
    ) -> dict[str, Any]:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            if session.mode != "human":
                raise BrowserDriverError("请先接管浏览器再执行人工导航")
            if action == "close_tab":
                label = value or session.active_label
                if label not in session.tabs:
                    raise BrowserDriverError("标签页不属于当前 Crew 会话")
                tab = session.tabs[label]
                await self._close_tab_target(owner, session, tab)
                session.tabs.pop(label, None)
                if session.active_label == label:
                    session.active_label = next(iter(session.tabs), "")
                self._clear_native_selection(owner)
                if session.active_label:
                    await self._select(owner, session)
                    await self._refresh_metadata(owner, session, workdir="")
                session.generation += 1
                self._invalidate_observation(session)
                session.last_action = "人工关闭标签页"
                state = self._page_state(owner, session).public_dict()
                await self._publish(owner.owner, session.session_id, {"type": "state", "state": state})
                return state
            if action == "select_tab":
                if value not in session.tabs:
                    raise BrowserDriverError("标签页不属于当前 Crew 会话")
                await self._close_session_stream(owner, session)
                session.active_label = value
                tab, _ = await self._select(owner, session)
            else:
                tab, _ = await self._select(owner, session)
            if action == "navigate":
                safe_url = self.policy.validate_navigation_url(value)
                await self._run(owner, session, "open", [safe_url], navigation=True, mutating=True)
                tab.url = safe_url
            elif action == "back":
                await self._run(owner, session, "back", mutating=True)
            elif action == "forward":
                await self._run(owner, session, "forward", mutating=True)
            elif action == "reload":
                await self._run(owner, session, "reload", mutating=True)
            elif action == "select_tab":
                pass
            else:
                raise BrowserDriverError("无效人工导航动作")
            session.generation += 1
            self._clear_ref_state(session)
            self._clear_screenshot(session)
            session.last_action = f"人工操作：{action}"
            await self._refresh_metadata(owner, session, workdir="")
            state = self._page_state(owner, session).public_dict()
            await self._publish(owner.owner, session.session_id, {"type": "state", "state": state})
            return state

    @_observed_browser("browser.open_for_user")
    async def open_for_user(
        self,
        owner_id: str,
        session_id: str,
        *,
        url: str = "",
        new_tab: bool = False,
        artifact_path: str = "",
        artifact_root: str = "",
    ) -> dict[str, Any]:
        """Open a user-controlled tab without exposing a model tool.

        Local HTML uses the Host's private artifact command.  The public
        navigation policy remains HTTP(S)-only and models never receive an
        arbitrary file path or file:// capability.
        """
        if url and artifact_path:
            raise BrowserDriverError("浏览器不能同时打开网页地址和本地 HTML")
        safe_url = self.policy.validate_navigation_url(url) if url else "about:blank"
        preview_file: Path | None = None
        preview_root: Path | None = None
        if artifact_path:
            try:
                preview_file = Path(artifact_path).expanduser().resolve(strict=True)
                preview_root = Path(artifact_root).expanduser().resolve(strict=True)
            except (OSError, ValueError) as exc:
                raise BrowserDriverError("本地 HTML 文件不存在") from exc
            if (
                not preview_root.is_dir()
                or not preview_file.is_file()
                or preview_file.suffix.lower() not in {".html", ".htm"}
                or not path_is_within(preview_file, [preview_root])
            ):
                raise BrowserDriverError("本地预览仅允许当前工作区内的 HTML 文件")

        owner = await self._owner(owner_id)
        async with owner.control_lock:
            return await self._open_for_user_with_control(
                owner,
                session_id,
                url=url,
                new_tab=new_tab,
                safe_url=safe_url,
                preview_file=preview_file,
                preview_root=preview_root,
            )

    async def _open_for_user_with_control(
        self,
        owner: _Owner,
        session_id: str,
        *,
        url: str,
        new_tab: bool,
        safe_url: str,
        preview_file: Path | None,
        preview_root: Path | None,
    ) -> dict[str, Any]:
        async with owner.lock:
            if owner.stopping or owner.actions_blocked or owner.stop_unconfirmed:
                raise BrowserDriverError("账号浏览器已停止；请先恢复浏览器后再打开")
            session = self._session(owner, session_id)
            if session.tabs and session.mode == "ai":
                await self._clear_human_buffers(owner, session)

            created = bool(new_tab or not session.tabs)
            if created:
                previous_active = session.active_label
                tab = self._new_tab(session)
                initial_url = "about:blank" if preview_file is not None else safe_url
                try:
                    await self._run(
                        owner,
                        session,
                        "tab",
                        ["new-user", "--label", tab.label, initial_url],
                        navigation=initial_url != "about:blank",
                        mutating=True,
                    )
                except BrowserDriverError as exc:
                    if not (exc.uncertain or exc.browser_stopped or exc.stop_unconfirmed):
                        self._rollback_new_tab(session, tab, previous_active)
                    raise
                owner.selected_label = tab.label
                self._clear_native_selection(owner)
                await self._select(owner, session)
            else:
                tab, _ = await self._select(owner, session)

            await self._set_driver_mode(owner, session, "human")
            session.mode = "human"
            if preview_file is not None and preview_root is not None:
                result = await self._run(
                    owner,
                    session,
                    "preview",
                    [str(preview_file), str(preview_root)],
                    navigation=True,
                    mutating=True,
                )
                tab.url = _text(result).strip().strip('"')
                session.last_action = f"人工预览 {preview_file.name}"
            elif not created and url:
                await self._run(
                    owner,
                    session,
                    "open",
                    [safe_url],
                    navigation=True,
                    mutating=True,
                )
                tab.url = safe_url
                session.last_action = f"人工导航到 {_public_url(safe_url)}"
            else:
                session.last_action = "人工打开浏览器"

            session.generation += 1
            self._invalidate_observation(session)
            await self._refresh_metadata(owner, session, workdir="")
            state = self._page_state(owner, session).public_dict()
            await self._publish(owner.owner, session.session_id, {"type": "state", "state": state})
            return state

    async def _publish(self, owner_id: str, session_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get((owner_id, session_id), set())):
            if queue.full():
                with suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    @staticmethod
    def _png_size(path: Path) -> tuple[int, int]:
        with path.open("rb") as stream:
            header = stream.read(24)
        if len(header) >= 24 and header[:8] == b"\x89PNG\r\n\x1a\n":
            return struct.unpack(">II", header[16:24])
        return 0, 0
