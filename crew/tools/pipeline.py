"""工具执行流水线的三个纯函数阶段。

Crew 的执行链路在 ``crew/agent/loop/tool_runner.py`` 与 ``crew/tools/registry.py``
里已经串起了 Stage 3（pre hook / guardrail）、Stage 5（执行）、Stage 7（post hook）、
Stage 8（消息发射）。本模块补齐 Crew 缺的三个阶段：

  - Stage 2 输入验证：``validate_arguments`` —— JSON Schema 强制 + 业务 validate 钩子
  - Stage 4 权限检查：``check_permission`` —— allow/deny/ask 规则匹配 + 交互确认
  - Stage 6 结果处理：``truncate_or_persist`` —— 大结果落盘 + 路径回灌

三者都是纯函数（check_permission 的 ask 分支由调用方走 followup 交互），不持有可变
全局状态，便于单测；唯一的进程内状态是「会话级始终允许」规则缓存，按 session_id 隔离，
对应 Crew ``session`` 权限来源（用户在弹窗里选「始终允许」时生成）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from crew.core.types import ToolCall
from crew.tools.redact import safe_public_error
from crew.state.home import get_crew_home
from crew.state.logging import get_logger

log = get_logger("tools.pipeline")

# 工具结果默认最大字符数。
DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000
# 结果预览的头/尾长度。
_RESULT_PREVIEW_HEAD = 4000
_RESULT_PREVIEW_TAIL = 4000


# --------------------------------------------------------------------------- #
# Stage 2：输入验证
# --------------------------------------------------------------------------- #
def validate_arguments(
    tool_name: str,
    parameters: dict[str, Any],
    args: Any,
) -> str | None:
    """按工具声明的 JSON Schema 校验 args。

    Returns:
        ``None`` 表示校验通过；否则返回格式化错误串（调用方包成 tool_use_error 回灌）。
        遵循 Crew「错误是数据不是异常」——校验失败不抛异常。
    """
    if not isinstance(args, dict):
        return _format_schema_error(
            tool_name, f"工具参数必须是对象，收到 {type(args).__name__}"
        )

    if not parameters:
        # 工具未声明 schema（兜底）：不做结构校验，交给业务层
        return None

    try:
        import jsonschema

        validator = jsonschema.Draft202012Validator(parameters)
        errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
    except Exception as exc:  # noqa: BLE001 - schema 本身非法时降级，不阻塞执行
        log.debug("工具 %s schema 校验器构造失败，跳过校验: %s", tool_name, exc)
        return None

    if not errors:
        return None

    lines = [f"工具 `{tool_name}` 参数校验失败："]
    for err in errors[:8]:
        loc = ".".join(str(p) for p in err.absolute_path) or "(root)"
        msg = err.message or "(校验失败)"
        lines.append(f"- {loc}: {msg}")
    if len(errors) > 8:
        lines.append(f"- ...还有 {len(errors) - 8} 个错误")
    return "\n".join(lines)


def _format_schema_error(tool_name: str, message: str) -> str:
    return f"工具 `{tool_name}` 参数校验失败：\n- {message}"


# --------------------------------------------------------------------------- #
# Stage 6：大结果处理
# --------------------------------------------------------------------------- #
def truncate_or_persist(
    tool_call_id: str,
    tool_name: str,
    content: str,
    *,
    max_chars: int = DEFAULT_MAX_RESULT_SIZE_CHARS,
) -> str:
    """结果超过 max_chars 时落盘，返回「路径 + 头尾预览 + 截断标记」。

    完整内容写到
    ``{CREW_HOME}/tool-results/{tool_call_id}.txt``，模型收到路径与预览，
    需要完整数据时由 file_read 按需拉取（on-demand read pattern）。

    非 str 内容原样返回（理论不会出现——registry.execute 已规整成 str）。
    """
    if not isinstance(content, str):
        return content
    if len(content) <= max_chars:
        return content
    if max_chars <= 0:
        return content

    try:
        path = _persist_tool_result(tool_call_id, content)
    except Exception as exc:  # noqa: BLE001 - 落盘失败降级为就地截断，不丢整段
        log.warning("工具 %s 大结果落盘失败，就地截断: %s", tool_name, exc)
        return _inline_truncate(content, max_chars)

    head = content[:_RESULT_PREVIEW_HEAD].rstrip()
    tail = content[-_RESULT_PREVIEW_TAIL:].lstrip() if _RESULT_PREVIEW_TAIL else ""
    truncated = len(content) - _RESULT_PREVIEW_HEAD - _RESULT_PREVIEW_TAIL
    return (
        f"<truncated>结果共 {len(content)} 字符，已超过 {max_chars} 上限，"
        f"完整内容已保存到：\n{path}\n</truncated>\n\n"
        f"--- 预览（开头 {_RESULT_PREVIEW_HEAD} 字符）---\n{head}\n"
        f"--- 省略 {max(truncated, 0)} 字符 ---\n"
        f"--- 预览（结尾 {_RESULT_PREVIEW_TAIL} 字符）---\n{tail}"
    )


def _inline_truncate(content: str, max_chars: int) -> str:
    """落盘失败时的兜底：保留头尾，中间插截断标记（不丢首尾信息量）。"""
    if len(content) <= max_chars:
        return content
    keep = max(max_chars // 2, 1)
    return (
        content[:keep]
        + f"\n<truncated>…省略 {len(content) - keep * 2} 字符…</truncated>\n"
        + content[-keep:]
    )


def _persist_tool_result(tool_call_id: str, content: str) -> Path:
    """把完整结果原子写入 {CREW_HOME}/tool-results/{id}.txt，返回路径。"""
    safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(tool_call_id or "result"))[:128]
    if not safe_id:
        safe_id = "result"
    out_dir = get_crew_home() / "tool-results"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{safe_id}.txt"
    # 先写临时文件再 rename，避免并发/中断读到半截内容
    tmp = out_dir / f".{safe_id}.{_random_suffix()}.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return path


def _random_suffix() -> str:
    # 不用 secrets/uuid 以保持纯 stdlib 且可测；时间戳+pid 足够避免并发碰撞
    import os
    import time

    return f"{int(time.monotonic() * 1e6):x}{os.getpid() & 0xffff:x}"


# --------------------------------------------------------------------------- #
# Stage 4：权限规则
# --------------------------------------------------------------------------- #
@dataclass
class PermissionRule:
    """一条权限规则。

    规则匹配支持三种模式：
      - 精确：``match == "git push"`` 仅匹配完全相同
      - 前缀：``match == "git commit:*"`` 或 ``"git *"`` 匹配以 prefix 开头
      - 通配：``match == "*"`` 匹配任意（blanket）
    """

    tool: str
    match: str
    behavior: str  # "allow" | "deny" | "ask"
    reason: str = ""

    def matches(self, tool_name: str, key: str) -> bool:
        if self.tool != "*" and self.tool != tool_name:
            return False
        pattern = self.match
        if pattern == "*" or pattern == "":
            return True
        if pattern.startswith("sha256:"):
            expected = pattern.removeprefix("sha256:")
            actual = hashlib.sha256(key.encode("utf-8")).hexdigest()
            return len(expected) == 64 and hmac.compare_digest(actual, expected)
        if pattern.endswith(":*"):
            prefix = pattern[:-2]
            return _key_startswith(key, prefix)
        if pattern.endswith(" *"):
            prefix = pattern[:-2]
            return _key_startswith(key, prefix)
        return key == pattern


def _key_startswith(key: str, prefix: str) -> bool:
    """前缀匹配：key 本身等于 prefix，或 key 以 prefix+空白 开头。"""
    if key == prefix:
        return True
    return key.startswith(prefix + " ") or key.startswith(prefix + "\t")


@dataclass
class PermissionConfig:
    """权限规则集合 + 会话级「始终允许」缓存。

    规则来源：
      config 规则（userSettings/projectSettings 合并）→ 进程内 session allows
    access_control（toolset 级开关）在更早的 Stage 1（工具是否暴露给模型）生效，
    与本层正交：本层只管「已暴露的工具调用是否放行」。
    """

    rules: list[PermissionRule] = field(default_factory=list)
    # session_id -> rules；用户在 ask 弹窗选「始终允许」时动态追加
    _session_allows: dict[str, list[PermissionRule]] = field(default_factory=dict)

    def add_session_allow(self, session_id: str, rule: PermissionRule) -> None:
        self._session_allows.setdefault(session_id, []).append(rule)

    def all_rules_for(self, session_id: str) -> list[PermissionRule]:
        return [*self._session_allows.get(session_id, []), *self.rules]

    def check(
        self,
        tool_name: str,
        key: str,
        session_id: str = "",
        *,
        default_behavior: str = "allow",
    ) -> tuple[str, str, str]:
        """返回 (behavior, reason, suggested_rule)。

        behavior: ``allow`` | ``deny`` | ``ask`` | default_behavior（默认无规则）
        suggested_rule: 仅 ask 时给出，供弹窗展示「保存为规则」选项
        """
        if default_behavior not in ("allow", "deny", "ask"):
            default_behavior = "deny"
        # Persistent deny is a ceiling: an older session approval cannot
        # override a newly installed administrative/project denial.
        matched_deny = None
        matched_ask = None
        matched_allow = None
        for rule in self.rules:
            if not rule.matches(tool_name, key):
                continue
            if rule.behavior == "deny":
                matched_deny = rule
            elif rule.behavior == "ask":
                matched_ask = rule
            elif rule.behavior == "allow":
                matched_allow = rule
        if matched_deny:
            return ("deny", matched_deny.reason or f"匹配拒绝规则: {matched_deny.match}", "")
        # Session approvals may override an ask rule, but never a deny ceiling.
        for rule in self._session_allows.get(session_id, []):
            if rule.matches(tool_name, key):
                return ("allow", rule.reason or "本会话已授权", "")
        if matched_ask:
            return ("ask", matched_ask.reason, _suggest_rule(tool_name, key))
        if matched_allow:
            return ("allow", matched_allow.reason, "")
        # 无规则时由调用方决定；未知值在上面已 fail-closed 为 deny。
        return (
            default_behavior,
            "",
            _suggest_rule(tool_name, key) if default_behavior == "ask" else "",
        )


def _suggest_rule(tool_name: str, key: str) -> str:
    """从命令/路径里提取一个稳定的 2 段前缀，作为「始终允许」建议规则。"""
    if tool_name == "terminal":
        parts = key.split()
        if len(parts) >= 2 and parts[0] not in {"bash", "sh", "sudo", "env", "source", "."}:
            return f"{parts[0]} {parts[1]}:*"
        if parts:
            return f"{parts[0]}:*"
        return "*"
    if tool_name in {"file_write", "file_read", "patch"}:
        # 路径不便于生成稳定前缀规则，回退到精确
        return key or "*"
    if "__" in tool_name and key:
        # MCP names are qualified as server__tool. Persist only a digest so
        # one approval cannot widen to different arguments or retain secrets.
        return f"sha256:{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    return "*"


# 按工具提取「匹配键」。
_PRIMARY_ARG: dict[str, str] = {
    "terminal": "command",
    "file_write": "path",
    "file_read": "path",
    "patch": "path",
    "glob": "pattern",
    "grep": "pattern",
    # record_replay.inputs may contain passwords, identifiers, and selected
    # values. Permission matching/cards bind only the opaque workflow identity;
    # the replay resolver separately binds a digest of the complete arguments.
    "record_replay": "workflow_id",
}


def extract_match_key(tool_name: str, args: dict[str, Any]) -> str:
    """提取用于规则匹配的稳定键。不同工具的「主参数」不同。"""
    primary = _PRIMARY_ARG.get(tool_name)
    if primary:
        return str(args.get(primary, "") or "")
    return json.dumps(args, ensure_ascii=False, sort_keys=True)


def load_permission_config(raw: Any) -> PermissionConfig:
    """从 config.yaml 的 tools.permissions 解析成 PermissionConfig。

    容错：单条非法规则跳过并告警，不整体失败。
    """
    cfg = PermissionConfig()
    if not isinstance(raw, list):
        return cfg
    for item in raw:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        match = str(item.get("match") or "*").strip() or "*"
        behavior = str(item.get("behavior") or "ask").strip().lower()
        if behavior not in {"allow", "deny", "ask"}:
            log.warning("权限规则 behavior 非法 %r，按 ask 处理: %s", behavior, item)
            behavior = "ask"
        if not tool:
            log.warning("权限规则缺少 tool 字段，跳过: %s", item)
            continue
        cfg.rules.append(
            PermissionRule(tool=tool, match=match, behavior=behavior,
                           reason=str(item.get("reason") or ""))
        )
    return cfg


# --------------------------------------------------------------------------- #
# 进程内 PermissionConfig 单例 + session allows 注册表
# --------------------------------------------------------------------------- #
_CONFIG: PermissionConfig | None = None


def get_permission_config() -> PermissionConfig:
    """懒加载 config.yaml 里的 tools.permissions，进程内缓存。"""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    try:
        from crew.state.config import load_config

        cfg = load_config()
        raw = cfg.raw_config.get("tools", {}).get("permissions", [])
        _CONFIG = load_permission_config(raw)
    except Exception as exc:  # noqa: BLE001 - 配置缺失时退化为「全 allow」，不阻塞
        log.warning("权限配置加载失败，按空规则处理: %s", exc)
        _CONFIG = PermissionConfig()
    return _CONFIG


def reset_permission_config() -> None:
    """测试用：清空进程内缓存与 session allows。"""
    global _CONFIG
    _CONFIG = None


def check_permission(
    tool_name: str,
    args: dict[str, Any],
    *,
    session_id: str = "",
    config: PermissionConfig | None = None,
    default_behavior: str = "allow",
) -> tuple[str, str, str]:
    """Stage 4 权限检查入口。返回 (behavior, reason, suggested_rule)。"""
    cfg = config if config is not None else get_permission_config()
    key = extract_match_key(tool_name, args)
    return cfg.check(
        tool_name,
        key,
        session_id=session_id,
        default_behavior=default_behavior,
    )


def grant_session_allow(
    session_id: str,
    tool_name: str,
    match: str,
    *,
    reason: str = "用户选择始终允许",
) -> None:
    """用户在 ask 弹窗选「始终允许」时调用：写一条 session 级 allow 规则。"""
    cfg = get_permission_config()
    cfg.add_session_allow(session_id, PermissionRule(
        tool=tool_name, match=match, behavior="allow", reason=reason,
    ))


def validate_input_hook(
    tool: Any, args: dict[str, Any]
) -> str | None:
    """调用工具自带的业务级 validate_input 钩子。

    工具可在注册时传 ``validate_input``（``args -> str | None``），
    返回非空串表示业务拒绝（如 FileEdit 检查文件是否已读过）。
    """
    fn: Callable[[dict[str, Any]], str | None] | None = getattr(tool, "validate_input", None)
    if not callable(fn):
        return None
    try:
        msg = fn(args)
    except Exception as exc:  # noqa: BLE001
        return safe_public_error(exc, "业务校验异常")
    return msg if msg else None


def should_block_for_tool_call(tc: ToolCall) -> bool:
    """供 guardrail/plan 复用：判断该 ToolCall 是否需要权限检查（写类工具）。

    只读工具默认 allow，不进 ask 流程，避免对 file_read/grep 等频繁打扰。
    """
    return tc.name in {"terminal", "file_write", "patch"}
