"""Account-scoped browser lifecycle, tab isolation and Crew ref protocol."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import shutil
import struct
import time
import uuid
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from urllib.parse import unquote, urlsplit

from crew.browser.driver import BrowserDriver, BrowserDriverError, BrowserOperationCancelled
from crew.browser.electron_driver import ElectronBrowserDriver
from crew.browser.security import BrowserNetworkPolicy, LoopbackPolicyProxy, path_is_within
from crew.browser.types import BrowserConfig, BrowserPageState, BrowserRef
from crew.core.types import MediaPart, ToolOutput, ToolPermissionDecision
from crew.state.home import get_owner_runtime_home
from crew.state.logging import get_logger
from crew.tools.redact import redact_sensitive_display_text, redact_url_for_display

log = get_logger("browser.manager")

# Accessibility snapshots render refs as ``[ref=e17]`` while the internal
# driver command form uses ``@e17``. Accept both at this boundary and always
# store the canonical command form below.
_REF_PATTERN = re.compile(r"(?<=ref=)@?e(\d+)(?=$|[\],\s])")
# 截断硬切兜底用：尾部一段还没闭合的 [ref=pN:eM 片段。留着它等于把一个残缺却仍然
# 合法的 ref（p42:e17 被切成 p42:e1）交给模型，会造成静默误点击。
_REF_TAIL_PATTERN = re.compile(r"\[ref=p?\d*:?e?\d*$")
_HIGH_RISK_PATTERN = re.compile(
    r"(?:send|submit|publish|post|buy|purchase|pay|checkout|place\s*order|subscribe|"
    r"delete|remove|confirm|grant|revoke|invite|transfer|withdraw|deactivate|suspend|"
    r"save\s*changes|sign\s*(?:in|up|out)|login|logout|log\s*(?:in|out)|"
    r"(?:change|reset)\s*(?:password|role|permission|email|phone)|"
    r"发送|提交|发布|购买|支付|下单|结算|订阅|删除|确认|确定|授权|撤销|邀请|转账|提现|"
    r"保存更改|开通|注销|退出登录|停用|禁用|修改密码|更改权限|角色变更|登录|登陆)",
    re.IGNORECASE,
)
_SAFE_KEY_PATTERN = re.compile(
    r"^(?:Enter|Tab|Escape|Backspace|Delete|Arrow(?:Up|Down|Left|Right)|Home|End|Page(?:Up|Down)|F[1-9]|F1[0-2]|[A-Za-z0-9])$"
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

# Chromium commonly reports a main-frame/same-document navigation shortly
# after an input dispatch returns. A short quiet period prevents publishing a
# mixed generation (for example new AX/title with the pre-pushState URL) while
# keeping every snapshot wait bounded. Real Baidu tracing on a narrow viewport
# observed the main-frame event about 57 ms after Enter returned.
_PAGE_TRANSITION_QUIET_SECONDS = 0.12
_PAGE_TRANSITION_MAX_SECONDS = 0.75
_PAGE_TRANSITION_POLL_SECONDS = 0.04

# browser_use 在开始执行时取得能力代次，并把它作为 task-local lease 带到
# BrowserManager。这样 revoke 即使发生在 handler 的入口校验之后、owner 冷启动
# 之前，_owner/_run 仍会在真正创建实例或发送 RPC 前拒绝旧动作。直接调用
# BrowserManager 的兼容代码不设置 lease，保持原 API 兼容。
_EXPECTED_CAPABILITY: ContextVar[tuple[str, int] | None] = ContextVar(
    "browser_expected_capability", default=None
)


def _navigation_requires_approval(description: str, navigation_url: str) -> bool:
    """Classify obvious final-action links from both label and destination.

    A GET is not intrinsically side-effect free, so this is deliberately a
    conservative product boundary rather than a proof. The Electron browser
    Host still opens only exact, handler-free links directly; destinations whose
    decoded path/query advertise a final action require one-shot approval.
    """
    if _HIGH_RISK_PATTERN.search(str(description or "")):
        return True
    try:
        parsed = urlsplit(str(navigation_url or ""))
        # Hash routers execute navigation entirely from the fragment, so it is
        # as security-sensitive as a server path/query for destination checks.
        risk_text = f"{parsed.hostname or ''} {parsed.path} {parsed.query} {parsed.fragment}"
    except ValueError:
        return True
    # Servers and frameworks sometimes decode routing values more than once.
    # Bound the work while recognizing common encoded action names.
    for _ in range(3):
        decoded = unquote(risk_text, errors="replace")
        if decoded == risk_text:
            break
        risk_text = decoded
    # Deeply nested escapes are intentionally opaque after the bounded
    # decode passes. A remaining valid escape therefore requires approval.
    if re.search(r"%[0-9a-f]{2}", risk_text, re.IGNORECASE):
        return True
    risk_text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", risk_text)
    risk_text = risk_text.replace("_", " ")
    normalized = re.sub(r"[^\w\u3400-\u9fff]+", " ", risk_text)
    return bool(_HIGH_RISK_PATTERN.search(normalized))


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


def _escape_wrapper_markers(text: str) -> str:
    """转义会伪造 Crew 固定包装标签的字符。

    所有写进 <untrusted_browser_*> 边界的页面派生文本——正文、title、url——都必须先
    过这里。漏掉任何一处，页面就能用字面 </untrusted_browser_content> 逃出隔离区，
    并伪造 tool.py 的 <browser_action_result> 信封谎报动作成功。
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _truncate_snapshot_at_line(text: str, limit: int) -> tuple[str, str]:
    """按行边界截断 snapshot 正文；返回 (正文, 截断说明)。

    绝不能裸切字符：ref 形如 p42:e17，切点落在中间会得到 p42:e1——一个同样合法、
    却指向别的元素的 ref，模型据此静默误点击。每个 ref 都完整落在一行内，所以在 \\n
    处截断即可保证不劈开任何 ref。

    截断说明**不写进正文**，由调用方作为 Crew 头部字段（与 page_generation 同级）发出。
    写进正文的话，页面只要把同样的句子塞进某个元素的可访问名，就能伪造出「下面全都没
    看全、未显示区域的 ref 不可引用」——一个稳定的观察拒止原语，而且恰好长在这层边界
    加固想守住的地方。头部字段位置由 Crew 独占，页面伪造不了。
    """
    limit = max(1, int(limit))
    if len(text) <= limit:
        return text, ""
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        # 单行超长（罕见）：退回到不会劈开 [ref=..] 的最近右括号，实在没有才硬切。
        bracket = text.rfind("]", 0, limit)
        cut = bracket + 1 if bracket > 0 else limit
    shown = text[:cut]
    # 硬切兜底仍可能落在 ref 中间：把尾部残缺的 ref 片段去掉，宁可少给也不给错的。
    shown = _REF_TAIL_PATTERN.sub("", shown)
    notice = (
        f"显示 {len(shown)}/{len(text)} 字符，其余未显示；页面并未到此为止，"
        f"需要其余元素请 scroll 或对目标区域重新 snapshot，未显示区域的 ref 不可引用"
    )
    return shown, notice


def _bounded(value: Any, *, kind: str = "content", limit: int = 30_000) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = redact_sensitive_display_text(text)
    # Browser responses are untrusted page content. Escape the only characters
    # that can forge Crew's fixed wrapper tags before adding the boundary.
    text = _escape_wrapper_markers(text)
    tag = "untrusted_browser_console" if kind == "console" else "untrusted_browser_content"
    return f"<{tag}>\n{text[:limit]}\n</{tag}>"


def _public_url(value: str) -> str:
    return redact_url_for_display(value)


def _safe_public_value(value: Any) -> Any:
    if isinstance(value, str):
        redacted = redact_sensitive_display_text(value)
        return _DEBUG_SECRET_ASSIGNMENT.sub(r"\1\2***", redacted)
    if isinstance(value, dict):
        return {
            str(key): "***" if _DEBUG_SECRET_KEY.fullmatch(str(key)) else _safe_public_value(item)
            for key, item in value.items()
        }
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
    ref_security: dict[str, str] = field(default_factory=dict)
    ref_navigation: dict[str, str] = field(default_factory=dict)
    mode: str = "ai"
    last_action: str = ""
    last_error: str = ""
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


@dataclass
class _Owner:
    owner: str
    runtime_key: str
    profile_dir: Path
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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
class _Approval:
    token: str
    owner: str
    session_id: str
    tool_call_id: str
    tool_name: str
    url: str
    generation: int
    page_marker: str
    target: str
    target_security: str
    pre_context: bool
    args_hash: str
    expires_at: float


