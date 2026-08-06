"""Compile recorded browser events into an executable Playwright workflow.

The compiler preserves executable recorder data exactly.  It validates the
versioned wire shape and value types, but does not impose product-specific
length, step-count, selector, URL-scheme, or artifact-size ceilings.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, unquote_plus, urljoin, urlsplit

from crew.agent.skills import (
    get_user_skills_dir,
    install_skill_from_dir,
    validate_generated_skill,
)
from crew.browser.electron_bridge import (
    normalize_recording_event_v11,
    recording_v11_phase_a_enabled,
)
from crew.core.runctx import (
    current_owner_account_id,
    current_session_id,
    current_tool_call_id,
)
from crew.core.types import ToolPermissionDecision

from .workflow_store import (
    _SUSPENDING_TAKEOVER_REASONS as _SUSPENDING_REASONS,
    ASSERT_STATES,
    WorkflowArtifact,
    WorkflowStoreError,
    WORKFLOW_CAPABILITIES,
    WORKFLOW_STORE_SCHEMA,
    WORKFLOW_STORE_SCHEMA_V3,
    WORKFLOW_V3_CAPABILITIES,
    build_workflow_artifact,
    publish_workflow,
    rollback_published_workflow,
)

COMPILE_TOOL_NAME = "record_compile"
INSTALL_TOOL_NAME = "record_install"
WORKFLOW_SCHEMA_VERSION = "crew.browser.workflow.v1"
DRAFT_SCHEMA_VERSION = 3

_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_RECORDING_ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
_DRAFT_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SAFE_STEPS = frozenset({"snapshot_full", "takeover"})
_TRACE_ACTIONS = frozenset(
    {
        "navigate",
        "click",
        "dblclick",
        "drag",
        "dialog",
        "upload",
        "input",
        "submit",
        "key",
        "scroll",
        "note",
        "limit",
    }
)
_TRACE_EVENT_ACTIONS = _TRACE_ACTIONS - {"note"}
_TRACE_TIERS = frozenset({"plain", "identifier", "secret", "handoff"})
_TRACE_EVENT_FIELDS = frozenset(
    {
        "schemaVersion",
        "recordingId",
        "label",
        "step",
        "causalId",
        "action",
        "url",
        "hint",
        "target",
        "dragTarget",
        "targetSelector",
        "tier",
        "value",
        "values",
        "valueTruncated",
        "key",
        "clickButton",
        "clickCount",
        "position",
        "dialogAction",
        "dialogType",
        "dialogText",
        "modifiers",
        "uploadMode",
        "paths",
        "fileCount",
        "multiple",
        "accept",
        "scrollX",
        "scrollY",
        "backendNodeId",
        "timestamp",
        "selector",
        "page",
        "pageTruncated",
        "page_dropped",
        "provenance",
    }
)
_TRACE_TOPOLOGY_FIELDS = frozenset(
    {
        "openerPage",
        "popupOrdinal",
        "createdByCausalId",
    }
)
# 用户标注由可信 Crew UI 直接写入，不经过 Electron Host，且没有可执行 step。
# 它只参与整份 trace 的 digest，绝不进入 records / IR / renderer。
_TRACE_NOTE_FIELDS = frozenset({"action", "hint", "tier"})
_TRACE_TARGET_FIELDS_V2 = frozenset(
    {
        "tag",
        "text",
        "ariaLabel",
        "href",
        "ordinal",
        "id",
        "name",
        "role",
        "inputType",
        "testId",
        "testIdAttribute",
        "cssPath",
        "framePath",
    }
)
_TRACE_TARGET_FIELDS_V3 = _TRACE_TARGET_FIELDS_V2 | {"contentEditable"}
_TRACE_PROVENANCE_FIELDS = frozenset(
    {
        "schemaVersion",
        "source",
        "capturePhase",
        "browserTrusted",
        "targetEvidence",
        "nativeInput",
        "transport",
    }
)
# Match Playwright RecorderSignalProcessor's production navigation threshold.
_CAUSAL_NAVIGATION_WINDOW_MS = 5_000
_VOLATILE_URL_KEY = re.compile(
    r"(?:^|[_-])(?:auth|cache|code|csrf|nonce|request|session|sig|signature|"
    r"state|timestamp|token|ts)(?:$|[_-])",
    re.IGNORECASE,
)
_TEXT_EDITING_KEYS = frozenset(
    {
        "Backspace",
        "Delete",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)


def _workflow_step_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "source_step": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "trace.jsonl 中已有的 step 编号",
                    }
                },
                "required": ["source_step"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "safe_step": {
                        "type": "string",
                        "enum": sorted(_SAFE_STEPS),
                        "description": "固定安全步骤；不能携带文字、selector、URL 或 value",
                    }
                },
                "required": ["safe_step"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "overlay_step": {
                        "type": "object",
                        "properties": {
                            "source_step": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "该 trace step 点击的元素是遮挡层的关闭控件；"
                                    "注册为自动处理器而不是按顺序执行一次"
                                ),
                            }
                        },
                        "required": ["source_step"],
                        "additionalProperties": False,
                    }
                },
                "required": ["overlay_step"],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "assert_step": {
                        "type": "object",
                        "properties": {
                            "source_step": {
                                "type": "integer",
                                "minimum": 1,
                                "description": (
                                    "断言目标取自这个 trace step 的元素；"
                                    "不能自己写 selector"
                                ),
                            },
                            "state": {
                                "type": "string",
                                "enum": sorted(ASSERT_STATES),
                            },
                        },
                        "required": ["source_step", "state"],
                        "additionalProperties": False,
                    }
                },
                "required": ["assert_step"],
                "additionalProperties": False,
            },
        ]
    }


COMPILE_SCHEMA: dict[str, Any] = {
    "name": COMPILE_TOOL_NAME,
    "description": (
        "把当前账号、当前会话的一段录制编译为确定性回放技能草稿。"
        "只接受 source_step 引用和固定安全步骤；不接受 Markdown/body/selector/URL/value。"
        "本工具只生成私有草稿，不会安装技能。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recording_id": {
                "type": "string",
                "pattern": r"^[0-9a-fA-F]{8,32}$",
            },
            "slug": {
                "type": "string",
                "minLength": 3,
                "maxLength": 64,
                "pattern": r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$",
                "description": "小写字母开头，只含字母、数字、连字符",
            },
            "workflow": {
                "type": "object",
                "properties": {
                    "schema_version": {
                        "type": "string",
                        "const": WORKFLOW_SCHEMA_VERSION,
                    },
                    "steps": {
                        "type": "array",
                        "minItems": 1,
                        "items": _workflow_step_schema(),
                    },
                },
                "required": ["schema_version", "steps"],
                "additionalProperties": False,
            },
        },
        "required": ["recording_id", "slug", "workflow"],
        "additionalProperties": False,
    },
}

INSTALL_SCHEMA: dict[str, Any] = {
    "name": INSTALL_TOOL_NAME,
    "description": (
        "安装 record_compile 生成的不可变确定性回放技能草稿。"
        "draft_id 与 digest 会在安装前后重新验证。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "recording_id": {
                "type": "string",
                "pattern": r"^[0-9a-fA-F]{8,32}$",
            },
            "draft_id": {"type": "string", "pattern": r"^[0-9a-f]{24}$"},
            "draft_digest": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
        },
        "required": ["recording_id", "draft_id", "draft_digest"],
        "additionalProperties": False,
    },
}


class WorkflowRejected(ValueError):
    """对模型只返回固定类别，不回显不可信字段。"""


@dataclass(frozen=True)
class TraceSnapshot:
    digest: str
    records: dict[int, dict[str, Any]]
    hosts: tuple[str, ...]
    schema_version: int = 0
    transactions: tuple["RecordedActionGroup", ...] = ()


@dataclass(frozen=True)
class RecordedEffect:
    event_index: int
    page_guid: str
    signal: dict[str, Any]
    details: dict[str, Any]


@dataclass(frozen=True)
class RecordedActionGroup:
    step: int
    transaction_id: int
    transaction_kind: str
    page_guid: str
    action: dict[str, Any] | None
    evidence: dict[str, Any] | None
    effects: tuple[RecordedEffect, ...]


@dataclass(frozen=True)
class ValidatedDraft:
    draft_id: str
    draft_digest: str
    path: Path
    payload: dict[str, Any]
    trace: TraceSnapshot
    workflow: WorkflowArtifact


def _canonical_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in encoded
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _binding(label: str, value: str) -> str:
    return _sha256_text(f"crew-record-draft:{label}:{value}")


def _context() -> tuple[str, str, str]:
    return (
        str(current_owner_account_id.get() or ""),
        str(current_session_id.get() or ""),
        str(current_tool_call_id.get() or ""),
    )


def _normalize_host(raw: str) -> str:
    try:
        value = raw.strip().rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        return ""
    return value if _HOST_RE.fullmatch(value) else ""


def _normalized_origin(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
        host = _normalize_host(parsed.hostname or "")
        port = parsed.port
    except (TypeError, ValueError):
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username
        or parsed.password
        or port is not None
        and not 1 <= port <= 65_535
    ):
        return ""
    default_port = (parsed.scheme == "https" and port == 443) or (
        parsed.scheme == "http" and port == 80
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return f"{parsed.scheme}://{netloc}"


def _has_unsafe_url_character(value: str) -> bool:
    return any(
        char == "\x00" or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    )


def _validate_slug(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise WorkflowRejected("slug_invalid")
    slug = value
    if not 3 <= len(slug) <= 64 or not _SLUG_RE.fullmatch(slug):
        raise WorkflowRejected("slug_invalid")
    return slug


def _validate_recording_id(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise WorkflowRejected("recording_id_invalid")
    recording_id = value.lower()
    if not _RECORDING_ID_RE.fullmatch(recording_id):
        raise WorkflowRejected("recording_id_invalid")
    return recording_id


def _validate_workflow(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "steps"}:
        raise WorkflowRejected("workflow_shape_invalid")
    if value.get("schema_version") != WORKFLOW_SCHEMA_VERSION:
        raise WorkflowRejected("workflow_version_unsupported")
    steps = value.get("steps")
    if not isinstance(steps, list) or not steps:
        raise WorkflowRejected("workflow_steps_invalid")

    clean_steps: list[dict[str, Any]] = []
    source_steps: list[int] = []
    for item in steps:
        if not isinstance(item, dict) or len(item) != 1:
            raise WorkflowRejected("workflow_step_shape_invalid")
        if "source_step" in item:
            source_step = item["source_step"]
            if (
                isinstance(source_step, bool)
                or not isinstance(source_step, int)
                or source_step < 1
            ):
                raise WorkflowRejected("source_step_invalid")
            source_steps.append(source_step)
            clean_steps.append({"source_step": source_step})
        elif "safe_step" in item:
            safe_step = item["safe_step"]
            if not isinstance(safe_step, str) or safe_step not in _SAFE_STEPS:
                raise WorkflowRejected("safe_step_invalid")
            clean_steps.append({"safe_step": safe_step})
        elif "overlay_step" in item:
            # 遮挡处理器：注册一次，之后 Playwright 在每次 actionability 检查前
            # 自动清掉遮挡。目标同样只能引用轨迹里的某一步——用户录制时点过
            # 「我知道了」，那就是遮挡层存在且这是它的关闭控件的证据。
            spec = item["overlay_step"]
            if (
                not isinstance(spec, dict)
                or set(spec) != {"source_step"}
                or isinstance(spec.get("source_step"), bool)
                or not isinstance(spec.get("source_step"), int)
                or spec["source_step"] < 1
            ):
                raise WorkflowRejected("overlay_step_invalid")
            clean_steps.append(
                {"overlay_step": {"source_step": spec["source_step"]}}
            )
        elif "assert_step" in item:
            # 断言引用轨迹里的某一步，selector 由编译器从那一步取出——
            # 模型依旧不能提供任何 selector/URL/value。断言步骤**不计入**
            # source_steps 的递增唯一性检查：断言天然要重复引用同一个元素
            # （点之前确认可见、点之后确认变了），而动作步骤不能重复执行。
            spec = item["assert_step"]
            if (
                not isinstance(spec, dict)
                or set(spec) != {"source_step", "state"}
                or isinstance(spec.get("source_step"), bool)
                or not isinstance(spec.get("source_step"), int)
                or spec["source_step"] < 1
                or spec.get("state") not in ASSERT_STATES
            ):
                raise WorkflowRejected("assert_step_invalid")
            clean_steps.append(
                {
                    "assert_step": {
                        "source_step": spec["source_step"],
                        "state": str(spec["state"]),
                    }
                }
            )
        else:
            # URL/selector/value/body/text 等任意行为字段都到不了 renderer。
            raise WorkflowRejected("workflow_step_field_forbidden")

    if not source_steps:
        raise WorkflowRejected("workflow_requires_source_step")
    if source_steps != sorted(set(source_steps)):
        raise WorkflowRejected("source_steps_must_be_unique_and_increasing")
    return {"schema_version": WORKFLOW_SCHEMA_VERSION, "steps": clean_steps}


def _validate_compile_args(args: Any) -> tuple[str, str, dict[str, Any]]:
    if not isinstance(args, dict) or set(args) != {"recording_id", "slug", "workflow"}:
        raise WorkflowRejected("compile_fields_forbidden")
    return (
        _validate_recording_id(args.get("recording_id")),
        _validate_slug(args.get("slug")),
        _validate_workflow(args.get("workflow")),
    )


def _validate_install_args(args: Any) -> tuple[str, str, str]:
    if not isinstance(args, dict) or set(args) != {
        "recording_id",
        "draft_id",
        "draft_digest",
    }:
        raise WorkflowRejected("install_fields_invalid")
    recording_id = _validate_recording_id(args.get("recording_id"))
    draft_id_value = args.get("draft_id")
    digest_value = args.get("draft_digest")
    if (
        not isinstance(draft_id_value, str)
        or draft_id_value != draft_id_value.strip()
        or not isinstance(digest_value, str)
        or digest_value != digest_value.strip()
    ):
        raise WorkflowRejected("draft_identity_invalid")
    draft_id = draft_id_value
    digest = digest_value
    if not _DRAFT_ID_RE.fullmatch(draft_id) or not _DIGEST_RE.fullmatch(digest):
        raise WorkflowRejected("draft_identity_invalid")
    return recording_id, draft_id, digest


def _safe_open_read(path: Path) -> bytes:
    # O_NOFOLLOW 不是跨平台契约（Windows 没有）。先 lstat 拒绝最终 symlink，
    # open 后再用 dev/ino 对齐，封住 lstat→open 的替换窗口；读完同时复核 fd
    # 与路径两侧，避免读取期间 rename/replace 后把旧证据当成当前证据。
    before_path = os.lstat(path)
    if (
        stat.S_ISLNK(before_path.st_mode)
        or not stat.S_ISREG(before_path.st_mode)
    ):
        raise WorkflowRejected("file_invalid")
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
            raise WorkflowRejected("file_invalid")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        raw = b"".join(chunks)
        after_fd = os.fstat(fd)
        try:
            after_path = os.lstat(path)
        except OSError as exc:
            raise WorkflowRejected("file_changed_during_read") from exc
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
            raise WorkflowRejected("file_changed_during_read")
        return raw
    finally:
        os.close(fd)


def _validate_selector(value: Any) -> str:
    if value == "":
        return ""
    if (
        not isinstance(value, str)
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise WorkflowRejected("trace_selector_invalid")
    return value


def _trace_text(value: Any, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise WorkflowRejected(f"trace_{field}_invalid")
    return value


def _trace_int(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int | None,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or maximum is not None
        and value > maximum
    ):
        raise WorkflowRejected(f"trace_{field}_invalid")
    return value


def _validate_trace_target(
    value: Any,
    *,
    schema_version: int,
) -> dict[str, Any] | None:
    if value is None:
        return None
    expected_fields = (
        _TRACE_TARGET_FIELDS_V3
        if schema_version >= 3
        else _TRACE_TARGET_FIELDS_V2
    )
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise WorkflowRejected("trace_target_invalid")

    tag = _trace_text(value.get("tag"), field="target_tag")
    text = _trace_text(value.get("text"), field="target_text")
    aria_label = _trace_text(
        value.get("ariaLabel"),
        field="target_aria_label",
    )
    href = _trace_text(value.get("href"), field="target_href")
    _trace_int(
        value.get("ordinal"),
        field="target_ordinal",
        minimum=0,
        maximum=None,
    )
    target_id = _trace_text(value.get("id"), field="target_id")
    name = _trace_text(value.get("name"), field="target_name")
    role = _trace_text(value.get("role"), field="target_role")
    input_type = _trace_text(
        value.get("inputType"), field="target_input_type"
    )
    test_id = _trace_text(value.get("testId"), field="target_test_id")
    _trace_text(
        value.get("testIdAttribute"),
        field="target_test_id_attribute",
    )
    _trace_text(
        value.get("cssPath"), field="target_css_path"
    )
    frame_path = value.get("framePath")
    if not isinstance(frame_path, list):
        raise WorkflowRejected("trace_target_frame_path_invalid")
    for item in frame_path:
        _trace_text(item, field="target_frame_path")
    content_editable = value.get("contentEditable", False)
    if type(content_editable) is not bool:
        raise WorkflowRejected("trace_target_content_editable_invalid")

    # text/ariaLabel 只用于生成可读的运行时参数名与 display_name；表单默认值
    # 来自独立的 recorder value/values 字段。selector 使用 recorder 的稳定字段。
    return {
        "href": href,
        "tag": tag.lower(),
        "text": text,
        "ariaLabel": aria_label,
        "id": target_id,
        "name": name,
        "role": role,
        "inputType": input_type.lower(),
        "testId": test_id,
        # v1/v2 没有该证明，按 false 处理；这样旧 contenteditable 轨迹会在
        # 运行时与真实目标不匹配并失败，而不会被静默提升为可写权限。
        "contentEditable": content_editable,
    }


def _validate_trace_provenance(
    value: Any,
    *,
    schema_version: int,
    action: str,
    tier: str,
    target_present: bool,
) -> None:
    if not isinstance(value, dict) or set(value) != _TRACE_PROVENANCE_FIELDS:
        raise WorkflowRejected("trace_provenance_invalid")
    if value.get("schemaVersion") != 1:
        raise WorkflowRejected("trace_provenance_version_invalid")
    if value.get("transport") != "authenticated-electron-host":
        raise WorkflowRejected("trace_transport_invalid")

    host_generated = action in {"navigate", "dialog", "limit"}
    target_evidence = value.get("targetEvidence")
    legacy_redacted = (
        tier in {"secret", "handoff"} and target_evidence == "redacted"
    )
    expected_target_evidence = (
        "redacted"
        if legacy_redacted
        else ("synchronous" if target_present else "none")
    )
    if value.get("targetEvidence") != expected_target_evidence:
        raise WorkflowRejected("trace_target_evidence_invalid")

    if host_generated:
        valid = (
            value.get("source") in {"host-navigation", "browser-host"}
            and value.get("capturePhase") == "host"
            and value.get("browserTrusted") is False
            and value.get("nativeInput") == "host"
        )
    elif schema_version >= 2:
        valid = (
            value.get("source")
            in {"document-world", "isolated-world", "legacy-isolated-world"}
            and value.get("capturePhase") == "event-callback"
            and value.get("browserTrusted") is True
            and value.get("nativeInput") in {"correlated", "unverified"}
        )
    else:
        valid = (
            value.get("source") == "legacy-isolated-world"
            and value.get("capturePhase") == "event-callback"
            and value.get("browserTrusted") is True
            and value.get("nativeInput") == "legacy-host-checked"
        )
    if not valid:
        raise WorkflowRejected("trace_provenance_binding_invalid")


def _validate_trace_note(value: dict[str, Any]) -> None:
    if set(value) != _TRACE_NOTE_FIELDS:
        raise WorkflowRejected("trace_note_shape_invalid")
    if value.get("action") != "note" or value.get("tier") != "plain":
        raise WorkflowRejected("trace_note_invalid")
    hint = _trace_text(value.get("hint"), field="note_hint")
    if not hint.strip():
        raise WorkflowRejected("trace_note_invalid")


def _validate_trace_record(
    value: Any,
    expected_step: int | None,
    recording_id: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        raise WorkflowRejected("trace_record_invalid")
    if value.get("action") == "note":
        _validate_trace_note(value)
        return None

    schema_version = value.get("schemaVersion")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version not in {1, 3, 4, 5, 6, 7, 8, 9, 10}
    ):
        raise WorkflowRejected("trace_schema_version_invalid")
    v8_fields = _TRACE_EVENT_FIELDS - {
        "causalId",
        "dialogAction",
        "dialogType",
        "dialogText",
    }
    v6_fields = v8_fields - {"position"}
    select_values_fields = {"values"}
    v5_fields = v6_fields - select_values_fields
    upload_fields = {"uploadMode", "paths", "fileCount", "multiple", "accept"}
    v4_fields = v5_fields - upload_fields
    drag_fields = {"dragTarget", "targetSelector"}
    click_fields = {"clickButton", "clickCount", "modifiers"}
    v3_fields = v4_fields - click_fields
    legacy_fields = v3_fields - {"valueTruncated"} - drag_fields
    pre_drag_fields = v3_fields - drag_fields
    expected_fields = (
        _TRACE_EVENT_FIELDS
        if schema_version >= 9
        else v8_fields
        if schema_version == 8
        else v6_fields
        if schema_version in {6, 7}
        else v5_fields
        if schema_version == 5
        else v4_fields
        if schema_version == 4
        else v3_fields
        if schema_version == 3
        else legacy_fields
    )
    # Explicitly migrate the two recorder-v3 revisions that predate
    # valueTruncated and drag's second target.
    actual_fields = set(value)
    topology_fields = actual_fields & _TRACE_TOPOLOGY_FIELDS
    base_actual_fields = actual_fields - topology_fields
    migrated_v3_shape = (
        schema_version == 3
        and frozenset(base_actual_fields)
        in {frozenset(legacy_fields), frozenset(pre_drag_fields)}
    )
    if (
        (schema_version < 10 and topology_fields)
        or (
            base_actual_fields != expected_fields
            and not migrated_v3_shape
        )
    ):
        raise WorkflowRejected("trace_record_shape_invalid")
    trace_recording_id = value.get("recordingId")
    if not isinstance(trace_recording_id, str):
        raise WorkflowRejected("trace_recording_id_invalid")
    if (
        schema_version >= 2
        and trace_recording_id != recording_id
        or schema_version == 1
        and trace_recording_id != ""
    ):
        raise WorkflowRejected("trace_recording_binding_invalid")

    step = value.get("step")
    step = _trace_int(
        step,
        field="step",
        minimum=1,
        maximum=None,
    )
    if (
        expected_step is None
        and step != 1
        or expected_step is not None
        and step != expected_step + 1
    ):
        raise WorkflowRejected("trace_steps_not_consecutive")

    action = value.get("action")
    if not isinstance(action, str) or action not in _TRACE_EVENT_ACTIONS:
        raise WorkflowRejected("trace_action_invalid")
    tier = value.get("tier")
    if not isinstance(tier, str) or tier not in _TRACE_TIERS:
        raise WorkflowRejected("trace_tier_invalid")
    label = _trace_text(value.get("label"), field="label")
    opener_page = _trace_text(
        value.get("openerPage", ""),
        field="opener_page",
    )
    if schema_version >= 10 and (
        re.fullmatch(r"p(?:0|[1-9]\d*)", label) is None
        or opener_page
        and re.fullmatch(r"p(?:0|[1-9]\d*)", opener_page) is None
    ):
        raise WorkflowRejected("trace_page_identity_invalid")
    raw_popup_ordinal = value.get("popupOrdinal")
    if raw_popup_ordinal is None:
        popup_ordinal: int | None = None
    elif (
        isinstance(raw_popup_ordinal, bool)
        or not isinstance(raw_popup_ordinal, int)
        or raw_popup_ordinal < 0
    ):
        raise WorkflowRejected("trace_popup_ordinal_invalid")
    else:
        popup_ordinal = raw_popup_ordinal
    created_by_causal_id = _trace_int(
        value.get("createdByCausalId", 0),
        field="created_by_causal_id",
        minimum=0,
        maximum=9_007_199_254_740_991,
    )
    url = _trace_text(value.get("url"), field="url")
    _trace_text(value.get("hint"), field="hint")
    _trace_text(value.get("page"), field="page")
    if _has_unsafe_url_character(url):
        raise WorkflowRejected("trace_url_invalid")
    selector = _validate_selector(value.get("selector"))
    raw_value = _trace_text(value.get("value"), field="value")
    if schema_version >= 6:
        raw_values = value.get("values")
        if not isinstance(raw_values, list):
            raise WorkflowRejected("trace_select_values_invalid")
        select_values = [
            _trace_text(
                item,
                field="select_value",
            )
            for item in raw_values
        ]
    else:
        select_values = []
    value_truncated = value.get("valueTruncated", False)
    if type(value_truncated) is not bool:
        raise WorkflowRejected("trace_value_truncated_invalid")
    key = _trace_text(value.get("key"), field="key")
    if any(ord(c) < 0x20 for c in key):
        raise WorkflowRejected("trace_key_invalid")
    causal_id = (
        _trace_int(
            value.get("causalId"),
            field="causal_id",
            minimum=0,
            maximum=9_007_199_254_740_991,
        )
        if schema_version >= 9
        else 0
    )
    if schema_version >= 4:
        click_button = value.get("clickButton")
        click_count = value.get("clickCount")
        modifiers = value.get("modifiers")
        if action in {"click", "dblclick"}:
            if (
                click_button not in {"left", "middle", "right"}
                or type(click_count) is not int
                or not 1 <= click_count <= 9_007_199_254_740_991
                or action == "dblclick"
                and click_count != 2
                or not isinstance(modifiers, list)
                or any(
                    not isinstance(modifier, str)
                    or modifier not in {"Alt", "Control", "Meta", "Shift"}
                    for modifier in modifiers
                )
                or len(set(modifiers)) != len(modifiers)
            ):
                raise WorkflowRejected("trace_click_options_invalid")
            click_modifiers = [
                modifier
                for modifier in ("Alt", "Control", "Meta", "Shift")
                if modifier in modifiers
            ]
        elif click_button != "" or click_count != 0 or modifiers != []:
            raise WorkflowRejected("trace_click_surface_mismatch")
        else:
            click_button = ""
            click_count = 0
            click_modifiers = []
    else:
        click_button = "left" if action in {"click", "dblclick"} else ""
        click_count = 2 if action == "dblclick" else 1 if action == "click" else 0
        click_modifiers = []
    if schema_version >= 8:
        raw_position = value.get("position")
        if raw_position is None:
            click_position = None
        elif (
            action not in {"click", "dblclick"}
            or not isinstance(raw_position, dict)
            or set(raw_position) != {"x", "y"}
            or any(
                type(raw_position.get(axis)) not in {int, float}
                or not math.isfinite(float(raw_position[axis]))
                or float(raw_position[axis]) < 0
                for axis in ("x", "y")
            )
        ):
            raise WorkflowRejected("trace_click_position_invalid")
        else:
            click_position = {
                "x": float(raw_position["x"]),
                "y": float(raw_position["y"]),
            }
    else:
        click_position = None
    if schema_version >= 9:
        raw_dialog_action = value.get("dialogAction")
        raw_dialog_type = value.get("dialogType")
        raw_dialog_text = value.get("dialogText")
        if action == "dialog":
            if (
                raw_dialog_action not in {"accept", "dismiss"}
                or raw_dialog_type
                not in {"alert", "confirm", "prompt", "beforeunload"}
                or not isinstance(raw_dialog_text, str)
                or "\x00" in raw_dialog_text
                or raw_dialog_type != "prompt"
                and raw_dialog_text != ""
                or raw_dialog_action == "dismiss"
                and raw_dialog_text != ""
            ):
                raise WorkflowRejected("trace_dialog_invalid")
            dialog_action = str(raw_dialog_action)
            dialog_type = str(raw_dialog_type)
            dialog_text = raw_dialog_text
        elif (
            raw_dialog_action != ""
            or raw_dialog_type != ""
            or raw_dialog_text != ""
        ):
            raise WorkflowRejected("trace_dialog_surface_mismatch")
        else:
            dialog_action = ""
            dialog_type = ""
            dialog_text = ""
    else:
        if action == "dialog":
            raise WorkflowRejected("trace_dialog_requires_v9")
        dialog_action = ""
        dialog_type = ""
        dialog_text = ""

    if schema_version >= 5:
        upload_mode = value.get("uploadMode")
        raw_paths = value.get("paths")
        file_count = value.get("fileCount")
        multiple = value.get("multiple")
        accept = _trace_text(value.get("accept"), field="upload_accept")
        if action == "upload":
            if (
                upload_mode not in {"paths", "handoff", "clear"}
                or not isinstance(raw_paths, list)
                or type(file_count) is not int
                or file_count < 0
                or type(multiple) is not bool
            ):
                raise WorkflowRejected("trace_upload_invalid")
            upload_paths = [
                _trace_text(item, field="upload_path")
                for item in raw_paths
            ]
            if (
                any(not item for item in upload_paths)
                or upload_mode == "paths"
                and (not upload_paths or len(upload_paths) != file_count)
                or upload_mode == "handoff"
                and (upload_paths or file_count < 1)
                or upload_mode == "clear"
                and (upload_paths or file_count != 0)
            ):
                raise WorkflowRejected("trace_upload_invalid")
        elif (
            upload_mode != ""
            or raw_paths != []
            or file_count != 0
            or multiple is not False
            or accept
        ):
            raise WorkflowRejected("trace_upload_surface_mismatch")
        else:
            upload_mode = ""
            upload_paths = []
            file_count = 0
            multiple = False
    else:
        if action == "upload":
            raise WorkflowRejected("trace_upload_requires_v5")
        upload_mode = ""
        upload_paths = []
        file_count = 0
        multiple = False
        accept = ""

    safe_target = _validate_trace_target(
        value.get("target"),
        schema_version=schema_version,
    )
    drag_target = _validate_trace_target(
        value.get("dragTarget"),
        schema_version=schema_version,
    )
    target_selector = _validate_selector(value.get("targetSelector", ""))
    scroll_x = _trace_int(
        value.get("scrollX"),
        field="scroll",
        minimum=-9_007_199_254_740_991,
        maximum=9_007_199_254_740_991,
    )
    scroll_y = _trace_int(
        value.get("scrollY"),
        field="scroll",
        minimum=-9_007_199_254_740_991,
        maximum=9_007_199_254_740_991,
    )
    _trace_int(
        value.get("backendNodeId"),
        field="backend_node_id",
        minimum=0,
        maximum=2_147_483_647,
    )
    timestamp = _trace_int(
        value.get("timestamp"),
        field="timestamp",
        minimum=0,
        maximum=9_007_199_254_740_991,
    )
    if not isinstance(value.get("pageTruncated"), bool) or not isinstance(
        value.get("page_dropped"), bool
    ):
        raise WorkflowRejected("trace_page_flags_invalid")
    _validate_trace_provenance(
        value.get("provenance"),
        schema_version=schema_version,
        action=action,
        tier=tier,
        target_present=(
            safe_target is not None
            and (action != "drag" or drag_target is not None)
        ),
    )

    # Legacy recorders intentionally emitted a fixed redacted envelope. Keep
    # validating that old wire contract so a partially redacted row is never
    # mistaken for executable data. Recorder v10 may instead preserve a full
    # ordinary action even when its historical tier is secret/handoff.
    legacy_redacted = (
        tier in {"secret", "handoff"}
        and isinstance(value.get("provenance"), dict)
        and value["provenance"].get("targetEvidence") == "redacted"
    )
    if legacy_redacted:
        if (
            url
            or raw_value
            or select_values
            or key
            or selector
            or value.get("target") is not None
            or value.get("dragTarget") is not None
            or target_selector
            or value.get("page")
            or value_truncated
        ):
            raise WorkflowRejected("trace_sensitive_surface_present")
    if action != "input" and raw_value:
        raise WorkflowRejected("trace_value_action_mismatch")
    target_input_type = (
        str(safe_target.get("inputType") or "").lower()
        if isinstance(safe_target, dict)
        else ""
    )
    multi_select_input = (
        action == "input"
        and isinstance(safe_target, dict)
        and safe_target.get("tag") == "select"
        and target_input_type == "select-multiple"
    )
    if schema_version >= 6 and not multi_select_input and select_values:
        raise WorkflowRejected("trace_select_values_action_mismatch")
    if action != "input" and value_truncated:
        raise WorkflowRejected("trace_value_truncated_action_mismatch")
    if action != "key" and key:
        raise WorkflowRejected("trace_key_action_mismatch")
    if action != "scroll" and (scroll_x or scroll_y):
        raise WorkflowRejected("trace_scroll_action_mismatch")
    if action != "drag" and (drag_target is not None or target_selector):
        raise WorkflowRejected("trace_drag_surface_mismatch")
    if action == "drag" and (
        safe_target is None
        or drag_target is None
        or not selector
        or not target_selector
    ):
        raise WorkflowRejected("trace_drag_target_invalid")
    if action == "upload":
        target_input_type = (
            str(safe_target.get("inputType") or "").lower()
            if isinstance(safe_target, dict)
            else ""
        )
        if (
            safe_target is None
            or not selector
            or target_input_type != "file"
            or raw_value
            or key
            or scroll_x
            or scroll_y
            or drag_target is not None
            or target_selector
        ):
            raise WorkflowRejected("trace_upload_target_invalid")
    if action in {"navigate", "dialog", "limit"} and (
        safe_target is not None or selector or raw_value or key
    ):
        raise WorkflowRejected("trace_host_action_surface_invalid")

    # 只复制 compiler/renderer 会消费的字段。页面快照、hint 和 provenance
    # 不进入草稿；精确 value/values/paths 会成为可覆盖的录制默认值。
    return {
        # Internal compile evidence only.  Navigation association changes at
        # recorder v10, so the coalescer must be able to distinguish an exact
        # causal trace from a legacy trace that needs conservative migration.
        "schemaVersion": schema_version,
        "step": step,
        "action": action,
        # v10 labels are stable recording-local pN identities. They normally
        # compile to `page`; cross-page dialogs also retain `label` as
        # diagnostics. Legacy labels remain in-memory compile evidence only.
        "label": label,
        "openerPage": opener_page,
        "popupOrdinal": popup_ordinal,
        "createdByCausalId": created_by_causal_id,
        "tier": tier,
        "legacyRedacted": legacy_redacted,
        "url": url,
        "selector": selector,
        "value": raw_value,
        "values": select_values,
        "valueTruncated": value_truncated,
        "key": key,
        "causalId": causal_id,
        "clickButton": click_button,
        "clickCount": click_count,
        "position": click_position,
        "dialogAction": dialog_action,
        "dialogType": dialog_type,
        "dialogText": dialog_text,
        "modifiers": click_modifiers,
        "uploadMode": upload_mode,
        # Native paths become exact recorded defaults when resolution was
        # complete; callers may override them at replay time.
        "paths": upload_paths,
        "fileCount": file_count,
        "multiple": multiple,
        "accept": accept,
        "target": safe_target,
        "dragTarget": drag_target,
        "targetSelector": target_selector,
        "scrollX": scroll_x,
        "scrollY": scroll_y,
        "timestamp": timestamp,
    }


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


def _read_trace_v11_rows(
    *,
    raw: bytes,
    parsed_rows: list[Any],
    recording_id: str,
) -> TraceSnapshot:
    if not recording_v11_phase_a_enabled():
        raise WorkflowRejected("trace_schema_v11_disabled")
    normalized_rows: list[dict[str, Any]] = []
    for parsed in parsed_rows:
        if (
            isinstance(parsed, dict)
            and parsed.get("action") == "note"
        ):
            _validate_trace_note(parsed)
            continue
        normalized = normalize_recording_event_v11(parsed, persisted=True)
        if normalized is None:
            raise WorkflowRejected("trace_v11_record_invalid")
        # Persisted v11 is canonical bridge output. Accepting a merely
        # normalizable row would make its digest and interpreted action differ.
        if normalized != parsed:
            raise WorkflowRejected("trace_v11_record_not_canonical")
        if normalized["recordingId"] != recording_id:
            raise WorkflowRejected("trace_recording_binding_invalid")
        normalized_rows.append(normalized)
    if not normalized_rows:
        raise WorkflowRejected("trace_step_count_invalid")

    expected_event_index = 1
    next_step = 1
    transaction_order: list[int] = []
    transaction_rows: dict[int, list[dict[str, Any]]] = {}
    transaction_meta: dict[int, tuple[int, str]] = {}
    for row in normalized_rows:
        if row["eventIndex"] != expected_event_index:
            raise WorkflowRejected("trace_event_indices_not_consecutive")
        expected_event_index += 1
        transaction_id = int(row["transactionId"])
        step = int(row["step"])
        transaction_kind = str(row["transactionKind"])
        meta = transaction_meta.get(transaction_id)
        if meta is None:
            if step != next_step:
                raise WorkflowRejected("trace_steps_not_consecutive")
            next_step += 1
            transaction_order.append(transaction_id)
            transaction_meta[transaction_id] = (step, transaction_kind)
            transaction_rows[transaction_id] = []
        elif meta != (step, transaction_kind):
            raise WorkflowRejected("trace_transaction_identity_invalid")
        transaction_rows[transaction_id].append(row)

    groups: list[RecordedActionGroup] = []
    hosts: set[str] = set()
    for transaction_id in transaction_order:
        rows = transaction_rows[transaction_id]
        step, transaction_kind = transaction_meta[transaction_id]
        action_rows = [row for row in rows if row["recordKind"] == "action"]
        signal_rows = [row for row in rows if row["recordKind"] == "signal"]
        if (
            transaction_kind == "action"
            and len(action_rows) != 1
            or transaction_kind == "observation"
            and action_rows
            or transaction_kind == "observation"
            and not signal_rows
        ):
            raise WorkflowRejected("trace_transaction_shape_invalid")
        action_row = action_rows[0] if action_rows else None
        page_guid = str(
            action_row["pageGuid"] if action_row else signal_rows[0]["pageGuid"]
        )
        popup_identities: set[tuple[str, int]] = set()
        popup_pages: set[str] = set()
        effects: list[RecordedEffect] = []
        for row in signal_rows:
            signal = dict(row["signal"])
            details = dict(row["details"])
            if signal["name"] == "popup":
                popup_identity = (
                    str(details["openerPageGuid"]),
                    int(details["popupIndex"]),
                )
                popup_page = str(signal["popupPageGuid"])
                if (
                    popup_identity in popup_identities
                    or popup_page in popup_pages
                    or popup_page == details["openerPageGuid"]
                ):
                    raise WorkflowRejected("trace_popup_identity_invalid")
                popup_identities.add(popup_identity)
                popup_pages.add(popup_page)
            effects.append(
                RecordedEffect(
                    event_index=int(row["eventIndex"]),
                    page_guid=str(row["pageGuid"]),
                    signal=signal,
                    details=details,
                )
            )
        action = dict(action_row["action"]) if action_row else None
        evidence = dict(action_row["evidence"]) if action_row else None
        groups.append(
            RecordedActionGroup(
                step=step,
                transaction_id=transaction_id,
                transaction_kind=transaction_kind,
                page_guid=page_guid,
                action=action,
                evidence=evidence,
                effects=tuple(
                    sorted(effects, key=lambda effect: effect.event_index)
                ),
            )
        )
        urls: list[str] = []
        if action is not None and isinstance(action.get("url"), str):
            urls.append(str(action["url"]))
        if evidence is not None:
            urls.append(str(evidence.get("url") or ""))
        urls.extend(
            str(effect.signal.get("url") or "")
            for effect in effects
            if effect.signal.get("name") == "navigation"
        )
        for url in urls:
            if not url:
                continue
            try:
                host = _normalize_host(urlsplit(url).hostname or "")
            except ValueError as exc:
                raise WorkflowRejected("trace_url_invalid") from exc
            if host:
                hosts.add(host)
    return TraceSnapshot(
        _sha256_bytes(raw),
        {},
        tuple(sorted(hosts)),
        schema_version=11,
        transactions=tuple(groups),
    )


def _read_trace(trace: Path, recording_id: str) -> TraceSnapshot:
    # The recorder creates this marker before capture starts and removes it only
    # after a clean Host stop has been reconciled against the exact persisted
    # step sequence.  Its presence means the process crashed, transport/write
    # lost data, or the recording is still open.
    try:
        os.lstat(trace.parent / "INCOMPLETE")
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise WorkflowRejected("trace_unavailable") from exc
    else:
        raise WorkflowRejected("trace_incomplete")
    try:
        raw = _safe_open_read(trace)
    except (OSError, WorkflowRejected) as exc:
        raise WorkflowRejected("trace_unavailable") from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkflowRejected("trace_not_utf8") from exc

    lines = text.splitlines()
    if not lines:
        raise WorkflowRejected("trace_step_count_invalid")
    parsed_rows: list[Any] = []
    for line in lines:
        try:
            parsed_rows.append(_strict_json_loads(line))
        except (RecursionError, TypeError, ValueError) as exc:
            raise WorkflowRejected("trace_json_invalid") from exc
    executable_rows = [
        row
        for row in parsed_rows
        if not (
            isinstance(row, dict)
            and row.get("action") == "note"
        )
    ]
    if executable_rows and any(
        isinstance(row, dict) and row.get("schemaVersion") == 11
        for row in executable_rows
    ):
        return _read_trace_v11_rows(
            raw=raw,
            parsed_rows=parsed_rows,
            recording_id=recording_id,
        )

    records: dict[int, dict[str, Any]] = {}
    hosts: set[str] = set()
    previous: int | None = None
    for parsed in parsed_rows:
        record = _validate_trace_record(parsed, previous, recording_id)
        if record is None:
            continue
        step = record["step"]
        previous = step
        records[step] = record
        if record["action"] == "limit":
            raise WorkflowRejected("trace_was_truncated")
        if record["url"]:
            try:
                parsed_url = urlsplit(record["url"])
            except ValueError as exc:
                raise WorkflowRejected("trace_url_invalid") from exc
            if not parsed_url.scheme:
                raise WorkflowRejected("trace_url_scheme_invalid")
            # Hosts are diagnostic metadata only. Non-host URL schemes are
            # executable browser destinations and intentionally add no host.
            host = _normalize_host(parsed_url.hostname or "")
            if host:
                hosts.add(host)
    return TraceSnapshot(
        _sha256_bytes(raw),
        records,
        tuple(sorted(hosts)),
    )


def _safe_navigation_url(raw: str, base: str) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        resolved = urljoin(base, raw)
        parsed = urlsplit(resolved)
    except (TypeError, ValueError):
        return ""
    if not parsed.scheme:
        return ""
    if _has_unsafe_url_character(resolved):
        return ""
    # Query/hash and scheme-specific payloads are executable application state.
    # Do not parse/re-encode them: doing so mutates signed URLs and data: pages.
    return resolved


def _looks_volatile_url_value(value: str) -> bool:
    if len(value) < 12:
        return False
    decoded = unquote_plus(value)
    return bool(
        re.fullmatch(
            r"(?:[0-9a-f]{16,}|[A-Za-z0-9_-]{16,}|"
            r"[0-9a-f]{8}-[0-9a-f-]{27,})",
            decoded,
            re.IGNORECASE,
        )
        and any(char.isalpha() for char in decoded)
        and any(char.isdigit() for char in decoded)
    )


def _dynamic_url_pattern(
    url: str,
    bindings: list[tuple[str, str]],
) -> tuple[str, list[dict[str, str]]] | None:
    """Turn input-bound/volatile URL pieces into a structural pattern.

    Literal URLs remain exact. Runtime form values become explicit input
    segments, while non-deterministic nonce/signature-like values become
    delimiter-bounded wildcards. The URL postcondition therefore contains
    neither the recorded form value nor a one-run OAuth nonce; the form value
    remains separately available as the replay input's recorded default.
    """

    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        # Patterned postconditions use an HTTP(S) origin. Every other valid
        # browser URL remains an exact literal postcondition.
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if not origin or not url.startswith(origin):
        return None
    tail_start = len(origin)
    spans: list[tuple[int, int, dict[str, str]]] = []

    def input_segment(
        raw: str,
        *,
        query_value: bool,
    ) -> dict[str, str] | None:
        decoded = unquote_plus(raw) if query_value else unquote(raw)
        for recorded, input_key in reversed(bindings):
            if not recorded or decoded != recorded:
                continue
            if query_value and "+" in raw:
                encoding = "plus"
            elif "%" in raw:
                encoding = "percent"
            elif query_value:
                # An alphanumeric demonstration cannot reveal whether the app
                # will serialize a future space as '+' or '%20'. Replay tries
                # both canonical query encodings.
                encoding = "query"
            else:
                encoding = "path"
            return {"input_key": input_key, "encoding": encoding}
        return None

    tail = url[tail_start:]
    for match in re.finditer(r"([?&])([^=&#]+)=([^&#]*)", tail):
        raw_key = match.group(2)
        raw_value = match.group(3)
        start = tail_start + match.start(3)
        end = tail_start + match.end(3)
        segment = input_segment(raw_value, query_value=True)
        if segment is not None:
            spans.append((start, end, segment))
            continue
        key = unquote_plus(raw_key)
        if _VOLATILE_URL_KEY.search(key) or _looks_volatile_url_value(raw_value):
            spans.append((start, end, {"wildcard": "query_value"}))

    path_end_candidates = [
        index for index in (url.find("?", tail_start), url.find("#", tail_start))
        if index >= 0
    ]
    path_end = min(path_end_candidates) if path_end_candidates else len(url)
    for match in re.finditer(r"[^/]+", url[tail_start:path_end]):
        start = tail_start + match.start()
        end = tail_start + match.end()
        raw_segment = match.group(0)
        segment = input_segment(raw_segment, query_value=False)
        if segment is not None:
            spans.append((start, end, segment))
        elif _looks_volatile_url_value(raw_segment):
            spans.append((start, end, {"wildcard": "path_segment"}))

    fragment_start = url.find("#", tail_start)
    if fragment_start >= 0:
        fragment_query = url.find("?", fragment_start + 1)
        fragment_path_end = fragment_query if fragment_query >= 0 else len(url)
        for match in re.finditer(r"[^/]+", url[fragment_start + 1:fragment_path_end]):
            start = fragment_start + 1 + match.start()
            end = fragment_start + 1 + match.end()
            raw_segment = match.group(0)
            segment = input_segment(raw_segment, query_value=False)
            if segment is not None:
                spans.append((start, end, segment))
            elif _looks_volatile_url_value(raw_segment):
                spans.append((start, end, {"wildcard": "fragment_value"}))

    if not spans:
        return None
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, dict[str, str]]] = []
    cursor = -1
    for span in spans:
        if span[0] < cursor:
            continue
        selected.append(span)
        cursor = span[1]

    segments: list[dict[str, str]] = []
    cursor = 0
    for start, end, segment in selected:
        if start > cursor:
            segments.append({"literal": url[cursor:start]})
        segments.append(segment)
        cursor = end
    if cursor < len(url):
        segments.append({"literal": url[cursor:]})
    return origin, segments


def _takeover(_source_step: int | None, reason: str) -> dict[str, Any]:
    # source_step is trace provenance and must not survive into the executable
    # artifact (or the globally installed entry skill).
    return {"kind": "takeover", "reason": reason}


def _normalize_source_step(
    record: dict[str, Any],
    *,
    input_key: str = "",
) -> dict[str, Any]:
    action = record["action"]
    tier = record["tier"]
    if record.get("legacyRedacted"):
        return _takeover(
            None,
            "handoff" if tier == "handoff" else "secret",
        )
    # handoff = 一次性凭据：短信/邮件验证码、图形码、扫码确认。
    #
    # 把录到的那一个码存成默认值再自动填，**不是"有安全风险"，是必然失败**——
    # 一次性码的定义就是用过即废。站点会拒绝它，而工作流不知道，会继续在登录页
    # 上执行后面的步骤，产出一串对不上任何元素的失败。停下来交还用户，是这一步
    # 唯一可能正确的语义。
    #
    # secret（密码）不走这条：密码可复用，自动填是有效的，其代价由安装前的
    # 知情披露与 owner 私有存储承担（见 _approval_scope）。
    if tier == "handoff" and action == "input":
        return _takeover(None, "handoff")
    # Recorder v3 persists the real human trigger (submitter click or Enter)
    # and intentionally omits submit DOM events. Ignore legacy duplicate submit
    # rows rather than adding a second submission.
    if action == "submit":
        return {"kind": "noop"}
    if action == "navigate":
        destination = _safe_navigation_url(
            record["url"],
            record["url"],
        )
        if not destination:
            raise WorkflowRejected("navigation_url_invalid")
        return {"kind": "navigate", "url": destination}
    if action == "dialog":
        return {
            "kind": "dialog",
            "type": str(record.get("dialogType") or ""),
            "accept": record.get("dialogAction") == "accept",
            "text": str(record.get("dialogText") or ""),
        }
    if action in {"click", "dblclick"}:
        selector = str(record.get("selector") or "")
        if not selector:
            raise WorkflowRejected(f"{action}_selector_missing")
        button = str(record.get("clickButton") or "left")
        click_count = int(record.get("clickCount") or (2 if action == "dblclick" else 1))
        modifiers = list(record.get("modifiers") or [])
        if (
            button == "left"
            and click_count == (2 if action == "dblclick" else 1)
            and not modifiers
            and record.get("position") is None
        ):
            return {"kind": action, "selector": selector}
        return {
            "kind": action,
            "selector": selector,
            "button": button,
            "click_count": click_count,
            "modifiers": modifiers,
            **(
                {"position": dict(record["position"])}
                if isinstance(record.get("position"), dict)
                else {}
            ),
        }
    if action == "drag":
        source_selector = str(record.get("selector") or "")
        target_selector = str(record.get("targetSelector") or "")
        if not source_selector or not target_selector:
            raise WorkflowRejected("drag_target_selector_missing")
        return {
            "kind": "drag",
            "source_selector": source_selector,
            "target_selector": target_selector,
        }
    if action == "upload":
        selector = str(record.get("selector") or "")
        if not selector:
            raise WorkflowRejected("upload_selector_missing")
        common = {
            "kind": "upload",
            "selector": selector,
            "multiple": bool(record.get("multiple")),
            "accept": str(record.get("accept") or ""),
        }
        if record.get("uploadMode") == "clear":
            return {**common, "files": []}
        if record.get("uploadMode") not in {"paths", "handoff"}:
            raise WorkflowRejected("upload_mode_invalid")
        if not input_key:
            raise WorkflowRejected("upload_parameter_missing")
        return {**common, "input_key": input_key}
    if action == "key":
        key = str(record.get("key") or "")
        if not key:
            raise WorkflowRejected("key_value_missing")
        # Playwright names the modifier "Control"; recorder uses the familiar
        # UI abbreviation "Ctrl".
        if key.startswith("Ctrl+"):
            key = "Control+" + key.removeprefix("Ctrl+")
        return {
            "kind": "press",
            "selector": str(record.get("selector") or ""),
            "key": key,
        }
    if action == "input":
        selector = str(record.get("selector") or "")
        if not selector:
            raise WorkflowRejected("input_selector_missing")
        target = record.get("target") or {}
        if not isinstance(target, dict) or not target:
            raise WorkflowRejected("input_target_missing")
        input_type = (
            str(target.get("inputType") or "").lower()
            if isinstance(target, dict)
            else ""
        )
        tag = (
            str(target.get("tag") or "").lower()
            if isinstance(target, dict)
            else ""
        )
        if input_type in {"checkbox", "radio"}:
            state = record.get("value")
            if state not in {"checked", "unchecked"}:
                raise WorkflowRejected("toggle_state_invalid")
            return {
                "kind": "form_field",
                "field": {
                    "type": input_type,
                    "selector": selector,
                    "value": state == "checked",
                },
            }
        if not input_key:
            raise WorkflowRejected("input_parameter_missing")
        if input_type == "range":
            field = {
                "type": "slider",
                "selector": selector,
                "input_key": input_key,
            }
            input_kind = "text"
        elif tag == "select" and input_type == "select-multiple":
            # Playwright Recorder persists selectedOptions as an array and
            # Locator.selectOption accepts that array natively. Keep this as a
            # first-class select step instead of forcing it through the
            # single-value fill_form combobox contract.
            return {
                "kind": "select",
                "selector": selector,
                "input_key": input_key,
                "input_kind": "select",
            }
        elif tag == "select" or input_type == "select-one":
            field = {
                "type": "combobox",
                "selector": selector,
                "input_key": input_key,
                "select_by": "value",
            }
            input_kind = "select"
        else:
            # Includes textarea, ordinary input and contenteditable editing
            # hosts. Playwright's typed fill_form implementation performs the
            # actual control/actionability check immediately before dispatch.
            field = {
                "type": "textbox",
                "selector": selector,
                "input_key": input_key,
            }
            input_kind = "text"
        return {
            "kind": "form_field",
            "field": field,
            "input_kind": input_kind,
        }
    if action == "scroll":
        scroll_y = int(record["scrollY"])
        scroll_x = int(record["scrollX"])
        if scroll_x == scroll_y == 0:
            raise WorkflowRejected("zero_scroll_source_step")
        return {
            "kind": "scroll",
            "selector": str(record.get("selector") or ""),
            "delta_x": scroll_x,
            "delta_y": scroll_y,
        }
    # note 是理解证据，不是可执行行为；limit 已在 trace 读取时拒绝。
    raise WorkflowRejected("source_action_not_executable")


def _field_parameter(
    record: dict[str, Any],
    *,
    field_index: int,
    used_keys: set[str],
) -> tuple[str, str, str]:
    """Derive one readable, stable runtime parameter from recorded semantics."""

    target = record.get("target")
    if not isinstance(target, dict):
        target = {}

    def readable(raw: Any) -> str:
        if not isinstance(raw, str):
            return ""
        return " ".join(raw.split())

    aria_label = readable(target.get("ariaLabel"))
    text = readable(target.get("text"))
    name = readable(target.get("name"))
    element_id = readable(target.get("id"))
    display_name = aria_label or text or name or element_id or f"字段 {field_index}"

    def slugify(raw: str) -> str:
        normalized = unicodedata.normalize("NFKD", raw)
        ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
        ascii_text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", ascii_text)
        value = re.sub(r"[^A-Za-z0-9]+", "_", ascii_text).strip("_").lower()
        if not value:
            return ""
        if not value[0].isalpha():
            value = f"field_{value}"
        return value

    base = next(
        (
            candidate
            for candidate in (
                slugify(aria_label),
                slugify(name),
                slugify(element_id),
                slugify(text),
            )
            if candidate
        ),
        f"field_{field_index}",
    )
    # Workflow input identifiers are part of the durable replay wire contract
    # and must match ``[a-z][a-z0-9_]{0,63}``.  Human-facing labels are
    # intentionally unbounded, so preserve a readable prefix and bind the
    # elided suffix through a stable content hash instead of silently chopping
    # it off (which would make two long labels indistinguishable).
    if len(base) > 64:
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:12]
        prefix = base[: 64 - len(digest) - 1].rstrip("_")
        if not prefix:
            prefix = "field"
        base_key = f"{prefix}_{digest}"
        hashed_base = True
    else:
        base_key = base
        digest = ""
        hashed_base = False

    key = base_key
    suffix = 2
    while key in used_keys:
        trailer = f"_{suffix}"
        # A 64-character base and every hashed long base leave no spare room.
        # Trim only the readable prefix.  For long semantic names the stable
        # hash remains intact even after appending a collision ordinal.
        if hashed_base:
            prefix = base[
                : 64 - len(digest) - len(trailer) - 1
            ].rstrip("_")
            if not prefix:
                prefix = "field"
            key = f"{prefix}_{digest}{trailer}"
        else:
            key = f"{base_key[: 64 - len(trailer)].rstrip('_')}{trailer}"
        suffix += 1
    used_keys.add(key)

    tag = readable(target.get("tag")).lower()
    input_type = readable(target.get("inputType")).lower()
    hint_parts = [part for part in (tag, input_type) if part]
    if name:
        hint_parts.append(f"name={name}")
    elif element_id:
        hint_parts.append(f"id={element_id}")
    recorded_hint = " · ".join(hint_parts)
    return key, display_name, recorded_hint


def _coalesced_workflow_steps(
    workflow: dict[str, Any],
    trace: TraceSnapshot,
) -> list[dict[str, Any]]:
    """Remove recorder mechanics that are already represented by final form state.

    Older recorder versions commonly emitted ``click(control) -> input``,
    editing keys around the final input, and ``action -> did-navigate``.
    Replaying those rows causes duplicate toggles, fragmented form batches and
    double navigation.
    """

    steps = list(workflow["steps"])

    def record_at(index: int) -> dict[str, Any] | None:
        item = steps[index]
        source_step = item.get("source_step")
        if not isinstance(source_step, int):
            return None
        return trace.records.get(source_step)

    def trace_adjacent(
        left: dict[str, Any] | None,
        right: dict[str, Any] | None,
    ) -> bool:
        return bool(
            left is not None
            and right is not None
            and int(right["step"]) == int(left["step"]) + 1
        )

    drop: set[int] = set()
    causal_postconditions: dict[int, list[dict[str, Any]]] = {}

    navigation_trigger_actions = {
        "click",
        "dblclick",
        "drag",
        "upload",
        "input",
        "key",
        "scroll",
    }
    # Recorder v9+ action ids come from the recording group's shared ledger,
    # not a page-local counter.  One id must therefore identify exactly one
    # executable action across every opener, popup and OOPIF.
    causal_action_anchors: dict[int, int] = {}
    for signal_index in range(len(steps)):
        signal_record = record_at(signal_index)
        if signal_record is None:
            continue
        causal_id = int(signal_record.get("causalId") or 0)
        if (
            str(signal_record.get("action") or "")
            not in navigation_trigger_actions
            or causal_id <= 0
        ):
            continue
        if causal_id in causal_action_anchors:
            raise WorkflowRejected("trace_causal_action_ambiguous")
        causal_action_anchors[causal_id] = signal_index

    navigation_signals: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    # The timestamp migration is intentionally page-scoped.  Old recorder
    # rows have no causal id, but a popup or another foreground tab must never
    # steal the navigation merely because its action was globally most recent.
    last_legacy_action_by_page: dict[str, tuple[int, dict[str, Any]]] = {}
    first_non_navigation_step_by_page: dict[str, int] = {}
    for trace_record in trace.records.values():
        if trace_record.get("action") == "navigate":
            continue
        identity = str(trace_record.get("label") or "")
        source_step = int(trace_record.get("step") or 0)
        previous = first_non_navigation_step_by_page.get(identity)
        if previous is None or source_step < previous:
            first_non_navigation_step_by_page[identity] = source_step
    for signal_index in range(len(steps)):
        signal_record = record_at(signal_index)
        if signal_record is None:
            continue
        signal_action = str(signal_record.get("action") or "")
        page_label = str(signal_record.get("label") or "")
        if signal_action != "navigate":
            if signal_action in navigation_trigger_actions:
                last_legacy_action_by_page[page_label] = (
                    signal_index,
                    signal_record,
                )
            continue

        schema_version = int(signal_record.get("schemaVersion") or 0)
        causal_id = int(signal_record.get("causalId") or 0)
        if (
            schema_version >= 10
            and causal_id <= 0
            and int(signal_record.get("step") or 0)
            < first_non_navigation_step_by_page.get(page_label, math.inf)
            and str(signal_record.get("openerPage") or "")
            and signal_record.get("popupOrdinal") is not None
        ):
            # The popup page does not have an execution context when its
            # bootstrap navigation is captured.  Its immutable creation id is
            # the only exact action identity available.  Never reuse this id
            # after the popup has emitted a real local action.
            causal_id = int(signal_record.get("createdByCausalId") or 0)

        anchor_index = (
            causal_action_anchors.get(causal_id)
            if causal_id > 0
            else None
        )
        if schema_version >= 10 and causal_id > 0 and anchor_index is None:
            raise WorkflowRejected("trace_navigation_causal_owner_missing")

        if anchor_index is None and schema_version < 10:
            # Legacy-only compatibility with Playwright Recorder's historical
            # five-second signal window.  Crucially, the candidate is selected
            # from the same page identity rather than a global last_action.
            legacy_candidate = last_legacy_action_by_page.get(page_label)
            if legacy_candidate is not None:
                candidate_index, candidate_record = legacy_candidate
                delta = int(signal_record["timestamp"]) - int(
                    candidate_record["timestamp"]
                )
                if 0 <= delta <= _CAUSAL_NAVIGATION_WINDOW_MS:
                    anchor_index = candidate_index

        # A v10 navigation with no exact causal identity is an explicit browser
        # navigation (recording start, address bar, history, reload, or timer).
        # It must remain a standalone navigate step rather than being guessed.
        if anchor_index is None:
            continue
        drop.add(signal_index)
        navigation_signals.setdefault(anchor_index, []).append(
            (signal_index, signal_record)
        )

    for anchor_index, indexed_navigations in navigation_signals.items():
        current = record_at(anchor_index)
        if current is None:
            continue
        last_navigation_index = max(index for index, _record in indexed_navigations)
        next_record: dict[str, Any] | None = None
        for candidate_index in range(last_navigation_index + 1, len(steps)):
            candidate = record_at(candidate_index)
            if (
                candidate is not None
                and candidate.get("action") not in {"navigate", "submit"}
            ):
                next_record = candidate
                break
        next_label = str(next_record.get("label") or "") if next_record else ""
        source_label = str(current.get("label") or "")
        background_popup = (
            str(current.get("clickButton") or "") == "middle"
            or bool({"Control", "Meta"} & set(current.get("modifiers") or []))
        )
        # Redirect chains can emit several did-navigate rows for one page.
        # Keep only the final URL for each destination identity.
        by_destination: dict[tuple[str, str], dict[str, Any]] = {}
        for _signal_index, navigation in indexed_navigations:
            destination_label = str(navigation.get("label") or "")
            target = "same_tab" if destination_label == source_label else "popup"
            condition: dict[str, Any] = {
                "kind": "url",
                "target": target,
                "url": str(navigation.get("url") or ""),
                # Compile-time identity; normalized_postconditions converts it
                # to the stable recording-local pN alias.
                "_page_label": destination_label,
            }
            if target == "popup":
                opener_label = str(
                    navigation.get("openerPage") or source_label
                )
                popup_ordinal = navigation.get("popupOrdinal")
                if (
                    int(navigation.get("schemaVersion") or 0) >= 10
                    and (
                        not opener_label
                        or isinstance(popup_ordinal, bool)
                        or not isinstance(popup_ordinal, int)
                        or popup_ordinal < 1
                    )
                ):
                    raise WorkflowRejected("trace_popup_topology_missing")
                condition["activate"] = (
                    next_record is None
                    and not background_popup
                    or next_record is not None
                    and next_label == destination_label
                )
                condition["_opener_label"] = opener_label
                condition["_popup_ordinal"] = popup_ordinal
            by_destination[(target, destination_label)] = condition
        causal_postconditions[anchor_index] = list(by_destination.values())

    # Dialog decisions are action signals, not standalone mutations.  Recorder
    # schema v9 gives both the trusted DOM action and every modal opened in that
    # exact JavaScript task one host-assigned causalId.  Associate by that
    # identity (and the page identity) only: a "nearest action within five
    # seconds" guess miscompiles delayed timers and interleaved tabs.
    causal_dialogs: dict[int, list[dict[str, Any]]] = {}
    # causalId is allocated from the recording group's shared ledger, so it is
    # unique across every opener, popup and OOPIF. Page label is an observation
    # target, not part of the causal identity.
    for signal_index in range(len(steps)):
        signal_record = record_at(signal_index)
        if signal_record is None or signal_record.get("action") != "dialog":
            continue
        causal_id = int(
            signal_record.get("causalId")
            or signal_record.get("createdByCausalId")
            or 0
        )
        # causalId=0 is deliberate: initial/onload and timer-created dialogs do
        # not belong to a trusted user action. Keep them as wait_dialog
        # steps rather than guessing an owner.
        if causal_id <= 0:
            continue
        anchor_index = causal_action_anchors.get(causal_id)
        if anchor_index is None:
            raise WorkflowRejected("trace_dialog_causal_owner_missing")
        drop.add(signal_index)
        causal_dialogs.setdefault(anchor_index, []).append(
            {
                "type": str(signal_record.get("dialogType") or ""),
                "accept": signal_record.get("dialogAction") == "accept",
                "text": str(signal_record.get("dialogText") or ""),
                # Compile-only topology. The stable workflow page aliases are
                # allocated below after all selected records are known.
                "_page_label": str(signal_record.get("label") or ""),
                "_opener_label": str(
                    signal_record.get("openerPage")
                    or (record_at(anchor_index) or {}).get("label")
                    or ""
                ),
                "_popup_ordinal": signal_record.get("popupOrdinal"),
            }
        )

    for index, item in enumerate(steps):
        current = record_at(index)
        if current is None:
            continue

        action = str(current.get("action") or "")
        key = str(current.get("key") or "")

        if current.get("action") == "click" and index + 1 < len(steps):
            following_index = index + 1
            previous_record = current
            while following_index < len(steps):
                candidate = record_at(following_index)
                if (
                    candidate is None
                    or not trace_adjacent(previous_record, candidate)
                ):
                    break
                if (
                    following_index in drop
                    and candidate.get("action") in {"dialog", "navigate"}
                ):
                    previous_record = candidate
                    following_index += 1
                    continue
                break
            following = (
                record_at(following_index)
                if following_index < len(steps)
                else None
            )
            if (
                following is not None
                and trace_adjacent(previous_record, following)
                and following.get("action") == "upload"
                and current.get("label") == following.get("label")
                and current.get("selector")
                and following.get("selector")
                and (
                    following.get("uploadMode") != "clear"
                    or (
                        not causal_postconditions.get(index)
                        and not causal_dialogs.get(index)
                    )
                )
            ):
                # File selection is the durable action. Preserve the preceding
                # click as an optional chooser/reveal trigger on that upload
                # step, then remove the standalone click so replay cannot open
                # a second chooser. If the click merely reveals an input,
                # replay falls back to the exact input selector.
                drop.add(index)
                following_item = dict(steps[following_index])
                if following.get("uploadMode") != "clear":
                    following_item["_upload_trigger_selector"] = str(
                        current["selector"]
                    )
                steps[following_index] = following_item
                # Dialog/navigation rows between the physical click and the
                # durable file-selection event have already been causally
                # attached to that click. Replay executes the trigger click
                # inside upload_with_trigger, so arm and await those exact
                # signals on the upload transaction before removing the
                # standalone click.
                click_postconditions = causal_postconditions.pop(index, [])
                if click_postconditions:
                    causal_postconditions[following_index] = [
                        *click_postconditions,
                        *causal_postconditions.get(following_index, []),
                    ]
                click_dialogs = causal_dialogs.pop(index, [])
                if click_dialogs:
                    causal_dialogs[following_index] = [
                        *click_dialogs,
                        *causal_dialogs.get(following_index, []),
                    ]
            direct_following = record_at(index + 1)
            if (
                trace_adjacent(current, direct_following)
                and direct_following is not None
                and direct_following.get("action") == "input"
            ):
                current_selector = str(current.get("selector") or "")
                next_selector = str(direct_following.get("selector") or "")
                if (
                    current_selector
                    and not causal_postconditions.get(index)
                    and not causal_dialogs.get(index)
                    and (
                        str(current.get("label") or ""),
                        current_selector,
                    )
                    == (
                        str(direct_following.get("label") or ""),
                        next_selector,
                    )
                ):
                    drop.add(index)

        if action == "key" and key in _TEXT_EDITING_KEYS:
            selector = str(current.get("selector") or "")
            previous = record_at(index - 1) if index > 0 else None
            following = record_at(index + 1) if index + 1 < len(steps) else None
            adjacent_input = any(
                candidate is not None
                and candidate.get("action") == "input"
                and (
                    str(candidate.get("label") or ""),
                    str(candidate.get("selector") or ""),
                )
                == (
                    str(current.get("label") or ""),
                    selector,
                )
                and (
                    trace_adjacent(candidate, current)
                    or trace_adjacent(current, candidate)
                )
                for candidate in (previous, following)
            )
            if (
                selector
                and adjacent_input
                and not causal_postconditions.get(index)
                and not causal_dialogs.get(index)
            ):
                drop.add(index)

    filtered: list[dict[str, Any]] = []
    for index, item in enumerate(steps):
        if index in drop:
            continue
        enriched = dict(item)
        if index in causal_postconditions:
            enriched["_postconditions"] = causal_postconditions[index]
        if index in causal_dialogs:
            enriched["_dialogs"] = causal_dialogs[index]
        filtered.append(enriched)
    coalesced: list[dict[str, Any]] = []
    index = 0
    while index < len(filtered):
        item = filtered[index]
        source_step = item.get("source_step")
        record = (
            trace.records.get(source_step)
            if isinstance(source_step, int)
            else None
        )
        if (
            record is None
            or record.get("action") != "input"
            or not record.get("selector")
        ):
            coalesced.append(item)
            index += 1
            continue

        field_order: list[tuple[str, str]] = []
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        while index < len(filtered):
            candidate = filtered[index]
            candidate_step = candidate.get("source_step")
            candidate_record = (
                trace.records.get(candidate_step)
                if isinstance(candidate_step, int)
                else None
            )
            if (
                candidate_record is None
                or candidate_record.get("action") != "input"
                or not candidate_record.get("selector")
            ):
                break
            identity = (
                str(candidate_record.get("label") or ""),
                str(candidate_record["selector"]),
            )
            if identity not in latest:
                field_order.append(identity)
            latest[identity] = candidate
            index += 1
            if candidate.get("_postconditions") or candidate.get("_dialogs"):
                # A causal navigation or modal signal is a hard form-stage
                # boundary. Without this break, a later edit reusing the same
                # selector could overwrite the triggering field and silently
                # erase the signal transaction.
                break
        # One continuous form stage has no intermediate submit/read
        # dependency. Keep one final state per (page, selector), in first-seen
        # field order. A second tab may legitimately reuse the exact selector;
        # its value is an independent action and must never overwrite this one.
        coalesced.extend(latest[identity] for identity in field_order)

    return coalesced


def _v11_effect_ir(effect: RecordedEffect) -> dict[str, Any]:
    name = str(effect.signal.get("name") or "")
    if name == "navigation":
        return {
            "kind": "navigation",
            "page": effect.page_guid,
            "url": str(effect.signal["url"]),
        }
    if name == "popup":
        return {
            "kind": "popup",
            "page": str(effect.signal["popupPageGuid"]),
            "opener_page": str(effect.details["openerPageGuid"]),
            "popup_index": int(effect.details["popupIndex"]),
            "activate": bool(effect.details["activate"]),
            "disposition": str(effect.details["disposition"]),
        }
    if name == "download":
        return {
            "kind": "download",
            "page": effect.page_guid,
            "alias": str(effect.signal["downloadAlias"]),
            "ordinal": int(effect.details["ordinal"]),
            "suggested_filename": str(
                effect.details["suggestedFilename"]
            ),
        }
    if name == "dialog":
        return {
            "kind": "dialog",
            "page": effect.page_guid,
            "alias": str(effect.signal["dialogAlias"]),
            "type": str(effect.details["type"]),
            "accept": effect.details["action"] == "accept",
            "text": str(effect.details["promptText"]),
        }
    if name == "x-crew-pageClosed":
        return {
            "kind": "page_closed",
            "page": effect.page_guid,
            "reason": str(effect.signal["reason"]),
        }
    raise WorkflowRejected("trace_v11_signal_unsupported")


def _v11_group_page_references(group: RecordedActionGroup) -> set[str]:
    references: set[str] = set()
    action_name = str((group.action or {}).get("name") or "")
    if group.action is not None and action_name != "openPage":
        references.add(group.page_guid)
    for effect in group.effects:
        name = str(effect.signal.get("name") or "")
        if name == "popup":
            references.add(str(effect.details["openerPageGuid"]))
        else:
            references.add(effect.page_guid)
    return references


def _v11_live_page(
    plan: list[dict[str, Any]],
) -> str:
    live_order: list[str] = []
    active = ""

    def define(page: str, activate: bool) -> None:
        nonlocal active
        if page not in live_order:
            live_order.append(page)
        if activate:
            active = page

    def close(page: str) -> None:
        nonlocal active
        if page in live_order:
            live_order.remove(page)
        if active == page:
            active = live_order[-1] if live_order else ""

    for step in plan:
        kind = str(step.get("kind") or "")
        if kind == "open_page":
            define(str(step["page"]), bool(step["activate"]))
        elif kind == "wait_page":
            define(str(step["page"]), bool(step["activate"]))
        elif kind == "activate_page":
            active = str(step["page"])
        for effect in step.get("effects", []):
            if effect["kind"] == "popup":
                define(str(effect["page"]), bool(effect["activate"]))
            elif effect["kind"] == "page_closed":
                close(str(effect["page"]))
        if kind == "wait_page_closed":
            close(str(step["page"]))
    if active in live_order:
        return active
    return live_order[-1] if live_order else ""


def _compile_plan_v11(
    workflow: dict[str, Any],
    trace: TraceSnapshot,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if (
        trace.schema_version != 11
        or not trace.transactions
        or not recording_v11_phase_a_enabled()
    ):
        raise WorkflowRejected("trace_schema_v11_disabled")
    groups = {group.step: group for group in trace.transactions}
    if len(groups) != len(trace.transactions):
        raise WorkflowRejected("trace_transaction_step_ambiguous")

    page_definitions: dict[str, int] = {}
    for group in trace.transactions:
        action_name = str((group.action or {}).get("name") or "")
        if action_name == "openPage":
            if group.page_guid in page_definitions:
                raise WorkflowRejected("trace_page_identity_ambiguous")
            page_definitions[group.page_guid] = group.step
        for effect in group.effects:
            if effect.signal.get("name") != "popup":
                continue
            page = str(effect.signal["popupPageGuid"])
            if page in page_definitions:
                raise WorkflowRejected("trace_page_identity_ambiguous")
            page_definitions[page] = group.step

    plan: list[dict[str, Any]] = []
    inputs: dict[str, dict[str, Any]] = {}
    emitted: set[int] = set()
    visiting: set[int] = set()
    root_count = 0
    input_index = 0
    terminated = False

    def evidence_tier(group: RecordedActionGroup) -> str:
        evidence = group.evidence or {}
        return str(evidence.get("tier") or "plain")

    def input_metadata(
        group: RecordedActionGroup,
        *,
        kind: str,
        default: Any,
    ) -> str:
        nonlocal input_index
        input_index += 1
        key = f"field_{input_index}"
        evidence = group.evidence or {}
        target = evidence.get("target")
        if not isinstance(target, dict):
            target = {}
        display_name = next(
            (
                " ".join(str(candidate).split())
                for candidate in (
                    target.get("ariaLabel"),
                    target.get("name"),
                    target.get("id"),
                    evidence.get("hint"),
                )
                if isinstance(candidate, str) and candidate.strip()
            ),
            f"Field {input_index}",
        )
        spec: dict[str, Any] = {
            "kind": kind,
            "required": True,
            "display_name": display_name,
            "recorded_hint": str(evidence.get("hint") or ""),
            "default": default,
        }
        # 凭据标记必须在 v11 路径上也生效。v11 是默认 schema——只给 v10 打标记
        # 等于安装前的知情披露对绝大多数录制永远报"不含凭据原值"。
        if evidence.get("tier") == "secret":
            spec["credential"] = True
        inputs[key] = spec
        return key

    def action_step(group: RecordedActionGroup) -> dict[str, Any]:
        nonlocal root_count
        action = group.action
        if action is None:
            raise WorkflowRejected("trace_transaction_action_missing")
        name = str(action.get("name") or "")
        effects = [_v11_effect_ir(effect) for effect in group.effects]
        page = group.page_guid
        if name == "openPage":
            step = {
                "kind": "open_page",
                "page": page,
                "url": str(action["url"]),
                "mode": "reuse_current" if root_count == 0 else "new",
                "activate": True,
                **(
                    {"viewport": dict(action["viewport"])}
                    if isinstance(action.get("viewport"), dict)
                    else {}
                ),
                "effects": effects,
            }
            root_count += 1
            return step
        if name == "closePage":
            if not any(
                effect["kind"] == "page_closed"
                and effect["page"] == page
                for effect in effects
            ):
                raise WorkflowRejected("trace_close_page_effect_missing")
            return {
                "kind": "close_page",
                "page": page,
                "effects": effects,
            }
        if name == "navigate":
            return {
                "kind": "navigate",
                "page": page,
                "operation": "goto",
                "url": str(action["url"]),
                "effects": effects,
            }
        if name == "x-crew-navigate":
            return {
                "kind": "navigate",
                "page": page,
                "operation": str(action["operation"]),
                "url": str(action["url"]),
                "effects": effects,
            }
        if name == "x-crew-activatePage":
            return {
                "kind": "activate_page",
                "page": page,
                "effects": effects,
            }
        if name == "x-crew-resize":
            return {
                "kind": "resize",
                "page": page,
                "width": action["width"],
                "height": action["height"],
                "effects": effects,
            }
        if name == "hover":
            return {
                "kind": "hover",
                "page": page,
                "selector": str(action["selector"]),
                "position": action["position"],
                "effects": effects,
            }
        if name == "click":
            click_count = int(action["clickCount"])
            return {
                "kind": "dblclick" if click_count == 2 else "click",
                "page": page,
                "selector": str(action["selector"]),
                "button": str(action["button"]),
                "click_count": click_count,
                "modifiers": list(action["modifiers"]),
                "position": action["position"],
                "effects": effects,
            }
        if name == "fill":
            # 一次性凭据必须交还用户，v11 与 v10 同口径。
            #
            # v11 是默认 schema，早先这条路径完全不看 tier：录到的验证码会成为
            # 运行时默认值被自动填入，而站点必然拒绝一个用过的码——工作流不知道，
            # 会继续在登录页上跑完剩下的步骤。
            if str(evidence_tier(group)) == "handoff":
                if effects:
                    # 有副作用说明这一步不只是填值（填完就跳走）。交还控制权
                    # 无法复现那个副作用，宁可拒绝整份草稿也不要静默丢掉它。
                    raise WorkflowRejected("handoff_with_effects_unsupported")
                return {"kind": "takeover", "reason": "handoff"}
            input_key = input_metadata(
                group,
                kind="text",
                default=str(action["text"]),
            )
            return {
                "kind": "fill",
                "page": page,
                "selector": str(action["selector"]),
                "input_key": input_key,
                "effects": effects,
            }
        if name in {"check", "uncheck"}:
            return {
                "kind": "check",
                "page": page,
                "selector": str(action["selector"]),
                "checked": name == "check",
                "effects": effects,
            }
        if name == "select":
            input_key = input_metadata(
                group,
                kind="select",
                default=list(action["options"]),
            )
            return {
                "kind": "select",
                "page": page,
                "selector": str(action["selector"]),
                "input_key": input_key,
                "effects": effects,
            }
        if name == "press":
            key = "+".join([*action["modifiers"], str(action["key"])])
            return {
                "kind": "press",
                "page": page,
                "selector": str(action["selector"]),
                "key": key,
                "effects": effects,
            }
        if name == "setInputFiles":
            files = list(action["files"])
            upload: dict[str, Any] = {
                "kind": "upload",
                "page": page,
                "selector": str(action["selector"]),
                "effects": effects,
            }
            if files:
                upload["input_key"] = input_metadata(
                    group,
                    kind="files",
                    default=files,
                )
            else:
                upload["files"] = []
            return upload
        if name == "x-crew-drag":
            return {
                "kind": "drag",
                "page": page,
                "source_selector": str(action["sourceSelector"]),
                "target_selector": str(action["targetSelector"]),
                "source_position": action["sourcePosition"],
                "target_position": action["targetPosition"],
                "effects": effects,
            }
        if name == "x-crew-drop":
            files = list(action["files"])
            drop: dict[str, Any] = {
                "kind": "drop",
                "page": page,
                "selector": str(action["selector"]),
                "data": dict(action["data"]),
                "effects": effects,
            }
            if files:
                drop["input_key"] = input_metadata(
                    group,
                    kind="files",
                    default=files,
                )
            else:
                drop["files"] = []
            return drop
        if name == "x-crew-pointerGesture":
            telemetry_names = {
                "pressure": "pressure",
                "tangentialPressure": "tangential_pressure",
                "tiltX": "tilt_x",
                "tiltY": "tilt_y",
                "twist": "twist",
                "width": "width",
                "height": "height",
            }

            def pointer_sample(
                sample: dict[str, Any],
                *,
                elapsed: bool,
            ) -> dict[str, Any]:
                return {
                    "x": sample["x"],
                    "y": sample["y"],
                    **(
                        {"elapsed_ms": sample["elapsedMs"]}
                        if elapsed
                        else {}
                    ),
                    **{
                        target: sample[source]
                        for source, target in telemetry_names.items()
                        if source in sample
                    },
                }

            return {
                "kind": "pointer_gesture",
                "page": page,
                "selector": str(action["selector"]),
                "button": str(action["button"]),
                "modifiers": list(action["modifiers"]),
                "start": pointer_sample(action["start"], elapsed=False),
                "points": [
                    pointer_sample(point, elapsed=True)
                    for point in action["points"]
                ],
                **(
                    {"pointer_type": str(action["pointerType"])}
                    if "pointerType" in action
                    else {}
                ),
                "effects": effects,
            }
        if name == "x-crew-scroll":
            return {
                "kind": "scroll",
                "page": page,
                "selector": str(action["selector"]),
                "delta_x": int(action["deltaX"]),
                "delta_y": int(action["deltaY"]),
                "effects": effects,
            }
        raise WorkflowRejected("trace_v11_action_unsupported")

    def observation_step(group: RecordedActionGroup) -> dict[str, Any]:
        if not group.effects:
            raise WorkflowRejected("trace_observation_signal_missing")
        primary = _v11_effect_ir(group.effects[0])
        effects = [
            _v11_effect_ir(effect)
            for effect in group.effects[1:]
        ]
        kind = primary["kind"]
        if kind == "popup":
            return {
                "kind": "wait_page",
                "page": primary["page"],
                "opener_page": primary["opener_page"],
                "popup_index": primary["popup_index"],
                "activate": primary["activate"],
                "disposition": primary["disposition"],
                "effects": effects,
            }
        if kind == "navigation":
            return {
                "kind": "wait_navigation",
                "page": primary["page"],
                "url": primary["url"],
                "effects": effects,
            }
        if kind == "page_closed":
            if effects:
                raise WorkflowRejected("trace_observation_shape_invalid")
            return {
                "kind": "wait_page_closed",
                "page": primary["page"],
                "reason": primary["reason"],
                "effects": [],
            }
        if kind == "download":
            return {
                "kind": "wait_download",
                "page": primary["page"],
                "alias": primary["alias"],
                "ordinal": primary["ordinal"],
                "suggested_filename": primary["suggested_filename"],
                "effects": effects,
            }
        if kind == "dialog":
            return {
                "kind": "wait_dialog",
                "page": primary["page"],
                "alias": primary["alias"],
                "type": primary["type"],
                "accept": primary["accept"],
                "text": primary["text"],
                "effects": effects,
            }
        raise WorkflowRejected("trace_observation_signal_unsupported")

    def emit_group(step: int) -> None:
        if step in emitted:
            return
        if step in visiting:
            raise WorkflowRejected("trace_page_dependency_cycle")
        group = groups.get(step)
        if group is None:
            raise WorkflowRejected("source_step_not_found")
        visiting.add(step)
        for page in sorted(_v11_group_page_references(group)):
            definition = page_definitions.get(page)
            if definition is None:
                raise WorkflowRejected("trace_page_definition_missing")
            if definition > step:
                raise WorkflowRejected("trace_page_definition_order_invalid")
            if definition != step:
                emit_group(definition)
        visiting.remove(step)
        plan.append(
            action_step(group)
            if group.transaction_kind == "action"
            else observation_step(group)
        )
        emitted.add(step)

    for item in workflow["steps"]:
        if terminated:
            raise WorkflowRejected("steps_after_takeover_forbidden")
        source_step = item.get("source_step")
        if isinstance(source_step, int):
            emit_group(source_step)
            continue
        overlay_step = item.get("overlay_step")
        if isinstance(overlay_step, dict):
            record = trace.records.get(overlay_step["source_step"])
            if record is None:
                raise WorkflowRejected("source_step_not_found")
            overlay_selector = str(record.get("selector") or "")
            if not overlay_selector:
                raise WorkflowRejected("overlay_selector_missing")
            page = _v11_live_page(plan)
            if not page:
                raise WorkflowRejected("overlay_page_missing")
            plan.append(
                {
                    "kind": "handle_overlay",
                    "selector": overlay_selector,
                    "page": page,
                    "effects": [],
                }
            )
            continue
        assert_step = item.get("assert_step")
        if isinstance(assert_step, dict):
            record = trace.records.get(assert_step["source_step"])
            if record is None:
                raise WorkflowRejected("source_step_not_found")
            # selector 必须来自那一步真实记录的元素。取不到就拒绝整份草稿，
            # 而不是退化成一个没有目标的断言——那种断言永远通过，等于没有。
            selector = str(record.get("selector") or "")
            if not selector:
                raise WorkflowRejected("assert_selector_missing")
            page = _v11_live_page(plan)
            if not page:
                raise WorkflowRejected("assert_page_missing")
            plan.append(
                {
                    "kind": "assert_state",
                    "selector": selector,
                    "state": str(assert_step["state"]),
                    "page": page,
                    "effects": [],
                }
            )
            continue
        safe_step = item.get("safe_step")
        if safe_step == "takeover":
            # safe_step 的 takeover 是"到此交还"，是终止型。
            plan.append({"kind": "takeover", "reason": "explicit"})
            terminated = True
        elif safe_step == "snapshot_full":
            page = _v11_live_page(plan)
            if not page:
                raise WorkflowRejected("snapshot_page_missing")
            plan.append(
                {
                    "kind": "snapshot_full",
                    "page": page,
                    "effects": [],
                }
            )
        else:
            raise WorkflowRejected("safe_step_invalid")

    if not terminated:
        page = _v11_live_page(plan)
        if page and (
            not plan
            or plan[-1].get("kind") != "snapshot_full"
        ):
            plan.append(
                {
                    "kind": "snapshot_full",
                    "page": page,
                    "effects": [],
                }
            )
    return plan, inputs


def _compile_plan(
    workflow: dict[str, Any],
    trace: TraceSnapshot,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if trace.schema_version == 11:
        return _compile_plan_v11(workflow, trace)
    plan: list[dict[str, Any]] = []
    inputs: dict[str, dict[str, Any]] = {}
    batch: list[dict[str, Any]] = []
    batch_selectors: set[str] = set()
    batch_page = ""
    field_index = 0
    used_input_keys: set[str] = set()
    url_bindings: list[tuple[str, str]] = []
    terminated = False
    page_aliases: dict[str, str] = {}
    selected_page_labels = {
        str(record.get("label") or "")
        for item in workflow["steps"]
        for source_step in [item.get("source_step")]
        if isinstance(source_step, int)
        for record in [trace.records.get(source_step)]
        if record is not None
    }
    selected_page_labels.update(
        str(record.get("openerPage") or "")
        for item in workflow["steps"]
        for source_step in [item.get("source_step")]
        if isinstance(source_step, int)
        for record in [trace.records.get(source_step)]
        if record is not None and record.get("openerPage")
    )
    multi_page = len(selected_page_labels) > 1
    reserved_page_aliases = {
        label
        for label in selected_page_labels
        if re.fullmatch(r"p(?:0|[1-9]\d*)", label)
    }
    allocated_page_aliases: set[str] = set()
    next_page_alias = 0

    def page_alias(label: Any) -> str:
        nonlocal next_page_alias
        identity = str(label or "")
        alias = page_aliases.get(identity)
        if alias is None:
            if re.fullmatch(r"p(?:0|[1-9]\d*)", identity):
                alias = identity
            else:
                while (
                    f"p{next_page_alias}" in reserved_page_aliases
                    or f"p{next_page_alias}" in allocated_page_aliases
                ):
                    next_page_alias += 1
                alias = f"p{next_page_alias}"
                next_page_alias += 1
            page_aliases[identity] = alias
            allocated_page_aliases.add(alias)
        return alias

    def normalized_postconditions(
        raw_conditions: Any,
    ) -> list[dict[str, Any]]:
        if raw_conditions is None:
            return []
        if not isinstance(raw_conditions, list) or not raw_conditions:
            raise WorkflowRejected("causal_postconditions_invalid")
        clean: list[dict[str, Any]] = []
        for raw in raw_conditions:
            if not isinstance(raw, dict):
                raise WorkflowRejected("causal_postconditions_invalid")
            target = raw.get("target")
            expected_fields = (
                {"kind", "target", "url", "_page_label"}
                if target == "same_tab"
                else {
                    "kind",
                    "target",
                    "url",
                    "activate",
                    "_page_label",
                    "_opener_label",
                    "_popup_ordinal",
                }
            )
            if (
                set(raw) != expected_fields
                or raw.get("kind") != "url"
                or target not in {"same_tab", "popup"}
                or target == "popup"
                and type(raw.get("activate")) is not bool
            ):
                raise WorkflowRejected("causal_postconditions_invalid")
            destination = _safe_navigation_url(
                str(raw.get("url") or ""),
                str(raw.get("url") or ""),
            )
            if not destination:
                raise WorkflowRejected("causal_navigation_url_invalid")
            dynamic = _dynamic_url_pattern(destination, url_bindings)
            condition: dict[str, Any] = {"kind": "url", "target": target}
            if dynamic is None:
                condition["url"] = destination
            else:
                condition["origin"] = dynamic[0]
                condition["url_pattern"] = dynamic[1]
            if target == "popup":
                condition["activate"] = bool(raw["activate"])
                condition["page"] = page_alias(raw["_page_label"])
                popup_ordinal = raw["_popup_ordinal"]
                if popup_ordinal is not None:
                    opener_label = str(raw["_opener_label"] or "")
                    if (
                        not opener_label
                        or isinstance(popup_ordinal, bool)
                        or not isinstance(popup_ordinal, int)
                        or popup_ordinal < 1
                    ):
                        raise WorkflowRejected(
                            "causal_postconditions_invalid"
                        )
                    condition["opener_page"] = page_alias(opener_label)
                    condition["popup_ordinal"] = popup_ordinal
            clean.append(condition)
        return clean

    def normalized_dialogs(raw_dialogs: Any) -> list[dict[str, Any]]:
        if raw_dialogs is None:
            return []
        if not isinstance(raw_dialogs, list) or not raw_dialogs:
            raise WorkflowRejected("causal_dialogs_invalid")
        clean: list[dict[str, Any]] = []
        for raw in raw_dialogs:
            required = {
                "type",
                "accept",
                "text",
                "_page_label",
                "_opener_label",
                "_popup_ordinal",
            }
            if (
                not isinstance(raw, dict)
                or set(raw) != required
                or raw.get("type")
                not in {"alert", "confirm", "prompt", "beforeunload"}
                or type(raw.get("accept")) is not bool
                or not isinstance(raw.get("text"), str)
                or "\x00" in raw["text"]
                or raw.get("type") != "prompt"
                and raw["text"] != ""
                or raw.get("accept") is False
                and raw["text"] != ""
            ):
                raise WorkflowRejected("causal_dialogs_invalid")
            dialog = {
                "type": str(raw["type"]),
                "accept": bool(raw["accept"]),
                "text": raw["text"],
            }
            page_label = str(raw["_page_label"])
            opener_label = str(raw["_opener_label"])
            popup_ordinal = raw["_popup_ordinal"]
            if (
                popup_ordinal is not None
                and (
                    isinstance(popup_ordinal, bool)
                    or not isinstance(popup_ordinal, int)
                    or popup_ordinal < 0
                )
            ):
                raise WorkflowRejected("causal_dialogs_invalid")
            if page_label != opener_label or popup_ordinal is not None:
                # `page` is the replay routing key. `label` is diagnostics
                # only; it may be empty for legacy popup traces. We can prove
                # the opener from the causal action but cannot truthfully infer
                # popup_ordinal until the recorder persists explicit topology.
                dialog.update(
                    {
                        "page": page_alias(page_label),
                        "label": page_label,
                        "opener_page": page_alias(opener_label),
                    }
                )
                if popup_ordinal is not None:
                    dialog["popup_ordinal"] = popup_ordinal
            clean.append(dialog)
        return clean

    def flush_batch(
        postconditions: list[dict[str, Any]] | None = None,
        dialogs: list[dict[str, Any]] | None = None,
    ) -> None:
        nonlocal batch, batch_selectors, batch_page
        if batch:
            step: dict[str, Any] = {"kind": "fill_form", "fields": batch}
            if batch_page:
                step["page"] = batch_page
            if postconditions:
                step["postconditions"] = postconditions
            if dialogs:
                step["dialogs"] = dialogs
            plan.append(step)
            batch = []
            batch_selectors = set()
            batch_page = ""
        elif postconditions or dialogs:
            raise WorkflowRejected("causal_signal_without_action")

    for item in _coalesced_workflow_steps(workflow, trace):
        if terminated:
            raise WorkflowRejected("steps_after_takeover_forbidden")
        if "overlay_step" in item:
            flush_batch()
            spec = item["overlay_step"]
            record = trace.records.get(spec["source_step"])
            if record is None:
                raise WorkflowRejected("source_step_not_found")
            overlay_selector = str(record.get("selector") or "")
            if not overlay_selector:
                raise WorkflowRejected("overlay_selector_missing")
            plan.append(
                {"kind": "handle_overlay", "selector": overlay_selector}
            )
            continue
        if "assert_step" in item:
            # 断言必须先把在批的表单 flush 掉：它要判定的是"到这一步为止页面
            # 是什么样"，混在 fill_form 批次中间就变成了判定一个还没填完的表单。
            flush_batch()
            spec = item["assert_step"]
            record = trace.records.get(spec["source_step"])
            if record is None:
                raise WorkflowRejected("source_step_not_found")
            # selector 只能来自轨迹。取不到就拒绝整份草稿，而不是退化成一个
            # 没有目标的断言——那种断言永远通过，等于没有。
            assert_selector = str(record.get("selector") or "")
            if not assert_selector:
                raise WorkflowRejected("assert_selector_missing")
            plan.append(
                {
                    "kind": "assert_state",
                    "selector": assert_selector,
                    "state": str(spec["state"]),
                }
            )
            continue
        if "safe_step" in item:
            safe_step = item["safe_step"]
            normalized = (
                {"kind": "snapshot_full"}
                if safe_step == "snapshot_full"
                else _takeover(None, "explicit")
            )
        else:
            source_step = item["source_step"]
            record = trace.records.get(source_step)
            if record is None:
                raise WorkflowRejected("source_step_not_found")
            target = record.get("target") or {}
            if (
                record["action"] == "input"
                and isinstance(target, dict)
                and str(target.get("inputType") or "").lower() == "radio"
                and record.get("value") == "unchecked"
            ):
                # Selecting a different radio atomically clears the previous
                # one. Playwright cannot independently uncheck a radio, so the
                # blur/change event for the old option is recorder noise.
                continue
            input_key = ""
            display_name = ""
            recorded_hint = ""
            if (
                record["action"] in {"input", "upload"}
            ):
                input_type = str(target.get("inputType") or "").lower()
                needs_input = (
                    record["action"] == "upload"
                    and record.get("uploadMode") != "clear"
                    or record["action"] == "input"
                    and input_type not in {"checkbox", "radio"}
                    # 一次性凭据编译成 takeover，不产生运行时入参。
                    and record["tier"] != "handoff"
                )
                if needs_input and record.get("selector"):
                    field_index += 1
                    input_key, display_name, recorded_hint = _field_parameter(
                        record,
                        field_index=field_index,
                        used_keys=used_input_keys,
                    )
                    if record["action"] == "upload":
                        upload_bits = [
                            "file upload",
                            f"recorded_count={int(record.get('fileCount') or 0)}",
                            f"multiple={str(bool(record.get('multiple'))).lower()}",
                        ]
                        accept = str(record.get("accept") or "").strip()
                        if accept:
                            upload_bits.append(f"accept={accept}")
                        recorded_hint = " · ".join(upload_bits)
            normalized = _normalize_source_step(
                record,
                input_key=input_key,
            )
            if normalized["kind"] in {"form_field", "select"} and input_key:
                input_spec: dict[str, Any] = {
                    "kind": str(normalized["input_kind"]),
                    "required": True,
                    "display_name": display_name,
                    "recorded_hint": recorded_hint,
                }
                # 凭据标记来自录制期的分级判定，不是安装期猜字段名。
                #
                # 分级是在页面进程里对着真实元素算的（type=password、
                # autocomplete、name/placeholder 的词边界匹配），比事后看
                # input_key 长得像不像密码可靠得多。安装前的知情披露据此计数，
                # 所以这个标记必须落进 artifact，而不只是留在 trace 里。
                if record["tier"] == "secret":
                    input_spec["credential"] = True
                recorded_values = (
                    list(record.get("values") or [])
                    if normalized["kind"] == "select"
                    else [str(record.get("value") or "")]
                )
                if not record.get("valueTruncated"):
                    input_spec["default"] = (
                        recorded_values
                        if input_spec["kind"] == "select"
                        else recorded_values[0]
                    )
                inputs[input_key] = input_spec
                for recorded_value in recorded_values:
                    if recorded_value:
                        url_bindings.append((recorded_value, input_key))
                if normalized["kind"] == "select":
                    normalized.pop("input_kind", None)
            elif normalized["kind"] == "upload" and input_key:
                input_spec = {
                    "kind": "files",
                    "required": True,
                    "display_name": display_name,
                    "recorded_hint": recorded_hint,
                }
                if record.get("uploadMode") == "paths":
                    input_spec["default"] = list(record.get("paths") or [])
                inputs[input_key] = input_spec
            if normalized["kind"] == "upload":
                trigger_selector = item.get("_upload_trigger_selector")
                if isinstance(trigger_selector, str) and trigger_selector:
                    normalized["trigger_selector"] = trigger_selector
            if multi_page and normalized["kind"] not in {"noop", "takeover"}:
                normalized["page"] = page_alias(record.get("label"))
                if normalized["kind"] == "dialog":
                    normalized["label"] = str(record.get("label") or "")
                    opener_page = str(record.get("openerPage") or "")
                    if opener_page:
                        normalized["opener_page"] = page_alias(opener_page)
                    popup_ordinal = record.get("popupOrdinal")
                    if isinstance(popup_ordinal, int) and not isinstance(
                        popup_ordinal,
                        bool,
                    ):
                        normalized["popup_ordinal"] = popup_ordinal

        postconditions = normalized_postconditions(item.get("_postconditions"))
        dialogs = normalized_dialogs(item.get("_dialogs"))
        if normalized["kind"] == "noop":
            if postconditions or dialogs:
                raise WorkflowRejected("causal_signal_without_action")
            continue
        if normalized["kind"] == "form_field":
            field = dict(normalized["field"])
            selector = str(field["selector"])
            field_page = str(normalized.get("page") or "")
            if (
                selector in batch_selectors
                or batch
                and batch_page != field_page
            ):
                flush_batch()
            if not batch:
                batch_page = field_page
            batch.append(field)
            batch_selectors.add(selector)
            if postconditions or dialogs:
                # A change handler on this field navigated.  The batch ends at
                # exactly this field so replay does not try to resolve controls
                # from the destination page before satisfying the transition.
                flush_batch(postconditions, dialogs)
            continue

        flush_batch()
        if postconditions:
            normalized["postconditions"] = postconditions
        if dialogs:
            normalized["dialogs"] = dialogs
        if normalized["kind"] == "takeover":
            plan.append(normalized)
            # 挂起型（handoff/secret）后面可以继续有步骤：用户填完验证码之后
            # 工作流要接着读工单。只有终止型才封计划。
            terminated = normalized.get("reason") not in _SUSPENDING_REASONS
        else:
            plan.append(normalized)

    flush_batch()
    if not terminated and (not plan or plan[-1]["kind"] != "snapshot_full"):
        plan.append({"kind": "snapshot_full"})
    return plan, inputs


def _diagnostic_hosts(trace: TraceSnapshot) -> tuple[str, ...]:
    """Return normalized hosts observed anywhere in the source recording.

    The persisted ``hosts`` field is retained for diagnostics and artifact
    compatibility only. It is deliberately not consulted while compiling or
    validating executable URLs, redirects or popup postconditions.
    """
    return trace.hosts


def _approval_scope(payload: dict[str, Any]) -> str:
    """生成只含结构范围的知情审批摘要，不暴露 URL path 或 trace 文本。"""
    hosts = payload.get("hosts")
    plan = payload.get("plan")
    if not isinstance(hosts, list) or not isinstance(plan, list):
        raise WorkflowRejected("draft_approval_scope_invalid")
    host_values = [str(host) for host in hosts]
    normalized_hosts = [_normalize_host(host) for host in host_values]
    if (
        any(not host for host in normalized_hosts)
        or host_values != sorted(set(normalized_hosts))
    ):
        raise WorkflowRejected("draft_approval_scope_invalid")

    action_order = (
        "open_page",
        "close_page",
        "navigate",
        "dialog",
        "activate_page",
        "resize",
        "hover",
        "click",
        "dblclick",
        "drag",
        "drop",
        "pointer_gesture",
        "press",
        "fill_form",
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
        "snapshot_full",
        "takeover",
    )
    counts = {
        action: sum(
            1
            for step in plan
            if isinstance(step, dict) and step.get("kind") == action
        )
        for action in action_order
    }
    if sum(counts.values()) != len(plan):
        raise WorkflowRejected("draft_approval_scope_invalid")
    action_summary = "、".join(
        f"{action}×{count}" for action, count in counts.items() if count
    )
    takeover = "是" if counts["takeover"] else "否"

    # 内含凭据的披露。
    #
    # 当前 recorder schema 会把密码这类可复用凭据的**原值**作为运行时入参的
    # 默认值写进 artifact，之后每次回放自动填。这在功能上是必要的（否则登录类
    # 工作流跑不动），但用户必须在安装前知道这件事——他授权的是"以后自动登录"，
    # 而不只是"重放一串点击"。
    #
    # 只报字段数与显示名，不报值：披露的目的是让用户判断要不要装，
    # 而不是把凭据再抄一份到审批文案里。
    inputs = payload.get("inputs")
    if not isinstance(inputs, dict):
        raise WorkflowRejected("draft_approval_scope_invalid")
    credential_labels = sorted(
        str(spec.get("display_name") or key)
        for key, spec in inputs.items()
        if isinstance(spec, dict)
        and spec.get("credential") is True
        and "default" in spec
    )
    credential_note = (
        f"；内含 {len(credential_labels)} 个凭据字段的录制原值"
        f"（{'、'.join(credential_labels)}），每次回放会自动填入"
        if credential_labels
        else "；不含凭据原值"
    )
    return (
        f"录制观察站点：{','.join(host_values) if host_values else '无'}；"
        f"固定动作：{action_summary}；共 {len(plan)} 步；包含人工接管：{takeover}"
        f"{credential_note}"
    )


def _render_skill(
    *,
    slug: str,
    workflow_id: str,
    capabilities: list[str],
) -> str:
    if (
        not capabilities
        or any(
            capability not in WORKFLOW_CAPABILITIES | WORKFLOW_V3_CAPABILITIES
            for capability in capabilities
        )
        or len(set(capabilities)) != len(capabilities)
    ):
        raise WorkflowRejected("workflow_capabilities_invalid")
    description = (
        f"运行本机已批准的 {slug} 浏览器录制工作流；"
        f"当用户明确要求执行 {slug} 时使用"
    )
    frontmatter = {
        "name": slug,
        "description": description,
        "metadata": {
            "zh_name": slug,
            "zh_description": description,
            "skillCategoryName": "通用办公",
            "version": "2.0.0",
            "generated_by": "crew.browser-record-replay",
            "workflow_id": workflow_id,
            "browser_policy": {
                "schema_version": "crew.browser.policy.v2",
                "readonly": False,
                "capabilities": list(capabilities),
            },
        },
    }
    body = "\n".join(
        [
        f"# 录制工作流：{slug}",
        "",
        "本技能不包含页面地址、目标、录制输入或执行计划。仅调用",
        f'`record_replay(workflow_id="{workflow_id}", inputs={{}})`；',
        "空 inputs 会使用录制时保存的精确默认值。仅当用户明确要求替换字段时，",
        "传入对应 override；若工具报告某字段没有默认值，再向用户询问。",
        ]
    )
    return (
        "---\n"
        # JSON 是 YAML 1.2 的子集；canonical JSON 避免 PyYAML 版本、换行和
        # quoting 风格变化影响 draft/preview digest。
        + _canonical_json(frontmatter)
        + "\n---\n\n"
        + body.rstrip()
        + "\n"
    )


def _draft_root(recording_dir: Path) -> Path:
    try:
        base = recording_dir.resolve(strict=True)
    except OSError as exc:
        raise WorkflowRejected("recording_directory_unavailable") from exc
    drafts = base / ".workflow-drafts"
    if drafts.exists() and drafts.is_symlink():
        raise WorkflowRejected("draft_directory_invalid")
    drafts.mkdir(mode=0o700, exist_ok=True)
    try:
        drafts.chmod(0o700)
        resolved = drafts.resolve(strict=True)
    except OSError as exc:
        raise WorkflowRejected("draft_directory_unavailable") from exc
    if resolved.parent != base:
        raise WorkflowRejected("draft_directory_escape")
    return resolved


def _write_immutable(path: Path, raw: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _safe_open_read(path)
        if existing != raw:
            raise WorkflowRejected("draft_id_collision")
        return
    try:
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)


def _draft_payload(
    *,
    owner: str,
    session_id: str,
    recording_id: str,
    slug: str,
    workflow: dict[str, Any],
    trace: TraceSnapshot,
) -> dict[str, Any]:
    request = {
        "recording_id": recording_id,
        "slug": slug,
        "workflow": workflow,
    }
    canonical_ir = _canonical_json(request)
    ir_digest = _sha256_text(canonical_ir)
    plan, inputs = _compile_plan(workflow, trace)
    diagnostic_hosts = _diagnostic_hosts(trace)
    artifact_schema = (
        WORKFLOW_STORE_SCHEMA_V3
        if trace.schema_version == 11
        else WORKFLOW_STORE_SCHEMA
    )
    try:
        artifact = build_workflow_artifact(
            owner=owner,
            hosts=diagnostic_hosts,
            inputs=inputs,
            plan=plan,
            schema_version=artifact_schema,
        )
    except WorkflowStoreError as exc:
        raise WorkflowRejected("workflow_artifact_invalid") from exc
    preview = _render_skill(
        slug=slug,
        workflow_id=artifact.workflow_id,
        capabilities=list(artifact.payload["capabilities"]),
    )
    return {
        "draft_schema_version": DRAFT_SCHEMA_VERSION,
        "workflow_schema_version": WORKFLOW_SCHEMA_VERSION,
        "owner_binding": _binding("owner", owner),
        "session_binding": _binding("session", session_id),
        "recording_id": recording_id,
        "slug": slug,
        "canonical_ir": canonical_ir,
        "ir_digest": ir_digest,
        "trace_digest": trace.digest,
        "hosts": list(diagnostic_hosts),
        "plan": plan,
        "inputs": inputs,
        "capabilities": list(artifact.payload["capabilities"]),
        "workflow_id": artifact.workflow_id,
        "workflow_digest": artifact.digest,
        "preview": preview,
        "preview_digest": _sha256_text(preview),
    }


def _load_json_object(raw: bytes) -> dict[str, Any]:
    try:
        value = _strict_json_loads(raw.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, TypeError, ValueError) as exc:
        raise WorkflowRejected("draft_json_invalid") from exc
    if not isinstance(value, dict):
        raise WorkflowRejected("draft_json_invalid")
    return value


_DRAFT_KEYS = {
    "draft_schema_version",
    "workflow_schema_version",
    "owner_binding",
    "session_binding",
    "recording_id",
    "slug",
    "canonical_ir",
    "ir_digest",
    "trace_digest",
    "hosts",
    "plan",
    "inputs",
    "capabilities",
    "workflow_id",
    "workflow_digest",
    "preview",
    "preview_digest",
}


class RecordWorkflowTools:
    def __init__(
        self,
        manager: Any,
        capability_check: Callable[[], str | None] | None = None,
    ) -> None:
        self._manager = manager
        self._capability_check = capability_check
        self._lock = threading.Lock()

    def _capability_denial(self) -> str | None:
        if self._capability_check is None:
            return None
        try:
            denied = self._capability_check()
        except Exception:  # noqa: BLE001 - capability uncertainty must fail closed
            return "BROWSER_CAPABILITY_DISABLED: 浏览器能力状态无法确认"
        return str(denied) if denied else None

    def compile_permission_resolver(
        self, _args: dict[str, Any]
    ) -> ToolPermissionDecision | None:
        denied = self._capability_denial()
        if denied:
            return ToolPermissionDecision(
                "deny",
                denied,
                allow_always=False,
            )
        return None

    def _recording_paths(
        self, owner: str, session_id: str, recording_id: str
    ) -> tuple[Path, Path]:
        try:
            directory = self._manager.recording_dir(owner, session_id, recording_id)
            session_root = self._manager.recording_dir(owner, session_id)
            if directory.is_symlink() or session_root.is_symlink():
                raise WorkflowRejected("recording_directory_symlink")
            resolved_root = session_root.resolve(strict=True)
            resolved_directory = directory.resolve(strict=True)
        except (OSError, ValueError) as exc:
            raise WorkflowRejected("recording_not_found") from exc
        if resolved_directory.parent != resolved_root:
            raise WorkflowRejected("recording_directory_escape")
        return resolved_directory, resolved_directory / "trace.jsonl"

    def _read_validated_draft(
        self,
        owner: str,
        session_id: str,
        recording_id: str,
        draft_id: str,
        expected_digest: str,
    ) -> ValidatedDraft:
        directory, trace_path = self._recording_paths(owner, session_id, recording_id)
        drafts = _draft_root(directory)
        path = drafts / f"{draft_id}.json"
        try:
            raw = _safe_open_read(path)
        except (OSError, WorkflowRejected) as exc:
            raise WorkflowRejected("draft_not_found") from exc
        draft_digest = _sha256_bytes(raw)
        if not secrets.compare_digest(draft_digest, expected_digest):
            raise WorkflowRejected("draft_digest_mismatch")
        payload = _load_json_object(raw)
        if set(payload) != _DRAFT_KEYS:
            raise WorkflowRejected("draft_shape_invalid")
        if (
            payload.get("draft_schema_version") != DRAFT_SCHEMA_VERSION
            or payload.get("workflow_schema_version") != WORKFLOW_SCHEMA_VERSION
            or payload.get("owner_binding") != _binding("owner", owner)
            or payload.get("session_binding") != _binding("session", session_id)
            or payload.get("recording_id") != recording_id
        ):
            raise WorkflowRejected("draft_binding_invalid")
        if _sha256_text(_canonical_json(payload))[:24] != draft_id:
            raise WorkflowRejected("draft_id_mismatch")

        canonical_ir = payload.get("canonical_ir")
        if not isinstance(canonical_ir, str) or _sha256_text(canonical_ir) != payload.get("ir_digest"):
            raise WorkflowRejected("draft_ir_digest_mismatch")
        request = _load_json_object(canonical_ir.encode("utf-8"))
        if _canonical_json(request) != canonical_ir:
            raise WorkflowRejected("draft_ir_not_canonical")
        request_recording, request_slug, workflow = _validate_compile_args(request)
        if request_recording != recording_id or request_slug != payload.get("slug"):
            raise WorkflowRejected("draft_request_mismatch")

        trace = _read_trace(trace_path, recording_id)
        if not secrets.compare_digest(trace.digest, str(payload.get("trace_digest") or "")):
            raise WorkflowRejected("trace_digest_changed")
        diagnostic_hosts = _diagnostic_hosts(trace)
        if list(diagnostic_hosts) != payload.get("hosts"):
            raise WorkflowRejected("draft_hosts_mismatch")
        plan, inputs = _compile_plan(workflow, trace)
        if plan != payload.get("plan") or inputs != payload.get("inputs"):
            raise WorkflowRejected("draft_plan_mismatch")
        try:
            artifact = build_workflow_artifact(
                owner=owner,
                hosts=diagnostic_hosts,
                inputs=inputs,
                plan=plan,
                schema_version=(
                    WORKFLOW_STORE_SCHEMA_V3
                    if trace.schema_version == 11
                    else WORKFLOW_STORE_SCHEMA
                ),
            )
        except WorkflowStoreError as exc:
            raise WorkflowRejected("draft_workflow_invalid") from exc
        if (
            artifact.workflow_id != payload.get("workflow_id")
            or artifact.digest != payload.get("workflow_digest")
            or artifact.payload["capabilities"] != payload.get("capabilities")
        ):
            raise WorkflowRejected("draft_workflow_digest_mismatch")
        preview = _render_skill(
            slug=request_slug,
            workflow_id=artifact.workflow_id,
            capabilities=list(artifact.payload["capabilities"]),
        )
        if (
            preview != payload.get("preview")
            or _sha256_text(preview) != payload.get("preview_digest")
        ):
            raise WorkflowRejected("draft_preview_mismatch")
        return ValidatedDraft(
            draft_id,
            draft_digest,
            path,
            payload,
            trace,
            artifact,
        )

    async def compile_handler(self, args: dict[str, Any]) -> str:
        if self._capability_denial():
            return "DRAFT_REJECTED: browser_capability_disabled"
        owner, session_id, _tool_call_id = _context()
        if not owner or not session_id:
            return "DRAFT_FAILED: missing_session_context"
        try:
            recording_id, slug, workflow = _validate_compile_args(args)
            directory, trace_path = self._recording_paths(owner, session_id, recording_id)
            trace = _read_trace(trace_path, recording_id)
            payload = _draft_payload(
                owner=owner,
                session_id=session_id,
                recording_id=recording_id,
                slug=slug,
                workflow=workflow,
                trace=trace,
            )
            canonical_payload = _canonical_json(payload)
            draft_id = _sha256_text(canonical_payload)[:24]
            raw = (canonical_payload + "\n").encode("utf-8")
            path = _draft_root(directory) / f"{draft_id}.json"
            if self._capability_denial():
                raise WorkflowRejected("browser_capability_disabled")
            with self._lock:
                _write_immutable(path, raw)
            draft_digest = _sha256_bytes(raw)
        except WorkflowRejected as exc:
            return f"DRAFT_REJECTED: {exc}"
        except OSError:
            return "DRAFT_FAILED: draft_storage_unavailable"

        # 不返回 preview、host、URL、selector、value、页面正文或任何 trace 片段。
        result = {
            "draft_id": draft_id,
            "draft_digest": draft_digest,
            "slug": slug,
            "workflow_schema": WORKFLOW_SCHEMA_VERSION,
            "step_count": len(payload["plan"]),
            "requires_install_approval": False,
        }
        return "DRAFT_OK: " + _canonical_json(result)

    def install_permission_resolver(
        self, args: dict[str, Any]
    ) -> ToolPermissionDecision | None:
        denied = self._capability_denial()
        if denied:
            return ToolPermissionDecision("deny", denied, allow_always=False)
        owner, session_id, _tool_call_id = _context()
        if not owner or not session_id:
            return ToolPermissionDecision(
                "deny",
                "安装缺少当前账号或会话",
                allow_always=False,
            )
        try:
            recording_id, draft_id, draft_digest = _validate_install_args(args)
            draft = self._read_validated_draft(
                owner, session_id, recording_id, draft_id, draft_digest
            )
        except (WorkflowRejected, OSError):
            return ToolPermissionDecision(
                "deny",
                "技能草稿不存在、已变化或不属于当前会话",
                allow_always=False,
            )
        denied = self._capability_denial()
        if denied:
            return ToolPermissionDecision("deny", denied, allow_always=False)
        # 安装不弹确认。
        #
        # 用户已经做了两次显式动作才走到这里：他自己按下录制按钮，又自己点了
        # 「生成技能」。再插一道确认弹窗只是把同一个意图问第三遍，而每一次多问
        # 都是一次流程中断。
        #
        # 范围摘要仍然生成——它作为安装结果的一部分回给模型，让模型在对话里
        # 用平白语言告诉用户装了什么（走过哪些站点、几步、是否内含凭据原值）。
        # 知情靠**说清楚**，不靠**拦一下**。
        del draft
        return None

    def install_permission_approver(self, token: str, args: dict[str, Any]) -> bool:
        # Kept for source compatibility with older registries. The current
        # resolver never returns "ask", so this callback is not used.
        return self.install_permission_resolver(args) is None

    async def install_handler(self, args: dict[str, Any]) -> str:
        owner, session_id, _tool_call_id = _context()
        denied = self._capability_denial()
        if denied:
            return "INSTALL_REJECTED: browser_capability_disabled"
        try:
            recording_id, draft_id, draft_digest = _validate_install_args(args)
        except (TypeError, ValueError, WorkflowRejected):
            return "INSTALL_REJECTED: install_fields_invalid"

        try:
            draft = self._read_validated_draft(
                owner, session_id, recording_id, draft_id, draft_digest
            )
            if self._capability_denial():
                raise WorkflowRejected("browser_capability_disabled")
            content = str(draft.payload["preview"])
            slug = str(draft.payload["slug"])
        except (WorkflowRejected, OSError):
            return "INSTALL_REJECTED: draft_or_trace_changed_after_approval"

        staging = Path(tempfile.mkdtemp(prefix="crew-record-install-"))
        installed = False
        published = None
        try:
            staged = staging / slug
            staged.mkdir(mode=0o700)
            skill_md = staged / "SKILL.md"
            skill_md.write_text(content, encoding="utf-8")
            skill_md.chmod(0o600)
            problems = validate_generated_skill(staged, slug)
            if problems:
                return "INSTALL_REJECTED: deterministic_skill_validation_failed"
            if (get_user_skills_dir() / slug).exists():
                return "INSTALL_REJECTED: slug_already_installed"

            # 紧贴安装前再次验证两份证据，缩窄审批后的 TOCTOU 窗口。
            final_draft = self._read_validated_draft(
                owner, session_id, recording_id, draft_id, draft_digest
            )
            if (
                final_draft.payload["preview"] != content
                or not secrets.compare_digest(final_draft.trace.digest, draft.trace.digest)
                or not secrets.compare_digest(final_draft.draft_digest, draft.draft_digest)
                or self._capability_denial()
            ):
                return "INSTALL_REJECTED: evidence_changed_before_install"
            try:
                published = publish_workflow(owner, final_draft.workflow)
            except (OSError, WorkflowStoreError):
                return "INSTALL_REJECTED: private_workflow_publish_failed"
            installed = install_skill_from_dir(
                staged,
                slug=slug,
                operator_account_id=owner,
                source="browser-recorder",
            )
        except Exception:  # noqa: BLE001 - installation failures are fail-closed
            installed = False
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        if not installed:
            # 装技能失败时回滚刚发布的 owner 私有 artifact。
            #
            # 放在 finally 之后、按 `published is not None` 触发，是为了**同时覆盖
            # 两条失败路**：install_skill_from_dir 返回 False，以及它抛异常被上面
            # 的 except 捕获。放在 try 内部只能覆盖前者。
            #
            # 不回滚不会阻断重试（publish 是内容寻址、幂等的），但会在盘上留下一个
            # 没有入口技能、workflow_id 不可知的孤儿产物——一个惰性磁盘泄漏。
            # rollback_published_workflow 之前 export 了却零调用（又一处死代码），
            # 这里把它接回：只删本次事务创建的那个 inode（published.created 才删）。
            if published is not None:
                # rollback_published_workflow 在目标 inode 被并发替换/变成软链时抛
                # WorkflowStoreError（ValueError，不是 OSError）——语义是"这个 inode
                # 已不是本次发布的那个，别删"。正确响应是静默放弃回滚，而不是让异常
                # 逃出 install_handler、把一次干净的 INSTALL_FAILED 变成未捕获异常。
                # 回滚移到 try 外之后必须显式覆盖它（原先由外层 except Exception 兜底）。
                try:
                    rollback_published_workflow(published)
                except (OSError, WorkflowStoreError):
                    pass
            return "INSTALL_FAILED: governed_skill_install_failed"
        # 范围摘要随安装结果回给模型，让它在对话里用平白语言告诉用户装了什么。
        # 知情靠说清楚，不靠拦一下——所以这里不是审批文案，是汇报素材。
        try:
            scope = _approval_scope(final_draft.payload)
        except WorkflowRejected:
            scope = ""
        return "INSTALL_OK: " + _canonical_json(
            {
                "draft_id": draft_id,
                "slug": slug,
                "workflow_id": draft.workflow.workflow_id,
                **({"scope": scope} if scope else {}),
            }
        )


def register_record_compile_tool(
    ctx: Any,
    manager: Any,
    *,
    capability_check: Callable[[], str | None] | None = None,
) -> RecordWorkflowTools:
    from .replay_tool import register_record_replay_tool

    tools = RecordWorkflowTools(manager, capability_check)
    ctx.register_tool(
        name=COMPILE_TOOL_NAME,
        toolset="browser",
        schema=COMPILE_SCHEMA,
        handler=tools.compile_handler,
        check_fn=manager.available,
        is_async=True,
        display_name="生成录制技能草稿",
        should_defer=False,
        permission_resolver=tools.compile_permission_resolver,
    )
    ctx.register_tool(
        name=INSTALL_TOOL_NAME,
        toolset="browser",
        schema=INSTALL_SCHEMA,
        handler=tools.install_handler,
        check_fn=manager.available,
        is_async=True,
        display_name="安装录制技能",
        should_defer=False,
        permission_resolver=tools.install_permission_resolver,
        # Resolver never returns "ask"; retained only for registry/API
        # compatibility with clients that expect an approver field.
        permission_approver=tools.install_permission_approver,
    )
    register_record_replay_tool(
        ctx,
        manager,
        capability_check=capability_check,
    )
    return tools
