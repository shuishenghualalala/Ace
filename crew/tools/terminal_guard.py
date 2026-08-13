"""终端命令安全检测。

第一道防线（hardline / dangerous）保留原有正则模式匹配。
第二道防线（classify_command）是多层独立验证器架构，逐项检查命令的
引号语义、注入风险、解析器差异等安全问题。每项验证器独立运作，
任一触发即返回 ask 或 allow。

hardline：无条件阻止（rm -rf /、mkfs、dd 到块设备、fork bomb、shutdown 等）。
dangerous：返回宿主批准需求；模型参数不能授权。
classify_command：在 hardline/dangerous 之后执行更深层语义分析。
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# 第一道防线：正则模式匹配（hardline / dangerous）
# ---------------------------------------------------------------------------

# 命令开始位置片段，用于 shutdown/reboot 等锚定
_CMDPOS = (
    r'(?:^|[;&|`\n]|\$\()'
    r'\s*'
    r'(?:sudo\s+(?:-[^\s]+\s+)*)?'
    r'(?:env\s+(?:\w+=\S*\s+)*)?'
    r'(?:(?:exec|nohup|setsid|time)\s+)*'
    r'\s*'
)

_RE_FLAGS = re.IGNORECASE | re.DOTALL

# 无条件阻止（任何对话模式和批准均不能覆盖）
HARDLINE_PATTERNS = [
    (r'\brm\s+(-[^\s]*\s+)*(/|/\*|/ \*)(\s|$)', "recursive delete of root filesystem"),
    (r'\brm\s+(-[^\s]*\s+)*(/home|/home/\*|/root|/root/\*|/etc|/etc/\*|/usr|/usr/\*|/var|/var/\*|/bin|/bin/\*|/sbin|/sbin/\*|/boot|/boot/\*|/lib|/lib/\*|/lib64|/lib64/\*)(\s|$)', "recursive delete of system directory"),
    (r'\brm\s+(-[^\s]*\s+)*(~|\$HOME)(/?|/\*)?(\s|$)', "recursive delete of home directory"),
    (r'\bmkfs(\.[a-z0-9]+)?\b', "format filesystem (mkfs)"),
    (r'\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*', "dd to raw block device"),
    (r'>\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)[a-z0-9]*\b', "redirect to raw block device"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    (r'\bkill\s+(-[^\s]+\s+)*-1\b', "kill all processes"),
    (_CMDPOS + r'(shutdown|reboot|halt|poweroff)\b', "system shutdown/reboot"),
    (_CMDPOS + r'init\s+[06]\b', "init 0/6 (shutdown/reboot)"),
    (_CMDPOS + r'systemctl\s+(poweroff|reboot|halt|kexec)\b', "systemctl poweroff/reboot"),
    (_CMDPOS + r'telinit\s+[06]\b', "telinit 0/6 (shutdown/reboot)"),
    (r'\bDROP\s+(TABLE|DATABASE)\b', "destructive SQL DROP"),
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "destructive SQL TRUNCATE"),
    # Windows / PowerShell system-destructive commands.
    (r'\bformat-volume\b', "format filesystem (Format-Volume)"),
    (r'\bclear-disk\b', "clear disk (Clear-Disk)"),
    (r'\bdiskpart\b', "raw disk partition tool (diskpart)"),
    (r'\bstop-computer\b', "system shutdown (Stop-Computer)"),
    (r'\brestart-computer\b', "system reboot (Restart-Computer)"),
    (r'\bshutdown\b[^\n]*\b/[sr]\b', "system shutdown/reboot (shutdown.exe /s|/r)"),
    (r'\bformat\b[^\n]*[a-z]:[\\/][^\n]*\b/q\b', "format Windows drive (format /q)"),
    (r'\bremove-item\b[^\n]*-recurse[^\n]*-force[^\n]*[a-z]:\\(?:windows|program files|users|system32)\b',
     "recursive delete of Windows system directory (Remove-Item)"),
    (r'(?:^|[;&|\n])\s*(?:cmd\s+/c\s+)?(?:rd|rmdir)\s+/s\b[^\n]*[a-z]:\\(?:windows|program files|users|system32)?',
     "recursive delete on Windows system path (rd /s)"),
    (r'(?:^|[;&|\n])\s*(?:cmd\s+/c\s+)?del\s+/[fs]\b[^\n]*[a-z]:\\(?:windows|program files|users|system32)\b',
     "recursive delete of Windows system directory (del /s)"),
]

HARDLINE_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in HARDLINE_PATTERNS
]

# 危险且需要宿主批准
DANGEROUS_PATTERNS = [
    (r'\brm\s+(-[^\s]*\s+)*/', "delete in root path"),
    (r'\brm\s+-[^\s]*r', "recursive delete"),
    (r'\brm\s+--recursive\b', "recursive delete (long flag)"),
    (r'\bchmod\s+(-[^\s]*\s+)*(777|666|o\+[rwx]*w|a\+[rwx]*w)\b', "world/other-writable permissions"),
    (r'\bchown\s+(-[^\s]*)?R\s+root', "recursive chown to root"),
    (r'\bmkfs\b', "format filesystem"),
    (r'\bdd\s+.*if=', "disk copy"),
    (r'>\s*/dev/sd', "write to block device"),
    (r'>\s*(?:/etc/|/private/(?:etc|var|tmp|home)/)', "overwrite system config"),
    (r'\bsystemctl\s+(-[^\s]+\s+)*(stop|restart|disable|mask)\b', "stop/restart system service"),
    (r'\bpkill\s+-9\b', "force kill processes"),
    (r'\bkillall\s+(-[^\s]*\s+)*-(9|KILL|SIGKILL)\b', "force kill processes (killall -KILL)"),
    (r':\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:', "fork bomb"),
    (r'\b(bash|sh|zsh|ksh)\s+-[^\s]*c(\s+|$)', "shell command via -c/-lc flag"),
    (r'\b(python[23]?|perl|ruby|node)\s+-[ec]\s+', "script execution via -e/-c flag"),
    (r'\b(curl|wget)\b.*\|\s*(?:[/\w]*/)?(?:ba)?sh(?:\s|$|-c)', "pipe remote content to shell"),
    (r'\bxargs\s+.*\brm\b', "xargs with rm"),
    (r'\bfind\b.*-exec(?:dir)?\s+(/\S*/)?rm\b', "find -exec/-execdir rm"),
    (r'\bfind\b.*-delete\b', "find -delete"),
    (r'\bdocker\s+(restart|stop|kill)\b', "docker restart/stop/kill (container lifecycle)"),
    (r'\bgit\s+reset\s+--hard\b', "git reset --hard (destroys uncommitted changes)"),
    (r'\bgit\s+push\b.*--force\b', "git force push (rewrites remote history)"),
    (r'\bgit\s+push\b.*-f\b', "git force push short flag (rewrites remote history)"),
    (r'\bgit\s+clean\s+-[^\s]*f', "git clean with force (deletes untracked files)"),
    (r'\bgit\s+branch\s+-D\b', "git branch force delete"),
    # Windows / PowerShell dangerous actions that need host approval
    (r'\bremove-item\b[^\n]*-recurse[^\n]*-force', "recursive force delete (Remove-Item -Recurse -Force)"),
    (r'(?:^|[;&|\n])\s*(?:cmd\s+/c\s+)?(?:rd|rmdir)\s+/s\b', "recursive directory delete (rd /s)"),
    (r'(?:^|[;&|\n])\s*(?:cmd\s+/c\s+)?del\s+/s\b', "recursive delete (del /s)"),
    (r'\b(?:invoke-expression|iex)\b\s', "PowerShell Invoke-Expression (arbitrary code)"),
    (r'\b(?:irm|invoke-restmethod|iwr|invoke-webrequest)\b[^\n]*\|\s*(?:iex|invoke-expression)\b',
     "pipe remote content to Invoke-Expression"),
    (r'\bset-executionpolicy\b', "change PowerShell execution policy"),
    (r'\bstart-process\b[^\n]*-verb\s+runas', "elevated process spawn (Start-Process -Verb RunAs)"),
    (r'(?:^|[;&|\n])\s*cmd\s+/c\b', "arbitrary command via cmd /c"),
]

DANGEROUS_PATTERNS_COMPILED = [
    (re.compile(pattern, _RE_FLAGS), description)
    for pattern, description in DANGEROUS_PATTERNS
]


def _normalize_command(command: str) -> str:
    """归一化命令字符串：去掉 ANSI、空字符、Unicode 全角字符。"""
    command = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', command)
    command = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', command)
    command = command.replace('\x00', '')
    command = unicodedata.normalize('NFKC', command)
    return command


def detect_hardline_command(command: str) -> tuple[bool, str | None]:
    """检查命令是否命中无条件阻止模式。"""
    normalized = _normalize_command(command).lower()
    for pattern_re, description in HARDLINE_PATTERNS_COMPILED:
        if pattern_re.search(normalized):
            return True, description
    return False, None


def detect_dangerous_command(command: str) -> tuple[bool, str | None]:
    """检查命令是否命中需要宿主批准的危险模式。"""
    normalized = _normalize_command(command).lower()
    for pattern_re, description in DANGEROUS_PATTERNS_COMPILED:
        if pattern_re.search(normalized):
            return True, description
    return False, None


# ---------------------------------------------------------------------------
# 第二道防线：多层独立验证器（classify_command）
#
# 每项验证器接收一个 ValidationContext，返回 ValidatorResult。
# behavior 取值：
#   "allow"       — 命令安全，跳过后续验证器
#   "ask"         — 命令需要用户确认
#   "passthrough" — 当前验证器无意见，继续下一个
# ---------------------------------------------------------------------------

# 命令替换 / 参数展开 / Zsh 特有语法等危险模式
_COMMAND_SUBSTITUTION_PATTERNS = [
    (re.compile(r'<\('), 'process substitution <()'),
    (re.compile(r'>\('), 'process substitution >()'),
    (re.compile(r'=\('), 'Zsh process substitution =()'),
    # Zsh EQUALS expansion: =cmd at word start expands to $(which cmd).
    (re.compile(r'(?:^|[\s;&|])=[a-zA-Z_]'), 'Zsh equals expansion (=cmd)'),
    (re.compile(r'\$\('), '$() command substitution'),
    (re.compile(r'\$\{'), '${} parameter substitution'),
    (re.compile(r'\$\['), '$[] legacy arithmetic expansion'),
    (re.compile(r'~\['), 'Zsh-style parameter expansion'),
    (re.compile(r'\(e:'), 'Zsh-style glob qualifiers'),
    (re.compile(r'\(\+'), 'Zsh glob qualifier with command execution'),
    (re.compile(r'\}\s*always\s*\{'), 'Zsh always block (try/always construct)'),
    (re.compile(r'<#'), 'PowerShell comment syntax'),
]

# Zsh 特有危险命令
_ZSH_DANGEROUS_COMMANDS = frozenset([
    'zmodload', 'emulate',
    'sysopen', 'sysread', 'syswrite', 'sysseek',
    'zpty', 'ztcp', 'zsocket', 'mapfile',
    'zf_rm', 'zf_mv', 'zf_ln', 'zf_chmod', 'zf_chown',
    'zf_mkdir', 'zf_rmdir', 'zf_chgrp',
])

_ZSH_PRECOMMAND_MODIFIERS = frozenset(['command', 'builtin', 'noglob', 'nocorrect'])

# 不可打印控制字符（不含 \t \n \r，它们由其他验证器处理）
_CONTROL_CHAR_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# Unicode 空白字符
_UNICODE_WS_RE = re.compile(
    r'[\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]'
)

# shell 操作符（用于转义操作符检测）
_SHELL_OPERATORS = frozenset([';', '|', '&', '<', '>'])

_HEREDOC_IN_SUBSTITUTION = re.compile(r'\$\(.*<<')


class ValidatorResult:
    """单个验证器的返回值。"""
    __slots__ = ('behavior', 'message')

    def __init__(self, behavior: str, message: str = '') -> None:
        self.behavior = behavior
        self.message = message


_PASSTHROUGH = ValidatorResult('passthrough')


class _QuoteExtraction:
    """引号感知提取的结果。"""
    __slots__ = (
        'fully_unquoted',
        'unquoted_keep_quote_chars',
        'with_double_quotes',
    )

    def __init__(self) -> None:
        self.with_double_quotes = ''
        self.fully_unquoted = ''
        self.unquoted_keep_quote_chars = ''


class _ValidationContext:
    """所有验证器共享的预计算上下文。"""
    __slots__ = (
        'base_command',
        'fully_unquoted_content',
        'fully_unquoted_pre_strip',
        'original_command',
        'unquoted_content',
        'unquoted_keep_quote_chars',
    )

    def __init__(self, original: str, base: str, qe: _QuoteExtraction) -> None:
        self.original_command = original
        self.base_command = base
        self.unquoted_content = qe.with_double_quotes
        self.fully_unquoted_content = _strip_safe_redirections(qe.fully_unquoted)
        self.fully_unquoted_pre_strip = qe.fully_unquoted
        self.unquoted_keep_quote_chars = qe.unquoted_keep_quote_chars


# ---------------------------------------------------------------------------
# 引号感知工具函数
# ---------------------------------------------------------------------------

def _extract_quoted_content(command: str, *, is_jq: bool = False) -> _QuoteExtraction:
    """遍历命令字符串，按 bash 引号规则提取不同层级的内容。

    - with_double_quotes: 去掉单引号内容，保留双引号内容
    - fully_unquoted: 去掉单引号和双引号内容
    - unquoted_keep_quote_chars: 去掉引号内容但保留引号字符本身
    """
    result = _QuoteExtraction()
    in_single = False
    in_double = False
    escaped = False

    for char in command:
        if escaped:
            escaped = False
            if not in_single:
                result.with_double_quotes += char
            if not in_single and not in_double:
                result.fully_unquoted += char
                result.unquoted_keep_quote_chars += char
            continue

        if char == '\\' and not in_single:
            escaped = True
            if not in_single:
                result.with_double_quotes += char
            if not in_single and not in_double:
                result.fully_unquoted += char
                result.unquoted_keep_quote_chars += char
            continue

        if char == "'" and not in_double:
            in_single = not in_single
            result.unquoted_keep_quote_chars += char
            continue

        if char == '"' and not in_single:
            in_double = not in_double
            result.unquoted_keep_quote_chars += char
            if not is_jq:
                continue

        if not in_single:
            result.with_double_quotes += char
        if not in_single and not in_double:
            result.fully_unquoted += char
            result.unquoted_keep_quote_chars += char

    return result


def _strip_safe_redirections(content: str) -> str:
    """剥离安全的重定向（>/dev/null、2>&1、</dev/null）。

    所有模式必须有尾部边界，否则 > /dev/nullo 会前缀匹配 /dev/null。
    """
    content = re.sub(r'\s+2\s*>&\s*1(?=\s|$)', '', content)
    content = re.sub(r'[012]?\s*>\s*/dev/null(?=\s|$)', '', content)
    content = re.sub(r'\s*<\s*/dev/null(?=\s|$)', '', content)
    return content


def _has_unescaped_char(content: str, char: str) -> bool:
    """检查内容中是否包含未转义的指定单字符。"""
    i = 0
    while i < len(content):
        if content[i] == '\\' and i + 1 < len(content):
            i += 2
            continue
        if content[i] == char:
            return True
        i += 1
    return False


def _has_shell_quote_single_quote_bug(command: str) -> bool:
    """检测 shell-quote 对单引号内反斜杠处理不正确的模式。

    shell-quote 的 chunker 正则把单引号内的 \\' 当作转义序列，
    而 bash 把反斜杠当字面量。这会导致 token 合并差异。
    """
    in_single = False
    in_double = False

    i = 0
    while i < len(command):
        char = command[i]

        if char == '\\' and not in_single:
            i += 2
            continue

        if char == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        if char == "'" and not in_double:
            in_single = not in_single

            if not in_single:
                # 刚关闭一个单引号，检查内容末尾的反斜杠数量
                backslash_count = 0
                j = i - 1
                while j >= 0 and command[j] == '\\':
                    backslash_count += 1
                    j -= 1
                # 奇数个尾部反斜杠：总是 bug
                if backslash_count > 0 and backslash_count % 2 == 1:
                    return True
                # 偶数个尾部反斜杠：仅当后续还有 ' 时才是 bug
                if (
                    backslash_count > 0
                    and backslash_count % 2 == 0
                    and "'" in command[i + 1:]
                ):
                    return True

            i += 1
            continue

        i += 1

    return False


def _has_malformed_tokens(command: str) -> bool:
    """检测不平衡的引号和分隔符。"""
    in_single = False
    in_double = False
    double_count = 0
    single_count = 0

    i = 0
    while i < len(command):
        c = command[i]
        if c == '\\' and not in_single:
            i += 2
            continue
        if c == '"' and not in_single:
            double_count += 1
            in_double = not in_double
        elif c == "'" and not in_double:
            single_count += 1
            in_single = not in_single
        i += 1

    if double_count % 2 != 0 or single_count % 2 != 0:
        return True

    # 检查不平衡的 {} [] ()
    for open_ch, close_ch in [('{', '}'), ('(', ')'), ('[', ']')]:
        if command.count(open_ch) != command.count(close_ch):
            return True

    return False


def _has_backslash_escaped_whitespace(command: str) -> bool:
    """检测引号外的反斜杠转义空白（\\space、\\tab）。

    bash 中 echo\\ test 是单个 token，但解析器可能拆成两个，
    造成路径遍历攻击。
    """
    in_single = False
    in_double = False

    i = 0
    while i < len(command):
        char = command[i]

        if char == '\\' and not in_single:
            if not in_double:
                next_char = command[i + 1] if i + 1 < len(command) else ''
                if next_char in (' ', '\t'):
                    return True
            i += 2
            continue

        if char == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        if char == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue

        i += 1

    return False


def _has_backslash_escaped_operator(command: str) -> bool:
    """检测引号外的反斜杠转义 shell 操作符（\\; \\| \\& \\< \\>）。

    splitCommand 会把 \\; 归一化为裸 ;，导致下游二次解析时产生
    假分隔，隐藏敏感路径。
    """
    in_single = False
    in_double = False

    i = 0
    while i < len(command):
        char = command[i]

        if char == '\\' and not in_single:
            if not in_double:
                next_char = command[i + 1] if i + 1 < len(command) else ''
                if next_char in _SHELL_OPERATORS:
                    return True
            i += 2
            continue

        if char == "'" and not in_double:
            in_single = not in_single
            i += 1
            continue

        if char == '"' and not in_single:
            in_double = not in_double
            i += 1
            continue

        i += 1

    return False


# ---------------------------------------------------------------------------
# 早期验证器（可返回 allow）
# ---------------------------------------------------------------------------

def _validate_empty(ctx: _ValidationContext) -> ValidatorResult:
    if not ctx.original_command.strip():
        return ValidatorResult('allow', 'Empty command is safe')
    return _PASSTHROUGH


def _validate_incomplete_commands(ctx: _ValidationContext) -> ValidatorResult:
    original = ctx.original_command
    trimmed = original.strip()

    if re.match(r'^\s*\t', original):
        return ValidatorResult('ask', 'Command appears to be an incomplete fragment (starts with tab)')
    if trimmed.startswith('-'):
        return ValidatorResult('ask', 'Command appears to be an incomplete fragment (starts with flags)')
    if re.match(r'^\s*(&&|\|\||;|>>?|<)', original):
        return ValidatorResult('ask', 'Command appears to be a continuation line (starts with operator)')
    return _PASSTHROUGH


def _validate_safe_command_substitution(ctx: _ValidationContext) -> ValidatorResult:
    """检测安全的 heredoc 命令替换 $(cat <<'EOF'...) 并允许通过。"""
    original = ctx.original_command
    if not _HEREDOC_IN_SUBSTITUTION.search(original):
        return _PASSTHROUGH

    if _is_safe_heredoc(original):
        return ValidatorResult('allow', 'Safe command substitution: cat with quoted/escaped heredoc delimiter')
    return _PASSTHROUGH


def _is_safe_heredoc(command: str) -> bool:
    """检查 $(cat <<'DELIM'...DELIM) 形式的安全 heredoc 替换。

    要求：
    - 分隔符必须单引号包裹或反斜杠转义
    - 关闭分隔符必须单独一行（或行尾紧跟 )）
    - $( 前必须有非空白文本
    """
    heredoc_pattern = re.compile(
        r'\$\(cat[ \t]*<<(-?)[ \t]*(?:\'+([A-Za-z_]\w*)\'+|\\([A-Za-z_]\w*))'
    )
    match = heredoc_pattern.search(command)
    if not match:
        return False

    start = match.start()
    # $( 前必须有非空白文本
    before = command[:start].rstrip()
    if not before:
        return False

    delimiter = match.group(2) or match.group(3)
    if not delimiter:
        return False
    is_dash = match.group(1) == '-'

    operator_end = match.end()
    after_operator = command[operator_end:]
    open_line_end = after_operator.find('\n')
    if open_line_end == -1:
        return False
    # 开头行分隔符后只能有空白
    open_line_tail = after_operator[:open_line_end]
    if not re.match(r'^[ \t]*$', open_line_tail):
        return False

    body_start = operator_end + open_line_end + 1
    body = command[body_start:]
    body_lines = body.split('\n')

    for i, raw_line in enumerate(body_lines):
        line = re.sub(r'^\t*', '', raw_line) if is_dash else raw_line
        if line == delimiter:
            next_line = body_lines[i + 1] if i + 1 < len(body_lines) else ''
            return bool(re.match(r'^([ \t]*)\)', next_line))
        if line.startswith(delimiter):
            after_delim = line[len(delimiter):]
            if re.match(r'^([ \t]*)\)', after_delim):
                return True
    return False


def _validate_git_commit(ctx: _ValidationContext) -> ValidatorResult:
    """允许带简单引号消息的 git commit。"""
    original = ctx.original_command
    base = ctx.base_command

    if base != 'git':
        return _PASSTHROUGH

    # 提取 git 子命令
    after_git = original[len('git'):].strip()
    if not after_git.startswith('commit'):
        return _PASSTHROUGH

    # 提取 -m 消息
    m_match = re.search(
        r"(?:-m|--message)\s+(['\"])((?:\\.|(?!\1).)*)\1",
        after_git,
    )
    if not m_match:
        return _PASSTHROUGH

    message_content = m_match.group(2)

    # 检查 commit 后的剩余部分是否包含危险元字符
    remainder = after_git[:m_match.start()]
    if remainder and re.search(r'[;|&()`]|\$\(|\$\{', remainder):
        return ValidatorResult('passthrough', 'Git commit remainder contains shell metacharacters')

    # 检查未引用的重定向操作符
    if remainder:
        unquoted = _strip_quoted_content(remainder)
        if re.search(r'[<>]', unquoted):
            return ValidatorResult('passthrough', 'Git commit remainder contains unquoted redirect operator')

    # 阻止以 dash 开头的消息
    if message_content.startswith('-'):
        return ValidatorResult('ask', 'Command contains quoted characters in flag names')

    # 检查消息之后的部分是否包含重定向或 shell 元字符
    after_match = after_git[m_match.end():]
    if after_match and re.search(r'[;|&()`]|\$\(|\$\{', after_match):
        return _PASSTHROUGH
    if after_match:
        unquoted_after = _strip_quoted_content(after_match)
        if re.search(r'[<>]', unquoted_after):
            return _PASSTHROUGH

    return ValidatorResult('allow', 'Git commit with simple quoted message is allowed')


def _strip_quoted_content(text: str) -> str:
    """去除引号内容，返回引号外的部分。"""
    result = ''
    in_single = False
    in_double = False
    i = 0
    while i < len(text):
        c = text[i]
        if c == "'" and not in_double:
            in_single = not in_single
            continue
        if c == '"' and not in_single:
            in_double = not in_double
            continue
        if not in_single and not in_double:
            result += c
        i += 1
    return result


# ---------------------------------------------------------------------------
# 主验证器（只能返回 ask 或 passthrough）
# ---------------------------------------------------------------------------

def _validate_jq_command(ctx: _ValidationContext) -> ValidatorResult:
    if ctx.base_command != 'jq':
        return _PASSTHROUGH

    if re.search(r'\bsystem\s*\(', ctx.original_command):
        return ValidatorResult('ask', 'jq command contains system() function which executes arbitrary commands')

    after_jq = ctx.original_command[3:].strip()
    if re.search(r'(?:^|\s)(?:-f\b|--from-file|--rawfile|--slurpfile|-L\b|--library-path)', after_jq):
        return ValidatorResult('ask', 'jq command contains dangerous flags that could execute code or read arbitrary files')

    return _PASSTHROUGH


def _validate_obfuscated_flags(ctx: _ValidationContext) -> ValidatorResult:
    original = ctx.original_command

    # 检测空格后跟引号包裹的 dash（混淆 flag）
    i = 0
    while i < len(original):
        current_char = original[i]
        next_char = original[i + 1] if i + 1 < len(original) else ''

        if current_char in ' \t' and next_char in ('"', "'", '`'):
            quote_char = next_char
            j = i + 2
            inside_quote = ''
            while j < len(original) and original[j] != quote_char:
                inside_quote += original[j]
                j += 1

            if j < len(original) and original[j] == quote_char:
                char_after_quote = original[j + 1] if j + 1 < len(original) else ''
                has_flag_inside = bool(re.match(r'^-+[a-zA-Z0-9$`]', inside_quote))
                has_flag_continuing = (
                    re.match(r'^-+$', inside_quote)
                    and char_after_quote
                    and bool(re.match(r'[a-zA-Z0-9\\${`-]', char_after_quote))
                )
                if has_flag_inside or has_flag_continuing:
                    return ValidatorResult('ask', 'Command contains quoted characters in flag names')

        # 空格后跟 dash
        if current_char in ' \t' and next_char == '-':
            j = i + 1
            flag_content = ''
            while j < len(original):
                flag_char = original[j]
                if flag_char in ' \t=':
                    break
                if flag_char in ('"', "'", '`'):
                    if ctx.base_command == 'cut' and flag_content == '-d':
                        break
                    if j + 1 < len(original):
                        next_flag = original[j + 1]
                        if next_flag and not re.match(r'[a-zA-Z0-9_\'"-]', next_flag):
                            break
                flag_content += flag_char
                j += 1
            if '"' in flag_content or "'" in flag_content:
                return ValidatorResult('ask', 'Command contains quoted characters in flag names')

        i += 1

    # 引号开头后跟 dash
    if re.search(r'\s[\'"`]-', ctx.fully_unquoted_content):
        return ValidatorResult('ask', 'Command contains quoted characters in flag names')
    if re.search(r'["\'`]{2}-', ctx.fully_unquoted_content):
        return ValidatorResult('ask', 'Command contains quoted characters in flag names')

    return _PASSTHROUGH


def _validate_shell_metacharacters(ctx: _ValidationContext) -> ValidatorResult:
    unquoted = ctx.unquoted_content
    message = 'Command contains shell metacharacters (;, |, or &) in arguments'

    if re.search(r'(?:^|\s)["\'][^"\']*[;&][^"\']*["\'](?:\s|$)', unquoted):
        return ValidatorResult('ask', message)

    glob_patterns = [
        re.compile(r'-name\s+["\'][^"\']*[;|&][^"\']*["\']'),
        re.compile(r'-path\s+["\'][^"\']*[;|&][^"\']*["\']'),
        re.compile(r'-iname\s+["\'][^"\']*[;|&][^"\']*["\']'),
    ]
    if any(p.search(unquoted) for p in glob_patterns):
        return ValidatorResult('ask', message)

    if re.search(r'-regex\s+["\'][^"\']*[;&][^"\']*["\']', unquoted):
        return ValidatorResult('ask', message)

    return _PASSTHROUGH


def _validate_dangerous_variables(ctx: _ValidationContext) -> ValidatorResult:
    content = ctx.fully_unquoted_content
    if re.search(r'[<>|]\s*\$[A-Za-z_]', content) or re.search(r'\$[A-Za-z_][A-Za-z0-9_]*\s*[|<>]', content):
        return ValidatorResult('ask', 'Command contains variables in dangerous contexts (redirections or pipes)')
    return _PASSTHROUGH


def _validate_dangerous_patterns(ctx: _ValidationContext) -> ValidatorResult:
    unquoted = ctx.unquoted_content

    if _has_unescaped_char(unquoted, '`'):
        return ValidatorResult('ask', 'Command contains backticks (`) for command substitution')

    for pattern, message in _COMMAND_SUBSTITUTION_PATTERNS:
        if pattern.search(unquoted):
            return ValidatorResult('ask', f'Command contains {message}')

    return _PASSTHROUGH


def _validate_redirections(ctx: _ValidationContext) -> ValidatorResult:
    content = ctx.fully_unquoted_content
    if '<' in content:
        return ValidatorResult('ask', 'Command contains input redirection (<) which could read sensitive files')
    if '>' in content:
        return ValidatorResult('ask', 'Command contains output redirection (>) which could write to arbitrary files')
    return _PASSTHROUGH


def _validate_newlines(ctx: _ValidationContext) -> ValidatorResult:
    content = ctx.fully_unquoted_pre_strip
    if not re.search(r'[\n\r]', content):
        return _PASSTHROUGH

    # 非反斜杠续行的换行后跟非空白
    if re.search(r'(?<![\s\\])[\n\r]\s*\S', content):
        return ValidatorResult('ask', 'Command contains newlines that could separate multiple commands')
    return _PASSTHROUGH


def _validate_carriage_return(ctx: _ValidationContext) -> ValidatorResult:
    original = ctx.original_command
    if '\r' not in original:
        return _PASSTHROUGH

    in_single = False
    in_double = False
    escaped = False
    for char in original:
        if escaped:
            escaped = False
            continue
        if char == '\\' and not in_single:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == '\r' and not in_double:
            return ValidatorResult('ask', 'Command contains carriage return (\\r) which shell-quote and bash tokenize differently')
    return _PASSTHROUGH


def _validate_ifs_injection(ctx: _ValidationContext) -> ValidatorResult:
    if re.search(r'\$IFS|\$\{[^}]*IFS', ctx.original_command):
        return ValidatorResult('ask', 'Command contains IFS variable usage which could bypass security validation')
    return _PASSTHROUGH


def _validate_proc_environ_access(ctx: _ValidationContext) -> ValidatorResult:
    if re.search(r'/proc/.*/environ', ctx.original_command):
        return ValidatorResult('ask', 'Command accesses /proc/*/environ which could expose sensitive environment variables')
    return _PASSTHROUGH


def _validate_malformed_token_injection(ctx: _ValidationContext) -> ValidatorResult:
    original = ctx.original_command

    # 检查命令分隔符
    has_separator = bool(re.search(r'(?:;|&&|\|\|)', original))
    if not has_separator:
        return _PASSTHROUGH

    if _has_malformed_tokens(original):
        return ValidatorResult('ask', 'Command contains ambiguous syntax with command separators that could be misinterpreted')

    return _PASSTHROUGH


def _validate_backslash_escaped_whitespace(ctx: _ValidationContext) -> ValidatorResult:
    if _has_backslash_escaped_whitespace(ctx.original_command):
        return ValidatorResult('ask', 'Command contains backslash-escaped whitespace that could alter command parsing')
    return _PASSTHROUGH


def _validate_backslash_escaped_operators(ctx: _ValidationContext) -> ValidatorResult:
    if _has_backslash_escaped_operator(ctx.original_command):
        return ValidatorResult('ask', 'Command contains a backslash before a shell operator (;, |, &, <, >) which can hide command structure')
    return _PASSTHROUGH


def _validate_unicode_whitespace(ctx: _ValidationContext) -> ValidatorResult:
    if _UNICODE_WS_RE.search(ctx.original_command):
        return ValidatorResult('ask', 'Command contains Unicode whitespace characters that could cause parsing inconsistencies')
    return _PASSTHROUGH


def _validate_mid_word_hash(ctx: _ValidationContext) -> ValidatorResult:
    """检测非空白字符后的 #（中间 #）。

    shell-quote 把中间 # 当注释起始，bash 当字面量，造成解析差异。
    排除 ${# 语法（bash 字符串长度）。
    """
    content = ctx.unquoted_keep_quote_chars
    # 也检查续行合并后的版本
    joined = re.sub(r'\\+\n', lambda m: '' if (len(m.group()) - 1) % 2 == 1 else m.group(), content)

    # 非空白后跟 #，但排除 ${
    for text in (content, joined):
        i = 0
        while i < len(text):
            if text[i] == '#' and i > 0 and not text[i - 1].isspace():
                # 检查前面是否是 ${
                if i >= 2 and text[i - 2:i] == '${':
                    i += 1
                    continue
                return ValidatorResult('ask', 'Command contains mid-word # which is parsed differently by shell-quote vs bash')
            i += 1
    return _PASSTHROUGH


def _validate_brace_expansion(ctx: _ValidationContext) -> ValidatorResult:
    """检测未引用的花括号展开 {a,b} 或 {1..5}。

    Bash 展开花括号但解析器当字面量，造成权限绕过。
    """
    content = ctx.fully_unquoted_pre_strip

    open_count = content.count('{')
    close_count = content.count('}')
    if open_count == 0 or open_count != close_count:
        return _PASSTHROUGH

    # 检查逗号分隔 {a,b} 或序列 {1..5}
    i = 0
    depth = 0
    while i < len(content):
        if content[i] == '{':
            depth += 1
            # 检查后续是否有逗号或 ..
            j = i + 1
            inner_depth = 0
            found_comma = False
            found_sequence = False
            while j < len(content) and (content[j] != '}' or inner_depth > 0):
                if content[j] == '{':
                    inner_depth += 1
                elif content[j] == '}':
                    inner_depth -= 1
                elif content[j] == ',':
                    found_comma = True
                elif content[j] == '.' and j + 1 < len(content) and content[j + 1] == '.':
                    found_sequence = True
                j += 1
            if found_comma or found_sequence:
                return ValidatorResult('ask', 'Command contains brace expansion which could bypass permission checks')
            depth -= 1
        i += 1

    return _PASSTHROUGH


def _validate_zsh_dangerous_commands(ctx: _ValidationContext) -> ValidatorResult:
    original = ctx.original_command
    trimmed = original.strip()
    tokens = re.split(r'\s+', trimmed)
    base_cmd = ''
    for token in tokens:
        if re.match(r'^[A-Za-z_]\w*=', token):
            continue
        if token in _ZSH_PRECOMMAND_MODIFIERS:
            continue
        base_cmd = token
        break

    if base_cmd in _ZSH_DANGEROUS_COMMANDS:
        return ValidatorResult('ask', f"Command uses Zsh-specific '{base_cmd}' which can bypass security checks")

    if base_cmd == 'fc' and re.search(r'\s-\S*e', trimmed):
        return ValidatorResult('ask', "Command uses 'fc -e' which can execute arbitrary commands via editor")

    return _PASSTHROUGH


def _validate_comment_quote_desync(ctx: _ValidationContext) -> ValidatorResult:
    """检测 # 注释中包含引号字符，可能导致引号状态追踪器不同步。"""
    original = ctx.original_command
    in_single = False
    in_double = False
    escaped = False

    i = 0
    while i < len(original):
        char = original[i]

        if escaped:
            escaped = False
            i += 1
            continue

        if in_single:
            if char == "'":
                in_single = False
            i += 1
            continue

        if char == '\\':
            escaped = True
            i += 1
            continue

        if in_double:
            if char == '"':
                in_double = False
            i += 1
            continue

        if char == "'":
            in_single = True
            i += 1
            continue

        if char == '"':
            in_double = True
            i += 1
            continue

        # 未引用的 # — 注释开始
        if char == '#':
            line_end = original.find('\n', i)
            if line_end == -1:
                comment_text = original[i + 1:]
            else:
                comment_text = original[i + 1:line_end]
            if re.search(r'["\']', comment_text):
                return ValidatorResult('ask', 'Command contains quote characters inside a # comment which can desync quote tracking')
            if line_end == -1:
                break
            i = line_end
            continue

        i += 1

    return _PASSTHROUGH


def _validate_quoted_newline(ctx: _ValidationContext) -> ValidatorResult:
    """检测引号内的换行且下一行以 # 开头（可被注释剥离器隐藏）。

    stripCommentLines 按行处理不跟踪引号状态，引号内换行可以让攻击者
    把下一行伪装成注释被剥离。
    """
    original = ctx.original_command

    if '\n' not in original or '#' not in original:
        return _PASSTHROUGH

    in_single = False
    in_double = False
    escaped = False

    i = 0
    while i < len(original):
        char = original[i]

        if escaped:
            escaped = False
            i += 1
            continue

        if in_single:
            if char == "'":
                in_single = False
            elif char == '\n':
                # 检查下一行是否以 # 开头（trim 后）
                next_line_start = i + 1
                remaining = original[next_line_start:]
                next_line = remaining.split('\n', 1)[0] if remaining else ''
                if next_line.lstrip().startswith('#'):
                    return ValidatorResult('ask', 'Command contains a quoted newline followed by a # line which can hide content from validation')
            i += 1
            continue

        if char == '\\':
            escaped = True
            i += 1
            continue

        if in_double:
            if char == '"':
                in_double = False
            elif char == '\n':
                next_line_start = i + 1
                remaining = original[next_line_start:]
                next_line = remaining.split('\n', 1)[0] if remaining else ''
                if next_line.lstrip().startswith('#'):
                    return ValidatorResult('ask', 'Command contains a quoted newline followed by a # line which can hide content from validation')
            i += 1
            continue

        if char == "'":
            in_single = True
        elif char == '"':
            in_double = True

        i += 1

    return _PASSTHROUGH


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def classify_command(command: str) -> tuple[str, str]:
    """对命令执行多层独立安全验证。

    在 hardline / dangerous 正则检查之后调用。
    返回 (verdict, reason)：
      ("allow", reason)       — 命令安全
      ("ask", reason)         — 命令需要用户确认
      ("passthrough", reason) — 所有验证器均无意见
    """
    original = command

    # 预检查：控制字符
    if _CONTROL_CHAR_RE.search(original):
        return ('ask', 'Command contains non-printable control characters that could be used to bypass security checks')

    # 预检查：单引号反斜杠 bug
    if _has_shell_quote_single_quote_bug(original):
        return ('ask', 'Command contains single-quoted backslash pattern that could bypass security checks')

    base_command = original.split(' ')[0] if original else ''

    qe = _extract_quoted_content(original, is_jq=(base_command == 'jq'))
    ctx = _ValidationContext(original, base_command, qe)

    # 早期验证器（可 allow）
    early_validators = [
        _validate_empty,
        _validate_incomplete_commands,
        _validate_safe_command_substitution,
        _validate_git_commit,
    ]
    for validator in early_validators:
        result = validator(ctx)
        if result.behavior == 'allow':
            return ('allow', result.message)
        if result.behavior != 'passthrough':
            return ('ask', result.message)

    # 主验证器
    # 换行和重定向是"非误解析"验证器，延迟到其他验证器之后
    non_misparsing_validators = {_validate_newlines, _validate_redirections}
    main_validators = [
        _validate_jq_command,
        _validate_obfuscated_flags,
        _validate_shell_metacharacters,
        _validate_dangerous_variables,
        _validate_comment_quote_desync,
        _validate_quoted_newline,
        _validate_carriage_return,
        _validate_newlines,
        _validate_ifs_injection,
        _validate_proc_environ_access,
        _validate_dangerous_patterns,
        _validate_redirections,
        _validate_backslash_escaped_whitespace,
        _validate_backslash_escaped_operators,
        _validate_unicode_whitespace,
        _validate_mid_word_hash,
        _validate_brace_expansion,
        _validate_zsh_dangerous_commands,
        _validate_malformed_token_injection,
    ]

    deferred_result: ValidatorResult | None = None
    for validator in main_validators:
        result = validator(ctx)
        if result.behavior == 'ask':
            if validator in non_misparsing_validators:
                if deferred_result is None:
                    deferred_result = result
                continue
            return ('ask', result.message)

    if deferred_result is not None:
        return ('ask', deferred_result.message)

    return ('passthrough', 'Command passed all security checks')


