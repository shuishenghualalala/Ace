"""微信（个人号 iLink 渠道）运行配置解析。

从 PlatformConfig.extra + 环境变量解析：账号凭证（account_id / token / base_url /
cdn_base_url）、访问控制（dm_policy 三档 + group_policy 三档 + 白名单）、文本分块与
批处理、去重持久化、媒体目录与大小限制。凭证既支持 extra 也支持环境变量（WEIXIN_*）。

account_id/token 是启动必需项：token 可来自 extra.token / config.token / WEIXIN_TOKEN /
已持久化的账号文件（account_id 命中时自动加载，见 adapter）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from crew.state.home import get_crew_home
from crew.state.logging import get_logger

log = get_logger("platform.weixin")

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# dm_policy 取值（私聊）：open / allowlist / disabled
_DM_POLICIES = {"open", "allowlist", "disabled"}
# group_policy 取值（群聊）：disabled / open / allowlist
_GROUP_POLICIES = {"disabled", "open", "allowlist"}

DEFAULT_TEXT_CHUNK_LIMIT = 2000          # iLink 单条文本上限约 2048 字符，留余量
DEFAULT_DEDUP_TTL_S = 300.0              # 消息去重 TTL（5 分钟，长轮询幂等）
DEFAULT_DEDUP_MAX = 4096                 # 去重缓存条数上限
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


def _as_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0:
        return default
    return parsed


def _as_set(value: Any) -> set[str]:
    """逗号分隔字符串 或 列表 → 去空去重的字符串集合。"""
    if isinstance(value, (list, tuple, set)):
        return {str(v).strip() for v in value if str(v).strip()}
    if isinstance(value, str):
        return {p.strip() for p in value.replace("\n", ",").split(",") if p.strip()}
    return set()


@dataclass
class WeixinSettings:
    account_id: str = ""
    token: str = ""
    base_url: str = ILINK_BASE_URL
    cdn_base_url: str = WEIXIN_CDN_BASE_URL
    workspace_id: str = "default"
    # -- 访问控制 --
    dm_policy: str = "open"
    group_policy: str = "disabled"
    allowed_users: set[str] = field(default_factory=set)
    group_allowed_users: set[str] = field(default_factory=set)
    # -- 行为 --
    text_chunk_limit: int = DEFAULT_TEXT_CHUNK_LIMIT
    send_chunk_delay_seconds: float = 1.5
    send_chunk_retries: int = 4
    send_chunk_retry_delay_seconds: float = 1.0
    split_multiline_messages: bool = False
    text_batch_delay_seconds: float = 3.0
    text_batch_split_delay_seconds: float = 5.0
    dedup_ttl_s: float = DEFAULT_DEDUP_TTL_S
    dedup_max: int = DEFAULT_DEDUP_MAX
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    file_dir: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_extra(cls, extra: dict[str, Any] | None, *, use_env: bool = True) -> WeixinSettings:
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

        dm_policy = str(pick("dmPolicy", "dm_policy", "WEIXIN_DM_POLICY", "open") or "open").strip().lower()
        if dm_policy not in _DM_POLICIES:
            log.warning("Weixin dm_policy 非法值 %r，按 open 处理", dm_policy)
            dm_policy = "open"

        group_policy = str(pick("groupPolicy", "group_policy", "WEIXIN_GROUP_POLICY", "disabled") or "disabled").strip().lower()
        if group_policy not in _GROUP_POLICIES:
            log.warning("Weixin group_policy 非法值 %r，按 disabled 处理", group_policy)
            group_policy = "disabled"

        allowed = _as_set(e.get("allowedUsers") or e.get("allowed_users"))
        group_allowed = _as_set(e.get("groupAllowedUsers") or e.get("group_allowed_users"))
        if use_env:
            allowed |= _as_set(os.getenv("WEIXIN_ALLOWED_USERS"))
            group_allowed |= _as_set(os.getenv("WEIXIN_GROUP_ALLOWED_USERS"))

        base_url = str(pick("baseUrl", "base_url", "WEIXIN_BASE_URL", ILINK_BASE_URL) or ILINK_BASE_URL).strip().rstrip("/") or ILINK_BASE_URL
        cdn_base_url = str(
            pick("cdnBaseUrl", "cdn_base_url", "WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL) or WEIXIN_CDN_BASE_URL
        ).strip().rstrip("/") or WEIXIN_CDN_BASE_URL

        return cls(
            account_id=str(pick("accountId", "account_id", "WEIXIN_ACCOUNT_ID") or "").strip(),
            token=str(pick("token", "token", "WEIXIN_TOKEN") or "").strip(),
            base_url=base_url,
            cdn_base_url=cdn_base_url,
            workspace_id=str(pick("workspaceId", "workspace_id", "WEIXIN_WORKSPACE_ID", "default") or "default"),
            dm_policy=dm_policy,
            group_policy=group_policy,
            allowed_users=allowed,
            group_allowed_users=group_allowed,
            text_chunk_limit=_as_int(
                pick("textChunkLimit", "text_chunk_limit", "WEIXIN_TEXT_CHUNK_LIMIT", DEFAULT_TEXT_CHUNK_LIMIT),
                DEFAULT_TEXT_CHUNK_LIMIT,
            ),
            send_chunk_delay_seconds=_as_float(
                pick("sendChunkDelaySeconds", "send_chunk_delay_seconds", "WEIXIN_SEND_CHUNK_DELAY_SECONDS", 1.5),
                1.5,
            ),
            send_chunk_retries=_as_int(
                pick("sendChunkRetries", "send_chunk_retries", "WEIXIN_SEND_CHUNK_RETRIES", 4),
                4,
            ),
            send_chunk_retry_delay_seconds=_as_float(
                pick("sendChunkRetryDelaySeconds", "send_chunk_retry_delay_seconds", "WEIXIN_SEND_CHUNK_RETRY_DELAY_SECONDS", 1.0),
                1.0,
            ),
            split_multiline_messages=_as_bool(
                pick("splitMultilineMessages", "split_multiline_messages", "WEIXIN_SPLIT_MULTILINE_MESSAGES"),
                False,
            ),
            text_batch_delay_seconds=_as_float(
                pick("textBatchDelaySeconds", "text_batch_delay_seconds", "WEIXIN_TEXT_BATCH_DELAY_SECONDS", 3.0),
                3.0,
            ),
            text_batch_split_delay_seconds=_as_float(
                pick("textBatchSplitDelaySeconds", "text_batch_split_delay_seconds", "WEIXIN_TEXT_BATCH_SPLIT_DELAY_SECONDS", 5.0),
                5.0,
            ),
            dedup_ttl_s=_as_float(pick("dedupTtlS", "dedup_ttl_s", "WEIXIN_DEDUP_TTL_S", DEFAULT_DEDUP_TTL_S), DEFAULT_DEDUP_TTL_S),
            dedup_max=_as_int(pick("dedupMax", "dedup_max", "WEIXIN_DEDUP_MAX", DEFAULT_DEDUP_MAX), DEFAULT_DEDUP_MAX),
            max_file_bytes=_as_int(
                pick("maxFileBytes", "max_file_bytes", "WEIXIN_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES),
                DEFAULT_MAX_FILE_BYTES,
            ),
            file_dir=str(pick("fileDir", "file_dir", "WEIXIN_FILE_DIR") or "").strip(),
            extra=dict(e),
        )

    @property
    def configured(self) -> bool:
        return bool(self.account_id and self.token)

    def files_dir(self) -> Path:
        if self.file_dir:
            return Path(self.file_dir)
        return get_crew_home() / "tmp" / "weixin-files"

    def accounts_dir(self) -> Path:
        """扫码登录持久化的账号凭证目录。"""
        return get_crew_home() / "weixin" / "accounts"

    def dedup_path(self) -> Path:
        """去重持久化文件（跨重启 TTL）。"""
        return get_crew_home() / "tmp" / "weixin_seen_message_ids.json"

    def sync_buf_path(self) -> Path:
        """长轮询 get_updates_buf 持久化（断线续拉）。"""
        return get_crew_home() / "tmp" / "weixin_sync_buf.json"

    def collect_warnings(self) -> list[str]:
        warnings: list[str] = []
        if self.group_policy != "disabled":
            warnings.append(
                "WEIXIN_GROUP_POLICY 已开启，但扫码登录连接的是 iLink 机器人身份，"
                "普通微信群事件通常不会推送给机器人，群消息可能永远到不了。"
                "若群消息不生效，属于 iLink 侧限制，而非本渠道代码问题。"
            )
        if self.dm_policy == "allowlist" and not self.allowed_users:
            warnings.append("dm_policy=allowlist 但 allowed_users 为空：将无人能触发机器人。")
        return warnings


def decide_access(
    *,
    sender_id: str,
    account_id: str,
    chat_type: str,
    chat_id: str,
    settings: WeixinSettings,
) -> tuple[bool, str]:
    """入站访问控制门禁。返回 (是否放行, 原因)。"""
    if not sender_id or sender_id == account_id:
        return False, "self-echo"
    if chat_type == "group":
        if settings.group_policy == "disabled":
            return False, "group-disabled"
        if settings.group_policy == "allowlist" and chat_id not in settings.group_allowed_users:
            return False, "group-not-allowlisted"
        return True, ""
    if settings.dm_policy == "disabled":
        return False, "dm-disabled"
    if settings.dm_policy == "allowlist" and sender_id not in settings.allowed_users:
        return False, "dm-not-allowlisted"
    return True, ""
