"""System Prompt 构建。

采用静态/动态分离设计：
- system_static: 身份 + 规则 + 工具指南 + 安全规则 + 输出风格（几乎不变，可走 KV Cache）
- user_reminder: 项目文件 + workspace + skills + 记忆 + 用户画像 + 会话记忆 + 日期（每轮可能变，通过 <system-reminder> 注入 user 消息）

上下文文件发现（仅一种）：
  .crew.md / CREW.md — 从 cwd 向上走到 git root，找到即加载
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from crew.state.home import load_soul_md, load_memory_md, load_user_md
from crew.agent.skills import build_skills_index_prompt, build_optional_skills_index_prompt

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
    "  - 输出信息：直接输出文本（不要用 echo/printf）\n"
    "- 可以在同一条消息中并行调用多个无依赖关系的工具，提高效率。\n"
    "- 有依赖关系的工具调用必须按顺序执行。"
)

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
# 上下文文件发现：仅 .crew.md / CREW.md
# ---------------------------------------------------------------------------

def _find_git_root(start: Path) -> Optional[Path]:
    """从 start 向上查找包含 .git 的目录。"""
    current = start.resolve()
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


def _load_crew_md(cwd: Path) -> str:
    """从 cwd 向上走到 git root，查找 .crew.md / CREW.md。

    第一个找到即返回，不再继续向上。
    """
    stop_at = _find_git_root(cwd)
    current = cwd.resolve()
    for directory in [current, *current.parents]:
        for name in [".crew.md", "CREW.md"]:
            candidate = directory / name
            if candidate.is_file():
                try:
                    content = candidate.read_text(encoding="utf-8").strip()
                    if content:
                        return f"## {name}\n\n{content}"
                except Exception as e:
                    logger.debug("无法读取 %s: %s", candidate, e)
        # 到 git root 就停止
        if stop_at and directory == stop_at:
            break
    return ""


def _load_profile(profile_path: str) -> str:
    """加载 prompt profile markdown 文件。"""
    try:
        content = Path(profile_path).read_text(encoding="utf-8").strip()
        return content
    except Exception as e:
        logger.warning("无法加载 prompt profile %s: %s", profile_path, e)
        return ""


def build_context_files_prompt(cwd: str | None = None) -> str:
    """发现并加载项目上下文文件。

    仅查找 .crew.md / CREW.md，从 cwd 向上走到 git root。
    返回格式化内容，未找到则返回空串。
    """
    if cwd is None:
        cwd = os.getcwd()
    cwd_path = Path(cwd).resolve()
    return _load_crew_md(cwd_path)


# ---------------------------------------------------------------------------
# 静态/动态分离 System Prompt 构建
# ---------------------------------------------------------------------------

def build_prompt_parts(
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
) -> dict[str, str]:
    """组装 prompt 为 system_static / user_reminder 两部分。

    返回 dict:
      - system_static: 身份 + 记忆引导 + 工具指南 + 安全规则 + 输出风格
                       （几乎不变，可走 KV Cache）
      - user_reminder: 项目文件 + workspace + skills + 记忆 + 用户画像 + 会话记忆 + 日期
                       （每轮可能变，通过 <system-reminder> 注入 user 消息）

    """
    # ── System Static 层（几乎不变） ──
    static_parts: list[str] = []
    if profile_path:
        profile_content = _load_profile(profile_path)
        if profile_content:
            static_parts.append(profile_content)

    if not static_parts:
        if lightweight:
            # 子 agent：用默认身份 + 工具/安全/风格规则，跳过 SOUL 与记忆引导
            static_parts.append(DEFAULT_AGENT_IDENTITY)
            static_parts.append(DEFAULT_TOOL_GUIDELINE)
            static_parts.append(DEFAULT_SAFETY_RULES)
            static_parts.append(DEFAULT_OUTPUT_STYLE)
        else:
            t = time.perf_counter()
            soul = load_soul_md()
            logger.debug("[PERF] load_soul_md       %.3fs", time.perf_counter() - t)
            if soul:
                static_parts.append(soul)
            else:
                static_parts.append(DEFAULT_AGENT_IDENTITY)
            static_parts.append(MEMORY_GUIDANCE)
            static_parts.append(DEFAULT_TOOL_GUIDELINE)
            static_parts.append(DEFAULT_SAFETY_RULES)
            static_parts.append(DEFAULT_OUTPUT_STYLE)

    # ── User Reminder 层（每轮可能变） ──
    reminder_parts = []
    # 子 agent（lightweight）：跳过全局 workspace/上下文文件/skills/记忆/用户画像注入，
    # 只保留日期，保持聚焦（用于 skip_memory / skip_context_files）。
    if not lightweight:
        if workspace_instructions.strip():
            reminder_parts.append(f"# 项目提示词\n{workspace_instructions.strip()}")
        t = time.perf_counter()
        context_files = build_context_files_prompt(cwd)
        logger.debug("[PERF] context_files      %.3fs", time.perf_counter() - t)
        if context_files:
            reminder_parts.append(context_files)
        try:
            t = time.perf_counter()
            skills_index = build_skills_index_prompt(
                enabled=enabled_skills,
                disabled=disabled_skills,
            )
            if include_optional_skills:
                optional_index = build_optional_skills_index_prompt(
                    enabled=enabled_skills,
                    disabled=disabled_skills,
                )
                if optional_index:
                    skills_index = f"{skills_index}\n\n{optional_index}" if skills_index else optional_index
            logger.info("[PERF] skills_index       %.3fs", time.perf_counter() - t)
            if skills_index:
                reminder_parts.append(skills_index)
        except Exception:
            pass  # skills 索引不影响主流程

        t = time.perf_counter()
        memory_md = load_memory_md()
        logger.debug("[PERF] load_memory_md     %.3fs", time.perf_counter() - t)
        if memory_md:
            reminder_parts.append(f"# 持久记忆\n{memory_md}")
        t = time.perf_counter()
        user_md = load_user_md()
        logger.debug("[PERF] load_user_md       %.3fs", time.perf_counter() - t)
        if user_md:
            reminder_parts.append(f"# 用户画像\n{user_md}")
        if memory_text.strip():
            reminder_parts.append(f"# 会话记忆\n{memory_text.strip()}")
    # 子 agent（lightweight）继承技能时：单独注入 skills 索引，其余全局上下文仍跳过。
    # 对照 delegate_task 的 skills 参数——只放开 skills，不放开 workspace/记忆/用户画像。
    if lightweight and inject_skills:
        try:
            skills_index = build_skills_index_prompt(
                enabled=enabled_skills,
                disabled=disabled_skills,
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


# ---------------------------------------------------------------------------
# 向后兼容：旧版调用方式
# ---------------------------------------------------------------------------

def build_system_prompt_parts(
    workspace_instructions: str = "",
    memory_text: str = "",
    cwd: str | None = None,
    profile_path: str | None = None,
    enabled_skills: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    user_type: str = "internal",
) -> dict[str, str]:
    """向后兼容：返回 stable/context/volatile 三层（内部使用新的 build_prompt_parts）。

    .. deprecated::
        请使用 :func:`build_prompt_parts` 代替。
    """
    parts = build_prompt_parts(
        workspace_instructions=workspace_instructions,
        memory_text=memory_text,
        cwd=cwd,
        profile_path=profile_path,
        enabled_skills=enabled_skills,
        disabled_skills=disabled_skills,
        user_type=user_type,
    )
    # 映射：新接口只有两层，旧接口三层
    return {
        "stable": parts["system_static"],
        "context": "",
        "volatile": parts["user_reminder"],
    }


def build_system_prompt(
    base: str = "",
    memory_text: str = "",
    workspace_instructions: str = "",
    cwd: str | None = None,
    profile_path: str | None = None,
    enabled_skills: list[str] | None = None,
    disabled_skills: list[str] | None = None,
    user_type: str = "internal",
) -> str:
    """组装完整 system prompt（向后兼容旧版调用方式）。

    base 参数保留但不再作为主身份——身份由 SOUL.md 或 DEFAULT_AGENT_IDENTITY 决定。
    """
    parts = build_prompt_parts(
        workspace_instructions=workspace_instructions,
        memory_text=memory_text,
        cwd=cwd,
        profile_path=profile_path,
        enabled_skills=enabled_skills,
        disabled_skills=disabled_skills,
        user_type=user_type,
    )
    return "\n\n".join(p for p in (parts["system_static"], parts["user_reminder"]) if p)
