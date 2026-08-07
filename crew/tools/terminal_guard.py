"""终端命令安全检测（对齐 Hermes tools/approval.py 核心模式）。

本模块只保留模式匹配部分，剥离 Hermes 的审批状态机、LLM 智能审批、持久化 allowlist 等。
Crew 当前策略：
- hardline：无条件阻止（rm -rf /、mkfs、dd 到块设备、fork bomb、shutdown 等）。
- dangerous：返回宿主批准需求；模型参数不能授权。
"""

from __future__ import annotations

import re
import unicodedata


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
    # Windows / PowerShell system-destructive commands. These are the direct
    # equivalents of the Unix hardline set: irreversible volume/disk destruction
    # and system power state. Refusing them here gives Windows parity with the
    # Unix ``mkfs``/``dd``/``shutdown`` hardlines instead of leaving PowerShell
    # exempt just because the regex above was Unix-only.
    (r'\bformat-volume\b', "format filesystem (Format-Volume)"),
    (r'\bclear-disk\b', "clear disk (Clear-Disk)"),
    (r'\bdiskpart\b', "raw disk partition tool (diskpart)"),
    (r'\bstop-computer\b', "system shutdown (Stop-Computer)"),
    (r'\brestart-computer\b', "system reboot (Restart-Computer)"),
    (r'\bshutdown\b[^\n]*\b/[sr]\b', "system shutdown/reboot (shutdown.exe /s|/r)"),
    (r'\bformat\b[^\n]*[a-z]:[\\/][^\n]*\b/q\b', "format Windows drive (format /q)"),
    # Recursive delete of a Windows system directory (C:\Windows, Program Files,
    # System32, Users) — parity with ``rm -rf /``-style Unix hardlines.
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
    (r'\bDROP\s+(TABLE|DATABASE)\b', "SQL DROP"),
    (r'\bDELETE\s+FROM\b(?![^\n]*\bWHERE\b)', "SQL DELETE without WHERE"),
    (r'\bTRUNCATE\s+(TABLE)?\s*\w', "SQL TRUNCATE"),
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
    # Windows / PowerShell dangerous actions that need host approval — parity with
    # the Unix dangerous set (recursive delete, shell -c, remote-pipe-to-shell).
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
    # ANSI escape sequences
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
