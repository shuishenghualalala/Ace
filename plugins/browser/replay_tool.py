"""Execute owner-private recorded workflows through Playwright.

Replay is intentionally a normal functional tool: it does not ask for a
second approval and does not mint per-step permits.  The immutable artifact is
parsed once, every selector must resolve uniquely in the Browser Host, and the
Host's Playwright actionability/timeout semantics decide whether an action can
run.  Partial form completion is reported explicitly and never retried here.
"""

from __future__ import annotations

import json
import secrets
from typing import Any, Callable
from urllib.parse import quote, quote_plus

from crew.browser.driver import BrowserDriverError
from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_tool_call_id,
)
from crew.core.types import ToolPermissionDecision

from .workflow_store import (
    INPUT_KEY_RE,
    LEGACY_WORKFLOW_STORE_SCHEMA,
    WORKFLOW_STORE_SCHEMA,
    WORKFLOW_STORE_SCHEMA_V3,
    WORKFLOW_V3_CAPABILITIES,
    WORKFLOW_ID_RE,
    WorkflowArtifact,
    WorkflowStoreError,
    read_workflow,
    workflow_v3_phase_a_enabled,
)

REPLAY_TOOL_NAME = "record_replay"

REPLAY_SCHEMA: dict[str, Any] = {
    "name": REPLAY_TOOL_NAME,
    "description": (
        "运行已安装的 owner 私有 Playwright 录制工作流。"
        "按 workflow_id 加载固定动作计划；inputs 是按键提供的可选覆盖值，"
        "未覆盖字段使用录制时保存的精确默认值。"
        "仅当字段既无覆盖值也无录制默认值时返回所需键和类型。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "pattern": r"^[0-9a-f]{64}$",
            },
            "inputs": {
                "type": "object",
                "propertyNames": {
                    "pattern": r"^[a-z][a-z0-9_]{0,63}$",
                },
                "additionalProperties": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "array",
                            "items": {
                                "type": "string",
                            },
                        },
                    ]
                },
            },
            "resume_token": {
                "type": "string",
                "minLength": 16,
                "maxLength": 128,
                "description": (
                    "上一次返回 REPLAY_SUSPENDED 时给出的续跑凭证。"
                    "只在用户已完成手工步骤后使用；一次性有效。"
                ),
            },
        },
        "required": ["workflow_id", "inputs"],
        "additionalProperties": False,
    },
}


class ReplayRejected(ValueError):
    """Stable error category that never includes runtime input values."""


class ReplayInputsRequired(ReplayRejected):
    """One or more bindings lack a usable override or recorded default."""

    def __init__(self, reason: str, keys: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.keys = keys


def _canonical_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in encoded
    )


def _context() -> tuple[str, str, str]:
    return (
        str(current_owner_account_id.get() or ""),
        str(current_session_id.get() or ""),
        str(current_tool_call_id.get() or ""),
    )


def _validate_call_shape(args: Any) -> tuple[str, dict[str, Any], str]:
    if (
        not isinstance(args, dict)
        or not set(args) <= {"workflow_id", "inputs", "resume_token"}
        or not {"workflow_id", "inputs"} <= set(args)
        or not isinstance(args.get("workflow_id"), str)
        or WORKFLOW_ID_RE.fullmatch(args["workflow_id"]) is None
        or not isinstance(args.get("inputs"), dict)
    ):
        raise ReplayRejected("replay_fields_invalid")
    for key in args["inputs"]:
        if not isinstance(key, str) or INPUT_KEY_RE.fullmatch(key) is None:
            raise ReplayRejected("replay_input_key_invalid")
    resume_token = args.get("resume_token", "")
    if not isinstance(resume_token, str) or (
        resume_token and not 16 <= len(resume_token) <= 128
    ):
        raise ReplayRejected("replay_resume_token_invalid")
    return args["workflow_id"], dict(args["inputs"]), resume_token