class BrowserManager:
    def __init__(self, config: BrowserConfig, driver: BrowserDriver | None = None) -> None:
        self.config = config
        self.driver = driver or ElectronBrowserDriver(config)
        self.policy = BrowserNetworkPolicy(config)
        self._owners: dict[str, _Owner] = {}
        self._owners_lock = asyncio.Lock()
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = {}
        self._idle_task: asyncio.Task | None = None
        self._prepare_task: asyncio.Task | None = None
        self._pending_approvals: dict[str, _Approval] = {}
        self._granted_approvals: dict[tuple[str, str, str, str], _Approval] = {}
        # 能力代次：用户关闭/重开 Browser 能力时单调递增。旧代次签发的审批、
        # ref、截图与标签页句柄一律不可复用（见 revoke_owner）。
        self._capability_generations: dict[str, int] = {}
        self._closed = False

    def available(self) -> bool:
        return bool(self.config.enabled and self.driver.available())

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
        return self._capability_generations.get(str(owner_account_id or ""), 0)

    def _bump_capability_generation(self, owner_account_id: str) -> int:
        owner_id = str(owner_account_id or "")
        new_value = self._capability_generations.get(owner_id, 0) + 1
        self._capability_generations[owner_id] = new_value
        return new_value

    def renew_capability(self, owner_account_id: str) -> int:
        """重新启用能力时递增代次，并失效旧审批与页面观察句柄。"""
        owner_id = str(owner_account_id or "").strip()
        self._clear_owner_approvals(owner_id)
        owner = self._owners.get(owner_id)
        if owner is not None:
            self._clear_native_selection(owner)
            for session in owner.sessions.values():
                self._invalidate_observation(session)
        return self._bump_capability_generation(owner_id)

    def capability_runtime_state(self, owner_account_id: str) -> dict[str, bool]:
        """Return a synchronous fail-closed view for capability enable gates."""
        owner = self._owners.get(str(owner_account_id or "").strip())
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
        owner_id = str(owner_account_id or "").strip()
        token = _EXPECTED_CAPABILITY.set((owner_id, int(expected_generation)))
        try:
            yield
        finally:
            _EXPECTED_CAPABILITY.reset(token)

    def _ensure_leased_capability_current(self, owner_account_id: str) -> None:
        lease = _EXPECTED_CAPABILITY.get()
        if lease is None or lease[0] != str(owner_account_id or "").strip():
            return
        self.ensure_capability_current(lease[0], lease[1])

    def _clear_owner_approvals(self, owner_id: str) -> None:
        """清空某 owner 的待审批与已授权 token（revoke 后旧 token 必须全部失效）。"""
        self._pending_approvals = {
            token: value
            for token, value in self._pending_approvals.items()
            if value.owner != owner_id
        }
        self._granted_approvals = {
            key: value
            for key, value in self._granted_approvals.items()
            if key[0] != owner_id
        }

    async def revoke_owner(self, owner_account_id: str) -> None:
        """立即撤销某 owner 的 Browser 能力（用户关闭插件开关）。

        顺序：递增 capability generation → 置 closing/actions_blocked → 清空审批 →
        关闭其标签页与 Host owner（磁盘 Profile/Cookie 保留）。fail-stop：任何一步
        失败该 owner 保持 blocked 且不回收到可用集合，绝不静默恢复。其它 owner 不受影响。
        """
        owner_id = str(owner_account_id or "").strip()
        if not owner_id:
            return
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
                self._clear_owner_approvals(owner_id)

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
            self._prune_approvals()
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
        """Start the mandatory account proxy, cleaning partial startup safely."""
        if owner.proxy is not None:
            owner.initialized = True
            return
        proxy = LoopbackPolicyProxy(self.policy)
        try:
            await proxy.start()
        except BaseException:
            with suppress(Exception, asyncio.CancelledError):
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

    @staticmethod
    def _session(owner: _Owner, session_id: str) -> _Session:
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
        if exc.uncertain and session is not None:
            # 结果未知：这一会话的旧 ref 不能再用，必须重新观察。但其它会话的标签页
            # 与本次动作无关，不受牵连。
            self._invalidate_observation(session)
            if owner.native_ref_session == session.session_id:
                owner.native_ref_session = ""
                owner.native_ref_generation = 0

    async def _raise_driver_error(
        self,
        owner: _Owner,
        session: _Session,
        exc: BrowserDriverError,
    ) -> None:
        """Promote driver lifecycle failures consistently across all paths."""
        await self._apply_driver_lifecycle_failure(owner, session, exc)
        safe_error = redact_sensitive_display_text(str(exc))
        session.last_error = safe_error
        await self._publish(
            owner.owner,
            session.session_id,
            {"type": "error", "error": safe_error},
        )
        raise BrowserDriverError(
            safe_error,
            uncertain=exc.uncertain,
            browser_stopped=exc.browser_stopped,
            stop_unconfirmed=exc.stop_unconfirmed,
            # code 必须透传：工具层据此把「ref 失效」这类可恢复失败补上最新观察，
            # 丢了 code 就退化成模型无法据以行动的死错误。
            code=getattr(exc, "code", ""),
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
    ) -> dict[str, Any]:
        self._ensure_leased_capability_current(owner.owner)
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；请先交还 AI 后再执行浏览器动作")
        owner.last_activity = time.monotonic()
        timeout = (
            self.config.navigation_timeout_seconds
            if navigation
            else self.config.command_timeout_seconds
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
                    max_bytes=self.config.max_transfer_bytes,
                    timeout=timeout,
                    proxy_url=proxy_url,
                    download_dir=quarantine,
                )
            else:
                result = await self.driver.execute(
                    owner.runtime_key,
                    owner.profile_dir,
                    command,
                    args,
                    timeout=timeout,
                    proxy_url=proxy_url,
                    # The Electron Host owner is shared by all sessions of an
                    # account. A fixed account quarantine prevents cross-task
                    # writes; browser_download grants one exact staging target
                    # and restores deny before releasing this account lock.
                    download_dir=quarantine,
                    mutating=mutating,
                )
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

    def _new_tab(self, session: _Session) -> _Tab:
        if len(session.tabs) >= self.config.max_tabs_per_session:
            raise BrowserDriverError(f"单会话最多允许 {self.config.max_tabs_per_session} 个标签页")
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
            opener_target_id = item.get("openerTargetId")
            if (
                not isinstance(tab_id, str)
                or re.fullmatch(r"t[1-9]\d*", tab_id) is None
                or tab_id in tab_ids
                or (label is not None and not isinstance(label, str))
                or (target_id is not None and not isinstance(target_id, str))
                or (opener_target_id is not None and not isinstance(opener_target_id, str))
                or not isinstance(active, bool)
            ):
                raise BrowserDriverError("浏览器返回了无效的标签页状态")
            tab_ids.add(tab_id)
            rows.append(
                {
                    "tabId": tab_id,
                    "label": label,
                    "active": active,
                    "url": str(item.get("url") or "")[:4096],
                    "title": str(item.get("title") or "")[:2048],
                    "targetId": str(target_id or "")[:256],
                    "openerTargetId": str(opener_target_id or "")[:256],
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

    async def _select(self, owner: _Owner, session: _Session) -> tuple[_Tab, bool]:
        if not session.active_label or session.active_label not in session.tabs:
            raise BrowserDriverError("当前会话没有浏览器标签页，请先调用 browser_navigate")
        tab = session.tabs[session.active_label]
        owned_labels = {
            value.label
            for owned_session in owner.sessions.values()
            for value in owned_session.tabs.values()
            if value.native_labeled and value.label
        }
        owned_target_ids = {
            value.target_id
            for owned_session in owner.sessions.values()
            for value in owned_session.tabs.values()
            if value.target_id
        }
        session_target_ids = {value.target_id for value in session.tabs.values() if value.target_id}
        owned_native_ids = {
            value.native_id
            for owned_session in owner.sessions.values()
            for value in owned_session.tabs.values()
            if value.native_id
        }
        changed_native_target = False

        # A page can open and activate an unlabeled popup between Crew calls.
        # owner.selected_label is therefore only a hint; tab list is the native
        # source of truth. Reconcile a bounded number of times because closing
        # or switching a target may itself deliver pending Target events.
        for _ in range(4):
            try:
                rows = self._native_tab_rows(await self._run(owner, session, "tab", ["list"]))
            except BrowserDriverError:
                self._reject_ambiguous_native_selection(owner, session)
                raise

            active_rows = [row for row in rows if row["active"]]
            desired_rows = (
                [row for row in rows if row["label"] == tab.label]
                if tab.native_labeled
                else [row for row in rows if tab.target_id and row.get("targetId") == tab.target_id]
            )
            if len(active_rows) != 1 or len(desired_rows) != 1:
                self._reject_ambiguous_native_selection(owner, session)
                raise BrowserDriverError("无法唯一确认当前 Crew 标签页，已拒绝执行浏览器动作")

            desired = desired_rows[0]
            desired_target_id = str(desired.get("targetId") or "")
            if tab.target_id:
                if desired_target_id != tab.target_id:
                    self._reject_ambiguous_native_selection(owner, session)
                    raise BrowserDriverError(
                        "当前标签页 targetId 已变化；拒绝使用可能复用的原生 tabId"
                    )
            elif desired_target_id:
                # targetId is immutable once captured. Native tN may be reused
                # after a Host tab-epoch reset and is never an ownership identity.
                tab.target_id = desired_target_id
                owned_target_ids.add(desired_target_id)
                session_target_ids.add(desired_target_id)
            elif not tab.native_labeled:
                self._reject_ambiguous_native_selection(owner, session)
                raise BrowserDriverError("人工弹窗缺少不可伪造的 targetId，已拒绝操作")

            unowned = [
                row
                for row in rows
                if row["label"] not in owned_labels and row.get("targetId") not in owned_target_ids
            ]
            if unowned:
                # An unlabeled popup may be adopted only when the browser host
                # proves its immutable opener belongs to this exact Crew
                # session. Resolve chains to a fixed point so a popup opened by
                # an already verified child is safe as well. Everything else —
                # including initial about:blank pages and another session's
                # popup — is closed by exact target identity.
                pending = list(unowned)
                adopted_active: _Tab | None = None
                while pending:
                    progressed = False
                    for row in list(pending):
                        target_id = str(row.get("targetId") or "")
                        opener_target_id = str(row.get("openerTargetId") or "")
                        native_id = str(row.get("tabId") or "")
                        if (
                            not target_id
                            or opener_target_id not in session_target_ids
                            or len(session.tabs) >= self.config.max_tabs_per_session
                            or native_id in session.tabs
                            or native_id in owned_native_ids
                        ):
                            continue
                        popup = _Tab(
                            id=native_id,
                            label=native_id,
                            native_id=native_id,
                            target_id=target_id,
                            native_labeled=False,
                            url=str(row.get("url") or ""),
                            title=str(row.get("title") or ""),
                        )
                        session.tabs[popup.id] = popup
                        session_target_ids.add(target_id)
                        owned_target_ids.add(target_id)
                        owned_native_ids.add(native_id)
                        pending.remove(row)
                        progressed = True
                        if row.get("active") is True:
                            adopted_active = popup
                    if not progressed:
                        break

                # Cross-session isolation: a popup whose immutable opener belongs
                # to ANOTHER live session of this account is that session's tab
                # (e.g. an in-progress OAuth/payment window); it must never be
                # closed from here. Only genuinely orphaned popups (no opener, or
                # an own opener that could not be adopted) are closed by exact
                # target identity.
                foreign_target_ids = {
                    other_tab.target_id
                    for other in owner.sessions.values()
                    if other is not session
                    for other_tab in other.tabs.values()
                    if other_tab.target_id
                }
                for row in pending:
                    if str(row.get("openerTargetId") or "") in foreign_target_ids:
                        continue
                    try:
                        await self._close_tab_target(
                            owner,
                            session,
                            _Tab(
                                id="",
                                label="",
                                target_id=str(row.get("targetId") or ""),
                                native_labeled=False,
                            ),
                        )
                    except BrowserDriverError:
                        self._reject_ambiguous_native_selection(owner, session)
                        raise
                if pending or adopted_active is not None:
                    changed_native_target = True
                    self._clear_native_selection(owner)
                if adopted_active is not None:
                    session.active_label = adopted_active.id
                    tab = adopted_active
                if pending or adopted_active is not None:
                    continue

            tab.native_id = str(desired["tabId"])
            if active_rows[0]["tabId"] == desired["tabId"]:
                owner.selected_label = tab.label
                return tab, changed_native_target

            try:
                await self._run(owner, session, "tab", [tab.native_id or tab.label])
            except BrowserDriverError:
                # BrowserHost clears its ref epoch before the native tab switch;
                # never retain Crew refs after an uncertain switch failure.
                self._reject_ambiguous_native_selection(owner, session)
                raise
            changed_native_target = True
            self._clear_native_selection(owner)
            owner.selected_label = tab.label

        self._reject_ambiguous_native_selection(owner, session)
        raise BrowserDriverError("浏览器标签页持续变化，已拒绝执行动作；请重新观察页面")

    @staticmethod
    def _invalidate_observation(session: _Session, *, bump_generation: bool = True) -> None:
        if bump_generation:
            session.generation += 1
        session.refs.clear()
        session.ref_security.clear()
        session.ref_navigation.clear()
        BrowserManager._clear_screenshot(session)
        session.page_marker = ""

    async def _select_checked(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str = "",
        verify_page: bool = True,
    ) -> _Tab:
        """Select the session tab and reject refs after an out-of-band navigation."""
        had_observation = bool(session.refs or session.page_marker or session.screenshot_id)
        tab, switched = await self._select(owner, session)
        if switched and had_observation:
            # Native refs belong to the selected tab. Never send a previously
            # issued @e ref after another Crew session has been active; require
            # a fresh snapshot instead.
            self._invalidate_observation(session)
            await self._refresh_metadata(owner, session, workdir=workdir)
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
        if not verify_page:
            # ElectronBrowserHost 会在 exact-ref 动作内部、实际输入事件发送前，
            # 原子复核页面身份、AX 签名、DOM 安全指纹与命中目标。这里保留
            # tab/ref 归属检查，但避免再做一遍跨进程 page_guard 往返。
            return tab
        marker = await self._read_page_guard(owner, session, workdir=workdir)
        if session.page_marker and not self._same_page_marker(marker, session.page_marker):
            session.generation += 1
            session.refs.clear()
            self._clear_screenshot(session)
            session.page_marker = marker
            await self._refresh_metadata(owner, session, workdir=workdir)
            raise BrowserDriverError(
                "页面已在审批或上次观察后变化；旧 ref/截图已失效，请重新 snapshot"
            )
        # Mutation counters and scrolling are intentionally not page-ref
        # invalidators.  Keep the latest marker, however, so coordinate clicks
        # can still reject a screenshot whose DOM/scroll position is stale.
        session.page_marker = marker
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
        """Screenshots additionally bind DOM mutations and scroll position."""
        left = cls._marker_data(current)
        right = cls._marker_data(observed)
        if left is None or right is None:
            return current == observed
        fields = (
            "token",
            "targetId",
            "frameId",
            "loaderId",
            "securityDigest",
            "counter",
            "href",
            "timeOrigin",
            "scrollX",
            "scrollY",
            "width",
            "height",
            "dpr",
        )
        return all(left.get(field) == right.get(field) for field in fields)

    @classmethod
    def _same_security_surface_marker(cls, current: str, observed: str) -> bool:
        """Bind snapshot refs while tolerating cosmetic/live text updates."""
        left = cls._marker_data(current)
        right = cls._marker_data(observed)
        if left is None or right is None:
            return current == observed
        return cls._same_page_marker(current, observed) and left.get("securityDigest") == right.get(
            "securityDigest"
        )

    @classmethod
    def _same_capture_marker(cls, current: str, capture: str) -> bool:
        """Require one settled transition and security surface for snapshot publication.

        The quiet gate runs before the AX capture.  A navigation can start just
        after that gate while href/loader/security still describe the old page;
        comparing only the security surface would then publish AX/title from a
        transition under the old URL.  Bind both post-capture checks to the
        exact host transition epoch and require the observed state to remain
        settled.  Drivers without a transition epoch keep the legacy strict
        security-surface comparison.
        """
        current_signature = cls._page_transition_signature(current)
        capture_signature = cls._page_transition_signature(capture)
        if current_signature is None or capture_signature is None:
            return cls._same_security_surface_marker(current, capture)
        return bool(
            current_signature == capture_signature
            and cls._page_transition_ready(current)
            and cls._same_security_surface_marker(current, capture)
        )

    @classmethod
    def _page_transition_signature(cls, marker: str) -> tuple[Any, ...] | None:
        """Return only fields that describe a main-page transition.

        Mutation counters and security-surface churn are deliberately absent:
        dynamic pages may mutate forever, while snapshot's existing capture
        guards still bind the exact security surface before refs are published.

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
    def _result_scalar(result: dict[str, Any]) -> str:
        value = _data(result)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, dict):
            for key in ("text", "attribute", "attr", "value", "result"):
                item = value.get(key)
                if item is None:
                    continue
                if isinstance(item, (str, int, float, bool)):
                    return str(item)
        return ""

    @staticmethod
    def _snapshot_target_signature(stored: str) -> tuple[str, str]:
        """Extract the role/name rendered by an accessibility snapshot line."""
        line = stored.split("\n", 1)[-1]
        match = re.match(
            r'^\s*-\s*([A-Za-z][\w-]*)(?:\s+"((?:\\.|[^"\\])*)")?',
            line,
        )
        if match is None:
            return "", ""
        role = match.group(1).strip().casefold()
        encoded_name = match.group(2)
        if encoded_name is None:
            return role, ""
        try:
            name = json.loads(f'"{encoded_name}"')
        except (TypeError, ValueError, json.JSONDecodeError):
            name = encoded_name
        return role, " ".join(str(name).split()).casefold()

    async def _target_still_matches_snapshot(
        self,
        owner: _Owner,
        session: _Session,
        ref: str,
        native_ref: str,
        *,
        workdir: str,
    ) -> bool:
        """Revalidate one ref after unrelated live-DOM mutations.

        This deliberately uses only fixed ``get`` operations.  It never
        exposes selectors or arbitrary JavaScript to the model.  A failure or
        ambiguous target is fail-closed and requires a fresh snapshot.
        """
        baseline_role, baseline_name = self._snapshot_target_signature(session.refs.get(ref, ""))
        if not baseline_role or not baseline_name:
            return False
        baseline_security = session.ref_security.get(ref, "")
        if isinstance(self.driver, ElectronBrowserDriver) and not baseline_security:
            return False

        values: dict[str, str] = {}
        commands = (
            ("text", ["text", native_ref]),
            ("aria-label", ["attr", native_ref, "aria-label"]),
            ("title", ["attr", native_ref, "title"]),
            ("alt", ["attr", native_ref, "alt"]),
            ("role", ["attr", native_ref, "role"]),
            ("type", ["attr", native_ref, "type"]),
        )
        try:
            for key, args in commands:
                result = await self._run(owner, session, "get", args, workdir=workdir)
                values[key] = " ".join(self._result_scalar(result).split())[:500]
        except BrowserDriverError:
            return False

        current_name = next(
            (values[key] for key in ("aria-label", "text", "title", "alt") if values.get(key)),
            "",
        )
        explicit_role = values.get("role", "").casefold()
        if not current_name or current_name.casefold() != baseline_name:
            return False
        if explicit_role and explicit_role != baseline_role:
            return False

        # A harmless label can become a submit control without changing its
        # text.  Treat all current target metadata as security-sensitive.
        current_description = " ".join(values.values())
        baseline_description = session.refs.get(ref, "")
        if _HIGH_RISK_PATTERN.search(current_description) and not _HIGH_RISK_PATTERN.search(
            baseline_description
        ):
            return False

        marker = await self._read_page_guard(owner, session, workdir=workdir)
        if not self._same_page_marker(marker, session.page_marker):
            return False
        if baseline_security:
            marker_data = self._marker_data(marker) or {}
            element_security = marker_data.get("elementSecurity")
            key = f"{baseline_role}\0{baseline_name}"
            if (
                not isinstance(element_security, dict)
                or element_security.get(key) != baseline_security
            ):
                return False
        # Refs bind document identity and the exact element fingerprint, not
        # the global MutationObserver counter or scroll position. Preserve the
        # latest valid marker so a second pre-dispatch check can detect a target
        # replacement without rejecting unrelated live-DOM churn.
        session.page_marker = marker
        return True

    def _ref_marker_still_matches(
        self,
        session: _Session,
        ref: str,
        current: str,
        observed: str,
    ) -> bool:
        """Compare a ref's exact security identity across a short action race."""
        if not self._same_page_marker(current, observed):
            return False
        baseline_security = session.ref_security.get(ref, "")
        if not baseline_security:
            # Compatibility drivers do not expose per-element fingerprints.
            # Their security-surface digest remains the strict fallback.
            return self._same_security_surface_marker(current, observed)
        role, name = self._snapshot_target_signature(session.refs.get(ref, ""))
        marker_data = self._marker_data(current) or {}
        element_security = marker_data.get("elementSecurity")
        return bool(
            role
            and name
            and isinstance(element_security, dict)
            and element_security.get(f"{role}\0{name}") == baseline_security
        )

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

    async def _reset_page_guard(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str,
    ) -> None:
        session.page_marker = await self._page_guard(
            owner,
            session,
            reset=True,
            workdir=workdir,
        )

    async def _stable_capture_marker(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str,
    ) -> str:
        """Wait for a bounded quiet main-page marker, then arm capture guard.

        This runs at the start of every snapshot, including explicit recovery
        snapshots. Host-reported pending navigation and short delayed
        pushState events therefore settle before a fresh generation can be
        published.
        """
        marker = await self._page_guard(
            owner,
            session,
            reset=True,
            include_security=False,
            workdir=workdir,
        )
        signature = self._page_transition_signature(marker)
        if signature is None:
            # Alternative/older drivers have no host transition epoch. Preserve
            # compatibility, but ensure a partially upgraded driver cannot make
            # snapshot capture proceed without the full security surface.
            if "securityDigest" not in (self._marker_data(marker) or {}):
                marker = await self._page_guard(
                    owner,
                    session,
                    reset=True,
                    include_security=True,
                    workdir=workdir,
                )
            session.page_marker = marker
            return marker

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PAGE_TRANSITION_MAX_SECONDS
        stable_since = loop.time() if self._page_transition_ready(marker) else None
        while True:
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "页面仍处于导航或 URL 更新的过渡状态，本次未发布 snapshot；"
                    "请稍后重新 snapshot"
                )
            if (
                stable_since is not None
                and now - stable_since >= _PAGE_TRANSITION_QUIET_SECONDS
            ):
                capture_marker = await self._page_guard(
                    owner,
                    session,
                    reset=True,
                    include_security=True,
                    workdir=workdir,
                )
                capture_signature = self._page_transition_signature(capture_marker)
                if (
                    capture_signature == signature
                    and self._page_transition_ready(capture_marker)
                ):
                    session.page_marker = capture_marker
                    return capture_marker
                marker = capture_marker
                signature = capture_signature
                stable_since = loop.time() if self._page_transition_ready(marker) else None
                continue
            await asyncio.sleep(min(_PAGE_TRANSITION_POLL_SECONDS, remaining))
            current = await self._page_guard(
                owner,
                session,
                reset=False,
                include_security=False,
                workdir=workdir,
            )
            current_signature = self._page_transition_signature(current)
            current_ready = self._page_transition_ready(current)
            if current_signature != signature:
                signature = current_signature
                stable_since = loop.time() if current_ready else None
            elif not current_ready:
                stable_since = None
            elif stable_since is None:
                stable_since = loop.time()
            marker = current

    async def _read_page_guard(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str,
    ) -> str:
        return await self._page_guard(
            owner,
            session,
            reset=False,
            workdir=workdir,
        )

    async def _page_guard(
        self,
        owner: _Owner,
        session: _Session,
        *,
        reset: bool,
        include_security: bool = True,
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
                "timeout": self.config.command_timeout_seconds,
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
            )
        )

    async def _observe_after_mutation(
        self,
        owner: _Owner,
        session: _Session,
        *,
        workdir: str,
    ) -> str:
        try:
            # Navigation/click/input events can synchronously open and activate
            # an unlabeled popup. Reconcile against native tab state before any
            # post-action dialog check, metadata read, or snapshot.
            await self._select(owner, session)
            return await self._snapshot_locked(owner, session, full=False, workdir=workdir)
        except BrowserDriverError as exc:
            if exc.uncertain:
                raise
            raise BrowserDriverError(
                f"动作已发送，但后置页面观察失败：{exc}；结果未知，请重新观察，Crew 不会自动重复该动作",
                uncertain=True,
            ) from None

    @staticmethod
    def _require_ai(owner: _Owner, session: _Session) -> None:
        if owner.closing or owner.stopping or owner.actions_blocked:
            raise BrowserDriverError("账号浏览器已停止；请先交还 AI 后再执行浏览器动作")
        if session.mode == "human":
            raise BrowserDriverError("用户正在接管浏览器，AI 动作已暂停")
        if session.mode == "paused":
            raise BrowserDriverError("浏览器动作已暂停")

    async def navigate(self, owner_id: str, session_id: str, url: str, *, workdir: str = "") -> str:
        safe_url = self.policy.validate_navigation_url(url)
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            requires_approval = _navigation_requires_approval("", safe_url)
            if not session.tabs:
                if requires_approval:
                    self._consume_approval(
                        "browser_navigate",
                        {"url": safe_url},
                        owner,
                        session,
                    )
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
                if requires_approval:
                    tab = await self._select_checked(owner, session, workdir=workdir)
                    self._consume_approval(
                        "browser_navigate",
                        {"url": safe_url},
                        owner,
                        session,
                    )
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

    async def snapshot(
        self, owner_id: str, session_id: str, *, full: bool = False, workdir: str = ""
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select(owner, session)
            return await self._snapshot_locked(owner, session, full=full, workdir=workdir)

    async def _snapshot_locked(
        self, owner: _Owner, session: _Session, *, full: bool, workdir: str
    ) -> str:
        dialog = _data(await self._run(owner, session, "dialog", ["status"], workdir=workdir))
        if isinstance(dialog, dict) and dialog.get("hasDialog") is True:
            self._invalidate_observation(session)
            owner.native_ref_session = ""
            owner.native_ref_generation = 0
            safe_dialog = {
                "dialog_pending": True,
                "type": dialog.get("type"),
                "message": dialog.get("message"),
                "default_value": dialog.get("defaultValue"),
                "instruction": (
                    "请调用 browser_use 的 dialog_status action；"
                    "confirm/prompt 不会被自动接受。"
                ),
            }
            return _bounded(safe_dialog, limit=self.config.max_output_chars)
        capture_marker = await self._stable_capture_marker(owner, session, workdir=workdir)
        args = ["--compact"] if not full else []
        result = await self._run(owner, session, "snapshot", args, workdir=workdir)
        observed_marker = await self._read_page_guard(owner, session, workdir=workdir)
        if not self._same_capture_marker(observed_marker, capture_marker):
            self._invalidate_observation(session)
            owner.native_ref_session = ""
            owner.native_ref_generation = 0
            raise BrowserDriverError("页面在 snapshot 采集期间发生导航或视口变化，请重新观察")
        raw = _text(result)
        session.generation += 1
        generation = session.generation
        session.refs = {}
        session.ref_security = {}
        session.ref_navigation = {}
        self._clear_screenshot(session)

        ref_nonce = uuid.uuid4().hex
        placeholder_refs: dict[str, tuple[str, str]] = {}

        def rewrite(match: re.Match[str]) -> str:
            native = f"@e{match.group(1)}"
            crew_ref = f"p{generation}:e{match.group(1)}"
            placeholder = f"__CREW_REF_{ref_nonce}_{match.group(1)}__"
            placeholder_refs.setdefault(placeholder, (crew_ref, native))
            return placeholder

        # Redact the complete document, not individual lines. Private-key and
        # similar canonical patterns span newlines. Register refs only from
        # the redacted output, so a ref hidden inside a secret block cannot be
        # addressed later through an unsafe description.
        redacted = redact_sensitive_display_text(_REF_PATTERN.sub(rewrite, raw))
        placeholder_pattern = re.compile(rf"__CREW_REF_{re.escape(ref_nonce)}_(\d+)__")
        output_lines: list[str] = []
        for safe_line in redacted.splitlines():
            found = list(placeholder_pattern.finditer(safe_line))
            rendered = placeholder_pattern.sub(
                lambda match: f"p{generation}:e{match.group(1)}",
                safe_line,
            )
            output_lines.append(rendered)
            for match in found:
                placeholder = match.group(0)
                crew_ref, native = placeholder_refs[placeholder]
                session.refs.setdefault(
                    crew_ref,
                    f"{native}\n{rendered.strip()[:500]}",
                )
        bounded = "\n".join(output_lines)
        owner.native_ref_session = session.session_id
        owner.native_ref_generation = generation
        result_data = _data(result)
        if (
            isinstance(self.driver, ElectronBrowserDriver)
            and isinstance(result_data, dict)
            and isinstance(result_data.get("url"), str)
            and isinstance(result_data.get("title"), str)
        ):
            tab = self._active_tab(session)
            tab.url = str(result_data.get("url") or tab.url)[:2048]
            tab.title = str(result_data.get("title") or tab.title)[:2048]
            session.can_go_back = result_data.get("can_go_back") is True
            session.can_go_forward = result_data.get("can_go_forward") is True
        else:
            await self._refresh_metadata(owner, session, workdir=workdir)
        await self._lock_downloads(owner)
        final_marker = await self._read_page_guard(owner, session, workdir=workdir)
        if not self._same_capture_marker(final_marker, capture_marker):
            self._invalidate_observation(session)
            owner.native_ref_session = ""
            owner.native_ref_generation = 0
            raise BrowserDriverError("页面在 snapshot 发布前发生导航或视口变化，请重新观察")
        session.page_marker = final_marker
        marker_data = self._marker_data(final_marker) or {}
        element_security = marker_data.get("elementSecurity")
        element_navigation = marker_data.get("elementNavigation")
        if isinstance(element_security, dict):
            for crew_ref, description in session.refs.items():
                role, name = self._snapshot_target_signature(description)
                key = f"{role}\0{name}"
                value = element_security.get(key)
                if isinstance(value, str) and value:
                    session.ref_security[crew_ref] = value
                navigation = (
                    element_navigation.get(key) if isinstance(element_navigation, dict) else None
                )
                if isinstance(navigation, str) and navigation:
                    session.ref_navigation[crew_ref] = navigation
        await self._publish(
            owner.owner,
            session.session_id,
            {"type": "state", "state": self._page_state(owner, session).public_dict()},
        )
        # title 与 url 均取自页面（title 见 _snapshot_locked 里 tab.title=页面标题），
        # 是不可信数据，必须与正文走同一转义，否则页面可在 title 里塞
        # </untrusted_browser_content> 逃出隔离区并伪造 <browser_action_result> 信封。
        title = _escape_wrapper_markers(redact_sensitive_display_text(self._active_tab(session).title))
        url = _escape_tag_markers(_public_url(self._active_tab(session).url))
        boundary_safe, truncation = _truncate_snapshot_at_line(
            _escape_wrapper_markers(bounded), self.config.max_output_chars
        )
        # 截断说明走 Crew 独占的头部位置（与 page_generation 同级），不混进正文——
        # 混进正文页面就能用元素名伪造同样的句子来制造「什么都没看全」的假象。
        truncated_line = f"truncated: {truncation}\n" if truncation else ""
        return (
            "<untrusted_browser_content>\n"
            f"page_generation: p{generation}\nurl: {url}\n"
            f"title: {title}\n{truncated_line}{boundary_safe}\n"
            "</untrusted_browser_content>"
        )

    async def _lock_downloads(self, owner: _Owner) -> None:
        if owner.downloads_locked:
            return
        quarantine = self._download_quarantine(owner)
        try:
            await self.driver.deny_downloads(
                owner.runtime_key,
                owner.profile_dir,
                proxy_url=owner.proxy.url if owner.proxy else "",
                download_dir=quarantine,
            )
        except BrowserOperationCancelled as exc:
            await self._apply_driver_lifecycle_failure(owner, None, exc)
            owner.downloads_locked = False
            raise
        except Exception as exc:
            # Continuing with an unknown global download directory could let a
            # later ordinary click write into a previously approved task. Stop
            # the shared browser and require an explicit return/restart.
            owner.actions_blocked = True
            stopped = False
            try:
                await self.driver.interrupt(owner.runtime_key, owner.profile_dir)
                stopped = True
            except BrowserOperationCancelled as cancel_exc:
                await self._apply_driver_lifecycle_failure(owner, None, cancel_exc)
                raise
            except Exception:
                pass
            if stopped:
                owner.running = False
                owner.selected_label = ""
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
            owner.stop_unconfirmed = not stopped
            owner.downloads_locked = False
            raise BrowserDriverError(f"无法锁定浏览器下载策略，账号浏览器已冻结：{exc}") from None
        owner.downloads_locked = True
        if quarantine.exists():
            await asyncio.to_thread(shutil.rmtree, quarantine, True)

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
        """Clear model-readable debug buffers at both sides of takeover."""
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
                setattr(tab, field_name, value[:2048])
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
            "title": redact_sensitive_display_text(tab.title),
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

    async def _announce_target(
        self,
        owner: _Owner,
        session: _Session,
        native_ref: str,
        description: str,
        *,
        workdir: str,
    ) -> None:
        target: dict[str, float] = {}
        if not isinstance(self.driver, ElectronBrowserDriver):
            with suppress(BrowserDriverError, TypeError, ValueError):
                value = _data(
                    await self._run(owner, session, "get", ["box", native_ref], workdir=workdir)
                )
                if isinstance(value, dict) and isinstance(value.get("box"), dict):
                    value = value["box"]
                if isinstance(value, dict):
                    target = {
                        key: float(value[key])
                        for key in ("x", "y", "width", "height")
                        if key in value
                    }
        await self._publish(
            owner.owner,
            session.session_id,
            {"type": "action", "description": description[:500], "target": target},
        )

    async def click(self, owner_id: str, session_id: str, ref: str, *, workdir: str = "") -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            navigation_url = session.ref_navigation.get(ref, "")
            host_exact_ref = isinstance(self.driver, ElectronBrowserDriver) and not navigation_url
            await self._select_checked(
                owner,
                session,
                workdir=workdir,
                verify_page=not host_exact_ref,
            )
            native = self._native_ref(session, ref)
            requires_approval = _navigation_requires_approval(
                session.refs.get(ref, ""),
                navigation_url,
            )
            await self._announce_target(owner, session, native, f"点击 {ref}", workdir=workdir)
            if not host_exact_ref:
                if not await self._target_still_matches_snapshot(
                    owner,
                    session,
                    ref,
                    native,
                    workdir=workdir,
                ):
                    self._invalidate_observation(session)
                    owner.native_ref_session = ""
                    owner.native_ref_generation = 0
                    raise BrowserDriverError(
                        "目标元素在 snapshot 后发生变化或无法建立唯一安全身份；"
                        "请调用 browser_use 的 snapshot action，仍失败时请由用户接管"
                    )
                pre_click_marker = await self._read_page_guard(owner, session, workdir=workdir)
                if not self._ref_marker_still_matches(
                    session,
                    ref,
                    pre_click_marker,
                    session.page_marker,
                ):
                    self._invalidate_observation(session)
                    owner.native_ref_session = ""
                    owner.native_ref_generation = 0
                    raise BrowserDriverError(
                        "目标元素在点击前再次变化；旧 ref 已失效，"
                        "请调用 browser_use 的 snapshot action"
                    )
                session.page_marker = pre_click_marker
            if navigation_url:
                if requires_approval:
                    self._consume_approval("browser_click", {"ref": ref}, owner, session)
                safe_url = self.policy.validate_navigation_url(navigation_url)
                await self._run(
                    owner,
                    session,
                    "open",
                    [safe_url],
                    navigation=True,
                    mutating=True,
                    workdir=workdir,
                )
                self._active_tab(session).url = safe_url
                session.last_action = f"打开安全链接 {ref}"
            else:
                if requires_approval:
                    self._consume_approval("browser_click", {"ref": ref}, owner, session)
                await self._run(owner, session, "click", [native], mutating=True, workdir=workdir)
                session.last_action = f"点击 {ref}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def fill(
        self,
        owner_id: str,
        session_id: str,
        ref: str,
        text: str,
        *,
        submit: bool = False,
        workdir: str = "",
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            host_exact_ref = isinstance(self.driver, ElectronBrowserDriver)
            await self._select_checked(
                owner,
                session,
                workdir=workdir,
                verify_page=not host_exact_ref,
            )
            native = self._native_ref(session, ref)
            await self._announce_target(owner, session, native, f"填写 {ref}", workdir=workdir)
            if not host_exact_ref:
                if not await self._target_still_matches_snapshot(
                    owner,
                    session,
                    ref,
                    native,
                    workdir=workdir,
                ):
                    self._invalidate_observation(session)
                    owner.native_ref_session = ""
                    owner.native_ref_generation = 0
                    raise BrowserDriverError(
                        "输入目标属性与 snapshot 不一致；旧 ref 已失效，"
                        "请调用 browser_use 的 snapshot action"
                    )
                pre_fill_marker = await self._read_page_guard(owner, session, workdir=workdir)
                if not self._ref_marker_still_matches(
                    session,
                    ref,
                    pre_fill_marker,
                    session.page_marker,
                ):
                    self._invalidate_observation(session)
                    owner.native_ref_session = ""
                    owner.native_ref_generation = 0
                    raise BrowserDriverError(
                        "输入目标在填写前再次变化；旧 ref 已失效，"
                        "请调用 browser_use 的 snapshot action"
                    )
                session.page_marker = pre_fill_marker
            if submit:
                # type+submit 会在填完后原子按 Enter 提交表单（=导航），与 press Enter
                # 同属高危，消一次性审批。关键：输入本身在审批通过后才发生，审批延迟
                # 期间页面未被改动，ref 不会因等待审批而失效（这正是修复搜索流程的点）。
                self._consume_approval(
                    "browser_type", {"ref": ref, "text": text, "submit": True}, owner, session
                )
            fill_args = [native, str(text)] + (["--submit"] if submit else [])
            await self._run(
                owner, session, "fill", fill_args, mutating=True, workdir=workdir
            )
            session.last_action = f"填写并提交 {ref}" if submit else f"填写 {ref}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def scroll(
        self, owner_id: str, session_id: str, direction: str, pixels: int, *, workdir: str = ""
    ) -> str:
        if direction not in {"up", "down", "left", "right"}:
            raise BrowserDriverError("scroll direction 无效")
        return await self._action(
            owner_id,
            session_id,
            "scroll",
            [direction, str(max(1, min(int(pixels), 5000)))],
            f"向{direction}滚动",
            workdir=workdir,
        )

    async def back(self, owner_id: str, session_id: str, *, workdir: str = "") -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            await self._run(owner, session, "back", [], mutating=True, workdir=workdir)
            session.last_action = "后退"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def press(
        self,
        owner_id: str,
        session_id: str,
        key: str,
        *,
        ref: str = "",
        workdir: str = "",
    ) -> str:
        if not _SAFE_KEY_PATTERN.fullmatch(str(key or "")):
            raise BrowserDriverError("仅允许单键和安全导航键；禁止剪贴板/组合快捷键")
        if key == "Enter" and not ref:
            raise BrowserDriverError(
                "Enter 必须绑定最近 snapshot 中的明确 ref；不能向未知焦点提交表单"
            )
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            native = ""
            host_exact_ref = isinstance(self.driver, ElectronBrowserDriver) and bool(ref)
            await self._select_checked(
                owner,
                session,
                workdir=workdir,
                verify_page=not host_exact_ref,
            )
            if ref:
                native = self._native_ref(session, ref)
                await self._announce_target(
                    owner,
                    session,
                    native,
                    f"在 {ref} 按 {key}",
                    workdir=workdir,
                )
                if not host_exact_ref and not await self._target_still_matches_snapshot(
                    owner,
                    session,
                    ref,
                    native,
                    workdir=workdir,
                ):
                    self._invalidate_observation(session)
                    owner.native_ref_session = ""
                    owner.native_ref_generation = 0
                    raise BrowserDriverError(
                        "按键目标属性与 snapshot 不一致；旧 ref 已失效，请重新观察"
                    )
            if key == "Enter":
                self._consume_approval(
                    "browser_press",
                    {"key": key, "ref": ref},
                    owner,
                    session,
                )
            await self._run(
                owner,
                session,
                "press",
                [key, native] if native else [key],
                mutating=True,
                workdir=workdir,
            )
            session.last_action = f"在 {ref} 按键 {key}" if ref else f"按键 {key}"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def console(
        self,
        owner_id: str,
        session_id: str,
        *,
        kind: str = "console",
        clear: bool = False,
        workdir: str = "",
    ) -> str:
        if kind not in {"console", "network"}:
            raise BrowserDriverError("console kind 仅支持 console/network")
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            command = "console" if kind == "console" else "network"
            args = (
                (["--clear"] if clear else [])
                if kind == "console"
                else ["requests", *(["--clear"] if clear else [])]
            )
            result = await self._run(owner, session, command, args, workdir=workdir)
            return _bounded(
                _text(result),
                kind="console" if kind == "console" else "content",
                limit=self.config.max_output_chars,
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
            await self._reset_page_guard(owner, session, workdir=workdir)
            capture_marker = session.page_marker
            screenshot_id = uuid.uuid4().hex
            path = self._artifact_dir(session) / f"{screenshot_id}.png"
            args = [str(path)]
            result = await self._run(owner, session, "screenshot", args, workdir=workdir)
            observed_marker = await self._read_page_guard(owner, session, workdir=workdir)
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
            marker_matches = self._same_screenshot_marker(
                observed_marker,
                capture_marker,
            )
            if not marker_matches:
                path.unlink(missing_ok=True)
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "页面在视觉截图采集期间发生变化，请重新调用 browser_use 的 vision action"
                )
            width, height = self._png_size(path)
            session.screenshot_id = screenshot_id
            session.screenshot_host_epoch = host_epoch
            session.screenshot_generation = session.generation
            session.screenshot_path = str(path)
            session.viewport_width, session.viewport_height = width, height
            session.last_action = "获取视觉截图"
            session.page_marker = observed_marker
            marker = self._marker_data(session.page_marker) or {}
            dpr = float(marker.get("dpr") or 1)
            session.screenshot_dpr = dpr if dpr > 0 else 1
            session.screenshot_coordinates_allowed = True
            session.screenshot_css_width = float(
                marker.get("width") or (width / session.screenshot_dpr if width else 0)
            )
            session.screenshot_css_height = float(
                marker.get("height") or (height / session.screenshot_dpr if height else 0)
            )
            session.screenshot_marker = session.page_marker
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
        settled: bool = True,
        workdir: str = "",
    ) -> str:
        """把当前页面视口截图导出为 PNG，保存到当前任务 downloads/browser/ 并返回路径。

        与 vision（给模型自己看的多模态输入）不同，这是面向用户的导出：
        不经过 VLM、不要求模型视觉能力；文件边界与 download 一致（工作区内
        downloads/browser/，符号链接与大小检查相同）。默认 settled 模式只释放
        由 Crew 填写遗留的页面焦点/高亮，不执行任意 Escape；无需审批。
        """
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            await self._reset_page_guard(owner, session, workdir=workdir)
            safe_name = self._safe_download_name(filename)
            if not safe_name.lower().endswith(".png"):
                safe_name = f"{safe_name}.png"
            staging_target = self._artifact_dir(session) / f"shot-{uuid.uuid4().hex}.png"
            screenshot_args = [str(staging_target)]
            if settled:
                # User-facing exports remove only focus/highlight left by Crew
                # automation. vision keeps the exact live interaction state so
                # its screenshot epoch remains suitable for coordinate clicks.
                screenshot_args.insert(0, "--settled")
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
            if staging_target.stat().st_size > self.config.max_transfer_bytes:
                staging_target.unlink(missing_ok=True)
                raise BrowserDriverError("截图超过 browser max_transfer_bytes，已删除")
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
            return str(target)

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
            if not session.screenshot_marker or not self._same_screenshot_marker(
                session.page_marker,
                session.screenshot_marker,
            ):
                self._clear_screenshot(session)
                raise BrowserDriverError(
                    "页面内容、滚动位置或视口已变化，请重新调用 browser_use 的 vision action"
                )
            if x < 0 or y < 0 or x >= session.viewport_width or y >= session.viewport_height:
                raise BrowserDriverError("坐标超出截图范围")
            self._consume_approval(
                "browser_click",
                {"screenshot_id": screenshot_id, "x": x, "y": y},
                owner,
                session,
            )
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
                    download_dir=self._download_quarantine(owner),
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
                # for a second approval after any atomic-host failure.
                self._clear_screenshot(session)
                await self._raise_driver_error(owner, session, exc)
                raise AssertionError("unreachable")
            if atomic_result is not None:
                owner.running = True
                session.last_error = ""
                await self._publish(
                    owner.owner,
                    session.session_id,
                    {
                        "type": "action",
                        "description": f"坐标点击 ({x}, {y})",
                        "target": {"x": x - 8, "y": y - 8, "width": 16, "height": 16},
                    },
                )
                session.last_action = f"坐标点击 ({x}, {y})"
                return await self._observe_after_mutation(owner, session, workdir=workdir)

            # Compatibility drivers without the Host's atomic primitive retain
            # the fixed hit-test + input sequence below. Production Electron
            # never exposes or executes this page-world eval path.
            hit_test = (
                "(()=>{const e=document.elementFromPoint(" + str(css_x) + "," + str(css_y) + ");"
                "if(!e)return null;const r=e.getBoundingClientRect();return {tag:e.tagName,role:e.getAttribute('role')||'',"
                "name:(e.getAttribute('aria-label')||e.innerText||e.value||'').trim().slice(0,160),"
                "box:[r.x,r.y,r.width,r.height]}})()"
            )
            hit = _text(await self._run(owner, session, "eval", [hit_test], workdir=workdir))
            if (
                not hit
                or hit in {"null", "{}"}
                or re.search(r'"tag"\s*:\s*"(?:HTML|BODY)"', hit, re.I)
            ):
                raise BrowserDriverError("DOM hit-test 无法确定坐标目标，需要用户接管或确认")
            await self._publish(
                owner.owner,
                session.session_id,
                {
                    "type": "action",
                    "description": f"坐标点击 ({x}, {y})",
                    "target": {"x": x - 8, "y": y - 8, "width": 16, "height": 16},
                },
            )
            # Upstream has move/down/up/wheel subcommands, but no
            # ``mouse click x y`` command. Always release a pressed button on
            # an error; a duplicate mouse-up cannot create a second click.
            await self._run(
                owner,
                session,
                "mouse",
                ["move", str(css_x), str(css_y)],
                mutating=True,
                workdir=workdir,
            )
            moved_marker = await self._read_page_guard(owner, session, workdir=workdir)
            if not self._same_screenshot_marker(moved_marker, session.screenshot_marker):
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "页面在鼠标移动后发生变化；视觉坐标和审批已失效，请重新观察"
                )
            pressed = False
            try:
                await self._run(
                    owner,
                    session,
                    "mouse",
                    ["down", "left"],
                    mutating=True,
                    workdir=workdir,
                )
                pressed = True
                down_marker = await self._read_page_guard(owner, session, workdir=workdir)
                if not self._same_screenshot_marker(
                    down_marker,
                    session.screenshot_marker,
                ):
                    self._invalidate_observation(session)
                    owner.native_ref_session = ""
                    owner.native_ref_generation = 0
                    raise BrowserDriverError(
                        "页面在鼠标按下后发生变化；结果未知，视觉坐标和审批已失效",
                        uncertain=True,
                    )
                await self._run(
                    owner,
                    session,
                    "mouse",
                    ["up", "left"],
                    mutating=True,
                    workdir=workdir,
                )
                pressed = False
            finally:
                if pressed:
                    with suppress(BrowserDriverError):
                        await self._run(
                            owner,
                            session,
                            "mouse",
                            ["up", "left"],
                            mutating=True,
                            workdir=workdir,
                        )
            session.last_action = f"坐标点击 ({x}, {y})"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def upload(
        self, owner_id: str, session_id: str, ref: str, paths: list[str], *, workdir: str = ""
    ) -> str:
        owner_home = get_owner_runtime_home(owner_id)
        allowed = [owner_home / "uploads"]
        if workdir:
            allowed.append(Path(workdir).expanduser())
        resolved: list[str] = []
        total = 0
        for raw in paths:
            path = Path(raw).expanduser().resolve()
            if not path.is_file() or not path_is_within(path, allowed):
                raise BrowserDriverError("上传文件仅允许来自当前任务工作区或账号 uploads/ 目录")
            total += path.stat().st_size
            if total > self.config.max_transfer_bytes:
                raise BrowserDriverError("上传文件总大小超过 browser max_transfer_bytes")
            resolved.append(str(path))
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            native = self._native_ref(session, ref)
            await self._announce_target(owner, session, native, f"上传到 {ref}", workdir=workdir)
            if not await self._target_still_matches_snapshot(
                owner,
                session,
                ref,
                native,
                workdir=workdir,
            ):
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "上传目标属性与 snapshot 不一致；旧 ref 已失效，"
                    "请调用 browser_use 的 snapshot action"
                )
            pre_upload_marker = await self._read_page_guard(owner, session, workdir=workdir)
            if not self._ref_marker_still_matches(
                session,
                ref,
                pre_upload_marker,
                session.page_marker,
            ):
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "上传目标在执行前再次变化；旧 ref 已失效，"
                    "请调用 browser_use 的 snapshot action"
                )
            session.page_marker = pre_upload_marker
            self._consume_approval("browser_upload", {"ref": ref, "paths": paths}, owner, session)
            await self._run(
                owner, session, "upload", [native, *resolved], mutating=True, workdir=workdir
            )
            session.last_action = f"上传 {len(resolved)} 个文件"
            return await self._observe_after_mutation(owner, session, workdir=workdir)

    async def download(
        self, owner_id: str, session_id: str, ref: str, filename: str = "", *, workdir: str = ""
    ) -> str:
        owner = await self._owner(owner_id)
        async with owner.lock:
            session = self._session(owner, session_id)
            self._require_ai(owner, session)
            await self._select_checked(owner, session, workdir=workdir)
            native = self._native_ref(session, ref)
            await self._announce_target(owner, session, native, f"从 {ref} 下载", workdir=workdir)
            if not await self._target_still_matches_snapshot(
                owner,
                session,
                ref,
                native,
                workdir=workdir,
            ):
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "下载目标在点击前再次变化或属性与 snapshot 不一致；"
                    "旧 ref 已失效，请调用 browser_use 的 snapshot action"
                )
            pre_download_marker = await self._read_page_guard(owner, session, workdir=workdir)
            if not self._ref_marker_still_matches(
                session,
                ref,
                pre_download_marker,
                session.page_marker,
            ):
                self._invalidate_observation(session)
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                raise BrowserDriverError(
                    "下载目标在点击前再次变化；旧 ref 已失效，"
                    "请调用 browser_use 的 snapshot action"
                )
            session.page_marker = pre_download_marker
            self._consume_approval(
                "browser_download",
                {"ref": ref, "filename": filename},
                owner,
                session,
            )
            safe_name = self._safe_download_name(filename)
            staging_parent = owner.profile_dir.parent / "approved-downloads"
            if staging_parent.is_symlink():
                raise BrowserDriverError("浏览器下载暂存目录不安全")
            staging_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging_dir = staging_parent / uuid.uuid4().hex
            staging_dir.mkdir(mode=0o700)
            staging_target = staging_dir / safe_name
            action_error: BrowserDriverError | None = None
            action_cancelled = False
            # browser_download 会临时签发单次 Host grant；finally 路径必须重新
            # 执行 deny，而不能被幂等缓存短路。
            owner.downloads_locked = False
            try:
                await self._run(
                    owner,
                    session,
                    "download",
                    [native, str(staging_target)],
                    mutating=True,
                    workdir=workdir,
                )
            except asyncio.CancelledError:
                action_cancelled = True
            except BrowserDriverError as exc:
                action_error = exc
            if action_error is not None and (
                action_error.browser_stopped or action_error.stop_unconfirmed
            ):
                # The driver already fail-stopped (or froze) this account. A
                # follow-up safety RPC could reconnect the browser we just stopped.
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise action_error
            if action_cancelled and owner.actions_blocked:
                # _run has already applied BrowserOperationCancelled lifecycle
                # metadata. Do not issue a deny RPC that could recreate a Host
                # owner after a confirmed/uncertain browser stop.
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise asyncio.CancelledError
            try:
                cleanup_cancelled = await self._complete_critical(self._lock_downloads(owner))
            except BrowserDriverError as exc:
                # The per-browser download destination is global. If restoring
                # deny fails, freeze all account actions until the browser is
                # stopped; otherwise another session could write to this task.
                owner.actions_blocked = True
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise BrowserDriverError(
                    f"下载后无法恢复安全策略：{exc}；结果未知，账号浏览器已锁定",
                    uncertain=True,
                ) from None
            if action_cancelled or cleanup_cancelled:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise asyncio.CancelledError
            if action_error is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise action_error
            try:
                if not staging_target.is_file():
                    raise BrowserDriverError("浏览器未生成下载文件")
                if staging_target.stat().st_size > self.config.max_transfer_bytes:
                    staging_target.unlink(missing_ok=True)
                    raise BrowserDriverError("下载文件超过 browser max_transfer_bytes，已删除")
                download_root = self._prepare_download_dir(session, workdir)
                target = download_root / safe_name
                if target.is_symlink() or target.is_file():
                    target.unlink()
                elif target.exists():
                    raise BrowserDriverError("下载目标已存在且不是普通文件")
                await asyncio.to_thread(shutil.move, str(staging_target), str(target))
                if not target.is_file() or not path_is_within(target, [download_root]):
                    with suppress(OSError):
                        target.unlink()
                    raise BrowserDriverError("下载目标在保存期间离开任务目录，文件已删除")
            except BrowserDriverError as exc:
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise BrowserDriverError(
                    f"下载动作已发送，但文件校验失败：{exc}；结果未知，请勿自动重复",
                    uncertain=True,
                ) from None
            shutil.rmtree(staging_dir, ignore_errors=True)
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
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", raw).strip(" .")[:180]
        if not value:
            raise BrowserDriverError("下载 filename 无效")
        stem = value.split(".", 1)[0].upper()
        if stem in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"(?:COM|LPT)[1-9]", stem):
            value = f"_{value}"
        return value

    async def dialog(
        self, owner_id: str, session_id: str, action: str, text: str = "", *, workdir: str = ""
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
            current_url = (
                _text(await self._run(owner, session, "get", ["url"], workdir=workdir))
                .strip()
                .strip('"')
            )
            if tab.url and current_url and current_url != tab.url:
                session.generation += 1
                session.refs.clear()
                self._clear_screenshot(session)
                tab.url = current_url
                raise BrowserDriverError("页面已在审批后变化；对话框操作已拒绝，请重新观察")
            if action == "accept":
                self._consume_approval(
                    "browser_dialog",
                    {"action": action, "text": text},
                    owner,
                    session,
                )
            args = [action, *([text] if text else [])]
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
                return _bounded(
                    {
                        "tabs": [self._public_tab(tab) for tab in session.tabs.values()],
                        "active": session.active_label,
                    },
                    limit=self.config.max_output_chars,
                )
            if action == "new":
                safe_url = self.policy.validate_navigation_url(url) if url else "about:blank"
                # Stream shutdown can yield while the current page navigates.
                # Finish it before the final page revalidation/approval consume.
                await self._close_session_stream(owner, session)
                if url and _navigation_requires_approval("", safe_url):
                    if session.tabs:
                        await self._select_checked(owner, session, workdir=workdir)
                    self._consume_approval(
                        "browser_tabs",
                        {"action": "new", "tab_id": "", "url": safe_url},
                        owner,
                        session,
                    )
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
            if tab_id not in session.tabs:
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
                if owner.selected_label == tab_id:
                    owner.selected_label = ""
                owner.native_ref_session = ""
                owner.native_ref_generation = 0
                session.tabs.pop(tab_id, None)
                if was_active:
                    session.active_label = next(iter(session.tabs), "")
                    session.refs.clear()
                    self._clear_screenshot(session)
                    session.page_marker = ""
                    if session.active_label:
                        await self._select(owner, session)
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
                            value.refs.clear()
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
            if not trusted_user:
                self._require_model_control(owner, session)
            if action == "takeover":
                if owner.stopping or owner.actions_blocked:
                    raise BrowserDriverError("账号浏览器已停止；请先交还 AI")
                cancelled = await self._complete_critical(self._clear_human_buffers(owner, session))
                if cancelled:
                    raise asyncio.CancelledError
                await self._set_driver_mode(owner, session, "human")
                session.mode = "human"
                session.refs.clear()
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
                # Stay in human mode until both buffers are definitely empty.
                # If clearing fails, fail closed instead of making human input
                # available to a subsequent browser_console/network call.
                # A paused session has accepted no human input and the Host
                # deliberately blocks every page/debug command, so it must be
                # switched back directly instead of trying the human cleanup
                # path (which would deadlock against the paused-mode gate).
                cancelled = False
                if session.tabs:
                    if session.mode == "human":
                        cancelled = await self._complete_critical(
                            self._clear_human_buffers(owner, session)
                        )
                        if cancelled:
                            raise asyncio.CancelledError
                    await self._set_driver_mode(owner, session, "ai")
                owner.actions_blocked = False
                if not session.tabs:
                    owner.running = False
                session.mode = "ai"
                session.refs.clear()
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
                session.refs.clear()
            else:
                raise BrowserDriverError("takeover action 无效")
            if action in {"takeover", "return"}:
                await self._publish(
                    owner.owner,
                    session.session_id,
                    {"type": "debug_clear"},
                )
            await self._publish(
                owner.owner,
                session.session_id,
                {"type": "state", "state": self._page_state(owner, session).public_dict()},
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
            # A task waiting on a permission prompt must not recreate or act
            # in a session after the user deletes it.
            self._pending_approvals = {
                token: approval
                for token, approval in self._pending_approvals.items()
                if not (approval.owner == owner.owner and approval.session_id == session.session_id)
            }
            self._granted_approvals = {
                key: approval
                for key, approval in self._granted_approvals.items()
                if not (approval.owner == owner.owner and approval.session_id == session.session_id)
            }
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
        halves finish, so a stale tab/ref/proxy can never leak into the new
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

        self._pending_approvals = {
            token: value
            for token, value in self._pending_approvals.items()
            if value.owner != owner_key
        }
        self._granted_approvals = {
            key: value
            for key, value in self._granted_approvals.items()
            if value.owner != owner_key
        }
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
                # The Electron Host refuses to create any owner without Crew's
                # mandatory policy proxy. Cold clears therefore initialize a
                # temporary proxied owner before the idempotent tab-list/clear
                # transaction; no page is created or navigated.
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
                self._pending_approvals = {
                    token: value
                    for token, value in self._pending_approvals.items()
                    if value.owner != owner_key
                }
                self._granted_approvals = {
                    key: value
                    for key, value in self._granted_approvals.items()
                    if value.owner != owner_key
                }
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
                    owner.stop_unconfirmed = True
        return {"ok": True, "cleared": True, "owner_hash": _hash(owner_key)}

    def session_for_target(self, owner_id: str, target_id: str) -> str | None:
        """Resolve one exact Host target to one non-human Crew session."""
        owner = self._owners.get(str(owner_id or ""))
        target = str(target_id or "")
        if owner is None or not target:
            return None
        matches = [
            session.session_id
            for session in owner.sessions.values()
            if session.mode != "human"
            and any(tab.target_id == target for tab in session.tabs.values())
        ]
        return matches[0] if len(matches) == 1 else None

    async def publish_host_debug(
        self,
        owner_id: str,
        session_id: str,
        target_id: str,
        channel: str,
        record: dict[str, Any],
    ) -> bool:
        """Revalidate and publish a bounded Host debug event without owner locks."""
        if channel not in {"console", "network"}:
            return False
        if self.session_for_target(owner_id, target_id) != str(session_id or ""):
            return False
        safe_record = _safe_public_value(record)
        try:
            encoded = json.dumps(
                safe_record,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode()
        except (TypeError, ValueError):
            return False
        if len(encoded) > 8 * 1024:
            safe_record = {"truncated": True}
        # Recheck after sanitization: takeover can become active without an
        # await, and human-mode page content must never enter model history.
        if self.session_for_target(owner_id, target_id) != str(session_id or ""):
            return False
        await self._publish(
            str(owner_id or ""),
            str(session_id or ""),
            {"type": "debug", "channel": channel, "record": safe_record},
        )
        return True

    def permission_for(
        self, tool_name: str, args: dict[str, Any], owner_id: str, session_id: str
    ) -> ToolPermissionDecision | None:
        reason = ""
        if tool_name in {"browser_upload", "browser_download"}:
            reason = "上传或下载文件需要一次性确认"
        elif tool_name == "browser_dialog" and args.get("action") == "accept":
            reason = "接受 confirm/prompt 对话框可能产生外部副作用"
        elif tool_name == "browser_click" and args.get("screenshot_id"):
            reason = "视觉坐标目标只经过 DOM hit-test，需要确认本次点击"
        elif tool_name == "browser_click":
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            ref = str(args.get("ref") or "")
            description = session.refs.get(ref, "") if session else ""
            navigation_url = session.ref_navigation.get(ref, "") if session else ""
            if _navigation_requires_approval(description, navigation_url):
                target_summary = description.splitlines()[-1][:160]
                destination = _public_url(navigation_url) if navigation_url else ""
                reason = f"目标可能是高风险最终动作：{target_summary}"
                if destination:
                    reason += f"；目标地址：{destination[:240]}"
            elif navigation_url:
                # The action path revalidates the exact element signature and
                # opens this HTTP(S) URL directly, bypassing page click
                # handlers. It is therefore an ordinary navigation, not an
                # untrusted DOM click.
                reason = ""
        elif tool_name == "browser_type" and bool(args.get("submit")):
            # type+submit 结尾原子按 Enter 提交表单，与 press Enter 同级高危：绑定明确
            # ref 并请求一次性确认。审批 args 含 submit，普通 type 的批准不会被复用到此。
            owner = self._owners.get(owner_id)
            session = owner.sessions.get(session_id) if owner else None
            ref = str(args.get("ref") or "")
            if session is None or ref not in session.refs:
                return ToolPermissionDecision(
                    "deny", "提交目标的 ref 不属于当前页面或已失效"
                )
            reason = "填写后按 Enter 可能提交表单，需要一次性确认"
        elif tool_name == "browser_press":
            key = str(args.get("key") or "")
            ref = str(args.get("ref") or "")
            if not _SAFE_KEY_PATTERN.fullmatch(key):
                return ToolPermissionDecision(
                    "deny",
                    "仅允许单键和安全导航键；禁止剪贴板或组合快捷键",
                )
            if key == "Enter" and not ref:
                return ToolPermissionDecision(
                    "deny",
                    "Enter 必须绑定最近 snapshot 中的明确 ref；不能向未知焦点提交表单",
                )
            if key == "Enter":
                owner = self._owners.get(owner_id)
                session = owner.sessions.get(session_id) if owner else None
                if session is None or ref not in session.refs:
                    return ToolPermissionDecision("deny", "Enter 的 ref 不属于当前页面或已失效")
                reason = "在明确元素上按 Enter 可能提交表单，需要一次性确认"
        elif tool_name == "browser_navigate":
            destination = str(args.get("url") or "")
            if _navigation_requires_approval("", destination):
                reason = (
                    "目标地址可能执行高风险最终动作，需要一次性确认："
                    f"{_public_url(destination)[:240]}"
                )
        elif tool_name == "browser_tabs" and args.get("action") == "new":
            destination = str(args.get("url") or "")
            if destination and _navigation_requires_approval("", destination):
                reason = (
                    "新标签页目标可能执行高风险最终动作，需要一次性确认："
                    f"{_public_url(destination)[:240]}"
                )
        if not reason:
            return None
        approval = self._issue_approval(tool_name, args, owner_id, session_id)
        return ToolPermissionDecision(
            "ask",
            reason,
            allow_always=False,
            approval_token=approval.token,
        )

    @staticmethod
    def _args_hash(args: dict[str, Any]) -> str:
        encoded = json.dumps(
            args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _approval_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "browser_click":
            if args.get("screenshot_id"):
                return {
                    "screenshot_id": str(args.get("screenshot_id") or ""),
                    "x": int(args.get("x", -1)),
                    "y": int(args.get("y", -1)),
                }
            return {"ref": str(args.get("ref") or "")}
        if tool_name == "browser_type":
            return {
                "ref": str(args.get("ref") or ""),
                "text": str(args.get("text") or ""),
                "submit": bool(args.get("submit")),
            }
        if tool_name == "browser_upload":
            return {
                "ref": str(args.get("ref") or ""),
                "paths": [str(item) for item in args.get("paths") or []],
            }
        if tool_name == "browser_download":
            return {
                "ref": str(args.get("ref") or ""),
                "filename": str(args.get("filename") or ""),
            }
        if tool_name == "browser_dialog":
            return {
                "action": str(args.get("action") or ""),
                "text": str(args.get("text") or ""),
            }
        if tool_name == "browser_press":
            return {
                "key": str(args.get("key") or ""),
                "ref": str(args.get("ref") or ""),
            }
        if tool_name == "browser_navigate":
            return {"url": str(args.get("url") or "").strip()}
        if tool_name == "browser_tabs":
            return {
                "action": str(args.get("action") or ""),
                "tab_id": str(args.get("tab_id") or ""),
                "url": str(args.get("url") or "").strip(),
            }
        return dict(args)

    @staticmethod
    def _tool_call_id() -> str:
        from crew.core.runctx import current_tool_call_id

        return str(current_tool_call_id.get() or "")

    def _issue_approval(
        self,
        tool_name: str,
        args: dict[str, Any],
        owner_id: str,
        session_id: str,
    ) -> _Approval:
        args = self._approval_args(tool_name, args)
        now = time.monotonic()
        self._prune_approvals(now)
        owner = self._owners.get(str(owner_id or ""))
        session = owner.sessions.get(str(session_id or "")) if owner else None
        tab = session.tabs.get(session.active_label) if session else None
        target = str(
            args.get("ref")
            or args.get("screenshot_id")
            or args.get("key")
            or args.get("action")
            or args.get("url")
            or ""
        )
        approval = _Approval(
            token=uuid.uuid4().hex,
            owner=str(owner_id or ""),
            session_id=str(session_id or ""),
            tool_call_id=self._tool_call_id(),
            tool_name=tool_name,
            url=tab.url if tab else "",
            generation=session.generation if session else 0,
            page_marker=session.page_marker if session else "",
            target=target,
            target_security=(session.ref_security.get(target, "") if session else ""),
            pre_context=tab is None,
            args_hash=self._args_hash(args),
            expires_at=now + 120,
        )
        self._pending_approvals[approval.token] = approval
        return approval

    def confirm_approval(
        self,
        token: str,
        tool_name: str,
        args: dict[str, Any],
        owner_id: str,
        session_id: str,
    ) -> bool:
        args = self._approval_args(tool_name, args)
        self._prune_approvals()
        approval = self._pending_approvals.pop(str(token or ""), None)
        owner = self._owners.get(str(owner_id or ""))
        session = owner.sessions.get(str(session_id or "")) if owner else None
        tab = session.tabs.get(session.active_label) if session else None
        invalid_common = (
            approval is None
            or approval.expires_at <= time.monotonic()
            or approval.owner != str(owner_id or "")
            or approval.session_id != str(session_id or "")
            or approval.tool_call_id != self._tool_call_id()
            or approval.tool_name != tool_name
            or approval.args_hash != self._args_hash(args)
        )
        if invalid_common:
            return False
        assert approval is not None
        if approval.pre_context:
            if approval.tool_name not in {"browser_navigate", "browser_tabs"}:
                return False
            # The prompt may stay open while another call creates or changes
            # this session. A URL-only approval is valid only while the exact
            # destination still has no page context to bind against.
            if session is not None and (
                session.mode != "ai"
                or session.tabs
                or session.page_marker
                or session.generation != approval.generation
            ):
                return False
        elif (
            session is None
            or tab is None
            or session.mode != "ai"
            or approval.url != tab.url
            or approval.generation != session.generation
            or not approval.page_marker
            or not session.page_marker
        ):
            return False
        key = (approval.owner, approval.session_id, approval.tool_call_id, approval.tool_name)
        self._granted_approvals[key] = approval
        return True

    def _consume_approval(
        self,
        tool_name: str,
        args: dict[str, Any],
        owner: _Owner,
        session: _Session,
    ) -> None:
        args = self._approval_args(tool_name, args)
        self._prune_approvals()
        key = (owner.owner, session.session_id, self._tool_call_id(), tool_name)
        approval = self._granted_approvals.pop(key, None)
        tab = session.tabs.get(session.active_label)
        marker_matches = False
        pre_context_matches = bool(
            approval is not None
            and approval.pre_context
            and tool_name in {"browser_navigate", "browser_tabs"}
            and not session.tabs
            and not session.page_marker
            and session.generation == approval.generation
        )
        if approval is not None and session.page_marker and approval.page_marker:
            if tool_name == "browser_click" and args.get("screenshot_id"):
                marker_matches = self._same_screenshot_marker(
                    session.page_marker,
                    approval.page_marker,
                )
            elif approval.target_security:
                description = session.refs.get(approval.target, "")
                role, name = self._snapshot_target_signature(description)
                marker_data = self._marker_data(session.page_marker) or {}
                element_security = marker_data.get("elementSecurity")
                marker_matches = bool(
                    self._same_page_marker(session.page_marker, approval.page_marker)
                    and isinstance(element_security, dict)
                    and element_security.get(f"{role}\0{name}") == approval.target_security
                )
            else:
                marker_matches = self._same_security_surface_marker(
                    session.page_marker,
                    approval.page_marker,
                )
        if (
            approval is None
            or approval.expires_at <= time.monotonic()
            or (
                not pre_context_matches
                and (
                    tab is None
                    or approval.url != tab.url
                    or approval.generation != session.generation
                    or not approval.page_marker
                    or not session.page_marker
                    or not marker_matches
                )
            )
            or approval.args_hash != self._args_hash(args)
        ):
            raise BrowserDriverError("缺少有效的一次性审批，或页面/目标已在审批后变化")

    def _prune_approvals(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self._pending_approvals = {
            token: value
            for token, value in self._pending_approvals.items()
            if value.expires_at > current
        }
        self._granted_approvals = {
            key: value
            for key, value in self._granted_approvals.items()
            if value.expires_at > current
        }

    def state(self, owner_id: str, session_id: str) -> dict[str, Any]:
        owner = self._owners.get(str(owner_id or ""))
        session = owner.sessions.get(str(session_id or "")) if owner else None
        if owner is None or session is None:
            state = BrowserPageState(_hash(owner_id), _hash(session_id))
            if self.config.enabled and not self.available():
                reason = getattr(self.driver, "availability_error", None)
                state.last_error = redact_sensitive_display_text(
                    str(reason() or "桌面内置浏览器不可用")
                    if callable(reason)
                    else "桌面内置浏览器不可用"
                )
            return state.public_dict()
        return self._page_state(owner, session).public_dict()

    def _page_state(self, owner: _Owner, session: _Session) -> BrowserPageState:
        tab = session.tabs.get(session.active_label) or _Tab("", "")
        return BrowserPageState(
            owner_hash=_hash(owner.owner),
            session_hash=_hash(session.session_id),
            tab_id=tab.id,
            tab_label=tab.label,
            url=_public_url(tab.url),
            title=redact_sensitive_display_text(tab.title),
            generation=session.generation,
            mode=session.mode,  # type: ignore[arg-type]
            running=owner.running,
            last_action=redact_sensitive_display_text(session.last_action),
            last_error=redact_sensitive_display_text(session.last_error),
            screenshot_id=session.screenshot_id,
            viewport_width=session.viewport_width,
            viewport_height=session.viewport_height,
            can_go_back=session.can_go_back,
            can_go_forward=session.can_go_forward,
            tabs=[self._public_tab(value) for value in session.tabs.values()],
            downloads=_safe_public_value(list(session.downloads[-50:])),
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
            session.refs.clear()
            self._clear_screenshot(session)
            session.last_action = f"人工操作：{action}"
            await self._refresh_metadata(owner, session, workdir="")
            state = self._page_state(owner, session).public_dict()
            await self._publish(owner.owner, session.session_id, {"type": "state", "state": state})
            return state

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
                or preview_file.stat().st_size > min(self.config.max_transfer_bytes, 20 * 1024 * 1024)
            ):
                raise BrowserDriverError("本地预览仅允许当前工作区内不超过 20 MB 的 HTML 文件")

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
            await self._publish(owner.owner, session.session_id, {"type": "debug_clear"})
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
