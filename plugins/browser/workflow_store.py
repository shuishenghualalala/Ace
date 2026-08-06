"""Owner-private immutable storage for executable Playwright replay plans.

Version 2 is deliberately capability-complete: recorded selectors and optional
input defaults are preserved exactly, form edits can be batched, URLs keep
business query/fragment state, and scrolling keeps its exact deltas. Version 1
artifacts and earlier v2 artifacts without defaults remain readable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass

from crew.browser.types import (
    WORKFLOW_CAPABILITY_ORDER_V2,
    WORKFLOW_CAPABILITY_ORDER_V3,
)
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from crew.state.home import get_owner_runtime_home

WORKFLOW_STORE_SCHEMA = "crew.browser.replay.v2"
WORKFLOW_STORE_SCHEMA_V3 = "crew.browser.replay.v3"
LEGACY_WORKFLOW_STORE_SCHEMA = "crew.browser.replay.v1"
WORKFLOW_V3_PHASE_A_ENV = "CREW_BROWSER_RECORDING_V11_PHASE_A"
WORKFLOW_ID_RE = re.compile(r"^[0-9a-f]{64}$")
INPUT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PAGE_ALIAS_RE = re.compile(r"^p(?:0|[1-9]\d*)$")
# 能力词表的权威定义在 crew/browser/types.py。这里只做别名，**不要抄一份**：
# 顺序漂移过一次（skills.py 的副本漏了 assert_state / handle_overlay，
# 导致含这两项的工作流 100% 装不上，而两侧测试都没覆盖），只能靠"只有一份"根治。
WORKFLOW_CAPABILITY_ORDER = WORKFLOW_CAPABILITY_ORDER_V2
WORKFLOW_CAPABILITIES = frozenset(WORKFLOW_CAPABILITY_ORDER)
WORKFLOW_V3_CAPABILITY_ORDER = WORKFLOW_CAPABILITY_ORDER_V3
WORKFLOW_V3_CAPABILITIES = frozenset(WORKFLOW_V3_CAPABILITY_ORDER)

_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
# 断言状态。全部可由 playwright-core 的公开 Locator API 判定，不需要
# @playwright/test 的 expect（那是 devDependency，主进程里拿不到）。
#
# visible/hidden 走 Locator.waitFor({state})，天然带 Playwright 的自动重试；
# 其余走有界轮询，语义与 expect 的 retry 一致——断言必须能等，否则每个断言
# 前面都得手工塞一个 wait，那就等于没有断言。
ASSERT_STATES = frozenset(
    {
        "visible",
        "hidden",
        "enabled",
        "disabled",
        "checked",
        "unchecked",
        "editable",
    }
)
# 交还控制权的原因，分两类语义：
#
# - **挂起型**（handoff / secret）：工作流在这里停下让用户做一件只有他能做的事
#   （填验证码、输密码），做完之后**要继续跑**。计划里允许其后有步骤。
# - **终止型**（explicit）：工作流到此结束，把浏览器留给用户。其后不允许有步骤。
#
# 早先两类都当终止处理，于是「登录（含验证码）→ 读工单 → 汇报」这种最典型的
# 内网流程根本编译不出来——用户填完码之后工作流已经没了。
_SUSPENDING_TAKEOVER_REASONS = frozenset({"handoff", "secret"})
_TERMINAL_TAKEOVER_REASONS = frozenset({"explicit"})
_TAKEOVER_REASONS = _SUSPENDING_TAKEOVER_REASONS | _TERMINAL_TAKEOVER_REASONS
_LEGACY_TAKEOVER_REASONS = frozenset(
    {
        "submit",
        "key",
        "dynamic_navigation",
        "mutation",
        "missing_selector",
        "missing_target",
        "invalid_toggle_state",
        "review_required",
    }
)


class WorkflowStoreError(ValueError):
    """Stable fail-closed error category; never includes private plan content."""


def workflow_v3_phase_a_enabled() -> bool:
    """Whether v3 is enabled; only an explicit ``0`` requests rollback."""

    return os.environ.get(WORKFLOW_V3_PHASE_A_ENV) != "0"


def _modern_schema(schema_version: str) -> bool:
    return schema_version in {WORKFLOW_STORE_SCHEMA, WORKFLOW_STORE_SCHEMA_V3}


@dataclass(frozen=True)
class WorkflowArtifact:
    workflow_id: str
    digest: str
    raw: bytes
    payload: dict[str, Any]


@dataclass(frozen=True)
class PublishedWorkflow:
    path: Path
    identity: tuple[int, int]
    created: bool
    artifact: WorkflowArtifact


def _canonical_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    # JSON.stringify escapes lone UTF-16 code units. Python keeps them in the
    # returned str when ensure_ascii=False, which would otherwise fail during
    # UTF-8 encoding. Escaping only those units keeps every historical ordinary
    # v1/v2 canonical byte unchanged while allowing exact browser strings in v3.
    return "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in encoded
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _owner_binding(owner: str) -> str:
    return _sha256_bytes(f"crew-browser-workflow:owner:{owner}".encode("utf-8"))


def _normalize_host(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        return ""
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        return ""
    return host if _HOST_RE.fullmatch(host) else ""


def _normalize_origin(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        return ""
    try:
        parsed = urlsplit(value)
        host = _normalize_host(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65_535
    ):
        return ""
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def _plain_text(
    value: Any,
    *,
    allow_empty: bool = False,
) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise WorkflowStoreError("workflow_shape_invalid")
    return value


def _v3_text(value: Any, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise WorkflowStoreError("workflow_shape_invalid")
    return value


def _validate_inputs(
    value: Any,
    *,
    schema_version: str,
) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise WorkflowStoreError("workflow_inputs_invalid")
    text_value = _v3_text if schema_version == WORKFLOW_STORE_SCHEMA_V3 else _plain_text
    clean: dict[str, dict[str, Any]] = {}
    for key in sorted(value):
        spec = value[key]
        base_fields = (
            {"kind", "required", "display_name", "recorded_hint"}
            if _modern_schema(schema_version)
            else {"kind", "required"}
        )
        spec_fields = set(spec) if isinstance(spec, dict) else set()
        if (
            not isinstance(key, str)
            or INPUT_KEY_RE.fullmatch(key) is None
            or not isinstance(spec, dict)
            or _modern_schema(schema_version)
            and not base_fields <= spec_fields <= base_fields | {
                "default",
                "credential",
            }
            or not _modern_schema(schema_version)
            and spec_fields != base_fields
            or spec.get("kind") not in {"text", "select", "files"}
            or spec.get("kind") == "files"
            and not _modern_schema(schema_version)
            or spec.get("required") is not True
            # credential 只允许 True，不接受 False/其它真值。
            # 允许 False 会造出两种"非凭据"的表示（缺字段 / 显式 False），
            # 而安装前的知情披露是按这个标记计数的——两种表示就是两条统计口径。
            or "credential" in spec_fields
            and spec.get("credential") is not True
        ):
            raise WorkflowStoreError("workflow_inputs_invalid")
        clean_spec: dict[str, Any] = {
            "kind": str(spec["kind"]),
            "required": True,
        }
        if _modern_schema(schema_version):
            clean_spec["display_name"] = text_value(
                spec.get("display_name"),
            )
            clean_spec["recorded_hint"] = text_value(
                spec.get("recorded_hint"),
                allow_empty=True,
            )
            if "default" in spec:
                default = spec["default"]
                if spec["kind"] == "text":
                    clean_spec["default"] = text_value(
                        default,
                        allow_empty=True,
                    )
                else:
                    if (
                        not isinstance(default, list)
                        or spec["kind"] == "files"
                        and schema_version != WORKFLOW_STORE_SCHEMA_V3
                        and not default
                    ):
                        raise WorkflowStoreError("workflow_inputs_invalid")
                    clean_spec["default"] = [
                        text_value(item, allow_empty=spec["kind"] == "select")
                        for item in default
                    ]
            # 凭据标记必须活着穿过 store：安装前的知情披露按它计数，
            # 在这里被静默丢掉的话，披露就永远报"不含凭据原值"。
            if spec.get("credential") is True:
                clean_spec["credential"] = True
        clean[key] = clean_spec
    return clean


_V3_EFFECT_CAPABILITY = {
    "popup": "popup",
    "navigation": "navigation_effect",
    "download": "download",
    "dialog": "dialog",
    "page_closed": "page_closed",
}


def _capabilities_for_plan(
    plan: list[dict[str, Any]],
    *,
    schema_version: str = WORKFLOW_STORE_SCHEMA,
) -> list[str]:
    kinds = {str(step.get("kind") or "") for step in plan}
    if schema_version == WORKFLOW_STORE_SCHEMA_V3:
        effect_capabilities = {
            _V3_EFFECT_CAPABILITY.get(str(effect.get("kind") or ""), "")
            for step in plan
            for effect in (
                step.get("effects", [])
                if isinstance(step, dict)
                and isinstance(step.get("effects", []), list)
                else []
            )
            if isinstance(effect, dict)
        }
        effect_capabilities.discard("")
        kinds.update(effect_capabilities)
        if not kinds or not kinds <= WORKFLOW_V3_CAPABILITIES:
            raise WorkflowStoreError("workflow_capabilities_invalid")
        return [
            kind
            for kind in WORKFLOW_V3_CAPABILITY_ORDER
            if kind in kinds
        ]
    if not kinds or not kinds <= WORKFLOW_CAPABILITIES:
        raise WorkflowStoreError("workflow_capabilities_invalid")
    return [kind for kind in WORKFLOW_CAPABILITY_ORDER if kind in kinds]


def _validate_capabilities(
    value: Any,
    *,
    plan: list[dict[str, Any]],
    schema_version: str = WORKFLOW_STORE_SCHEMA,
) -> list[str]:
    expected = _capabilities_for_plan(
        plan,
        schema_version=schema_version,
    )
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) for item in value)
        or value != expected
    ):
        raise WorkflowStoreError("workflow_capabilities_invalid")
    return expected


def _validate_navigation_url(value: Any) -> str:
    url = _plain_text(value)
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise WorkflowStoreError("workflow_navigation_invalid") from exc
    # Navigation schemes are a browser/Playwright capability, not a workflow
    # store policy.  Keep about:, data:, file:, extension and future schemes
    # byte-for-byte; the execution engine remains the source of truth.
    if not parsed.scheme:
        raise WorkflowStoreError("workflow_navigation_invalid")
    return url


def _validate_v3_navigation_url(value: Any) -> str:
    url = _v3_text(value)
    try:
        parsed = urlsplit(url)
    except (TypeError, ValueError) as exc:
        raise WorkflowStoreError("workflow_navigation_invalid") from exc
    if not parsed.scheme:
        raise WorkflowStoreError("workflow_navigation_invalid")
    return url


def _validate_v3_point(value: Any) -> dict[str, float] | None:
    if value is None:
        return None
    if (
        not isinstance(value, dict)
        or set(value) != {"x", "y"}
        or any(
            type(value.get(axis)) not in {int, float}
            or not math.isfinite(float(value[axis]))
            or float(value[axis]) < 0
            for axis in ("x", "y")
        )
    ):
        raise WorkflowStoreError("workflow_position_invalid")
    return {"x": float(value["x"]), "y": float(value["y"])}


def _validate_v3_resize_dimension(value: Any) -> float:
    # Keep this aligned with Playwright's public setViewportSize number surface:
    # the store rejects non-JSON/non-finite values, while browser-specific
    # integer/range validity remains Playwright's responsibility at execution.
    if (
        type(value) not in {int, float}
        or not math.isfinite(float(value))
    ):
        raise WorkflowStoreError("workflow_resize_invalid")
    return float(value)


def _validate_v3_viewport(value: Any) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {"width", "height"}:
        raise WorkflowStoreError("workflow_resize_invalid")
    return {
        "width": _validate_v3_resize_dimension(value.get("width")),
        "height": _validate_v3_resize_dimension(value.get("height")),
    }


_V3_POINTER_TELEMETRY_RANGES = {
    "pressure": (0.0, 1.0),
    "tangential_pressure": (-1.0, 1.0),
    "tilt_x": (-90.0, 90.0),
    "tilt_y": (-90.0, 90.0),
    "twist": (0.0, 359.0),
    "width": (0.0, math.inf),
    "height": (0.0, math.inf),
}


def _validate_v3_pointer_sample(
    value: Any,
    *,
    elapsed: bool,
) -> dict[str, float]:
    required = {"x", "y"} | ({"elapsed_ms"} if elapsed else set())
    allowed = required | set(_V3_POINTER_TELEMETRY_RANGES)
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(allowed)
        or any(
            type(value.get(axis)) not in {int, float}
            or not math.isfinite(float(value[axis]))
            for axis in required
        )
    ):
        raise WorkflowStoreError("workflow_pointer_gesture_invalid")
    clean = {"x": float(value["x"]), "y": float(value["y"])}
    if elapsed:
        clean["elapsed_ms"] = float(value["elapsed_ms"])
    for name, (minimum, maximum) in _V3_POINTER_TELEMETRY_RANGES.items():
        if name not in value:
            continue
        raw = value[name]
        if (
            type(raw) not in {int, float}
            or not math.isfinite(float(raw))
            or not minimum <= float(raw) <= maximum
        ):
            raise WorkflowStoreError("workflow_pointer_gesture_invalid")
        clean[name] = float(raw)
    return clean


def _validate_v3_pointer_points(value: Any) -> list[dict[str, float]]:
    if not isinstance(value, list) or not value:
        raise WorkflowStoreError("workflow_pointer_gesture_invalid")
    clean: list[dict[str, float]] = []
    previous_elapsed_ms = 0.0
    for value_point in value:
        point = _validate_v3_pointer_sample(value_point, elapsed=True)
        if point["elapsed_ms"] < previous_elapsed_ms:
            raise WorkflowStoreError("workflow_pointer_gesture_invalid")
        previous_elapsed_ms = point["elapsed_ms"]
        clean.append(point)
    return clean


def _validate_v3_pointer_start(value: Any) -> dict[str, float]:
    return _validate_v3_pointer_sample(value, elapsed=False)


def _validate_v3_page(value: Any) -> str:
    if not isinstance(value, str) or PAGE_ALIAS_RE.fullmatch(value) is None:
        raise WorkflowStoreError("workflow_page_invalid")
    return value


def _validate_v3_effects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise WorkflowStoreError("workflow_effects_invalid")
    clean: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise WorkflowStoreError("workflow_effects_invalid")
        kind = raw.get("kind")
        if kind == "navigation" and set(raw) == {"kind", "page", "url"}:
            effect = {
                "kind": kind,
                "page": _validate_v3_page(raw.get("page")),
                "url": _validate_v3_navigation_url(raw.get("url")),
            }
        elif kind == "popup" and set(raw) == {
            "kind",
            "page",
            "opener_page",
            "popup_index",
            "activate",
            "disposition",
        }:
            popup_index = raw.get("popup_index")
            if (
                type(popup_index) is not int
                or popup_index < 1
                or type(raw.get("activate")) is not bool
            ):
                raise WorkflowStoreError("workflow_effects_invalid")
            effect = {
                "kind": kind,
                "page": _validate_v3_page(raw.get("page")),
                "opener_page": _validate_v3_page(raw.get("opener_page")),
                "popup_index": popup_index,
                "activate": raw["activate"],
                "disposition": _v3_text(raw.get("disposition")),
            }
        elif kind == "download" and set(raw) == {
            "kind",
            "page",
            "alias",
            "ordinal",
            "suggested_filename",
        }:
            ordinal = raw.get("ordinal")
            if type(ordinal) is not int or ordinal < 1:
                raise WorkflowStoreError("workflow_effects_invalid")
            effect = {
                "kind": kind,
                "page": _validate_v3_page(raw.get("page")),
                "alias": _v3_text(raw.get("alias")),
                "ordinal": ordinal,
                "suggested_filename": _v3_text(
                    raw.get("suggested_filename"),
                    allow_empty=True,
                ),
            }
        elif kind == "dialog" and set(raw) == {
            "kind",
            "page",
            "alias",
            "type",
            "accept",
            "text",
        }:
            dialog_type = raw.get("type")
            accept = raw.get("accept")
            text = _v3_text(raw.get("text"), allow_empty=True)
            if (
                dialog_type
                not in {"alert", "confirm", "prompt", "beforeunload"}
                or type(accept) is not bool
                or dialog_type != "prompt"
                and text
                or accept is False
                and text
            ):
                raise WorkflowStoreError("workflow_effects_invalid")
            effect = {
                "kind": kind,
                "page": _validate_v3_page(raw.get("page")),
                "alias": _v3_text(raw.get("alias")),
                "type": dialog_type,
                "accept": accept,
                "text": text,
            }
        elif kind == "page_closed" and set(raw) == {
            "kind",
            "page",
            "reason",
        }:
            effect = {
                "kind": kind,
                "page": _validate_v3_page(raw.get("page")),
                "reason": _v3_text(raw.get("reason"), allow_empty=True),
            }
        else:
            raise WorkflowStoreError("workflow_effects_invalid")
        clean.append(effect)
    return clean


def _validate_plan_v3(
    value: Any,
    *,
    inputs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not workflow_v3_phase_a_enabled():
        raise WorkflowStoreError("workflow_schema_unsupported")
    if not isinstance(value, list) or not value:
        raise WorkflowStoreError("workflow_plan_invalid")

    def selector(raw: Any, *, allow_empty: bool = False) -> str:
        return _v3_text(raw, allow_empty=allow_empty)

    def input_binding(raw: Any, expected_kind: str) -> str:
        if (
            not isinstance(raw, str)
            or raw not in inputs
            or inputs[raw]["kind"] != expected_kind
        ):
            raise WorkflowStoreError("workflow_input_binding_invalid")
        return raw

    clean: list[dict[str, Any]] = []
    referenced_inputs: list[str] = []
    defined_pages: set[str] = set()
    closed_pages: set[str] = set()
    root_pages: list[str] = []
    popup_indices: set[tuple[str, int]] = set()
    download_aliases: set[str] = set()
    dialog_aliases: set[str] = set()
    terminal = False

    def require_live(page: str) -> None:
        if page not in defined_pages:
            raise WorkflowStoreError("workflow_page_unbound")
        if page in closed_pages:
            raise WorkflowStoreError("workflow_page_closed")

    def register_popup(effect: dict[str, Any]) -> None:
        page = effect["page"]
        opener_page = effect["opener_page"]
        require_live(opener_page)
        identity = (opener_page, int(effect["popup_index"]))
        if page in defined_pages or identity in popup_indices:
            raise WorkflowStoreError("workflow_popup_identity_invalid")
        defined_pages.add(page)
        popup_indices.add(identity)

    def apply_effect(effect: dict[str, Any]) -> None:
        kind = effect["kind"]
        if kind == "popup":
            register_popup(effect)
            return
        page = effect["page"]
        require_live(page)
        if kind == "page_closed":
            closed_pages.add(page)
        elif kind == "download":
            alias = effect["alias"]
            if alias in download_aliases:
                raise WorkflowStoreError("workflow_download_alias_invalid")
            download_aliases.add(alias)
        elif kind == "dialog":
            alias = effect["alias"]
            if alias in dialog_aliases:
                raise WorkflowStoreError("workflow_dialog_alias_invalid")
            dialog_aliases.add(alias)

    for raw in value:
        if terminal or not isinstance(raw, dict):
            raise WorkflowStoreError("workflow_plan_invalid")
        kind = raw.get("kind")
        effects: list[dict[str, Any]] = []
        page = ""
        defines_page = False

        if kind == "open_page" and set(raw) in (
            {
                "kind",
                "page",
                "url",
                "mode",
                "activate",
                "effects",
            },
            {
                "kind",
                "page",
                "url",
                "mode",
                "activate",
                "viewport",
                "effects",
            },
        ):
            page = _validate_v3_page(raw.get("page"))
            expected_mode = "reuse_current" if not root_pages else "new"
            if (
                page in defined_pages
                or raw.get("mode") != expected_mode
                or type(raw.get("activate")) is not bool
            ):
                raise WorkflowStoreError("workflow_open_page_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "url": _validate_v3_navigation_url(raw.get("url")),
                "mode": expected_mode,
                "activate": raw["activate"],
                **(
                    {"viewport": _validate_v3_viewport(raw.get("viewport"))}
                    if "viewport" in raw
                    else {}
                ),
                "effects": effects,
            }
            defines_page = True
            root_pages.append(page)
        elif kind == "close_page" and set(raw) == {
            "kind",
            "page",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            if sum(
                effect["kind"] == "page_closed" and effect["page"] == page
                for effect in effects
            ) != 1:
                raise WorkflowStoreError("workflow_close_page_invalid")
            step = {"kind": kind, "page": page, "effects": effects}
        elif kind == "navigate" and set(raw) == {
            "kind",
            "page",
            "operation",
            "url",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            operation = raw.get("operation")
            url = raw.get("url")
            if (
                operation not in {"goto", "back", "forward", "reload"}
                or not isinstance(url, str)
                or operation == "goto"
                and not url
                or operation != "goto"
                and url
            ):
                raise WorkflowStoreError("workflow_navigation_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "operation": operation,
                "url": (
                    _validate_v3_navigation_url(url)
                    if operation == "goto"
                    else ""
                ),
                "effects": effects,
            }
        elif kind == "activate_page" and set(raw) == {
            "kind",
            "page",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {"kind": kind, "page": page, "effects": effects}
        elif kind == "resize" and set(raw) == {
            "kind",
            "page",
            "width",
            "height",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "width": _validate_v3_resize_dimension(raw.get("width")),
                "height": _validate_v3_resize_dimension(raw.get("height")),
                "effects": effects,
            }
        elif kind == "hover" and set(raw) == {
            "kind",
            "page",
            "selector",
            "position",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "position": _validate_v3_point(raw.get("position")),
                "effects": effects,
            }
        elif kind in {"click", "dblclick"} and set(raw) == {
            "kind",
            "page",
            "selector",
            "button",
            "click_count",
            "modifiers",
            "position",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            modifiers = raw.get("modifiers")
            click_count = raw.get("click_count")
            if (
                raw.get("button") not in {"left", "middle", "right"}
                or type(click_count) is not int
                or click_count < 1
                or kind == "dblclick"
                and click_count != 2
                or not isinstance(modifiers, list)
                or any(
                    modifier not in {"Alt", "Control", "Meta", "Shift"}
                    for modifier in modifiers
                )
                or len(set(modifiers)) != len(modifiers)
            ):
                raise WorkflowStoreError("workflow_click_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "button": raw["button"],
                "click_count": click_count,
                "modifiers": [
                    modifier
                    for modifier in ("Alt", "Control", "Meta", "Shift")
                    if modifier in modifiers
                ],
                "position": _validate_v3_point(raw.get("position")),
                "effects": effects,
            }
        elif kind == "drag" and set(raw) == {
            "kind",
            "page",
            "source_selector",
            "target_selector",
            "source_position",
            "target_position",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "source_selector": selector(raw.get("source_selector")),
                "target_selector": selector(raw.get("target_selector")),
                "source_position": _validate_v3_point(
                    raw.get("source_position")
                ),
                "target_position": _validate_v3_point(
                    raw.get("target_position")
                ),
                "effects": effects,
            }
        elif kind == "drop" and (
            set(raw)
            == {
                "kind",
                "page",
                "selector",
                "input_key",
                "data",
                "effects",
            }
            or set(raw)
            == {
                "kind",
                "page",
                "selector",
                "files",
                "data",
                "effects",
            }
        ):
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            data = raw.get("data")
            if (
                not isinstance(data, dict)
                or any(
                    not isinstance(mime, str)
                    or not isinstance(payload, str)
                    for mime, payload in data.items()
                )
            ):
                raise WorkflowStoreError("workflow_drop_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "data": dict(data),
                "effects": effects,
            }
            if "input_key" in raw:
                input_key = input_binding(raw.get("input_key"), "files")
                referenced_inputs.append(input_key)
                step["input_key"] = input_key
            else:
                files = raw.get("files")
                if not isinstance(files, list) or files:
                    raise WorkflowStoreError("workflow_drop_invalid")
                step["files"] = []
        elif kind == "pointer_gesture" and set(raw) in (
            {
                "kind",
                "page",
                "selector",
                "button",
                "modifiers",
                "start",
                "points",
                "effects",
            },
            {
                "kind",
                "page",
                "selector",
                "button",
                "modifiers",
                "pointer_type",
                "start",
                "points",
                "effects",
            },
        ):
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            start = _validate_v3_pointer_start(raw.get("start"))
            modifiers = raw.get("modifiers")
            pointer_type = raw.get("pointer_type", "mouse")
            if (
                raw.get("button") not in {"left", "middle", "right"}
                or pointer_type not in {"mouse", "pen", "touch"}
                or pointer_type == "touch"
                and raw.get("button") != "left"
                or not isinstance(modifiers, list)
                or any(
                    not isinstance(modifier, str)
                    or modifier not in {"Alt", "Control", "Meta", "Shift"}
                    for modifier in modifiers
                )
                or len(set(modifiers)) != len(modifiers)
            ):
                raise WorkflowStoreError("workflow_pointer_gesture_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "button": raw["button"],
                "modifiers": [
                    modifier
                    for modifier in ("Alt", "Control", "Meta", "Shift")
                    if modifier in modifiers
                ],
                "start": start,
                "points": _validate_v3_pointer_points(raw.get("points")),
                **(
                    {"pointer_type": pointer_type}
                    if "pointer_type" in raw
                    else {}
                ),
                "effects": effects,
            }
        elif kind == "press" and set(raw) == {
            "kind",
            "page",
            "selector",
            "key",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector"), allow_empty=True),
                "key": _v3_text(raw.get("key")),
                "effects": effects,
            }
        elif kind in {"fill", "select"} and set(raw) == {
            "kind",
            "page",
            "selector",
            "input_key",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            input_key = input_binding(
                raw.get("input_key"),
                "text" if kind == "fill" else "select",
            )
            referenced_inputs.append(input_key)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "input_key": input_key,
                "effects": effects,
            }
        elif kind == "check" and set(raw) == {
            "kind",
            "page",
            "selector",
            "checked",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            if type(raw.get("checked")) is not bool:
                raise WorkflowStoreError("workflow_check_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "checked": raw["checked"],
                "effects": effects,
            }
        elif kind == "upload" and (
            set(raw)
            == {"kind", "page", "selector", "input_key", "effects"}
            or set(raw) == {"kind", "page", "selector", "files", "effects"}
        ):
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "effects": effects,
            }
            if "input_key" in raw:
                input_key = input_binding(raw.get("input_key"), "files")
                referenced_inputs.append(input_key)
                step["input_key"] = input_key
            else:
                files = raw.get("files")
                if not isinstance(files, list) or files:
                    raise WorkflowStoreError("workflow_upload_invalid")
                step["files"] = []
        elif kind == "scroll" and set(raw) == {
            "kind",
            "page",
            "selector",
            "delta_x",
            "delta_y",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            delta_x = raw.get("delta_x")
            delta_y = raw.get("delta_y")
            if (
                type(delta_x) is not int
                or type(delta_y) is not int
                or delta_x == delta_y == 0
            ):
                raise WorkflowStoreError("workflow_scroll_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector"), allow_empty=True),
                "delta_x": delta_x,
                "delta_y": delta_y,
                "effects": effects,
            }
        elif kind == "wait_page" and set(raw) == {
            "kind",
            "page",
            "opener_page",
            "popup_index",
            "activate",
            "disposition",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            opener_page = _validate_v3_page(raw.get("opener_page"))
            require_live(opener_page)
            popup_index = raw.get("popup_index")
            if (
                page in defined_pages
                or type(popup_index) is not int
                or popup_index < 1
                or type(raw.get("activate")) is not bool
                or (opener_page, popup_index) in popup_indices
            ):
                raise WorkflowStoreError("workflow_wait_page_invalid")
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "opener_page": opener_page,
                "popup_index": popup_index,
                "activate": raw["activate"],
                "disposition": _v3_text(raw.get("disposition")),
                "effects": effects,
            }
            defines_page = True
            popup_indices.add((opener_page, popup_index))
        elif kind == "wait_navigation" and set(raw) == {
            "kind",
            "page",
            "url",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "url": _validate_v3_navigation_url(raw.get("url")),
                "effects": effects,
            }
        elif kind == "wait_page_closed" and set(raw) == {
            "kind",
            "page",
            "reason",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            if effects:
                raise WorkflowStoreError("workflow_wait_page_closed_invalid")
            step = {
                "kind": kind,
                "page": page,
                "reason": _v3_text(raw.get("reason"), allow_empty=True),
                "effects": [],
            }
        elif kind == "wait_download" and set(raw) == {
            "kind",
            "page",
            "alias",
            "ordinal",
            "suggested_filename",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            alias = _v3_text(raw.get("alias"))
            ordinal = raw.get("ordinal")
            if (
                type(ordinal) is not int
                or ordinal < 1
                or alias in download_aliases
            ):
                raise WorkflowStoreError("workflow_wait_download_invalid")
            download_aliases.add(alias)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "alias": alias,
                "ordinal": ordinal,
                "suggested_filename": _v3_text(
                    raw.get("suggested_filename"),
                    allow_empty=True,
                ),
                "effects": effects,
            }
        elif kind == "wait_dialog" and set(raw) == {
            "kind",
            "page",
            "alias",
            "type",
            "accept",
            "text",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            alias = _v3_text(raw.get("alias"))
            dialog_type = raw.get("type")
            accept = raw.get("accept")
            text = _v3_text(raw.get("text"), allow_empty=True)
            if (
                alias in dialog_aliases
                or dialog_type
                not in {"alert", "confirm", "prompt", "beforeunload"}
                or type(accept) is not bool
                or dialog_type != "prompt"
                and text
                or accept is False
                and text
            ):
                raise WorkflowStoreError("workflow_wait_dialog_invalid")
            dialog_aliases.add(alias)
            effects = _validate_v3_effects(raw.get("effects"))
            step = {
                "kind": kind,
                "page": page,
                "alias": alias,
                "type": dialog_type,
                "accept": accept,
                "text": text,
                "effects": effects,
            }
        elif kind == "handle_overlay" and set(raw) == {
            "kind",
            "page",
            "selector",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            if _validate_v3_effects(raw.get("effects")):
                raise WorkflowStoreError("workflow_overlay_invalid")
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "effects": [],
            }
        elif kind == "assert_state" and set(raw) == {
            "kind",
            "page",
            "selector",
            "state",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            state = raw.get("state")
            if state not in ASSERT_STATES:
                raise WorkflowStoreError("workflow_assert_invalid")
            # 断言是只读判定，本身不产生任何原子副作用。允许 effects 非空
            # 就等于允许"断言顺手打开一个弹窗"，那不是断言。
            if _validate_v3_effects(raw.get("effects")):
                raise WorkflowStoreError("workflow_assert_invalid")
            step = {
                "kind": kind,
                "page": page,
                "selector": selector(raw.get("selector")),
                "state": str(state),
                "effects": [],
            }
        elif kind == "snapshot_full" and set(raw) == {
            "kind",
            "page",
            "effects",
        }:
            page = _validate_v3_page(raw.get("page"))
            require_live(page)
            effects = _validate_v3_effects(raw.get("effects"))
            if effects:
                raise WorkflowStoreError("workflow_snapshot_invalid")
            step = {"kind": kind, "page": page, "effects": []}
        elif kind == "takeover" and set(raw) == {"kind", "reason"}:
            reason = raw.get("reason")
            if reason not in _TAKEOVER_REASONS:
                raise WorkflowStoreError("workflow_takeover_invalid")
            step = {"kind": kind, "reason": reason}
            # 只有终止型才封计划；挂起型后面可以继续有步骤。
            terminal = reason in _TERMINAL_TAKEOVER_REASONS
        else:
            raise WorkflowStoreError("workflow_plan_invalid")

        if defines_page:
            defined_pages.add(page)
        for effect in effects:
            apply_effect(effect)
        if kind == "wait_page_closed":
            closed_pages.add(page)
        clean.append(step)

    if set(referenced_inputs) != set(inputs):
        raise WorkflowStoreError("workflow_inputs_unreferenced")
    live_pages = defined_pages - closed_pages
    if live_pages and clean[-1]["kind"] not in {"snapshot_full", "takeover"}:
        raise WorkflowStoreError("workflow_final_observation_required")
    if not live_pages and clean[-1]["kind"] not in {
        "close_page",
        "wait_page_closed",
        "takeover",
    }:
        raise WorkflowStoreError("workflow_final_observation_required")
    return clean


def _validate_plan(
    value: Any,
    *,
    schema_version: str,
    inputs: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if schema_version == WORKFLOW_STORE_SCHEMA_V3:
        return _validate_plan_v3(value, inputs=inputs)
    if not isinstance(value, list) or not value:
        raise WorkflowStoreError("workflow_plan_invalid")

    def selector(raw: Any, *, allow_empty: bool = False) -> str:
        return _plain_text(raw, allow_empty=allow_empty)

    def input_binding(raw: Any, expected_kind: str) -> str:
        if (
            not isinstance(raw, str)
            or raw not in inputs
            or inputs[raw]["kind"] != expected_kind
        ):
            raise WorkflowStoreError("workflow_input_binding_invalid")
        return raw

    def postconditions(raw: Any, *, step_kind: str) -> list[dict[str, Any]]:
        if (
            schema_version != WORKFLOW_STORE_SCHEMA
            or step_kind
            not in {
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
            }
            or not isinstance(raw, list)
            or not raw
        ):
            raise WorkflowStoreError("workflow_postconditions_invalid")
        clean_conditions: list[dict[str, Any]] = []
        for condition in raw:
            if not isinstance(condition, dict):
                raise WorkflowStoreError("workflow_postconditions_invalid")
            target = condition.get("target")
            patterned = "url_pattern" in condition or "origin" in condition
            expected_fields = (
                {"kind", "target", "origin", "url_pattern"}
                if patterned
                else {"kind", "target", "url"}
            )
            if target == "popup":
                expected_fields.add("activate")
                expected_fields.update(
                    field
                    for field in ("page", "opener_page", "popup_ordinal")
                    if field in condition
                )
            if (
                set(condition) != expected_fields
                or condition.get("kind") != "url"
                or target not in {"same_tab", "popup"}
                or target == "popup"
                and type(condition.get("activate")) is not bool
            ):
                raise WorkflowStoreError("workflow_postconditions_invalid")
            normalized: dict[str, Any] = {"kind": "url", "target": target}
            if not patterned:
                normalized["url"] = _validate_navigation_url(
                    condition.get("url"),
                )
            else:
                origin = _normalize_origin(condition.get("origin"))
                raw_pattern = condition.get("url_pattern")
                if (
                    not origin
                    or not isinstance(raw_pattern, list)
                    or not raw_pattern
                ):
                    raise WorkflowStoreError("workflow_postconditions_invalid")
                pattern: list[dict[str, str]] = []
                literal_chars = 0
                sample_parts: list[str] = []
                for segment in raw_pattern:
                    if not isinstance(segment, dict):
                        raise WorkflowStoreError(
                            "workflow_postconditions_invalid"
                        )
                    if set(segment) == {"literal"}:
                        literal = _plain_text(
                            segment.get("literal"),
                            allow_empty=True,
                        )
                        literal_chars += len(literal)
                        pattern.append({"literal": literal})
                        sample_parts.append(literal)
                    elif set(segment) == {"input_key", "encoding"}:
                        input_key = segment.get("input_key")
                        encoding = segment.get("encoding")
                        if (
                            not isinstance(input_key, str)
                            or input_key not in inputs
                            or inputs[input_key]["kind"]
                            not in {"text", "select"}
                            or encoding
                            not in {
                                "raw",
                                "percent",
                                "plus",
                                "query",
                                "path",
                            }
                        ):
                            raise WorkflowStoreError(
                                "workflow_postconditions_invalid"
                            )
                        pattern.append(
                            {
                                "input_key": input_key,
                                "encoding": str(encoding),
                            }
                        )
                        sample_parts.append("x")
                    elif (
                        set(segment) == {"wildcard"}
                        and segment.get("wildcard")
                        in {
                            "query_value",
                            "path_segment",
                            "fragment_value",
                        }
                    ):
                        wildcard = str(segment["wildcard"])
                        pattern.append({"wildcard": wildcard})
                        sample_parts.append("x")
                    else:
                        raise WorkflowStoreError(
                            "workflow_postconditions_invalid"
                        )
                sample = "".join(sample_parts)
                if (
                    not pattern
                    or set(pattern[0]) != {"literal"}
                    or not pattern[0]["literal"].startswith(origin)
                ):
                    raise WorkflowStoreError("workflow_postconditions_invalid")
                _validate_navigation_url(sample)
                normalized["origin"] = origin
                normalized["url_pattern"] = pattern
            if target == "popup":
                normalized["activate"] = condition["activate"]
                if "page" in condition:
                    page = condition.get("page")
                    if (
                        not isinstance(page, str)
                        or PAGE_ALIAS_RE.fullmatch(page) is None
                    ):
                        raise WorkflowStoreError(
                            "workflow_postconditions_invalid"
                        )
                    normalized["page"] = page
                has_opener = "opener_page" in condition
                has_ordinal = "popup_ordinal" in condition
                if has_opener != has_ordinal:
                    raise WorkflowStoreError(
                        "workflow_postconditions_invalid"
                    )
                if has_opener:
                    opener_page = condition.get("opener_page")
                    popup_ordinal = condition.get("popup_ordinal")
                    if (
                        not isinstance(opener_page, str)
                        or PAGE_ALIAS_RE.fullmatch(opener_page) is None
                        or isinstance(popup_ordinal, bool)
                        or not isinstance(popup_ordinal, int)
                        or popup_ordinal < 1
                    ):
                        raise WorkflowStoreError(
                            "workflow_postconditions_invalid"
                        )
                    normalized["opener_page"] = opener_page
                    normalized["popup_ordinal"] = popup_ordinal
            clean_conditions.append(normalized)
        return clean_conditions

    def dialog_item(raw: Any, *, error: str) -> dict[str, Any]:
        required = {"type", "accept", "text"}
        optional = {"page", "label", "opener_page", "popup_ordinal"}
        if (
            not isinstance(raw, dict)
            or not required <= set(raw) <= required | optional
            or raw.get("type")
            not in {"alert", "confirm", "prompt", "beforeunload"}
            or type(raw.get("accept")) is not bool
        ):
            raise WorkflowStoreError(error)
        text = _plain_text(raw.get("text"), allow_empty=True)
        if (
            raw.get("type") != "prompt"
            and text != ""
            or raw.get("accept") is False
            and text != ""
        ):
            raise WorkflowStoreError(error)
        clean_dialog: dict[str, Any] = {
            "type": str(raw["type"]),
            "accept": bool(raw["accept"]),
            "text": text,
        }
        if "page" in raw:
            page = raw.get("page")
            if not isinstance(page, str) or PAGE_ALIAS_RE.fullmatch(page) is None:
                raise WorkflowStoreError(error)
            clean_dialog["page"] = page
        if "label" in raw:
            clean_dialog["label"] = _plain_text(
                raw.get("label"),
                allow_empty=True,
            )
        if "opener_page" in raw:
            opener_page = raw.get("opener_page")
            if (
                not isinstance(opener_page, str)
                or PAGE_ALIAS_RE.fullmatch(opener_page) is None
            ):
                raise WorkflowStoreError(error)
            clean_dialog["opener_page"] = opener_page
        if "popup_ordinal" in raw:
            popup_ordinal = raw.get("popup_ordinal")
            if (
                isinstance(popup_ordinal, bool)
                or not isinstance(popup_ordinal, int)
                or popup_ordinal < 0
            ):
                raise WorkflowStoreError(error)
            clean_dialog["popup_ordinal"] = popup_ordinal
        return clean_dialog

    def dialogs(raw: Any, *, step_kind: str) -> list[dict[str, Any]]:
        if (
            schema_version != WORKFLOW_STORE_SCHEMA
            or step_kind
            not in {
                "navigate",
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
            }
            or not isinstance(raw, list)
            or not raw
        ):
            raise WorkflowStoreError("workflow_dialogs_invalid")
        return [
            dialog_item(item, error="workflow_dialogs_invalid")
            for item in raw
        ]

    clean: list[dict[str, Any]] = []
    referenced_inputs: list[str] = []
    terminated = False
    no_postconditions = object()
    no_dialogs = object()
    no_page = object()
    for raw in value:
        if terminated or not isinstance(raw, dict):
            raise WorkflowStoreError("workflow_plan_invalid")
        raw_postconditions = raw.get("postconditions", no_postconditions)
        if "postconditions" in raw:
            raw = {key: item for key, item in raw.items() if key != "postconditions"}
        raw_dialogs = raw.get("dialogs", no_dialogs)
        if "dialogs" in raw:
            raw = {key: item for key, item in raw.items() if key != "dialogs"}
        raw_page = raw.get("page", no_page)
        if "page" in raw:
            raw = {key: item for key, item in raw.items() if key != "page"}
        kind = raw.get("kind")
        if kind == "navigate" and set(raw) == {"kind", "url"}:
            step = {
                "kind": "navigate",
                "url": _validate_navigation_url(raw.get("url")),
            }
        elif (
            kind == "dialog"
            and {"kind", "type", "accept", "text"} <= set(raw)
            and set(raw)
            <= {
                "kind",
                "type",
                "accept",
                "text",
                "label",
                "opener_page",
                "popup_ordinal",
            }
        ):
            step = {
                "kind": "dialog",
                **dialog_item(
                    {key: item for key, item in raw.items() if key != "kind"},
                    error="workflow_dialog_invalid",
                ),
            }
        elif kind in {"click", "dblclick"} and frozenset(raw) in {
            frozenset({"kind", "selector"}),
            frozenset(
                {
                    "kind",
                    "selector",
                    "button",
                    "click_count",
                    "modifiers",
                }
            ),
            frozenset(
                {
                    "kind",
                    "selector",
                    "button",
                    "click_count",
                    "modifiers",
                    "position",
                }
            ),
        }:
            step = {"kind": kind, "selector": selector(raw.get("selector"))}
            if "button" in raw:
                button = raw.get("button")
                click_count = raw.get("click_count")
                modifiers = raw.get("modifiers")
                if (
                    button not in {"left", "middle", "right"}
                    or type(click_count) is not int
                    or not 1 <= click_count <= 9_007_199_254_740_991
                    or kind == "dblclick"
                    and click_count != 2
                    or not isinstance(modifiers, list)
                    or any(
                        not isinstance(modifier, str)
                        or modifier not in {"Alt", "Control", "Meta", "Shift"}
                        for modifier in modifiers
                    )
                    or len(set(modifiers)) != len(modifiers)
                ):
                    raise WorkflowStoreError("workflow_click_invalid")
                step.update(
                    {
                        "button": button,
                        "click_count": click_count,
                        "modifiers": [
                            modifier
                            for modifier in ("Alt", "Control", "Meta", "Shift")
                            if modifier in modifiers
                        ],
                    }
                )
                if "position" in raw:
                    position = raw.get("position")
                    if (
                        not isinstance(position, dict)
                        or set(position) != {"x", "y"}
                        or any(
                            type(position.get(axis)) not in {int, float}
                            or not math.isfinite(float(position[axis]))
                            or float(position[axis]) < 0
                            for axis in ("x", "y")
                        )
                    ):
                        raise WorkflowStoreError("workflow_click_invalid")
                    step["position"] = {
                        "x": float(position["x"]),
                        "y": float(position["y"]),
                    }
        elif kind == "drag" and set(raw) == {
            "kind",
            "source_selector",
            "target_selector",
        }:
            source_selector = selector(raw.get("source_selector"))
            target_selector = selector(raw.get("target_selector"))
            if source_selector == target_selector:
                raise WorkflowStoreError("workflow_drag_invalid")
            step = {
                "kind": "drag",
                "source_selector": source_selector,
                "target_selector": target_selector,
            }
        elif kind == "press" and set(raw) == {"kind", "selector", "key"}:
            key = _plain_text(raw.get("key"))
            step = {
                "kind": "press",
                "selector": selector(raw.get("selector"), allow_empty=True),
                "key": key,
            }
        elif kind == "handle_overlay" and set(raw) == {"kind", "selector"}:
            step = {
                "kind": "handle_overlay",
                "selector": selector(raw.get("selector")),
            }
        elif kind == "assert_state" and set(raw) == {
            "kind",
            "selector",
            "state",
        }:
            state = raw.get("state")
            if state not in ASSERT_STATES:
                raise WorkflowStoreError("workflow_assert_invalid")
            step = {
                "kind": "assert_state",
                # selector 一律来自 trace 里的某一步，模型不能自己写。
                # 校验在这里再做一次：artifact 是被签名的，签之前必须成形。
                "selector": selector(raw.get("selector")),
                "state": str(state),
            }
        elif kind == "scroll" and set(raw) == {
            "kind",
            "selector",
            "delta_x",
            "delta_y",
        }:
            delta_x = raw.get("delta_x")
            delta_y = raw.get("delta_y")
            if (
                isinstance(delta_x, bool)
                or not isinstance(delta_x, int)
                or isinstance(delta_y, bool)
                or not isinstance(delta_y, int)
                or delta_x == delta_y == 0
            ):
                raise WorkflowStoreError("workflow_scroll_invalid")
            step = {
                "kind": "scroll",
                "selector": selector(raw.get("selector"), allow_empty=True),
                "delta_x": delta_x,
                "delta_y": delta_y,
            }
        elif (
            schema_version == LEGACY_WORKFLOW_STORE_SCHEMA
            and kind == "scroll"
            and set(raw) == {"kind", "direction"}
        ):
            # v1 compatibility. New recordings always use exact deltas.
            direction = raw.get("direction")
            if direction not in {"up", "down", "left", "right"}:
                raise WorkflowStoreError("workflow_plan_invalid")
            step = {"kind": "scroll", "direction": direction}
        elif kind == "snapshot_full" and set(raw) == {"kind"}:
            step = {"kind": "snapshot_full"}
        elif kind in {"fill", "select"} and (
            set(raw) == {"kind", "selector", "input_key"}
            or schema_version == LEGACY_WORKFLOW_STORE_SCHEMA
            and set(raw)
            == {
                "kind",
                "selector",
                "input_key",
                "expected_action_kind",
                "expected_tag",
                "expected_input_type",
                "expected_role",
                "expected_content_editable",
                "expected_tier",
                "expected_document_host",
                "expected_document_origin",
            }
        ):
            input_key = input_binding(
                raw.get("input_key"),
                "text" if kind == "fill" else "select",
            )
            referenced_inputs.append(input_key)
            step: dict[str, Any] = {
                "kind": kind,
                "selector": selector(raw.get("selector")),
                "input_key": input_key,
            }
            # Preserve v1 fields byte-for-byte in v1 artifacts. Replay v2
            # intentionally ignores these historical attestations.
            for name in sorted(set(raw) - set(step)):
                item = raw[name]
                if isinstance(item, str):
                    item = _plain_text(item, allow_empty=True)
                elif name == "expected_content_editable" and type(item) is bool:
                    pass
                else:
                    raise WorkflowStoreError("workflow_plan_invalid")
                step[name] = item
        elif kind == "check" and (
            set(raw) == {"kind", "selector", "checked"}
            or schema_version == LEGACY_WORKFLOW_STORE_SCHEMA
            and set(raw)
            == {
                "kind",
                "selector",
                "checked",
                "expected_action_kind",
                "expected_tag",
                "expected_input_type",
                "expected_role",
                "expected_content_editable",
                "expected_tier",
                "expected_document_host",
                "expected_document_origin",
            }
        ):
            if type(raw.get("checked")) is not bool:
                raise WorkflowStoreError("workflow_check_invalid")
            step = {
                "kind": "check",
                "selector": selector(raw.get("selector")),
                "checked": bool(raw["checked"]),
            }
            for name in sorted(set(raw) - set(step)):
                item = raw[name]
                if isinstance(item, str):
                    item = _plain_text(item, allow_empty=True)
                elif name == "expected_content_editable" and type(item) is bool:
                    pass
                else:
                    raise WorkflowStoreError("workflow_plan_invalid")
                step[name] = item
        elif kind == "upload" and frozenset(raw) in {
            frozenset(
                {"kind", "selector", "input_key", "multiple", "accept"}
            ),
            frozenset(
                {
                    "kind",
                    "selector",
                    "trigger_selector",
                    "input_key",
                    "multiple",
                    "accept",
                }
            ),
            frozenset({"kind", "selector", "files", "multiple", "accept"}),
        }:
            multiple = raw.get("multiple")
            accept = _plain_text(
                raw.get("accept"),
                allow_empty=True,
            )
            if type(multiple) is not bool:
                raise WorkflowStoreError("workflow_upload_invalid")
            step = {
                "kind": "upload",
                "selector": selector(raw.get("selector")),
                "multiple": multiple,
                "accept": accept,
            }
            if "input_key" in raw:
                input_key = input_binding(raw.get("input_key"), "files")
                referenced_inputs.append(input_key)
                step["input_key"] = input_key
                if "trigger_selector" in raw:
                    step["trigger_selector"] = selector(
                        raw.get("trigger_selector")
                    )
            else:
                if raw.get("files") != []:
                    raise WorkflowStoreError("workflow_upload_invalid")
                step["files"] = []
        elif kind == "fill_form" and set(raw) == {"kind", "fields"}:
            raw_fields = raw.get("fields")
            if not isinstance(raw_fields, list) or not raw_fields:
                raise WorkflowStoreError("workflow_fill_form_invalid")
            fields: list[dict[str, Any]] = []
            field_selectors: set[str] = set()
            for field in raw_fields:
                if not isinstance(field, dict):
                    raise WorkflowStoreError("workflow_fill_form_invalid")
                field_type = field.get("type")
                if field_type in {"textbox", "slider"} and set(field) == {
                    "type",
                    "selector",
                    "input_key",
                }:
                    input_key = input_binding(field.get("input_key"), "text")
                    normalized_field = {
                        "type": field_type,
                        "selector": selector(field.get("selector")),
                        "input_key": input_key,
                    }
                    referenced_inputs.append(input_key)
                elif field_type == "combobox" and set(field) == {
                    "type",
                    "selector",
                    "input_key",
                    "select_by",
                }:
                    input_key = input_binding(field.get("input_key"), "select")
                    if field.get("select_by") not in {"label", "value"}:
                        raise WorkflowStoreError("workflow_fill_form_invalid")
                    normalized_field = {
                        "type": "combobox",
                        "selector": selector(field.get("selector")),
                        "input_key": input_key,
                        "select_by": field["select_by"],
                    }
                    referenced_inputs.append(input_key)
                elif field_type in {"checkbox", "radio"} and set(field) == {
                    "type",
                    "selector",
                    "value",
                }:
                    if type(field.get("value")) is not bool:
                        raise WorkflowStoreError("workflow_fill_form_invalid")
                    normalized_field = {
                        "type": field_type,
                        "selector": selector(field.get("selector")),
                        "value": field["value"],
                    }
                else:
                    raise WorkflowStoreError("workflow_fill_form_invalid")
                if normalized_field["selector"] in field_selectors:
                    raise WorkflowStoreError("workflow_fill_form_invalid")
                field_selectors.add(normalized_field["selector"])
                fields.append(normalized_field)
            step = {"kind": "fill_form", "fields": fields}
        elif kind == "takeover" and set(raw) == {"kind", "reason"}:
            reason = raw.get("reason")
            allowed_reasons = (
                _TAKEOVER_REASONS | _LEGACY_TAKEOVER_REASONS
                if schema_version == LEGACY_WORKFLOW_STORE_SCHEMA
                else _TAKEOVER_REASONS
            )
            if reason not in allowed_reasons:
                raise WorkflowStoreError("workflow_takeover_invalid")
            step = {"kind": "takeover", "reason": reason}
            # 与 v3 同口径：挂起型 takeover 后面可以继续有步骤。
            # legacy reason（submit/key/mutation…）保持历史的终止语义。
            terminated = reason not in _SUSPENDING_TAKEOVER_REASONS
        else:
            raise WorkflowStoreError("workflow_plan_invalid")
        if raw_page is not no_page:
            if (
                schema_version != WORKFLOW_STORE_SCHEMA
                or kind in {"snapshot_full", "takeover"}
                or not isinstance(raw_page, str)
                or PAGE_ALIAS_RE.fullmatch(raw_page) is None
            ):
                raise WorkflowStoreError("workflow_page_invalid")
            step["page"] = raw_page
        if raw_postconditions is not no_postconditions:
            step["postconditions"] = postconditions(
                raw_postconditions,
                step_kind=str(kind or ""),
            )
        if raw_dialogs is not no_dialogs:
            step["dialogs"] = dialogs(
                raw_dialogs,
                step_kind=str(kind or ""),
            )
        clean.append(step)
    if set(referenced_inputs) != set(inputs):
        raise WorkflowStoreError("workflow_inputs_unreferenced")
    if clean[-1]["kind"] not in {"snapshot_full", "takeover"}:
        raise WorkflowStoreError("workflow_final_observation_required")
    return clean


def _validate_core(value: Any, owner: str) -> dict[str, Any]:
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    modern_schema = schema_version in {
        WORKFLOW_STORE_SCHEMA,
        WORKFLOW_STORE_SCHEMA_V3,
    }
    supported_schemas = {
        WORKFLOW_STORE_SCHEMA,
        LEGACY_WORKFLOW_STORE_SCHEMA,
    }
    if workflow_v3_phase_a_enabled():
        supported_schemas.add(WORKFLOW_STORE_SCHEMA_V3)
    expected_fields = (
        {
            "schema_version",
            "owner_binding",
            "hosts",
            "inputs",
            "capabilities",
            "plan",
        }
        if modern_schema
        else {"schema_version", "owner_binding", "hosts", "inputs", "plan"}
    )
    if (
        not owner
        or not isinstance(value, dict)
        or set(value) != expected_fields
        or schema_version not in supported_schemas
        or value.get("owner_binding") != _owner_binding(owner)
    ):
        raise WorkflowStoreError("workflow_binding_invalid")
    raw_hosts = value.get("hosts")
    if not isinstance(raw_hosts, list):
        raise WorkflowStoreError("workflow_hosts_invalid")
    # Wire-compatible recording diagnostics, not an execution capability.
    # Keep the list canonical so content addressing stays deterministic, but
    # never compare executable navigation or postcondition URLs against it.
    hosts = tuple(_normalize_host(host) for host in raw_hosts)
    if any(not host for host in hosts) or tuple(sorted(set(hosts))) != hosts:
        raise WorkflowStoreError("workflow_hosts_invalid")
    inputs = _validate_inputs(value.get("inputs"), schema_version=schema_version)
    plan = _validate_plan(
        value.get("plan"),
        schema_version=schema_version,
        inputs=inputs,
    )
    clean = {
        "schema_version": schema_version,
        "owner_binding": _owner_binding(owner),
        "hosts": list(hosts),
        "inputs": inputs,
        "plan": plan,
    }
    if modern_schema:
        # Capabilities are executable IR, not advisory skill text.  They must
        # exactly describe the validated plan, in canonical order.
        clean["capabilities"] = _validate_capabilities(
            value.get("capabilities"),
            plan=plan,
            schema_version=str(schema_version),
        )
    return clean


def build_workflow_artifact(
    *,
    owner: str,
    hosts: list[str] | tuple[str, ...],
    inputs: dict[str, dict[str, Any]],
    plan: list[dict[str, Any]],
    schema_version: str = WORKFLOW_STORE_SCHEMA,
) -> WorkflowArtifact:
    """Build one canonical content-addressed owner-bound workflow artifact."""

    if schema_version == WORKFLOW_STORE_SCHEMA_V3:
        capability_order = WORKFLOW_V3_CAPABILITY_ORDER
    elif schema_version == WORKFLOW_STORE_SCHEMA:
        capability_order = WORKFLOW_CAPABILITY_ORDER
    else:
        raise WorkflowStoreError("workflow_schema_unsupported")
    # Let _validate_plan own malformed-step classification. Capability
    # equality is checked only after the plan has a canonical validated shape.
    declared_capabilities = [
        kind
        for kind in capability_order
        if (
            any(
                isinstance(step, dict) and step.get("kind") == kind
                for step in plan
            )
            or schema_version == WORKFLOW_STORE_SCHEMA_V3
            and kind
            in {
                _V3_EFFECT_CAPABILITY.get(
                    str(effect.get("kind") or ""),
                    "",
                )
                for step in plan
                for effect in (
                    step.get("effects", [])
                    if isinstance(step, dict)
                    and isinstance(step.get("effects", []), list)
                    else []
                )
                if isinstance(effect, dict)
            }
        )
    ]
    core = _validate_core(
        {
            "schema_version": schema_version,
            "owner_binding": _owner_binding(owner),
            "hosts": list(hosts),
            "inputs": inputs,
            "capabilities": declared_capabilities,
            "plan": plan,
        },
        owner,
    )
    workflow_id = _sha256_bytes(_canonical_json(core).encode("utf-8"))
    payload = {**core, "workflow_id": workflow_id}
    raw = (_canonical_json(payload) + "\n").encode("utf-8")
    return WorkflowArtifact(
        workflow_id=workflow_id,
        digest=_sha256_bytes(raw),
        raw=raw,
        payload=payload,
    )


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_unique_json_object,
        parse_constant=_reject_json_constant,
    )


def _safe_open_read(path: Path) -> bytes:
    """Read one exact regular 0600 inode and verify its pathname after EOF."""

    before_path = os.lstat(path)
    if (
        stat.S_ISLNK(before_path.st_mode)
        or not stat.S_ISREG(before_path.st_mode)
        or os.name != "nt"
        and stat.S_IMODE(before_path.st_mode) != 0o600
    ):
        raise WorkflowStoreError("workflow_file_invalid")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(path, flags)
    try:
        before_fd = os.fstat(fd)
        if (
            not stat.S_ISREG(before_fd.st_mode)
            or (before_fd.st_dev, before_fd.st_ino)
            != (before_path.st_dev, before_path.st_ino)
        ):
            raise WorkflowStoreError("workflow_file_invalid")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        after_fd = os.fstat(fd)
        after_path = os.lstat(path)
        if (
            before_fd.st_dev,
            before_fd.st_ino,
            before_fd.st_size,
            before_fd.st_mtime_ns,
            before_fd.st_ctime_ns,
        ) != (
            after_fd.st_dev,
            after_fd.st_ino,
            after_fd.st_size,
            after_fd.st_mtime_ns,
            after_fd.st_ctime_ns,
        ) or (
            before_path.st_dev,
            before_path.st_ino,
            before_path.st_size,
            before_path.st_mtime_ns,
            before_path.st_ctime_ns,
            before_path.st_mode,
        ) != (
            after_path.st_dev,
            after_path.st_ino,
            after_path.st_size,
            after_path.st_mtime_ns,
            after_path.st_ctime_ns,
            after_path.st_mode,
        ):
            raise WorkflowStoreError("workflow_changed_during_read")
        return raw
    finally:
        os.close(fd)


def _workflow_root(owner: str, *, create: bool) -> Path:
    if not owner:
        raise WorkflowStoreError("workflow_owner_missing")
    home = get_owner_runtime_home(owner, create=create)
    try:
        home_stat = os.lstat(home)
    except OSError as exc:
        raise WorkflowStoreError("workflow_home_unavailable") from exc
    if stat.S_ISLNK(home_stat.st_mode) or not stat.S_ISDIR(home_stat.st_mode):
        raise WorkflowStoreError("workflow_home_invalid")
    root = home / "browser-workflows"
    if create:
        try:
            os.mkdir(root, 0o700)
        except FileExistsError:
            pass
    try:
        root_stat = os.lstat(root)
    except OSError as exc:
        raise WorkflowStoreError("workflow_store_unavailable") from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise WorkflowStoreError("workflow_store_invalid")
    if os.name != "nt":
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            try:
                os.chmod(root, 0o700, follow_symlinks=False)
            except (NotImplementedError, OSError) as exc:
                raise WorkflowStoreError("workflow_store_permissions_invalid") from exc
        if stat.S_IMODE(os.lstat(root).st_mode) != 0o700:
            raise WorkflowStoreError("workflow_store_permissions_invalid")
    try:
        resolved_home = home.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise WorkflowStoreError("workflow_store_unavailable") from exc
    if resolved_root.parent != resolved_home:
        raise WorkflowStoreError("workflow_store_escape")
    return resolved_root


def _workflow_path(owner: str, workflow_id: str, *, create_root: bool) -> Path:
    if not isinstance(workflow_id, str) or WORKFLOW_ID_RE.fullmatch(workflow_id) is None:
        raise WorkflowStoreError("workflow_id_invalid")
    return _workflow_root(owner, create=create_root) / f"{workflow_id}.json"


def _artifact_from_raw(owner: str, workflow_id: str, raw: bytes) -> WorkflowArtifact:
    try:
        value = _strict_json_loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise WorkflowStoreError("workflow_json_invalid") from exc
    schema_version = value.get("schema_version") if isinstance(value, dict) else None
    expected_fields = (
        {
            "schema_version",
            "owner_binding",
            "workflow_id",
            "hosts",
            "inputs",
            "capabilities",
            "plan",
        }
        if schema_version in {WORKFLOW_STORE_SCHEMA, WORKFLOW_STORE_SCHEMA_V3}
        else {
            "schema_version",
            "owner_binding",
            "workflow_id",
            "hosts",
            "inputs",
            "plan",
        }
    )
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("workflow_id") != workflow_id
    ):
        raise WorkflowStoreError("workflow_shape_invalid")
    core = {key: value[key] for key in value if key != "workflow_id"}
    clean_core = _validate_core(core, owner)
    expected_id = _sha256_bytes(_canonical_json(clean_core).encode("utf-8"))
    clean_payload = {**clean_core, "workflow_id": expected_id}
    canonical_raw = (_canonical_json(clean_payload) + "\n").encode("utf-8")
    if (
        not secrets.compare_digest(expected_id, workflow_id)
        or not secrets.compare_digest(canonical_raw, raw)
    ):
        raise WorkflowStoreError("workflow_digest_invalid")
    return WorkflowArtifact(
        workflow_id=workflow_id,
        digest=_sha256_bytes(raw),
        raw=raw,
        payload=clean_payload,
    )


def read_workflow(
    owner: str,
    workflow_id: str,
    *,
    expected_digest: str = "",
) -> WorkflowArtifact:
    """Read and fully revalidate an owner-bound immutable workflow."""

    try:
        path = _workflow_path(owner, workflow_id, create_root=False)
        raw = _safe_open_read(path)
    except (OSError, WorkflowStoreError) as exc:
        raise WorkflowStoreError("workflow_unavailable") from exc
    artifact = _artifact_from_raw(owner, workflow_id, raw)
    if expected_digest and not secrets.compare_digest(
        artifact.digest,
        expected_digest,
    ):
        raise WorkflowStoreError("workflow_changed")
    return artifact


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def publish_workflow(owner: str, artifact: WorkflowArtifact) -> PublishedWorkflow:
    """Atomically publish without replacing a concurrent winner."""

    rebuilt = build_workflow_artifact(
        owner=owner,
        hosts=artifact.payload.get("hosts", []),
        inputs=artifact.payload.get("inputs", {}),
        plan=artifact.payload.get("plan", []),
        schema_version=str(
            artifact.payload.get("schema_version") or WORKFLOW_STORE_SCHEMA
        ),
    )
    if (
        rebuilt.workflow_id != artifact.workflow_id
        or rebuilt.digest != artifact.digest
        or rebuilt.raw != artifact.raw
    ):
        raise WorkflowStoreError("workflow_artifact_invalid")
    root = _workflow_root(owner, create=True)
    target = root / f"{artifact.workflow_id}.json"
    staging = root / f".publish-{artifact.workflow_id}-{secrets.token_hex(12)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(staging, flags, 0o600)
    stage_identity: tuple[int, int] | None = None
    created = False
    try:
        try:
            offset = 0
            while offset < len(artifact.raw):
                written = os.write(fd, artifact.raw[offset:])
                if written <= 0:
                    raise OSError("short workflow write")
                offset += written
            os.fchmod(fd, 0o600)
            os.fsync(fd)
            written_stat = os.fstat(fd)
            stage_identity = (written_stat.st_dev, written_stat.st_ino)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(staging)
        except OSError:
            pass
        raise

    try:
        try:
            if os.name == "nt":
                os.rename(staging, target)
            else:
                os.link(staging, target, follow_symlinks=False)
                os.unlink(staging)
            created = True
        except FileExistsError:
            # POSIX no-replace publication uses link(staging, target) followed
            # immediately by unlink(staging).  The unlink changes only the
            # inode link count/ctime, not its bytes, but a concurrent strict
            # reader can correctly observe that metadata transition and fail
            # closed.  Give the winning publisher a bounded window to finish;
            # every retry still performs the full inode/content validation.
            existing: WorkflowArtifact | None = None
            for attempt in range(8):
                try:
                    existing = read_workflow(owner, artifact.workflow_id)
                    break
                except WorkflowStoreError:
                    if attempt == 7:
                        raise
                    time.sleep(0.001)
            assert existing is not None
            if existing.raw != artifact.raw:
                raise WorkflowStoreError("workflow_id_collision")
        target_stat = os.lstat(target)
        identity = (target_stat.st_dev, target_stat.st_ino)
        if (
            created
            and stage_identity != identity
            or stat.S_ISLNK(target_stat.st_mode)
            or not stat.S_ISREG(target_stat.st_mode)
            or os.name != "nt"
            and stat.S_IMODE(target_stat.st_mode) != 0o600
        ):
            raise WorkflowStoreError("workflow_publish_invalid")
        published = read_workflow(
            owner,
            artifact.workflow_id,
            expected_digest=artifact.digest,
        )
        _fsync_directory(root)
        return PublishedWorkflow(target, identity, created, published)
    except Exception:
        if created and stage_identity is not None:
            try:
                current = os.lstat(target)
                if (current.st_dev, current.st_ino) == stage_identity:
                    os.unlink(target)
            except OSError:
                pass
        raise
    finally:
        try:
            staged = os.lstat(staging)
            if stage_identity is None or (staged.st_dev, staged.st_ino) == stage_identity:
                os.unlink(staging)
        except OSError:
            pass


def rollback_published_workflow(published: PublishedWorkflow) -> bool:
    """Remove only the inode published by this transaction."""

    if not published.created:
        return False
    try:
        current = os.lstat(published.path)
    except FileNotFoundError:
        return False
    if (
        stat.S_ISLNK(current.st_mode)
        or (current.st_dev, current.st_ino) != published.identity
    ):
        raise WorkflowStoreError("workflow_target_changed")
    os.unlink(published.path)
    _fsync_directory(published.path.parent)
    return True


__all__ = [
    "INPUT_KEY_RE",
    "LEGACY_WORKFLOW_STORE_SCHEMA",
    "PublishedWorkflow",
    "WORKFLOW_ID_RE",
    "WORKFLOW_STORE_SCHEMA",
    "WORKFLOW_STORE_SCHEMA_V3",
    "WORKFLOW_V3_CAPABILITIES",
    "WorkflowArtifact",
    "WorkflowStoreError",
    "build_workflow_artifact",
    "publish_workflow",
    "read_workflow",
    "rollback_published_workflow",
    "workflow_v3_phase_a_enabled",
]