def _input_requirements(
    artifact: WorkflowArtifact,
    keys: tuple[str, ...] | None = None,
) -> dict[str, dict[str, str]]:
    specs = artifact.payload["inputs"]
    selected = set(specs) if keys is None else set(keys)
    return {
        key: {
            "kind": str(specs[key]["kind"]),
            "display_name": str(specs[key].get("display_name") or key),
            "recorded_hint": str(specs[key].get("recorded_hint") or ""),
        }
        for key in sorted(specs)
        if key in selected
    }


def _validate_executable_capabilities(artifact: WorkflowArtifact) -> None:
    schema_version = artifact.payload.get("schema_version")
    plan = artifact.payload.get("plan")
    if not isinstance(plan, list) or not plan:
        raise ReplayRejected("replay_plan_invalid")
    kinds = {
        str(step.get("kind") or "")
        for step in plan
        if isinstance(step, dict)
    }
    if not kinds:
        raise ReplayRejected("replay_plan_invalid")
    if schema_version == LEGACY_WORKFLOW_STORE_SCHEMA:
        # Explicit v1 artifacts belong to the old read-only design. Preserve
        # observation/navigation compatibility, but never reinterpret them as
        # newly-authorized form or submit workflows.
        if not kinds <= {"navigate", "scroll", "snapshot_full", "takeover"}:
            raise ReplayRejected("legacy_readonly_action")
        return
    if schema_version == WORKFLOW_STORE_SCHEMA_V3:
        if not workflow_v3_phase_a_enabled():
            raise ReplayRejected("replay_schema_unsupported")
        capabilities = artifact.payload.get("capabilities")
        effect_capability = {
            "popup": "popup",
            "navigation": "navigation_effect",
            "download": "download",
            "dialog": "dialog",
            "page_closed": "page_closed",
        }
        expected = set(kinds)
        for step in plan:
            if not isinstance(step, dict):
                raise ReplayRejected("replay_plan_invalid")
            effects = step.get("effects", [])
            if not isinstance(effects, list):
                raise ReplayRejected("replay_plan_invalid")
            for effect in effects:
                if not isinstance(effect, dict):
                    raise ReplayRejected("replay_plan_invalid")
                capability = effect_capability.get(
                    str(effect.get("kind") or "")
                )
                if not capability:
                    raise ReplayRejected("replay_plan_invalid")
                expected.add(capability)
        if (
            not isinstance(capabilities, list)
            or len(capabilities) != len(set(capabilities))
            or set(capabilities) != expected
            or not expected <= WORKFLOW_V3_CAPABILITIES
        ):
            raise ReplayRejected("replay_capabilities_invalid")
        return
    if schema_version != WORKFLOW_STORE_SCHEMA:
        raise ReplayRejected("replay_schema_unsupported")
    capabilities = artifact.payload.get("capabilities")
    if not isinstance(capabilities, list) or set(capabilities) != kinds:
        raise ReplayRejected("replay_capabilities_invalid")


