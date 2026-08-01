"""轨迹提取器：从 SessionStore 中提取会话轨迹，转换为结构化历史日志。

将 list[Message] 转换为 TrajectoryLog，包含：
  - 用户意图（user 消息）
  - 工具调用记录（assistant 的 tool_calls + tool 结果）
  - 技能激活检测（从消息内容中匹配激活模式）
  - 工具使用统计
  - 错误统计
  - 自动摘要
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from crew.core.interfaces import SessionStore
from crew.core.types import Message
from crew.evolution.models import TrajectoryEntry, TrajectoryLog

logger = logging.getLogger(__name__)

# 检测 skill 激活消息的正则：[IMPORTANT: 用户激活了 "xxx" skill，请遵循以下指令。]
_SKILL_ACTIVATE_RE = re.compile(r'用户激活了\s+"([^"]+)"\s+skill')

# 误报模式正则（英文 + 中文）
_FALSE_POSITIVE_RE = re.compile(
    r'\b(0|no|zero)\s*(error|errors|failed|failures|exception|exceptions)\b'
    r'|\berror\s*(rate|count|number)?\s*[=:]\s*0\b'
    r'|\bsuccess(ful|fully)?\b'
    r'|\bcompleted\s+without\s+(error|errors|failed)\b'
    # 中文误报模式
    r'|无\s*(错误|失败|异常)'
    r'|没有\s*(错误|失败|异常)'
    r'|0\s*(个)?\s*(错误|失败|异常)'
    r'|完成\s*(成功|完毕)\b',
    re.IGNORECASE
)

# LLM 摘要提取 system prompt
_SUMMARIZE_SYSTEM_PROMPT = (
    "你负责摘要提取。请从过长的消息内容中提取关键信息，生成结构化摘要。\n\n"
    "摘要提取规则：\n"
    "1. 提取核心要素：用户意图、工具调用结果要点、决策结论、错误信息、关键参数与数值\n"
    "2. 忽略冗余内容：重复的描述、过长的代码片段、无关的格式化文本、装饰性语句\n"
    "3. 保持原文语言（中文保持中文，英文保持英文）\n"
    "4. 摘要应远短于原文，信息密度高，以要点形式呈现\n"
    "5. 直接输出摘要内容，不要添加任何解释、前缀或标记\n"
)

# 超过此长度才触发 LLM 摘要提取，短文本直接保留
_CONTENT_SUMMARIZE_THRESHOLD = 2000
_THINKING_SUMMARIZE_THRESHOLD = 1000


class TrajectoryExtractor:
    """从 SessionStore 提取会话轨迹。

    依赖注入 SessionStore 实例，不自行创建数据库连接。
    """

    def __init__(
        self,
        session_store: SessionStore,
        llm_provider: Any | None = None,
    ):
        self._store = session_store
        self._llm = llm_provider

    def extract(
        self,
        session_id: str,
        owner_account_id: str = "",
    ) -> TrajectoryLog | None:
        """提取单个会话的轨迹日志。

        Returns TrajectoryLog，会话不存在或无消息时返回 None。
        """
        messages = self._store.load(session_id, owner_account_id)
        if not messages:
            logger.debug("会话 %s 无消息或不存在", session_id)
            return None

        entries = self._build_entries(messages)
        skills = self._detect_skills(messages)
        tool_usage = self._compute_tool_usage(messages)
        error_count, error_tools = self._count_errors(messages)
        summary, structured = self._summarize(
            messages, skills, tool_usage, error_count, error_tools
        )

        # 如果有 LLM provider 且 summary 为空或过短，使用 LLM 补充摘要
        if self._llm and not summary:
            llm_summary = self._summarize_with_llm(
                "\n".join(m.content or "" for m in messages if m.content),
                max_length=500,
                text_type="会话消息",
            )
            if llm_summary:
                summary = llm_summary

        title = self._get_title(session_id, owner_account_id, messages)
        workspace_id = self._get_workspace_id(session_id, owner_account_id)

        return TrajectoryLog(
            session_id=session_id,
            title=title,
            workspace_id=workspace_id,
            owner_account_id=owner_account_id,
            created_at=messages[0].timestamp or "",
            updated_at=messages[-1].timestamp or "",
            message_count=len(messages),
            entries=entries,
            skills_activated=skills,
            tool_usage=tool_usage,
            error_count=error_count,
            error_tools=error_tools,
            summary=summary,
            structured_summary=structured,
        )

    def extract_batch(
        self,
        session_ids: list[str],
        owner_account_id: str = "",
    ) -> list[TrajectoryLog]:
        """批量提取多个会话的轨迹。"""
        results: list[TrajectoryLog] = []
        for sid in session_ids:
            log = self.extract(sid, owner_account_id)
            if log:
                results.append(log)
        return results

    def extract_all(
        self,
        owner_account_id: str = "",
        workspace_id: str | None = None,
        include_archived: bool = False,
    ) -> list[TrajectoryLog]:
        """提取所有会话的轨迹。

        workspace_id 非空时仅提取该工作空间的会话。
        """
        sessions = self._store.list_sessions(
            workspace_id=workspace_id,
            owner_account_id=owner_account_id,
            include_archived=include_archived,
        )
        logger.info("发现 %d 个会话，开始提取轨迹", len(sessions))

        results: list[TrajectoryLog] = []
        for s in sessions:
            sid = s.get("session_id", "")
            if not sid:
                continue
            log = self.extract(sid, owner_account_id)
            if log:
                results.append(log)

        logger.info("成功提取 %d 条轨迹", len(results))
        return results

    # ── 内部方法 ──────────────────────────────────────────────────────────

    def _build_entries(self, messages: list[Message]) -> list[TrajectoryEntry]:
        """将 Message 列表转为 TrajectoryEntry 列表（不压缩，保留原始内容）。"""
        entries: list[TrajectoryEntry] = []
        for msg in messages:
            # 跳过纯 system 消息（通常是大段 system prompt，不含轨迹信息）
            if msg.role == "system" and not msg.is_meta:
                continue

            tool_calls_data: list[dict] = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls_data.append({
                        "id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "started_at": tc.started_at,
                        "duration": tc.duration,
                        "result": tc.result or "",
                        "status": tc.status,
                        "ui_label": tc.ui_label,
                    })

            is_error = self._is_error_message(msg)

            entries.append(TrajectoryEntry(
                role=msg.role,
                content=msg.content or "",
                tool_calls=tool_calls_data,
                tool_call_id=msg.tool_call_id,
                name=msg.name,
                timestamp=msg.timestamp,
                turn_duration=msg.turn_duration,
                is_error=is_error,
                thinking=msg.thinking or "",
            ))
        return entries

    def _summarize_with_llm(
        self, text: str, max_length: int, text_type: str
    ) -> str:
        """使用 LLM 从过长文本中提取摘要，失败时回退到截断。

        Args:
            text: 原始文本
            max_length: 摘要的最大字符数
            text_type: 文本类型描述（如"消息内容"、"思维链"），用于 prompt

        LLM 不可用或调用失败时，回退到简单截断 text[:max_length] + "..."。
        """
        if self._llm is None:
            return text[:max_length] + "..."

        try:
            prompt = (
                f"请从以下{text_type}中提取关键信息摘要，不超过{max_length}字符，"
                f"以要点形式呈现核心内容（意图、决策、结论、错误、关键参数等），"
                f"忽略冗余内容：\n\n{text}"
            )
            messages = [
                Message.system(_SUMMARIZE_SYSTEM_PROMPT),
                Message.user(prompt),
            ]
            resp = self._run_llm_chat(messages)

            summary = ""
            if hasattr(resp, "text"):
                summary = resp.text or ""
            elif isinstance(resp, str):
                summary = resp

            summary = summary.strip()
            if summary and len(summary) < len(text):
                # LLM 摘要提取成功且确实更短
                if len(summary) > max_length:
                    summary = summary[:max_length] + "..."
                logger.debug(
                    "LLM 摘要%s: %d -> %d 字符",
                    text_type, len(text), len(summary),
                )
                return summary
            # LLM 返回为空或更长，回退到截断
            logger.warning("LLM 摘要%s返回无效结果，回退到截断", text_type)
        except Exception as exc:
            logger.warning("LLM 摘要%s失败，回退到截断: %s", text_type, exc)

        return text[:max_length] + "..."

    async def _stream_chat_full(self, messages: list[Any]) -> Any:
        """流式调用 LLM，收集完整文本后返回 ChatResponse。

        使用流式接口避免非流式长生成超时问题：
        非流式 chat() 受 60s read timeout 限制，而复杂 prompt 需要服务端
        长时间运算，极易超时。流式逐 token 返回，每次 token 重置 read
        超时计时器，且享有 stream_resilience 更长的 read_timeout 兜底。
        """
        from crew.core.types import ChatResponse as _ChatResponse

        text_parts: list[str] = []
        tool_calls: list[Any] = []
        finish_reason: str | None = None
        reasoning_content = ""

        async for chunk in self._llm.stream_chat(messages):
            if chunk.delta_text:
                text_parts.append(chunk.delta_text)
            if chunk.reasoning_content:
                reasoning_content += chunk.reasoning_content
            if chunk.done and chunk.tool_calls:
                tool_calls = chunk.tool_calls
            if chunk.finish_reason:
                finish_reason = chunk.finish_reason

        return _ChatResponse(
            text="".join(text_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning_content=reasoning_content,
        )

    def _run_llm_chat(self, messages: list[Any]) -> Any:
        """同步调用 async LLM stream_chat，兼容线程内和事件循环内调用。"""
        coro = self._stream_chat_full(messages)
        try:
            asyncio.get_running_loop()
            # 已在事件循环中（不应发生在 asyncio.to_thread 路径，但防御性处理）
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                return pool.submit(asyncio.run, coro).result()
        except RuntimeError:
            # 无运行中的事件循环，直接 asyncio.run
            return asyncio.run(coro)

    def _detect_skills(self, messages: list[Message]) -> list[str]:
        """检测会话中激活的 skill 名称。"""
        skills: list[str] = []
        seen: set[str] = set()
        for msg in messages:
            if not msg.content:
                continue
            for m in _SKILL_ACTIVATE_RE.finditer(msg.content):
                name = m.group(1)
                if name not in seen:
                    seen.add(name)
                    skills.append(name)
        return skills

    def _compute_tool_usage(self, messages: list[Message]) -> dict[str, int]:
        """统计各工具调用次数。"""
        usage: dict[str, int] = {}
        for msg in messages:
            if not msg.tool_calls:
                continue
            for tc in msg.tool_calls:
                name = tc.name or "unknown"
                usage[name] = usage.get(name, 0) + 1
        return usage

    def _count_errors(self, messages: list[Message]) -> tuple[int, list[str]]:
        """统计错误数量及出错工具。"""
        error_count = 0
        error_tools: list[str] = []
        for msg in messages:
            if msg.role != "tool" or not msg.content:
                continue
            if self._content_has_error(msg.content):
                error_count += 1
                if msg.name and msg.name not in error_tools:
                    error_tools.append(msg.name)
        return error_count, error_tools

    def _is_error_message(self, msg: Message) -> bool:
        """判断一条消息是否为错误消息。"""
        if msg.role == "tool" and msg.content:
            return self._content_has_error(msg.content)
        # assistant 消息中 tool_call status 为 error 也算
        if msg.tool_calls:
            for tc in msg.tool_calls:
                if tc.status == "error":
                    return True
        return False

    @staticmethod
    def _content_has_error(content: str) -> bool:
        """检查内容是否包含真正的错误信息，排除误报情况。

        逻辑：
        1. 先检查是否有明确的错误关键词（高优先级，如 traceback, RuntimeError, failed to）
        2. 如果有，返回True
        3. 如果没有明确的错误关键词，再检查是否有一般错误关键词（error, failed, 错误, 失败）
        4. 如果有一般错误关键词，再检查是否是误报模式
        5. 只有在完全没有错误关键词时，误报模式才生效
        """
        content_lower = content.lower()

        # 1. 检查明确的强错误标记（高优先级）
        strong_error_markers = ["traceback", "exception:", "error:", "runtimeerror", "valueerror", "typeerror", "failed to", "uncaught"]
        if any(marker in content_lower for marker in strong_error_markers):
            return True

        # 2. 检查一般错误关键词
        general_error_keywords = ("failed", "error", "errors", "exception", "exceptions", "failure", "失败", "错误", "异常", "崩溃")
        has_general_error = any(kw in content_lower for kw in general_error_keywords)

        if not has_general_error:
            # 没有错误关键词，直接返回False
            return False

        # 3. 有一般错误关键词时，检查是否是误报模式
        # 误报模式的典型特征：明确表示"0个错误"、"无错误"、"成功完成"等
        # 如果匹配误报模式，说明之前的错误关键词是误报
        has_false_positive = _FALSE_POSITIVE_RE.search(content_lower)
        if has_false_positive:
            # 进一步检查：误报模式是否完整覆盖了整个否定表述
            # 例如 "0 errors", "no error", "completed without errors" 等是误报
            # 但 "completed without errors but failed to process" 不是误报，因为有"failed to"
            # 检查是否在误报模式之后还有明确的错误动词
            false_positive_match = has_false_positive.group(0)
            pos = has_false_positive.start()
            # 检查误报模式之后的内容
            remainder = content_lower[pos + len(false_positive_match):].strip()
            # 如果之后还有"failed to"、"error:"等明确错误标记，不是误报
            if remainder and any(m in remainder for m in ["failed to", "error:", "exception:", "traceback"]):
                return True
            # 否则是误报
            return False

        # 4. 没有误报模式，有一般错误关键词，返回True
        return True

    def _summarize(
        self,
        messages: list[Message],
        skills: list[str],
        tool_usage: dict[str, int] | None = None,
        error_count: int = 0,
        error_tools: list[str] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """生成结构化会话摘要。

        返回 (text_summary, structured_summary)。
        包含五个要素：用户意图、使用工具、执行操作、执行结果、证据。
        执行操作和结果以结构化数据呈现，区分成功/失败，并分析错误对核心意图的影响。
        """
        tool_usage = tool_usage or {}
        error_tools = error_tools or []

        # ── 1. 用户意图 ──
        user_intents: list[str] = []
        for m in messages:
            if m.role == "user" and m.content and not m.is_meta:
                text = m.content.strip()
                if text and text not in user_intents:
                    user_intents.append(text[:200])

        # ── 2. 使用工具 ──
        tools_str = ""
        if tool_usage:
            tools_str = ", ".join(
                f"{name}({cnt})"
                for name, cnt in sorted(tool_usage.items(), key=lambda x: -x[1])
            )
        if skills:
            skills_str = f"激活技能: {', '.join(skills)}"
            tools_str = f"{tools_str} | {skills_str}" if tools_str else skills_str

        # ── 3. 执行操作（结构化） ──
        operations: list[dict[str, Any]] = []
        op_idx = 0
        for m in messages:
            if not m.tool_calls:
                continue
            for tc in m.tool_calls:
                op_idx += 1
                args_desc = self._summarize_tool_args(tc)
                # 从 tc.status 和 tool 结果消息两方面判断错误
                is_err, error_detail = self._is_tool_call_error(tc, messages)
                error_type = None
                if is_err:
                    error_type = self._classify_error_type(
                        error_detail or "", None
                    )
                operations.append({
                    "step": op_idx,
                    "tool": tc.name or "unknown",
                    "args_summary": args_desc,
                    "status": "error" if is_err else "success",
                    "is_error": is_err,
                    "error_type": error_type,
                    "error_detail": error_detail,
                })

        # ── 4. 执行结果（结构化） ──
        results: list[dict[str, Any]] = []
        for m in messages:
            if m.role != "tool" or not m.content:
                continue
            result_desc, result_struct = self._summarize_tool_result(m)
            if result_desc:
                results.append(result_struct)

        # ── 错误影响分析 ──
        error_analysis = self._analyze_error_impact(messages, operations)

        # ── 5. 证据 ──
        evidence = self._extract_evidence(messages)

        # ── 组装结构化摘要 ──
        structured: dict[str, Any] = {
            "user_intent": user_intents[:3],
            "tools_used": dict(tool_usage),
            "skills_activated": list(skills),
            "operations": operations,
            "results": results,
            "error_analysis": error_analysis,
            "evidence": evidence,
        }

        # ── 组装文本摘要（从结构化数据生成） ──
        parts: list[str] = []

        if user_intents:
            intent_text = " | ".join(user_intents[:3])
            parts.append(f"【用户意图】{intent_text}")

        if tools_str:
            parts.append(f"【使用工具】{tools_str}")

        if operations:
            parts.append("【执行操作】")
            for op in operations:
                status_tag = " [失败]" if op["is_error"] else ""
                impact_tag = ""
                if op["is_error"]:
                    impact = _error_impact_from_analysis(
                        error_analysis, op["step"]
                    )
                    if impact:
                        impact_tag = f" ({impact})"
                parts.append(
                    f"{op['step']}. {op['tool']}: {op['args_summary']}"
                    f"{status_tag}{impact_tag}"
                )

        if results:
            parts.append("【执行结果】")
            for r in results:
                parts.append(f"- {r['tool']}: {r['status']} - {r['summary']}")

        # 错误分析汇总
        if error_analysis["total_errors"] > 0:
            ea = error_analysis
            parts.append("【错误影响分析】")
            parts.append(
                f"- 共 {ea['total_errors']} 次错误，"
                f"阻断 {ea['blocking_errors']} 次，"
                f"可恢复 {ea['recoverable_errors']} 次"
            )
            parts.append(
                f"- 核心意图{'已达成' if ea['intent_achieved'] else '未达成'}"
            )
            for eb in ea["error_breakdown"]:
                parts.append(
                    f"  步骤{eb['step']} {eb['tool']}: "
                    f"{eb['error_type']} → {eb['impact']}"
                )

        if evidence:
            parts.append("【证据】")
            parts.extend(f"- {e}" for e in evidence)

        return ("\n".join(parts) if parts else "无摘要", structured)

    def _summarize_tool_args(self, tc: Any) -> str:
        """将工具调用的参数摘要为一行文本。"""
        name = tc.name or ""
        args = tc.arguments or {}

        if name in ("terminal", "execute_command"):
            cmd = args.get("command", "")
            if cmd:
                return self._summarize_terminal_command(cmd)
            return str(args)[:120]

        if name in ("search_files", "search_file", "search_content"):
            query = args.get("query", args.get("pattern", ""))
            path = args.get("path", args.get("target_directory", ""))
            segs = []
            if query:
                segs.append(f'"{query}"')
            if path:
                segs.append(f"in {path}")
            return " ".join(segs)[:120] if segs else str(args)[:120]

        if name in (
            "file_write", "write_to_file", "file_read", "read_file",
            "replace_in_file", "delete_file",
        ):
            path = args.get(
                "filePath", args.get("path", args.get("target_file", ""))
            )
            if path:
                return path[:120]
            return str(args)[:120]

        # 通用：JSON 摘要
        args_str = json.dumps(args, ensure_ascii=False)
        return args_str[:120]

    def _summarize_terminal_command(self, cmd: str) -> str:
        """将终端命令摘要为一行文本，智能处理多行脚本。"""
        cmd = cmd.strip()
        lines = cmd.split("\n")

        if len(lines) <= 1:
            return cmd[:150]

        # 检测 python -c "..." 模式
        python_match = re.search(r'python\s+(-\w+\s+)*-c\s+"(.+?)"', cmd, re.DOTALL)
        if python_match:
            script = python_match.group(2)
            # 提取脚本中的关键操作行
            key_ops: list[str] = []
            for line in script.split("\n"):
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # 提取关键操作
                if any(line.startswith(kw) for kw in (
                    "import", "from", "df ", "print", "result",
                    "data", "output", "with ", "for ", "if "
                )):
                    key_ops.append(line)
                if len(key_ops) >= 3:
                    break
            if key_ops:
                return f"python -c ({'; '.join(key_ops)})"[:200]
            return f"python -c (多行脚本, {len(script.splitlines())}行)"[:200]

        # 检测 cmd /c "..." 模式
        cmd_match = re.search(r'cmd\s+/c\s+"(.+?)"', cmd, re.DOTALL)
        if cmd_match:
            inner = cmd_match.group(1).strip()
            if "\n" in inner:
                return f"cmd /c (多行命令, {len(inner.splitlines())}行)"[:200]
            return inner[:200]

        # 通用多行命令：首行 + 行数
        first_line = lines[0].strip()
        return f"{first_line} (+{len(lines) - 1}行)"[:200]

    def _summarize_tool_result(self, msg: Message) -> tuple[str, dict[str, Any]] | None:
        """将工具结果消息摘要，返回 (text_desc, structured_dict) 或 None。"""
        content = msg.content or ""
        tool_name = msg.name or ""
        is_err = self._is_error_message(msg)

        # 尝试解析 JSON 结果
        try:
            data = json.loads(content)
            if isinstance(data, dict):
                success = data.get("success", data.get("ok", None))
                if success is True:
                    output = data.get(
                        "output", data.get("result", data.get("message", ""))
                    )
                    if output:
                        # 清理终端输出编码问题
                        if tool_name in ("terminal", "execute_command"):
                            output = self._clean_terminal_output(str(output))
                        lines = str(output).strip().split("\n")
                        key_lines = [line for line in lines if line.strip()][:3]
                        snippet = "; ".join(line.strip() for line in key_lines)
                        desc = f"{tool_name}: 成功 - {snippet[:150]}"
                    else:
                        desc = f"{tool_name}: 成功"
                    return desc, {
                        "tool": tool_name,
                        "status": "success",
                        "summary": snippet[:150] if output else "",
                        "is_error": False,
                    }
                if success is False:
                    error = data.get("error", data.get("message", ""))
                    desc = f"{tool_name}: 失败 - {str(error)[:100]}"
                    return desc, {
                        "tool": tool_name,
                        "status": "error",
                        "summary": str(error)[:150],
                        "is_error": True,
                        "error_type": self._classify_error_type(
                            str(error), None
                        ),
                    }
                exit_code = data.get("exit_code")
                if exit_code is not None:
                    output = data.get("output", "")
                    # 清理终端输出编码问题
                    if tool_name in ("terminal", "execute_command"):
                        output = self._clean_terminal_output(str(output))
                    status = "成功" if exit_code == 0 else f"退出码{exit_code}"
                    is_ec_err = exit_code != 0
                    if output:
                        lines = str(output).strip().split("\n")
                        key_lines = [line for line in lines if line.strip()][:2]
                        snippet = "; ".join(line.strip() for line in key_lines)
                        desc = f"{tool_name}: {status} - {snippet[:150]}"
                    else:
                        desc = f"{tool_name}: {status}"
                        snippet = ""
                    return desc, {
                        "tool": tool_name,
                        "status": "error" if is_ec_err else "success",
                        "summary": snippet[:150] if output else "",
                        "is_error": is_ec_err,
                        "exit_code": exit_code,
                    }
        except (json.JSONDecodeError, ValueError):
            pass

        # 非 JSON，直接截断
        text = content.strip()
        if tool_name in ("terminal", "execute_command"):
            text = self._clean_terminal_output(text)
        if is_err:
            desc = f"{tool_name}: 错误 - {text[:100]}"
            return desc, {
                "tool": tool_name,
                "status": "error",
                "summary": text[:150],
                "is_error": True,
                "error_type": self._classify_error_type(text, None),
            }
        desc = f"{tool_name}: {text[:150]}" if text else ""
        if not desc:
            return None
        return desc, {
            "tool": tool_name,
            "status": "success",
            "summary": text[:150],
            "is_error": False,
        }

    def _extract_evidence(self, messages: list[Message]) -> list[str]:
        """从助手消息中提取关键证据（结论、数据点、发现）。

        优先从最后一条助手消息提取（通常包含最终结论），
        不足时从倒数第二条补充。
        """
        evidence: list[str] = []

        assistant_msgs = [
            m for m in messages
            if m.role == "assistant" and m.content
            and len(m.content.strip()) > 30
        ]
        if not assistant_msgs:
            return evidence

        # 从最后一条助手消息提取要点
        for msg in reversed(assistant_msgs):
            content = msg.content.strip()
            lines = content.split("\n")
            for line in lines:
                line = line.strip()
                if not line or len(line) < 5:
                    continue
                # 去掉 markdown 格式符号
                clean = line.lstrip("#*-• ").strip()
                if clean and len(clean) > 5 and clean not in evidence:
                    evidence.append(clean[:200])
                if len(evidence) >= 8:
                    break
            if len(evidence) >= 8:
                break

        return evidence[:8]

    def _get_title(
        self,
        session_id: str,
        owner_account_id: str,
        messages: list[Message],
    ) -> str:
        """获取会话标题。"""
        try:
            sessions = self._store.list_sessions(owner_account_id=owner_account_id)
            for s in sessions:
                if s.get("session_id") == session_id:
                    return s.get("title") or ""
        except Exception:
            pass
        # fallback: 取第一条 user 消息
        for m in messages:
            if m.role == "user" and m.content:
                return m.content[:60]
        return ""

    def _get_workspace_id(
        self,
        session_id: str,
        owner_account_id: str,
    ) -> str | None:
        """获取会话所属 workspace_id。"""
        try:
            return self._store.get_workspace_id(session_id, owner_account_id)
        except Exception:
            return None

    # ── 编码清理 & 错误分析 ─────────────────────────────────────────────

    def _clean_terminal_output(self, text: str) -> str:
        """清理终端输出中的编码问题（GBK 乱码恢复）。

        中文 Windows 终端输出使用 GBK 编码，当被当作 UTF-8 解码时
        会产生乱码字符（如 λ、μ 等希腊字母）和替换字符（\\ufffd）。
        此方法尝试反向恢复希腊字母为中文，并移除无法恢复的乱码字符。
        """
        if not text:
            return text

        # 1. 先移除 \ufffd 替换字符，防止后续 GBK 恢复产生 "锟斤拷" 模式
        #    （\ufffd → UTF-8 \xef\xbf\xbd → GBK 解码 → "锟斤拷" 等变体）
        text = text.replace("\ufffd", "")

        # 2. 检测是否包含典型的 GBK-as-UTF-8 乱码字符（希腊字母等）
        _MOJIBAKE_CHARS = set("λμνξοπρστυφχψωΛΜΝΞΟΠΡΣΤΥΦΧΨΩ")
        has_mojibake = any(c in _MOJIBAKE_CHARS for c in text)

        if has_mojibake:
            try:
                # 反向恢复：UTF-8 字符 → GBK 字节 → 正确解码
                # 此时文本已无 \ufffd，恢复不会产生 "锟斤拷" 模式
                recovered = text.encode("utf-8").decode("gbk")
                # 验证恢复结果是否更合理（包含中文）
                if any("\u4e00" <= c <= "\u9fff" for c in recovered):
                    text = recovered  # 继续走后续清理步骤
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass

        # 3. 移除残留的 "锟斤拷" 变体（上游可能已做过 GBK 恢复）
        text = re.sub(r"锟[\u4e00-\u9fff]{0,3}", "", text)

        # 4. 移除不可读字符：控制字符、以及不属于终端输出的文字系统
        cleaned = []
        for c in text:
            cp = ord(c)
            # 控制字符（保留换行/制表符）
            if cp < 32 and c not in "\n\r\t":
                continue
            # 希腊和科普特文 (0x0370-0x03FF) — GBK 乱码常见
            if 0x0370 <= cp <= 0x03FF:
                continue
            # 西里尔文 (0x0400-0x04FF) — GBK 乱码常见
            if 0x0400 <= cp <= 0x04FF:
                continue
            # 希伯来文 (0x0590-0x05FF) — GBK 乱码常见
            if 0x0590 <= cp <= 0x05FF:
                continue
            # 阿拉伯文 (0x0600-0x06FF)
            if 0x0600 <= cp <= 0x06FF:
                continue
            # 天城文等南亚文字 (0x0900-0x0DFF)
            if 0x0900 <= cp <= 0x0DFF:
                continue
            cleaned.append(c)
        return "".join(cleaned)

    def _classify_error_type(
        self, error_msg: str, exit_code: int | None
    ) -> str:
        """分类错误类型。

        返回错误类型标签：policy_blocked, syntax_error, not_found,
        timeout, permission_denied, execution_error, unknown。
        """
        msg_lower = error_msg.lower()

        # 策略阻断（安全策略阻止执行）
        if any(kw in msg_lower for kw in (
            "dangerous", "policy", "安全策略", "阻止", "blocked",
            "prohibited", "不允许", "拒绝",
        )):
            return "policy_blocked"

        # PowerShell 管道符语法错误（|| / && 在 PowerShell 中无效）
        if "||" in error_msg or "&&" in error_msg:
            return "syntax_error"

        # 语法错误
        if any(kw in msg_lower for kw in (
            "syntax", "语法", "parse", "unexpected", "invalid",
            "unterminated", "token",
        )):
            return "syntax_error"

        # 文件/路径未找到
        if any(kw in msg_lower for kw in (
            "not found", "no such file", "未找到", "找不到",
            "does not exist", "不存在",
        )):
            return "not_found"

        # 超时
        if any(kw in msg_lower for kw in (
            "timeout", "timed out", "超时", "timeoutexpired",
        )):
            return "timeout"

        # 权限拒绝
        if any(kw in msg_lower for kw in (
            "permission", "denied", "权限", "access denied",
            "unauthorized",
        )):
            return "permission_denied"

        # 退出码非零但无特定关键词
        if exit_code is not None and exit_code != 0:
            return "execution_error"

        # 通用执行错误
        if any(kw in msg_lower for kw in (
            "error", "failed", "失败", "exception", "traceback",
        )):
            return "execution_error"

        return "unknown"

    def _find_tool_error_detail(
        self, messages: list[Message], tool_call_id: str
    ) -> str | None:
        """从工具结果消息中查找对应 tool_call_id 的错误详情。"""
        for m in messages:
            if m.role == "tool" and m.tool_call_id == tool_call_id:
                return m.content or None
        return None

    def _is_tool_call_error(
        self, tc: Any, messages: list[Message]
    ) -> tuple[bool, str | None]:
        """判断工具调用是否产生错误。

        从两方面检查：
        1. ToolCall.status == "error"
        2. 工具结果消息内容中 JSON 的 success/status/exit_code 字段

        返回 (is_error, error_detail)。
        """
        # 1. tc.status 直接标记为 error
        if tc.status == "error":
            detail = self._find_tool_error_detail(messages, tc.id) if tc.id else None
            return True, detail

        # 2. 从 tool 结果消息内容判断
        if not tc.id:
            return False, None

        detail = self._find_tool_error_detail(messages, tc.id)
        if not detail:
            return False, None

        # 尝试 JSON 解析
        try:
            data = json.loads(detail)
            if isinstance(data, dict):
                success = data.get("success", data.get("ok"))
                if success is False:
                    return True, detail
                status = data.get("status", "")
                if status == "error":
                    return True, detail
                exit_code = data.get("exit_code")
                if exit_code is not None and exit_code != 0:
                    return True, detail
        except (json.JSONDecodeError, ValueError):
            pass

        # 3. 内容包含错误关键词
        if self._content_has_error(detail):
            return True, detail

        return False, None

    def _analyze_error_impact(
        self, messages: list[Message], operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """分析错误对核心意图的影响。

        判定每个错误是"阻断"还是"可恢复"：
        - 可恢复：错误后仍有后续成功的工具调用，或最终助手消息有实质内容
        - 阻断：错误后无更多成功操作，且最终助手消息无实质内容

        同时判定核心意图是否达成（基于最终助手消息是否有实质性结论）。
        """
        error_ops = [op for op in operations if op.get("is_error")]
        total_errors = len(error_ops)

        # 判断核心意图是否达成：最后一条助手消息是否有实质内容
        last_assistant_content = ""
        for m in reversed(messages):
            if m.role == "assistant" and m.content:
                last_assistant_content = m.content.strip()
                break

        intent_achieved = len(last_assistant_content) > 50

        error_breakdown: list[dict[str, Any]] = []
        blocking_count = 0
        recoverable_count = 0

        for err_op in error_ops:
            step = err_op["step"]
            # 检查此错误之后是否有成功的操作
            has_subsequent_success = any(
                op["step"] > step and not op.get("is_error")
                for op in operations
            )

            if has_subsequent_success or intent_achieved:
                impact = "可恢复"
                recoverable_count += 1
            else:
                impact = "阻断"
                blocking_count += 1

            error_breakdown.append({
                "step": step,
                "tool": err_op["tool"],
                "error_type": err_op.get("error_type", "unknown"),
                "impact": impact,
            })

        return {
            "total_errors": total_errors,
            "blocking_errors": blocking_count,
            "recoverable_errors": recoverable_count,
            "intent_achieved": intent_achieved,
            "error_breakdown": error_breakdown,
        }


def _error_impact_from_analysis(error_analysis: dict, step: int) -> str:
    """从错误分析结果中提取指定步骤的影响标签。"""
    for eb in error_analysis.get("error_breakdown", []):
        if eb.get("step") == step:
            return eb.get("impact", "")
    return ""
