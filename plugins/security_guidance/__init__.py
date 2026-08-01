"""Security guidance plugin.

Crew Crew plugin that scans content written by file tools and
adds a warning to the tool result when common unsafe patterns are found.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

_MAX_SCAN_BYTES = 500_000
_DOC_EXTS = (".md", ".mdx", ".txt", ".rst", ".json", ".yaml", ".yml")
_PY_EXTS = (".py", ".pyi")
_JS_EXTS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte")


def _path_ends(path: str, exts: tuple[str, ...]) -> bool:
    return path.lower().endswith(exts)


_RULES = [
    {
        "name": "eval_injection",
        "regex": re.compile(r"(?<![a-zA-Z0-9_\.])eval\s*\("),
        "path_filter": lambda path: not _path_ends(path, _DOC_EXTS),
        "reminder": "Security warning: eval() executes code. Prefer JSON parsing, ast.literal_eval, or a safe expression parser.",
    },
    {
        "name": "python_subprocess_shell",
        "regex": re.compile(r"subprocess\.(?:run|call|Popen|check_output|check_call)\(.*shell\s*=\s*True", re.S),
        "path_filter": lambda path: not path or _path_ends(path, _PY_EXTS),
        "reminder": "Security warning: subprocess with shell=True can enable command injection. Prefer argument lists without a shell.",
    },
    {
        "name": "os_system_injection",
        "regex": re.compile(r"\bos\.system\s*\("),
        "path_filter": lambda path: not path or _path_ends(path, _PY_EXTS),
        "reminder": "Security warning: os.system() runs through a shell. Prefer subprocess.run([...], shell=False).",
    },
    {
        "name": "unsafe_yaml_load",
        "regex": re.compile(r"\byaml\.(?:load|unsafe_load)\s*\((?![^)\n]{0,100}\bSafe)"),
        "reminder": "Security warning: yaml.load()/unsafe_load() can construct arbitrary objects. Prefer yaml.safe_load().",
    },
    {
        "name": "pickle_deserialization",
        "regex": re.compile(r"\b(?:pickle|cPickle|cloudpickle|dill)\.(?:load|loads)\s*\("),
        "path_filter": lambda path: not path or _path_ends(path, _PY_EXTS),
        "reminder": "Security warning: pickle-style deserialization can execute code on untrusted input. Prefer JSON or schema-validated data.",
    },
    {
        "name": "react_dangerously_set_html",
        "substring": "dangerouslySetInnerHTML",
        "path_filter": lambda path: not path or _path_ends(path, _JS_EXTS),
        "reminder": "Security warning: dangerouslySetInnerHTML can cause XSS. Sanitize HTML or use safe rendering paths.",
    },
    {
        "name": "inner_html_xss",
        "regex": re.compile(r"\.(?:innerHTML|outerHTML)\s*="),
        "path_filter": lambda path: not path or _path_ends(path, _JS_EXTS),
        "reminder": "Security warning: assigning innerHTML/outerHTML can cause XSS. Prefer textContent or sanitized HTML.",
    },
    {
        "name": "tls_verification_disabled",
        "regex": re.compile(r"\bverify\s*=\s*False\b|rejectUnauthorized\s*:\s*false|InsecureSkipVerify\s*:\s*true|NODE_TLS_REJECT_UNAUTHORIZED\s*=\s*['\"]?0"),
        "reminder": "Security warning: disabling TLS verification allows man-in-the-middle attacks. Add a trusted CA instead.",
    },
]


def _disabled() -> bool:
    return os.getenv("CREW_SECURITY_GUIDANCE_DISABLE", "").lower() in {"1", "true", "yes", "on"}


def _block_mode() -> bool:
    return os.getenv("CREW_SECURITY_GUIDANCE_BLOCK", "").lower() in {"1", "true", "yes", "on"}


def _extract_targets(tool_name: str, args: Any) -> list[tuple[str, str]]:
    if not isinstance(args, dict):
        return []
    if tool_name == "file_write":
        path = str(args.get("path") or "")
        content = args.get("content")
        return [(path, content)] if isinstance(content, str) else []
    if tool_name == "patch":
        path = str(args.get("path") or "")
        targets = []
        for key in ("new", "new_string", "patch"):
            content = args.get(key)
            if isinstance(content, str):
                targets.append((path, content))
        return targets
    return []


def _scan_content(path: str, content: str) -> list[tuple[str, str]]:
    if not content or len(content.encode("utf-8", errors="ignore")) > _MAX_SCAN_BYTES:
        return []
    findings: list[tuple[str, str]] = []
    for rule in _RULES:
        path_filter = rule.get("path_filter")
        if path_filter is not None and not path_filter(path):
            continue
        matched = False
        substring = rule.get("substring")
        if isinstance(substring, str) and substring in content:
            matched = True
        regex = rule.get("regex")
        if regex is not None and regex.search(content):
            matched = True
        if matched:
            findings.append((str(rule["name"]), str(rule["reminder"])))
    return findings


def _scan_args(tool_name: str, args: Any) -> list[tuple[str, str]]:
    if _disabled():
        return []
    findings: list[tuple[str, str]] = []
    for path, content in _extract_targets(tool_name, args):
        findings.extend(_scan_content(path, content))
    return findings


def _format_warning(findings: list[tuple[str, str]]) -> str:
    names = ", ".join(name for name, _ in findings)
    lines = [
        "",
        "---",
        f"Security guidance: {len(findings)} unsafe pattern(s) matched ({names}).",
        "",
    ]
    for _, reminder in findings:
        lines.append(f"- {reminder}")
    lines.append("")
    lines.append("Pattern matches can be false positives; fix the code or briefly document why the construct is safe.")
    return "\n".join(lines)


def _on_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, str] | None:
    if not _block_mode():
        return None
    findings = _scan_args(tool_name, args)
    if not findings:
        return None
    return {
        "action": "block",
        "message": "security_guidance blocked this write:\n" + _format_warning(findings),
    }


def _on_transform_tool_result(tool_name: str = "", args: Any = None, result: Any = None, **_: Any) -> str | None:
    if _block_mode():
        return None
    findings = _scan_args(tool_name, args)
    if not findings or not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict) and parsed.get("error"):
            return None
    except (TypeError, ValueError):
        pass
    return result + "\n\n" + _format_warning(findings)


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
    ctx.register_hook("transform_tool_result", _on_transform_tool_result)