def _resolve_inputs(
    artifact: WorkflowArtifact,
    supplied: dict[str, Any],
) -> dict[str, str | list[str]]:
    specs = artifact.payload["inputs"]
    if set(supplied) - set(specs):
        raise ReplayRejected("replay_input_unknown")

    missing = tuple(
        key
        for key in sorted(specs)
        if key not in supplied and "default" not in specs[key]
    )
    if missing:
        raise ReplayInputsRequired("replay_inputs_missing", missing)

    schema_version = artifact.payload.get("schema_version")
    exact_v3_strings = schema_version == WORKFLOW_STORE_SCHEMA_V3
    clean: dict[str, str | list[str]] = {}
    for key in sorted(specs):
        kind = specs[key]["kind"]
        value = supplied[key] if key in supplied else specs[key]["default"]
        if kind == "text":
            # V3 保留录制的字节精确字符串（刻意契约，见 test_replay_v3 的
            # exact-strings 用例，text/select/files 三类都覆盖），override 与
            # default 一视同仁；只有 V2 legacy 做严格的 NUL/代理项校验。
            #
            # 已核实的线上事实（不要再写成"不经过会抛的序列化"）：出站命令经
            # Starlette WebSocket.send_json（ensure_ascii=False，不转义），到
            # uvicorn/websockets 用 .encode("utf-8") 编码文本帧。NUL 与控制符
            # 能过；**孤立代理项会在这一步抛 UnicodeEncodeError** → 被兜成
            # REPLAY_HALTED: internal_replay_failure。真实录制值来自 DOM、是良构
            # UTF-16，不含孤立代理项，所以正常流程碰不到；只有模型刻意构造孤立
            # 代理项覆盖值才会触发这个 HALT——低危、fail-closed、无安全影响，
            # 故此处不加校验、保持字节精确。
            if (
                not isinstance(value, str)
                or not exact_v3_strings
                and (
                    "\x00" in value
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
                )
            ):
                raise ReplayInputsRequired(
                    "replay_text_input_invalid",
                    (key,),
                )
            clean[key] = value
            continue
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = list(value)
        else:
            raise ReplayInputsRequired(
                "replay_file_input_invalid"
                if kind == "files"
                else "replay_select_input_invalid",
                (key,),
            )
        if (
            kind not in {"select", "files"}
            or kind == "files"
            and not values
            and not exact_v3_strings
            or any(
                not isinstance(item, str)
                or kind == "files"
                and not item
                or not exact_v3_strings
                and (
                    "\x00" in item
                    or any(0xD800 <= ord(char) <= 0xDFFF for char in item)
                )
                for item in values
            )
        ):
            raise ReplayInputsRequired(
                "replay_file_input_invalid"
                if kind == "files"
                else "replay_select_input_invalid",
                (key,),
            )
        clean[key] = values
    return clean


