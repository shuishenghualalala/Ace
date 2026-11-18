"""System Prompt 构建。

采用静态/动态分离设计：
- system_static: 身份 + 规则 + 工具指南 + 安全规则 + 输出风格（几乎不变，可走 KV Cache）
- user_reminder: 项目文件 + workspace + skills + 记忆 + 用户画像 + 会话记忆 + 日期（每轮可能变，通过 <system-reminder> 注入 user 消息）

上下文文件包括 owner 级 AGENTS.md 和工作目录所属项目的最近一份指令文件。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from crew.agent.instructions import build_context_files_prompt as build_context_files_prompt
from crew.agent.prompt_budget import render_prompt_source, source_token_budgets
from crew.state.home import (
    load_prompt_markdown,
    read_bounded_markdown,
)
from crew.agent.skills import (
    build_optional_skills_index_prompt,
    build_skills_index_prompt,
)
from crew.tools.process_registry import current_shell_kind

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 默认身份
# ---------------------------------------------------------------------------

DEFAULT_AGENT_IDENTITY = (
    "你是 Crew 智能助手。你可以调用工具来完成任务。\n"
    "- 需要查看文件、执行命令时，主动使用提供的工具。\n"
    "- 工具返回结果后，结合结果继续推理，直到任务完成再给出最终回答。\n"
    "- 回答简洁、准确，用中文。"
)

MEMORY_GUIDANCE = (
    "你拥有跨会话的持久记忆。保存持久事实使用记忆工具：用户偏好、环境细节、工具特性、稳定约定。\n"
    "记忆会在每轮注入，保持简洁聚焦。\n"
    "不要保存任务进度、会话结果、临时 TODO 到记忆。"
)

DEFAULT_TOOL_GUIDELINE = (
    "## 工具使用规则\n"
    "- 优先使用专用工具，而非通过 terminal 等通用命令。\n"
    "- 避免用 terminal 执行 `find`、`grep`、`rg`、`cat`、`head`、`tail`、`sed`、`awk`、`echo` 命令，"
    "除非已明确被要求、或已确认专用工具无法满足需求。\n"
    "- 工具偏好（专用工具在体验与可审查性上更优）：\n"
    "  - 按文件名查找：用 glob（不要用 find 或 ls）\n"
    "  - 按内容查找：用 grep（不要用 grep 或 rg）\n"
    "  - 读取文件：用 file_read（不要用 cat/head/tail）\n"
    "  - 编辑文件：用 patch（不要用 sed/awk）\n"
    "  - 写入文件：用 file_write（不要用 echo > 或 cat <<EOF）\n"
    "  - 删除单个文件：用 file_delete（不要用 rm/Remove-Item）；必须传实际目标路径\n"
    "  - 输出信息：直接输出文本（不要用 echo/printf）\n"
    "- 修改已有文件前先 file_read。局部修改使用实际读取到的原文做 patch；整文件覆盖需读完同一版本。"
    "工具返回 stale_file 时，重新读取并根据新内容计算修改。\n"
    "- 可以在同一条消息中并行调用多个无依赖关系的工具，提高效率。\n"
    "- 有依赖关系的工具调用必须按顺序执行。"
)


def render_tool_guideline(available_tool_names: frozenset[str] | None) -> str:
    if available_tool_names is None:
        return DEFAULT_TOOL_GUIDELINE
    if not available_tool_names:
        return ""
    specialized = (
        ("glob", "用 glob 按文件名查找。"),
        ("grep", "用 grep 按内容查找。"),
        ("file_read", "用 file_read 读取文件。"),
        ("patch", "用 patch 做局部编辑，参数引用实际读取到的原文。"),
        ("file_write", "用 file_write 写入文件。"),
        ("file_delete", "用 file_delete 删除单个文件，传入实际目标路径。"),
    )
    actions = [f"- {instruction}" for name, instruction in specialized if name in available_tool_names]
    if actions and "terminal" in available_tool_names:
        actions.insert(0, "- 已有专用工具覆盖的操作优先使用专用工具，terminal 用于其它需要执行命令的任务。")
    if "file_read" in available_tool_names and available_tool_names.intersection({"patch", "file_write"}):
        actions.append("- 修改已有文件前先 file_read。整文件覆盖需读完同一版本；遇到 stale_file 时重新读取并计算修改。")
    actions.extend((
        "- 按工具声明提供参数。无依赖的工具可以并行调用，有依赖时按顺序执行。",
        "- 直接向用户报告结果。",
    ))
    return "## 工具使用规则\n" + "\n".join(actions)

DEFAULT_SAFETY_RULES = (
    "## 操作安全规则\n"
    "- 仔细考虑操作的可逆性和影响范围。\n"
    "- 本地、可逆的操作（如编辑文件、运行测试）可以自由执行。\n"
    "- 难以恢复、影响范围大的操作（如删除文件、推代码、修改共享基础设施），必须先与用户确认。\n"
    "- 用户授权一次操作，不代表在所有场景下都授权——每次高风险操作都需确认。\n"
    "- 遇到障碍时，不要用破坏性操作绕过，应排查根本原因。"
)

DEFAULT_OUTPUT_STYLE = (
    "## 输出风格\n"
    "- 回答简洁、准确、用中文。\n"
    "- 不使用 emoji，除非用户明确要求。\n"
    "- 引用代码时包含文件路径和行号，方便定位。\n"
    "- 不要在工具调用前加冒号，如\"让我读取文件：\"应改为\"让我读取文件。\""
)

# ---------------------------------------------------------------------------
# Prompt 文件读取：mtime 缓存 + 线程读取
#
# Profile 缓存按路径和 mtime 复用文本。项目指令使用独立的、有界缓存。
# ---------------------------------------------------------------------------

_FILE_CACHE_LOCK = threading.Lock()
_FILE_CACHE: dict[str, tuple[tuple[int, int, int, int], str, bool]] = {}
_MAX_PROFILE_READ_BYTES = 64 * 1024


def _clear_file_cache() -> None:
    """清空 prompt 文件缓存（测试隔离用）。"""
    with _FILE_CACHE_LOCK:
        _FILE_CACHE.clear()


def _read_text_cached(path: Path) -> tuple[str, bool]:
    """读文本文件，mtime 未变时直接返回缓存内容。"""
    key = str(path)
    try:
        info = path.stat()
        version = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except OSError:
        version = None
    with _FILE_CACHE_LOCK:
        if version is not None:
            hit = _FILE_CACHE.get(key)
            if hit is not None and hit[0] == version:
                return hit[1], hit[2]
    content, truncated = read_bounded_markdown(path, _MAX_PROFILE_READ_BYTES)
    if version is not None:
        with _FILE_CACHE_LOCK:
            _FILE_CACHE[key] = (version, content, truncated)
            if len(_FILE_CACHE) > 128:
                _FILE_CACHE.pop(next(iter(_FILE_CACHE)))
    return content, truncated


def _load_profile(profile_path: str, max_tokens: int = 2_048) -> str:
    """加载 prompt profile markdown 文件。"""
    try:
        content, truncated = _read_text_cached(Path(profile_path))
        return render_prompt_source(
            "", content, max_tokens, "提示词配置已截断；必要时请用户核对其余配置。",
            truncated=truncated,
        )
    except Exception as e:
        logger.warning("无法加载 prompt profile %s: %s", profile_path, e)
        return ""


# ---------------------------------------------------------------------------
# 静态/动态分离 System Prompt 构建
# ---------------------------------------------------------------------------

_EXPLICIT_DELEGATION_DISCIPLINE = """# 子智能体委派纪律
仅当用户、项目指令（如 AGENTS.md / CREW.md）或当前技能明确要求使用子智能体、委派或并行协作时，才可调用子智能体工具。“深入研究”“复杂任务”等描述本身不构成委派授权。
所有子智能体共享同一工作目录，彼此改动立即可见；委派时划清文件与职责边界，不要覆盖或回退其他智能体的改动。父智能体仍对最终核验与答复负责。"""

_PROACTIVE_DELEGATION_DISCIPLINE = """# 子智能体委派纪律
当任务可被拆成边界清晰、能独立推进且并行收益明确的工作时，可以主动调用子智能体；不要为简单或强顺序依赖的工作增加委派开销。
所有子智能体共享同一工作目录，彼此改动立即可见；委派时划清文件与职责边界，不要覆盖或回退其他智能体的改动。父智能体仍对最终核验与答复负责。"""

SUBAGENT_HANDOFF_GUIDANCE = """# 子智能体交卷要求
结束前向父智能体返回自包含结果；不要向用户提问，因为只有父智能体负责与用户沟通。明确区分已验证事实、自述与推断，只完成受委派的范围，不得继续委派给其他智能体。"""


def _delegation_discipline(mode: str) -> str:
    return (
        _PROACTIVE_DELEGATION_DISCIPLINE
        if str(mode or "").strip().lower() == "proactive"
        else _EXPLICIT_DELEGATION_DISCIPLINE
    )


def append_subagent_handoff_guidance(prompt: str | None) -> str:
    """Append the child-only handoff contract exactly once."""

    base = str(prompt or "").strip()
    if SUBAGENT_HANDOFF_GUIDANCE in base:
        return base
    if not base:
        return SUBAGENT_HANDOFF_GUIDANCE
    return f"{base}\n\n{SUBAGENT_HANDOFF_GUIDANCE}"


def _shell_facts_block() -> str:
    """当前 shell 环境事实：与 spawn_local 实际执行 shell 保持同源。

    只注入环境事实而非教学长文，避免模型照搬 skill 里的 bash 语法在
    PowerShell 上触发 ParserError。lightweight 子 agent 同样注入。
    """
    if current_shell_kind() == "powershell":
        return (
            "- 内置 terminal 由 PowerShell 执行（pwsh/powershell -NoProfile）："
            "禁止 `VAR=val && cmd` 之类 bash 语法，环境变量用 `$env:VAR = \"...\"` 单独语句；"
            "`&&`/`||` 仅 PowerShell 7+ 可用，5.1 用 `;` 连接命令；"
            "路径用反斜杠/正斜杠均可，但含空格必须加引号；"
            "控制台默认代码页非 UTF-8，若输出乱码先执行 "
            "`[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` "
            "或设置 `$env:PYTHONIOENCODING=\"utf-8\"`。"
        )
    return "- 内置 terminal 由 bash 执行，使用 POSIX 语法。"


def _tool_available(name: str, available_tool_names: frozenset[str] | None) -> bool:
    return available_tool_names is None or name in available_tool_names


def _workspace_facts(cwd: str, available_tool_names: frozenset[str] | None) -> str:
    trusted_cwd = Path(cwd).expanduser().resolve()
    host_home = Path.home().expanduser().resolve(strict=False)
    facts = [
        "# 当前工作空间（可信）",
        f"- 当前工作目录：`{trusted_cwd}`",
        "- 本地工具以这个目录为起点；直接使用相对路径，不要再次切换到该目录。",
        "- 该目录由宿主运行时提供；不要用它推导、验证或重建当前工作空间路径。",
        f"- 宿主用户目录：`{host_home}`。受管模式仍由沙箱分别限制读写。",
        "- 受管模式可广泛只读；工作区和临时目录可写。工作区外写入必须使用宿主绝对路径申请 "
        "`with_additional_permissions`；它会在沙箱内生效，不需要 `require_escalated`。",
        "- 不要把相对文件名与额外路径权限组合，也不要把文件工具称为绕过沙箱。"
        "用户拒绝任一安全审批后，必须立即停止本轮操作，"
        "不得改用其他工具重试。",
        "- runtime_crashed、runtime_protocol_mismatch 和 sandbox_unavailable 表示安全运行时故障，"
        "不表示目标路径在沙箱外；不得据此改用其他文件工具。",
    ]
    if _tool_available("file_delete", available_tool_names):
        facts.append("- 删除单个文件优先使用 file_delete。")
    file_tools = [name for name in ("glob", "grep", "file_read") if _tool_available(name, available_tool_names)]
    if file_tools:
        facts.append(f"- 可用文件检索工具：{'、'.join(file_tools)}；均服从沙箱文件策略。")
    if _tool_available("terminal", available_tool_names):
        facts.append(
            "- 内置 terminal 的 `$HOME`/`%USERPROFILE%` 指向宿主用户目录。"
            "terminal 删除命令必须使用宿主绝对路径；文件工具也服从相同的文件策略。"
        )
        facts.append(_shell_facts_block())
    return "\n".join(facts)


def _build_prompt_parts_sync(
    workspace_instructions: str = "",
    memory_text: str = "",
    cwd: str | None = None,
    profile_path: str | None = None,
    lightweight: bool = False,
    enabled_skills: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    user_type: str = "internal",
    inject_skills: bool = False,
    include_optional_skills: bool = False,
    delegation_mode: str = "explicit",
    context_window: int | None = None,
    available_tool_names: frozenset[str] | None = None,
) -> dict[str, str]:
    """组装 prompt 为 system_static / user_reminder 两部分。

    返回 dict:
      - system_static: 身份 + 记忆引导 + 工具指南 + 安全规则 + 输出风格
                       （几乎不变，可走 KV Cache）
      - user_reminder: 项目文件 + workspace + skills + 记忆 + 用户画像 + 会话记忆 + 日期
                       （每轮可能变，通过 <system-reminder> 注入 user 消息）

    sync 实现：磁盘读取（profile/SOUL/上下文文件/记忆/用户画像）直读，调用方
    （:func:`build_prompt_parts`）负责决定在线程内还是事件循环内执行本函数。
    """
    budgets = source_token_budgets(context_window)
    profile_budget = 2_048 if context_window is None else min(2_048, max(0, context_window // 16))
    # ── System Static 层（几乎不变） ──
    static_parts: list[str] = []
    if profile_path:
        profile_content = _load_profile(profile_path, profile_budget)
        if profile_content:
            static_parts.append(profile_content)

    if not static_parts:
        if lightweight:
            # 子 agent：用默认身份 + 工具/安全/风格规则，跳过 SOUL 与记忆引导
            static_parts.append(DEFAULT_AGENT_IDENTITY)
            guideline = render_tool_guideline(available_tool_names)
            if guideline:
                static_parts.append(guideline)
            static_parts.append(DEFAULT_SAFETY_RULES)
            static_parts.append(DEFAULT_OUTPUT_STYLE)
        else:
            t = time.perf_counter()
            soul, truncated = load_prompt_markdown("SOUL.md", profile_budget * 4 + 12)
            logger.debug("[PERF] load_soul_md       %.3fs", time.perf_counter() - t)
            identity = render_prompt_source(
                "", soul, profile_budget, "SOUL.md 已截断；必要时请用户核对其余配置。",
                truncated=truncated,
            )
            if identity:
                static_parts.append(identity)
            else:
                static_parts.append(DEFAULT_AGENT_IDENTITY)
            if _tool_available("memory", available_tool_names):
                static_parts.append(MEMORY_GUIDANCE)
            guideline = render_tool_guideline(available_tool_names)
            if guideline:
                static_parts.append(guideline)
            static_parts.append(DEFAULT_SAFETY_RULES)
            static_parts.append(DEFAULT_OUTPUT_STYLE)

    if not lightweight and _tool_available("delegate_task", available_tool_names):
        static_parts.append(_delegation_discipline(delegation_mode))

    # ── User Reminder 层（每轮可能变） ──
    reminder_parts = []
    if cwd:
        reminder_parts.append(_workspace_facts(cwd, available_tool_names))
    # 子 agent（lightweight）：跳过全局 workspace/上下文文件/skills/记忆/用户画像注入，
    # 只保留日期，保持聚焦（用于 skip_memory / skip_context_files）。
    if not lightweight:
        t = time.perf_counter()
        context_files = build_context_files_prompt(
            cwd, workspace_instructions=workspace_instructions, budgets=budgets,
            allow_file_read=available_tool_names is None or "file_read" in available_tool_names,
        )
        logger.debug("[PERF] context_files      %.3fs", time.perf_counter() - t)
        if context_files:
            reminder_parts.append(context_files)
        try:
            t = time.perf_counter()
            skills_index = build_skills_index_prompt(
                enabled=enabled_skills,
                disabled=disabled_skills,
                max_tokens=budgets["skills"],
                available_tool_names=available_tool_names,
            )
            if include_optional_skills:
                optional_index = build_optional_skills_index_prompt(
                    enabled=enabled_skills,
                    disabled=disabled_skills,
                    max_tokens=budgets["optional_skills"],
                )
                if optional_index:
                    skills_index = f"{skills_index}\n\n{optional_index}" if skills_index else optional_index
            logger.info("[PERF] skills_index       %.3fs", time.perf_counter() - t)
            if skills_index:
                reminder_parts.append(skills_index)
        except Exception:
            pass  # skills 索引不影响主流程

        t = time.perf_counter()
        memory_md, memory_truncated = load_prompt_markdown("MEMORY.md", budgets["memory"] * 4 + 12)
        logger.debug("[PERF] load_memory_md     %.3fs", time.perf_counter() - t)
        reminder_parts.append(render_prompt_source(
            "# 持久记忆", memory_md, budgets["memory"],
            "MEMORY.md 已截断；必要时请用户核对其余记忆。", truncated=memory_truncated,
        ))
        t = time.perf_counter()
        user_md, user_truncated = load_prompt_markdown("USER.md", budgets["user"] * 4 + 12)
        logger.debug("[PERF] load_user_md       %.3fs", time.perf_counter() - t)
        reminder_parts.append(render_prompt_source(
            "# 用户画像", user_md, budgets["user"],
            "USER.md 已截断；必要时请用户核对其余偏好。", truncated=user_truncated,
        ))
        reminder_parts.append(render_prompt_source(
            "# 会话记忆", memory_text, budgets["recall"],
            "会话记忆已截断；需要其余信息时请核对当前会话记录。",
        ))
    # 子 agent（lightweight）继承技能时：单独注入 skills 索引，其余全局上下文仍跳过。
    # 对照 delegate_task 的 skills 参数——只放开 skills，不放开 workspace/记忆/用户画像。
    if lightweight and inject_skills:
        try:
            skills_index = build_skills_index_prompt(
                enabled=enabled_skills,
                disabled=disabled_skills,
                max_tokens=budgets["skills"],
                available_tool_names=available_tool_names,
            )
            if skills_index:
                reminder_parts.append(skills_index)
        except Exception:
            pass  # skills 索引不影响主流程

    reminder_parts.append(f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    return {
        "system_static": "\n\n".join(p for p in static_parts if p and p.strip()),
        "user_reminder": "\n\n".join(p for p in reminder_parts if p and p.strip()),
    }


async def build_prompt_parts(
    workspace_instructions: str = "",
    memory_text: str = "",
    cwd: str | None = None,
    profile_path: str | None = None,
    lightweight: bool = False,
    enabled_skills: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    user_type: str = "internal",
    inject_skills: bool = False,
    include_optional_skills: bool = False,
    delegation_mode: str = "explicit",
    context_window: int | None = None,
    available_tool_names: frozenset[str] | None = None,
) -> dict[str, str]:
    """在工作线程发现和组装 prompt 文件，避免文件系统探测阻塞 Gateway。"""
    return await asyncio.to_thread(
        _build_prompt_parts_sync,
        workspace_instructions=workspace_instructions,
        memory_text=memory_text,
        cwd=cwd,
        profile_path=profile_path,
        lightweight=lightweight,
        enabled_skills=enabled_skills,
        disabled_skills=disabled_skills,
        user_type=user_type,
        inject_skills=inject_skills,
        include_optional_skills=include_optional_skills,
        delegation_mode=delegation_mode,
        context_window=context_window,
        available_tool_names=available_tool_names,
    )


# ---------------------------------------------------------------------------
# 向后兼容：旧版调用方式
# ---------------------------------------------------------------------------

async def build_system_prompt_parts(
    workspace_instructions: str = "",
    memory_text: str = "",
    cwd: str | None = None,
    profile_path: str | None = None,
    enabled_skills: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    user_type: str = "internal",
    context_window: int | None = None,
    available_tool_names: frozenset[str] | None = None,
) -> dict[str, str]:
    """向后兼容：返回 stable/context/volatile 三层（内部使用新的 build_prompt_parts）。

    .. deprecated::
        请使用 :func:`build_prompt_parts` 代替。
    """
    parts = _build_prompt_parts_sync(
        workspace_instructions=workspace_instructions,
        memory_text=memory_text,
        cwd=cwd,
        profile_path=profile_path,
        enabled_skills=enabled_skills,
        disabled_skills=disabled_skills,
        user_type=user_type,
        context_window=context_window,
        available_tool_names=available_tool_names,
    )
    # 映射：新接口只有两层，旧接口三层
    return {
        "stable": parts["system_static"],
        "context": "",
        "volatile": parts["user_reminder"],
    }


async def build_system_prompt(
    base: str = "",
    memory_text: str = "",
    workspace_instructions: str = "",
    cwd: str | None = None,
    profile_path: str | None = None,
    enabled_skills: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    user_type: str = "internal",
    context_window: int | None = None,
    available_tool_names: frozenset[str] | None = None,
) -> str:
    """组装完整 system prompt（向后兼容旧版调用方式）。

    base 参数保留但不再作为主身份——身份由 SOUL.md 或 DEFAULT_AGENT_IDENTITY 决定。
    """
    parts = await build_prompt_parts(
        workspace_instructions=workspace_instructions,
        memory_text=memory_text,
        cwd=cwd,
        profile_path=profile_path,
        enabled_skills=enabled_skills,
        disabled_skills=disabled_skills,
        user_type=user_type,
        context_window=context_window,
        available_tool_names=available_tool_names,
    )
    return "\n\n".join(p for p in (parts["system_static"], parts["user_reminder"]) if p)
