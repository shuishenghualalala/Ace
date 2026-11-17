"""把工具原始结果转成适合前端 tool_event.detail 展示的短文本。"""

from __future__ import annotations

import json

# subagent 委派工具：前端 renderSubagentCard 需要完整结构化 JSON
# （{"results":[{status, summary, duration_seconds, tool_calls, ...}]}）
# 渲染任务描述/最终回复/执行摘要，不能走预览提取或截断。
# 结果本身有界（子任务摘要 × 批量上限 8），不防碍 WS/历史载荷体积。
SUBAGENT_FULL_RESULT_TOOLS = frozenset({"delegate_task", "run_agent"})
WIKI_SOURCE_ID_TOOLS = frozenset({
    "wiki_batch_ingest",
    "wiki_delete_source",
    "wiki_parse_source",
    "wiki_plan_ingest",
    "wiki_apply_ingest",
    "wiki_refresh_source",
})
_WIKI_INTERNAL_RESULT_KEYS = frozenset({
    "source_id",
    "source_ids",
    "existing_source_id",
    "duplicate_of",
    "kb_id",
})


def tool_result_detail_for_ui(name: str, content: str, *, max_len: int = 1200) -> str:
    """从工具完整返回值提取 UI 预览文本。

    terminal 等结构化工具返回 JSON，完整内容往往很长；若直接截断 JSON 头部，
    前端只能看到 ``success/cwd/command`` 元数据而看不到 ``output`` 字段。
    采用 ``_preview_tool_result_preview``：优先展示终端 stdout。
    """
    text = str(content or "")
    if not text:
        return ""

    if name in SUBAGENT_FULL_RESULT_TOOLS:
        return text

    if name == "terminal":
        return _terminal_detail(text, max_len=max_len)

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _clip(text, max_len)

    if isinstance(data, dict):
        if name in {"glob", "grep"} and isinstance(data.get("search_notice"), str):
            detail = data.get("content")
            if not detail and isinstance(data.get("files"), list):
                detail = "\n".join(path for path in data["files"] if isinstance(path, str))
            notice = data["search_notice"]
            return _clip(f"{notice}\n\n{detail}" if detail else notice, max_len)
        if name in WIKI_SOURCE_ID_TOOLS:
            data = _redact_wiki_internal_result(data)
            return _clip(json.dumps(data, ensure_ascii=False), max_len)
        surface = data.get("surface")
        if (name == "Widget" and isinstance(surface, dict) and surface.get("version") == 3
                and surface.get("mode") == "native" and len(text.encode("utf-8")) <= 400_000):
            return json.dumps({"ok": data.get("ok") is True, "surface": surface}, ensure_ascii=False)
        if name in {"Widget", "Canvas", "publish_site"} and isinstance(surface, dict):
            return _clip(json.dumps({"ok": bool(data.get("ok", True)), "surface": surface},
                                    ensure_ascii=False, separators=(",", ":")), max_len)
        for key in ("output", "content", "text", "result"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value.strip(), max_len)
        err = data.get("error")
        if isinstance(err, str) and err.strip():
            return _clip(err.strip(), max_len)

    return _clip(text, max_len)


def _redact_wiki_internal_result(value: object, *, nested_source: bool = False) -> object:
    """Remove internal Wiki identifiers from the user-facing result preview."""
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            if key in _WIKI_INTERNAL_RESULT_KEYS:
                continue
            if key == "succeeded" and isinstance(item, list):
                result["succeeded_count"] = len(item)
                continue
            # RawSource payloads use ``id`` rather than ``source_id``.
            if nested_source and key == "id":
                continue
            result[key] = _redact_wiki_internal_result(
                item,
                nested_source=nested_source or key == "source",
            )
        return result
    if isinstance(value, list):
        return [_redact_wiki_internal_result(item, nested_source=nested_source) for item in value]
    return value


def _terminal_detail(content: str, *, max_len: int) -> str:
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return _clip(content, max_len)

    if not isinstance(data, dict):
        return _clip(content, max_len)

    if max_len <= 0:
        return ""
    status = data.get("status")
    error = str(data.get("error") or "").strip()
    output = str(data.get("output") or "").strip()
    if status == "backgrounded" or data.get("background") is True:
        label = "Background process started"
        if session_id := data.get("session_id"):
            label = f"{label}: {session_id}"
        return _clip(label, max_len)
    if not error:
        if status == "timed_out":
            error = "terminal timed out"
        elif status == "cancelled":
            error = "terminal cancelled"
        elif (exit_code := data.get("exit_code")) not in (None, 0):
            error = f"terminal exited with code {exit_code}"
    if error:
        tail_budget = max_len - len(error) - 2
        if output and tail_budget > 0:
            return f"{error}\n\n{output[-tail_budget:]}"
        return _clip(error, max_len)
    if output:
        return output[-max_len:]
    if status == "completed":
        return _clip("terminal completed", max_len)
    return _clip(content, max_len)


def _clip(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    return text if len(text) <= max_len else text[:max_len]
