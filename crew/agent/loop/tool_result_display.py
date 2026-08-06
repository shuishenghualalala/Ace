"""把工具原始结果转成适合前端 tool_event.detail 展示的短文本。"""

from __future__ import annotations

import json

# subagent 委派工具：前端 renderSubagentCard 需要完整结构化 JSON
# （{"results":[{status, summary, duration_seconds, tool_calls, ...}]}）
# 渲染任务描述/最终回复/执行摘要，不能走预览提取或截断。
# 结果本身有界（子任务摘要 × 批量上限 8），不防碍 WS/历史载荷体积。
SUBAGENT_FULL_RESULT_TOOLS = frozenset({"delegate_task", "run_agent"})


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
    except Exception:
        return _clip(text, max_len)

    if isinstance(data, dict):
        surface = data.get("surface")
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


def _terminal_detail(content: str, *, max_len: int) -> str:
    try:
        data = json.loads(content)
    except Exception:
        return _clip(content, max_len)

    if not isinstance(data, dict):
        return _clip(content, max_len)

    output = str(data.get("output") or "").strip()
    if output:
        # 长输出保留尾部：日志/搜索结果的有效信息常在末尾。
        return output[-max_len:] if len(output) > max_len else output

    session_id = data.get("session_id")
    if session_id:
        return f"Background process started: {session_id}"

    exit_code = data.get("exit_code")
    if exit_code is not None and not data.get("success", True):
        return f"terminal exited with code {exit_code}"

    err = str(data.get("error") or "").strip()
    if err:
        return _clip(err, max_len)

    return _clip(content, max_len)


def _clip(text: str, max_len: int) -> str:
    if max_len <= 0:
        return ""
    return text if len(text) <= max_len else text[:max_len]
