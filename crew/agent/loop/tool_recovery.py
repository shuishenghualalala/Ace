"""Project known tool outcomes into concise, model-only recovery guidance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from crew.agent.loop.tool_result_classification import FILE_MUTATING_TOOL_NAMES
from crew.tools.process_registry import current_shell_kind

_SEARCH_TOOLS = frozenset({"glob", "grep"})
_GREP_COUNTS = {"files_with_matches": "num_files", "content": "num_lines", "count": "num_matches"}
_READ_VERSIONS = {
    "stale_file": "文件版本已变化，先重新读取当前内容，再根据新版本重新计算修改。",
    "file_not_read": "修改缺少当前会话的文件观察，先读取目标，再根据实际内容准备修改。",
    "incomplete_read": "当前读取未覆盖所需内容，先按行窗补齐；覆盖整文件前需要读完整版本。",
    "patch_no_match": "旧文本没有匹配项，重新读取目标范围后按当前原文编写修改。",
    "patch_ambiguous": "旧文本匹配多处，重新读取相关范围后增加独特的上下文。",
    "patch_overlap": "多项修改的范围重叠，重新读取后合并为互不重叠的编辑。",
}
_REJECTED_APPROVALS = frozenset({"approval_rejected", "approval_rejected_turn_stopped"})
_UNAVAILABLE_APPROVALS = frozenset({"approval_expired", "approval_unavailable", "approval_lost", "approval_required"})
_PERMISSION_DENIALS = frozenset({
    "permission_denied", "policy_denied", "security_file_denied", "file_not_writable",
    "tool_not_authorized", "workspace_denied",
})
_UNAVAILABLE_SECURITY = frozenset({
    "sandbox_denied", "sandbox_unavailable", "security_boundary_failed", "security_boundary_failed_turn_stopped",
    "runtime_crashed", "runtime_protocol_mismatch", "network_unavailable",
})
_TOOL_FAILURE_CODES = frozenset({"tool_execution_failed", "tool_error"})
_DISPATCH_CODES = frozenset({"tool_not_found", "tool_not_in_scope", "tool_not_authorized", "tool_not_disclosed", "workspace_denied"})
_WRAPPED_ERROR = "工具执行失败:"
_PLAIN_NOTE = "<tool-recovery "
_LOOP_SUFFIX = re.compile(
    r"\n\n\[Tool loop (?:warning: (?:repeated_exact_failure_warning|same_tool_failure_warning|"
    r"idempotent_no_progress_warning)|hard stop: same_tool_failure_halt); count=[0-9]+; [^\r\n]*\]"
)


def terminal_recovery_probe() -> str:
    """Read the current location and directory using the actual terminal shell."""
    if current_shell_kind() == "powershell":
        return "(Get-Location).Path; Get-ChildItem -Force"
    return "pwd && ls -la"


def _json_object(content: str) -> dict[str, Any] | None:
    candidate = content.lstrip()
    if not candidate.startswith("{"):
        return None
    try:
        payload = json.loads(candidate)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _wrapped_error_object(content: str) -> dict[str, Any] | None:
    """Recognize the registry's error envelope plus a generated loop suffix."""
    if not content.startswith(_WRAPPED_ERROR):
        return None
    candidate = content.removeprefix(_WRAPPED_ERROR).lstrip()
    try:
        payload, end = json.JSONDecoder().raw_decode(candidate)
    except (ValueError, RecursionError):
        return None
    suffix = candidate[end:]
    if not isinstance(payload, dict) or (suffix.strip() and not _LOOP_SUFFIX.fullmatch(suffix)):
        return None
    return payload


def _code(raw: Any) -> str:
    return raw.strip().casefold() if isinstance(raw, str) else ""


def _payload_error_code(payload: Mapping[str, Any], *, wrapped: bool) -> str:
    if payload.get("success") is True or not (wrapped or payload.get("success") is False or payload.get("error")):
        return ""
    code = _code(payload.get("error_code")) or _code(payload.get("code"))
    # Dispatch authority comes from runtime metadata, never a tool's payload.
    return "" if code in _DISPATCH_CODES else code


def structured_tool_error_code(content: str) -> str:
    """Return a failure code only from a whole unsuccessful result or a known error envelope."""
    if not isinstance(content, str):
        return ""
    payload = _json_object(content)
    if payload is not None:
        return _payload_error_code(payload, wrapped=False)
    wrapped = _wrapped_error_object(content)
    return _payload_error_code(wrapped, wrapped=True) if wrapped is not None else ""


def _search_recovery(
    tool_name: str, payload: Mapping[str, Any], args: Mapping[str, Any], available_tool_names: frozenset[str],
) -> tuple[str, str] | None:
    if payload.get("success") is not True:
        return None
    if payload.get("scan_complete") is False:
        reasons = payload.get("partial_reasons")
        timed_out = isinstance(reasons, list) and "timeout" in reasons
        message = "本次搜索超时，扫描未完成。" if timed_out else "本次扫描未完成。"
        message += "已返回的命中仍可使用；无结果也不能证明目标不存在。"
        if tool_name in available_tool_names:
            field = "pattern" if tool_name == "glob" else "glob 文件筛选"
            message += f"如需完整结论，缩小已授权的 path 或调整 {field} 后重新搜索。"
        return ("search_timeout" if timed_out else "partial_scan", message)
    if payload.get("scan_complete") is not True:
        return None
    next_offset = payload.get("next_offset")
    if type(next_offset) is int and next_offset > 0:
        advice = "搜索已完成，当前只展示一页结果。"
        if tool_name in available_tool_names:
            advice += f"如需继续查看，保持搜索条件不变并传入 offset={next_offset}。"
        return ("search_page", advice)
    offset = payload.get("applied_offset", args.get("offset", 0))
    if payload.get("truncated") is True or type(offset) is not int or offset != 0 or payload.get("files"):
        return None
    mode = payload.get("output_mode")
    count_field = "count" if tool_name == "glob" else _GREP_COUNTS.get(mode) if isinstance(mode, str) else None
    count = payload.get(count_field) if count_field is not None else None
    if type(count) is int and count == 0:
        advice = "扫描已完成，指定范围与条件内没有命中。"
        if tool_name in available_tool_names:
            advice += "只有需要检查不同假设时才调整搜索条件；不要原样重复调用。"
        return ("no_matches", advice)
    return None


