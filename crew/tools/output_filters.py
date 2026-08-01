"""终端/命令输出后处理：去 ANSI、头尾截断、阈值可配。

采用 的输出处理（tools/ansi_strip.py + tools/tool_output_limits.py +
terminal_tool 的 40/60 头尾截断），集中到一个子模块，供 terminal 工具调用。

处理顺序（见 builtin.handle_terminal）：strip_ansi → truncate_output → redact。
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# 去 ANSI 转义序列（原样复用 Crew tools/ansi_strip.py）
# ---------------------------------------------------------------------------

_ANSI_ESCAPE_RE = re.compile(
    r"\x1b"
    r"(?:"
        r"\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"     # CSI sequence
        r"|\][\s\S]*?(?:\x07|\x1b\\)"                  # OSC (BEL or ST terminator)
        r"|[PX^_][\s\S]*?(?:\x1b\\)"                   # DCS/SOS/PM/APC strings
        r"|[\x20-\x2f]+[\x30-\x7e]"                    # nF escape sequences
        r"|[\x30-\x7e]"                                 # Fp/Fe/Fs single-byte
    r")"
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"       # 8-bit CSI
    r"|\x9d[\s\S]*?(?:\x07|\x9c)"                       # 8-bit OSC
    r"|[\x80-\x9f]",                                    # Other 8-bit C1 controls
    re.DOTALL,
)

# 快速路径：没有 ESC / C1 字节时直接返回，避免跑完整正则。
_HAS_ESCAPE = re.compile(r"[\x1b\x80-\x9f]")


def strip_ansi(text: str) -> str:
    """去掉文本中的 ANSI 转义序列（颜色、光标控制等）。

    无 ESC / C1 字节时走快速路径原样返回，干净文本开销可忽略。
    """
    if not text or not _HAS_ESCAPE.search(text):
        return text
    return _ANSI_ESCAPE_RE.sub("", text)


# ---------------------------------------------------------------------------
# 输出截断阈值（可配，仿 builtin._terminal_max_timeout 读法）
# ---------------------------------------------------------------------------

# 默认值采用（terminal_tool.MAX_OUTPUT_CHARS = 50000）。
DEFAULT_MAX_OUTPUT_CHARS = 50_000


def get_max_output_chars() -> int:
    """读取终端输出截断上限（字符），来自 config 的 tools.terminal.max_output。

    异常或缺失时回退到默认值，本函数永不抛错。
    """
    try:
        from crew.state.config import load_config

        cfg = load_config()
        val = (
            cfg.raw_config.get("tools", {})
            .get("terminal", {})
            .get("max_output")
        )
        if isinstance(val, (int, float)) and val > 0:
            return int(val)
    except Exception:
        pass
    return DEFAULT_MAX_OUTPUT_CHARS


# ---------------------------------------------------------------------------
# 头尾保留截断（采用：头 40% + 尾 60%，中间挖掉并标注省略字数）
# ---------------------------------------------------------------------------


def truncate_output(text: str, max_chars: int | None = None) -> tuple[str, bool]:
    """超过 max_chars 时保留头 40% + 尾 60%，中间替换为省略标注。

    头部保留报错信息（常在开头），尾部保留最新/最终输出（如退出状态、结论）。

    返回 (处理后的文本, 是否被截断)。
    """
    if max_chars is None:
        max_chars = get_max_output_chars()
    if len(text) <= max_chars:
        return text, False

    head_chars = int(max_chars * 0.4)   # 头 40%：报错信息常在开头
    tail_chars = max_chars - head_chars  # 尾 60%：最新/最终输出最相关
    omitted = len(text) - head_chars - tail_chars
    notice = (
        f"\n\n...[输出已截断 - 省略 {omitted:,} 字符，"
        f"共 {len(text):,} 字符]...\n\n"
    )
    return text[:head_chars] + notice + text[-tail_chars:], True
