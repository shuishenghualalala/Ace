"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
from typing import Any


# 写文件工具及可选别名；未注册的别名不会命中。
FILE_MUTATING_TOOL_NAMES = frozenset({"file_write", "write_file", "patch"})


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name in ("write_file", "file_write"):
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
