"""Feishu/Lark 渠道运行配置（从 PlatformConfig.extra + 环境变量解析）。

用于 gateway/platforms/feishu.py 的设置面：访问控制(group_policy 五档 /
allowlist / blacklist / admins / allow_bots / require_mention)、bot 身份、reactions、
dedup 持久化、文本分块。app_id/secret 既支持 extra 也支持环境变量(FEISHU_*)。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crew.state.home import get_crew_home
from crew.state.logging import get_logger

log = get_logger("platform.feishu")

# group_policy 取值（用于：open / allowlist / blacklist / admin_only / disabled）
_GROUP_POLICIES = {"open", "allowlist", "blacklist", "admin_only", "disabled"}
_ALLOW_BOTS = {"none", "mentions", "all"}

DEFAULT_TEXT_CHUNK_LIMIT = 4000          # 飞书单条文本上限较宽，留足余量分块
DEFAULT_DEDUP_TTL_S = 24 * 60 * 60.0     # 去重 TTL（用于 24h）
DEFAULT_DEDUP_MAX = 2048                 # 去重缓存条数上限
DEFAULT_MAX_FILE_BYTES = 30 * 1024 * 1024  # 入站资源下载上限


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_set(value: Any) -> set[str]:
    """逗号分隔字符串 或 列表 → 去空去重的字符串集合。"""
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip() for v in value if str(v).strip()}
    if isinstance(value, str):
        return {p.strip() for p in value.replace("\n", ",").split(",") if p.strip()}
    return set()


def _normalize_domain(raw: str) -> str:
    d = (raw or "feishu").strip().lower()
    return "https://open.larksuite.com" if d in ("lark", "larksuite") else "https://open.feishu.cn"


@dataclass
class FeishuSettings:
    app_id: str = ""
    app_secret: str = ""
    verification_token: str = ""
    domain: str = "https://open.feishu.cn"
    workspace_id: str = "default"
    # -- 访问控制 --
    group_policy: str = "open"
    allowed_users: set[str] = field(default_factory=set)
    blocked_users: set[str] = field(default_factory=set)
    admins: set[str] = field(default_factory=set)
    allow_bots: str = "none"
    require_mention: bool = True
    group_rules: dict[str, dict[str, Any]] = field(default_factory=dict)
    # -- bot 身份（提示值；启动时用 /bot/v3/info 校正，见 adapter）--
    bot_open_id: str = ""
    bot_user_id: str = ""
    bot_name: str = ""
    # -- 行为 --
    reactions_enabled: bool = True
    text_chunk_limit: int = DEFAULT_TEXT_CHUNK_LIMIT
    dedup_ttl_s: float = DEFAULT_DEDUP_TTL_S
    dedup_max: int = DEFAULT_DEDUP_MAX
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    file_dir: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_extra(cls, extra: dict[str, Any] | None, *, use_env: bool = True) -> "FeishuSettings":
        e = extra or {}

        def pick(camel: str, snake: str, env: str, default: Any = "") -> Any:
            """优先级：extra(camelCase) → extra(snake_case) → 环境变量 → 默认。"""
            for key in (camel, snake):
                if key in e and e[key] not in (None, ""):
                    return e[key]
            if not use_env:
                return default
            env_val = os.getenv(env, "")
            return env_val if env_val != "" else default

        group_policy = str(pick("groupPolicy", "group_policy", "FEISHU_GROUP_POLICY", "open") or "open").strip().lower()
        if group_policy not in _GROUP_POLICIES:
            log.warning("Feishu group_policy 非法值 %r，按 open 处理", group_policy)
            group_policy = "open"

        allow_bots = str(pick("allowBots", "allow_bots", "FEISHU_ALLOW_BOTS", "none") or "none").strip().lower()
        if allow_bots not in _ALLOW_BOTS:
            log.warning("Feishu allow_bots 非法值 %r，按 none 处理", allow_bots)
            allow_bots = "none"

        # 白/黑名单/管理员：extra 列表 或 env 逗号串
        allowed = _as_set(e.get("allowedUsers") or e.get("allowed_users"))
        blocked = _as_set(e.get("blockedUsers") or e.get("blocked_users"))
        admins = _as_set(e.get("admins"))
        if use_env:
            allowed |= _as_set(os.getenv("FEISHU_ALLOWED_USERS"))
            blocked |= _as_set(os.getenv("FEISHU_BLOCKED_USERS"))
            admins |= _as_set(os.getenv("FEISHU_ADMINS"))

        # 按 chat 覆盖规则：extra.groupRules = {chat_id: {policy, allowlist, blacklist, require_mention}}
        raw_rules = e.get("groupRules") or e.get("group_rules") or {}
        group_rules: dict[str, dict[str, Any]] = {}
        if isinstance(raw_rules, dict):
            for chat_id, rule in raw_rules.items():
                if not isinstance(rule, dict):
                    continue
                norm: dict[str, Any] = {}
                pol = str(rule.get("policy") or "").strip().lower()
                if pol in _GROUP_POLICIES:
                    norm["policy"] = pol
                if "allowlist" in rule:
                    norm["allowlist"] = _as_set(rule.get("allowlist"))
                if "blacklist" in rule:
                    norm["blacklist"] = _as_set(rule.get("blacklist"))
                if "require_mention" in rule or "requireMention" in rule:
                    norm["require_mention"] = _as_bool(rule.get("require_mention", rule.get("requireMention")), True)
                group_rules[str(chat_id)] = norm

        return cls(
            app_id=str(pick("appId", "app_id", "FEISHU_APP_ID") or "").strip(),
            app_secret=str(pick("appSecret", "app_secret", "FEISHU_APP_SECRET") or "").strip(),
            verification_token=str(pick("verificationToken", "verification_token", "FEISHU_VERIFICATION_TOKEN") or "").strip(),
            domain=_normalize_domain(str(pick("domain", "domain", "FEISHU_DOMAIN", "feishu"))),
            workspace_id=str(pick("workspaceId", "workspace_id", "FEISHU_WORKSPACE_ID", "default") or "default"),
            group_policy=group_policy,
            allowed_users=allowed,
            blocked_users=blocked,
            admins=admins,
            allow_bots=allow_bots,
            require_mention=_as_bool(pick("requireMention", "require_mention", "FEISHU_REQUIRE_MENTION", True), True),
            group_rules=group_rules,
            bot_open_id=str(pick("botOpenId", "bot_open_id", "FEISHU_BOT_OPEN_ID") or "").strip(),
            bot_user_id=str(pick("botUserId", "bot_user_id", "FEISHU_BOT_USER_ID") or "").strip(),
            bot_name=str(pick("botName", "bot_name", "FEISHU_BOT_NAME") or "").strip(),
            reactions_enabled=_as_bool(pick("reactions", "reactions", "FEISHU_REACTIONS", True), True),
            text_chunk_limit=_as_int(pick("textChunkLimit", "text_chunk_limit", "FEISHU_TEXT_CHUNK_LIMIT", DEFAULT_TEXT_CHUNK_LIMIT), DEFAULT_TEXT_CHUNK_LIMIT),
            max_file_bytes=_as_int(pick("maxFileBytes", "max_file_bytes", "FEISHU_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES), DEFAULT_MAX_FILE_BYTES),
            file_dir=str(pick("fileDir", "file_dir", "FEISHU_FILE_DIR") or "").strip(),
            extra=dict(e),
        )

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def files_dir(self) -> Path:
        if self.file_dir:
            return Path(self.file_dir)
        # 经 get_crew_home() 解析，避免随进程 cwd 漂移（打包/服务化下尤为重要）。
        return get_crew_home() / "tmp" / "feishu-files"

    def dedup_path(self) -> Path:
        """去重持久化文件（跨重启 TTL，用于 feishu_seen_message_ids.json）。"""
        return get_crew_home() / "tmp" / "feishu_seen_message_ids.json"

    def collect_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.group_policy == "allowlist" and not self.allowed_users:
            warnings.append("group_policy=allowlist 但 allowed_users 为空：群内将无人能触发机器人。")
        if self.group_policy == "blacklist" and not self.blocked_users:
            warnings.append("group_policy=blacklist 但 blocked_users 为空：等同于 open。")
        if self.allow_bots == "all":
            warnings.append("allow_bots=all：将响应其它机器人消息，注意避免机器人互相触发的回环。")
        return warnings