def _failure_recovery(
    tool_name: str, code: str, side_effect_state: str, available_tool_names: frozenset[str],
) -> tuple[str, str] | None:
    if code in _REJECTED_APPROVALS:
        return ("approval_rejected", "用户已拒绝安全审批。停止本轮工具调用，不得换工具、改变路径或再次发起相同操作。")
    if code in _UNAVAILABLE_SECURITY:
        return ("security_boundary_failed", "安全边界未建立或已拒绝操作。停止本轮工具调用，报告状态，不要换工具、换路径或扩大权限重试。")
    if code in _UNAVAILABLE_APPROVALS | _PERMISSION_DENIALS:
        return ("permission_denied", "当前操作没有有效授权。保留原权限边界，报告受阻原因或等待用户决定，不要通过其他工具绕过。")
    if code == "invalid_arguments":
        advice = "本次调用的参数校验未通过。"
        if tool_name in available_tool_names:
            advice += "对照本轮工具 schema 和校验错误逐项修正字段名、类型及必填值，再提交更正后的调用。"
        else:
            advice += "根据校验错误说明缺失信息，当前范围内无法修正后调用该工具。"
        return (code, advice)
    if code == "tool_not_disclosed" and "tool_search" in available_tool_names:
        selector = f"（query=`select:{tool_name}`）" if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool_name) else ""
        return (code, f"这个工具尚未在本轮披露。使用 tool_search{selector}查找目标的当前 schema，再按其字段调用。")
    if code in {"tool_not_found", "tool_not_in_scope", "tool_not_disclosed"}:
        return (code, "目标工具当前不可调用。仅使用本轮已经披露且获准的工具，不要猜测名称或参数。")
    if code in _READ_VERSIONS and tool_name in FILE_MUTATING_TOOL_NAMES:
        if "file_read" not in available_tool_names:
            return (code, "当前内容或文件版本需要重新核实。报告原因，等待获得可读内容后再重新准备修改。")
        advice = f"使用 file_read。{_READ_VERSIONS[code]}"
        if (
            code in {"stale_file", "file_not_read", "incomplete_read"}
            and tool_name in {"patch", "file_write"} and tool_name in available_tool_names
        ):
            advice += "修改时使用本次读取的 file_version 作为 expected_version。"
        return (code, advice)
    if code == "timeout":
        if tool_name in _SEARCH_TOOLS and tool_name in available_tool_names:
            return (code, "本次搜索超时，不能据此判断目标不存在。如需继续，缩小已授权的路径或筛选范围。")
        advice = "超时只说明调用未及时结束，不能据此判断执行成功或没有副作用。先检查现有结果和执行状态，避免原样重试。"
        return (code, advice)
    if code in {"cancelled", "aborted"}:
        return ("cancelled", "工具调用已取消。保留已观察到的结果，不要自行重启本次操作。")
    if tool_name == "terminal" and code in _TOOL_FAILURE_CODES | {"nonzero_exit", "unknown_exit"}:
        reason = "tool_execution_failed" if code in _TOOL_FAILURE_CODES else code
        if side_effect_state == "none" and tool_name in available_tool_names:
            return (
                reason,
                "先依据退出状态和输出排查实际原因。若需要确认工作目录，可在当前授权目录执行只读检查 "
                f"`{terminal_recovery_probe()}`。重试失败命令前，确认它是否已经产生修改。",
            )
        return (reason, "先核对退出状态、输出和可能已发生的修改，确认后再决定下一步；不要原样重试。")
    return None


def project_tool_recovery(
    tool_name: str,
    content: str,
    *,
    available_tool_names: frozenset[str],
    code: str = "",
    side_effect_state: str = "unknown",
    arguments: Mapping[str, Any] | None = None,
) -> str:
    """Add guidance to the provider view while preserving unknown results and existing fields."""
    if not isinstance(content, str):
        return content
    payload = _json_object(content)
    if payload is not None and "recovery" in payload:
        return content
    if payload is None and _PLAIN_NOTE in content:
        return content

    wrapped = _wrapped_error_object(content) if payload is None else None
    structured = payload if payload is not None else wrapped
    if wrapped is not None and "recovery" in wrapped:
        return content
    stable = _code(code)
    if structured is not None and (stable in _TOOL_FAILURE_CODES or not stable):
        stable = _payload_error_code(structured, wrapped=wrapped is not None) or stable
    if structured is not None and structured.get("side_effect_state") in ("unknown", "committed"):
        side_effect_state = "unknown"
    hint = _failure_recovery(tool_name, stable, side_effect_state, available_tool_names)
    if hint is None and tool_name in _SEARCH_TOOLS and payload is not None and not stable:
        hint = _search_recovery(tool_name, payload, arguments or {}, available_tool_names)
    if hint is None:
        return content
    reason, instruction = hint
    if payload is not None:
        return json.dumps({**payload, "recovery": {"reason": reason, "instruction": instruction}}, ensure_ascii=False)
    return f'{content}\n\n<tool-recovery reason="{reason}">{instruction}</tool-recovery>'