def _resolved_step(
    plan_step: dict[str, Any],
    inputs: dict[str, str | list[str]],
    *,
    schema_version: str = WORKFLOW_STORE_SCHEMA,
) -> dict[str, Any]:
    kind = plan_step["kind"]
    postconditions = plan_step.get("postconditions")
    replay_v3 = schema_version == WORKFLOW_STORE_SCHEMA_V3

    def with_postconditions(step: dict[str, Any]) -> dict[str, Any]:
        if "page" in plan_step:
            step["page"] = plan_step["page"]
        if "dialogs" in plan_step:
            step["dialogs"] = [
                dict(dialog) for dialog in plan_step["dialogs"]
            ]
        if replay_v3 and "effects" in plan_step:
            step["effects"] = [
                dict(effect) for effect in plan_step["effects"]
            ]
        if postconditions is not None:
            resolved_conditions: list[dict[str, Any]] = []
            for condition in postconditions:
                if "url_pattern" not in condition:
                    resolved_conditions.append(dict(condition))
                    continue
                resolved_condition = {
                    key: value
                    for key, value in condition.items()
                    if key != "url_pattern"
                }
                segments: list[dict[str, Any]] = []

                def literal(value: str) -> None:
                    if segments and set(segments[-1]) == {"literal"}:
                        segments[-1]["literal"] += value
                    else:
                        segments.append({"literal": value})

                for segment in condition["url_pattern"]:
                    if "literal" in segment:
                        literal(str(segment["literal"]))
                        continue
                    if "wildcard" in segment:
                        segments.append({"wildcard": segment["wildcard"]})
                        continue
                    value = inputs[segment["input_key"]]
                    if isinstance(value, list):
                        if len(value) != 1:
                            raise ReplayInputsRequired(
                                "replay_url_binding_requires_one_value",
                                (segment["input_key"],),
                            )
                        raw_value = value[0]
                    elif isinstance(value, str):
                        raw_value = value
                    else:
                        raise ReplayRejected("replay_input_binding_invalid")
                    encoding = segment["encoding"]
                    if encoding == "raw":
                        literal(raw_value)
                    elif encoding in {"percent", "path"}:
                        literal(quote(raw_value, safe=""))
                    elif encoding == "plus":
                        literal(quote_plus(raw_value, safe=""))
                    elif encoding == "query":
                        alternatives = list(
                            dict.fromkeys(
                                (
                                    quote_plus(raw_value, safe=""),
                                    quote(raw_value, safe=""),
                                )
                            )
                        )
                        if len(alternatives) == 1:
                            literal(alternatives[0])
                        else:
                            segments.append({"alternatives": alternatives})
                    else:
                        raise ReplayRejected("replay_url_binding_invalid")
                resolved_condition["url_pattern"] = segments
                resolved_conditions.append(resolved_condition)
            step["postconditions"] = resolved_conditions
        return step

    if kind == "fill":
        value = inputs[plan_step["input_key"]]
        if not isinstance(value, str):
            raise ReplayRejected("replay_input_binding_invalid")
        resolved_fill = {
            "kind": "fill",
            "selector": plan_step["selector"],
            "text": value,
        }
        return with_postconditions(resolved_fill)
    if kind == "select":
        value = inputs[plan_step["input_key"]]
        if not isinstance(value, list):
            raise ReplayRejected("replay_input_binding_invalid")
        return with_postconditions(
            {
                "kind": "select",
                "selector": plan_step["selector"],
                (
                    "options"
                    if replay_v3
                    else "values"
                ): list(value),
            }
        )
    if kind == "check":
        return with_postconditions(
            {
                "kind": "check",
                "selector": plan_step["selector"],
                "checked": bool(plan_step["checked"]),
            }
        )
    if kind == "upload":
        if replay_v3:
            if "input_key" in plan_step:
                value = inputs[plan_step["input_key"]]
                if not isinstance(value, list):
                    raise ReplayRejected("replay_input_binding_invalid")
                files = list(value)
            else:
                files = list(plan_step["files"])
            return with_postconditions(
                {
                    "kind": "upload",
                    "selector": plan_step["selector"],
                    "files": files,
                }
            )
        if "input_key" in plan_step:
            value = inputs[plan_step["input_key"]]
            if not isinstance(value, list):
                raise ReplayRejected("replay_input_binding_invalid")
            if not plan_step["multiple"] and len(value) > 1:
                raise ReplayInputsRequired(
                    "replay_upload_target_accepts_one_file",
                    (plan_step["input_key"],),
                )
            resolved = {
                "kind": "upload",
                "selector": plan_step["selector"],
                "paths": list(value),
                "multiple": bool(plan_step["multiple"]),
                "accept": str(plan_step["accept"]),
            }
            if "trigger_selector" in plan_step:
                resolved["trigger_selector"] = plan_step["trigger_selector"]
            return with_postconditions(resolved)
        return with_postconditions(
            {
                "kind": "upload",
                "selector": plan_step["selector"],
                "paths": [],
                "multiple": bool(plan_step["multiple"]),
                "accept": str(plan_step["accept"]),
            }
        )
    if kind == "drop":
        if "input_key" in plan_step:
            value = inputs[plan_step["input_key"]]
            if not isinstance(value, list):
                raise ReplayRejected("replay_input_binding_invalid")
            files = list(value)
        else:
            files = list(plan_step["files"])
        return with_postconditions(
            {
                "kind": "drop",
                "selector": plan_step["selector"],
                "files": files,
                "data": dict(plan_step["data"]),
            }
        )
    if kind != "fill_form":
        return with_postconditions(
            {
                key: value
                for key, value in plan_step.items()
                if key != "postconditions"
            }
        )

    fields: list[dict[str, Any]] = []
    for field in plan_step["fields"]:
        field_type = field["type"]
        if field_type in {"textbox", "slider"}:
            value = inputs[field["input_key"]]
            if not isinstance(value, str):
                raise ReplayRejected("replay_input_binding_invalid")
            fields.append(
                {
                    "type": field_type,
                    "selector": field["selector"],
                    "value": value,
                }
            )
        elif field_type == "combobox":
            value = inputs[field["input_key"]]
            if not isinstance(value, list) or len(value) != 1:
                raise ReplayInputsRequired(
                    "replay_combobox_requires_one_value",
                    (field["input_key"],),
                )
            fields.append(
                {
                    "type": "combobox",
                    "selector": field["selector"],
                    "value": value[0],
                    "select_by": field["select_by"],
                }
            )
        else:
            fields.append(
                {
                    "type": field_type,
                    "selector": field["selector"],
                    "value": bool(field["value"]),
                }
            )
    return with_postconditions({"kind": "fill_form", "fields": fields})


class RecordReplayTool:
    def __init__(
        self,
        manager: Any,
        capability_check: Callable[[], str | None] | None = None,
    ) -> None:
        self._manager = manager
        self._capability_check = capability_check

    def _capability_denial(self) -> str | None:
        if self._capability_check is None:
            return None
        try:
            denial = self._capability_check()
        except Exception:  # noqa: BLE001 - capability uncertainty is unavailable
            return "BROWSER_CAPABILITY_DISABLED: 浏览器能力状态无法确认"
        return str(denial) if denial else None

    def permission_resolver(
        self,
        _args: dict[str, Any],
    ) -> ToolPermissionDecision | None:
        denied = self._capability_denial()
        if denied:
            return ToolPermissionDecision("deny", denied, allow_always=False)
        # A published recording is already the user's reusable automation.
        # Replaying it is a normal browser operation, not another approval
        # ceremony.
        return None

    async def handler(self, args: dict[str, Any]) -> str:
        owner, session_id, tool_call_id = _context()
        if not owner or not session_id or not tool_call_id:
            return "REPLAY_REJECTED: missing_execution_context"
        if self._capability_denial():
            return "REPLAY_REJECTED: browser_capability_disabled"
        try:
            workflow_id, supplied, resume_token = _validate_call_shape(args)
            artifact = read_workflow(owner, workflow_id)
            _validate_executable_capabilities(artifact)
        except ReplayRejected as exc:
            return f"REPLAY_REJECTED: {exc}"
        except (OSError, WorkflowStoreError):
            return "REPLAY_REJECTED: workflow_unavailable"
        try:
            inputs = _resolve_inputs(artifact, supplied)
            resolved_plan = [
                _resolved_step(
                    step,
                    inputs,
                    schema_version=str(artifact.payload["schema_version"]),
                )
                for step in artifact.payload["plan"]
            ]
        except ReplayInputsRequired as exc:
            return "REPLAY_INPUTS_REQUIRED: " + _canonical_json(
                {"inputs": _input_requirements(artifact, exc.keys)}
            )
        except ReplayRejected as exc:
            return f"REPLAY_REJECTED: {exc}"

        generation = self._manager.capability_generation(owner)
        workdir = str(current_agent_workdir.get() or "")
        begun = False
        completed = False
        cleanup_ok = True
        executed_steps = 0
        final_observation = ""
        failure: str | None = None
        # 挂起后再次进入时，租约已经存在：不要 begin 一段新的回放，
        # 而是换回 AI 模式、重绑本次调用，从 next_step 接着跑。
        suspended: dict[str, Any] | None = None
        start_index = 0
        try:
            if resume_token:
                resumed = await self._manager.resume_replay(
                    owner,
                    session_id,
                    workflow_id=workflow_id,
                    workflow_digest=artifact.digest,
                    resume_token=resume_token,
                )
                nonce = str(resumed["replay_nonce"])
                start_index = int(resumed["next_step"])
                begun = True
            else:
                nonce = secrets.token_urlsafe(32)
                await self._manager.begin_replay(
                    owner,
                    session_id,
                    workflow_id=workflow_id,
                    workflow_digest=artifact.digest,
                    capability_generation=generation,
                    replay_nonce=nonce,
                    allowed_hosts=tuple(artifact.payload["hosts"]),
                    workdir=workdir,
                    schema_version=str(artifact.payload["schema_version"]),
                )
                begun = True
            for step_index in range(start_index, len(resolved_plan)):
                step = resolved_plan[step_index]
                final_observation = await self._manager.replay_step(
                    owner,
                    session_id,
                    workflow_id=workflow_id,
                    workflow_digest=artifact.digest,
                    replay_nonce=nonce,
                    step_index=step_index,
                    step=step,
                    workdir=workdir,
                )
                executed_steps += 1
                # 挂起点：租约要活下来等用户，所以这一轮**不能**走 end_replay。
                #
                # 状态从 manager 结构化地问，不从 replay_step 的返回串里解析——
                # 那个返回值是包裹过的不可信页面内容，把控制流建立在对它的解析上，
                # 页面只要伪造一段同形状的 JSON 就能操纵回放。
                pending = self._manager.suspended_replay(owner, session_id)
                if pending:
                    suspended = {
                        "resume_token": str(pending["resume_token"]),
                        "next_step": int(pending["next_step"]),
                        "remaining_steps": (
                            len(resolved_plan) - int(pending["next_step"])
                        ),
                    }
                    break
            completed = suspended is None
        except BrowserDriverError as exc:
            completed_count = int(getattr(exc, "completed_count", 0) or 0)
            if bool(getattr(exc, "partial", False)) or completed_count:
                failure = "REPLAY_PARTIAL: " + _canonical_json(
                    {
                        "workflow_id": workflow_id,
                        "executed_steps": executed_steps,
                        "completed_fields": completed_count,
                        "outcome_uncertain": bool(
                            getattr(exc, "uncertain", False)
                        ),
                        "code": str(getattr(exc, "code", "") or "action_failed"),
                        "last_snapshot": final_observation,
                    }
                )
            else:
                failure = "REPLAY_HALTED: " + _canonical_json(
                    {
                        "workflow_id": workflow_id,
                        "executed_steps": executed_steps,
                        "outcome_uncertain": bool(
                            getattr(exc, "uncertain", False)
                        ),
                        "code": str(getattr(exc, "code", "") or "action_failed"),
                        "last_snapshot": final_observation,
                    }
                )
        except Exception:  # noqa: BLE001 - do not echo runtime values/plan data
            failure = "REPLAY_HALTED: internal_replay_failure"
        finally:
            if begun and suspended is None:
                try:
                    cleanup_ok = (
                        await self._manager.end_replay(
                            owner,
                            session_id,
                            workflow_id=workflow_id,
                            workflow_digest=artifact.digest,
                            capability_generation=generation,
                            replay_nonce=nonce,
                            reason="completed" if completed else "failed",
                        )
                        is True
                    )
                except Exception:  # noqa: BLE001 - lease cleanup is terminal
                    cleanup_ok = False

        if failure is not None:
            return failure
        if suspended is not None:
            # 工作流没有失败，只是停在一个只有用户能完成的步骤上。这与
            # REPLAY_HALTED 是两件完全不同的事：halted 要排查，suspended 要等人。
            return "REPLAY_SUSPENDED: " + _canonical_json(
                {
                    "workflow_id": workflow_id,
                    "executed_steps": executed_steps,
                    **suspended,
                    "snapshot": final_observation,
                }
            )
        if not completed or not cleanup_ok:
            return "REPLAY_HALTED: replay_lease_cleanup_failed"
        takeover = bool(
            artifact.payload["plan"]
            and artifact.payload["plan"][-1]["kind"] == "takeover"
        )
        return "REPLAY_OK: " + _canonical_json(
            {
                "workflow_id": workflow_id,
                "executed_steps": executed_steps,
                "takeover": takeover,
                "snapshot": final_observation,
            }
        )


def register_record_replay_tool(
    ctx: Any,
    manager: Any,
    *,
    capability_check: Callable[[], str | None] | None = None,
) -> RecordReplayTool:
    tool = RecordReplayTool(manager, capability_check)
    ctx.register_tool(
        name=REPLAY_TOOL_NAME,
        toolset="browser",
        schema=REPLAY_SCHEMA,
        handler=tool.handler,
        check_fn=manager.available,
        is_async=True,
        display_name="运行录制工作流",
        should_defer=False,
        permission_resolver=tool.permission_resolver,
    )
    return tool


__all__ = [
    "REPLAY_SCHEMA",
    "REPLAY_TOOL_NAME",
    "RecordReplayTool",
    "register_record_replay_tool",
]
