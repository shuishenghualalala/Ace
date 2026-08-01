"""飞书访问控制（用于 gateway/platforms/feishu.py 的 _allow_* 链路）。

判定顺序（decide）：
  1. 自回声 → 拒（机器人自己发的消息）
  2. 机器人发送者 → 按 allow_bots(none/mentions/all)
  3. 私聊(p2p) → allowed_users 非空时仅放行白名单/管理员
  4. 群聊 → 按 group_policy(open/allowlist/blacklist/admin_only/disabled)，管理员绕过策略
  5. require_mention（群聊，最后统一)：未 @机器人 → 拒

身份匹配：发送者的 open_id/user_id/union_id 任一命中名单集合即算命中。
@机器人 匹配：mentions 中命中 bot 的 open_id/user_id/name，或 @所有人；bot 身份完全
未知时退化为"存在任意 @"以免完全失声。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BotIdentity:
    open_id: str = ""
    user_id: str = ""
    name: str = ""

    @property
    def known(self) -> bool:
        return bool(self.open_id or self.user_id or self.name)


def sender_ids(parsed: dict[str, Any]) -> set[str]:
    return {parsed.get("sender_open_id", ""), parsed.get("sender_user_id", ""),
            parsed.get("sender_union_id", "")} - {""}


def _hits(ids: set[str], names: set[str]) -> bool:
    return bool(ids & names)


def is_self_message(parsed: dict[str, Any], bot: BotIdentity) -> bool:
    ids = sender_ids(parsed)
    return (bool(bot.open_id) and bot.open_id in ids) or (bool(bot.user_id) and bot.user_id in ids)


def is_bot_sender(parsed: dict[str, Any]) -> bool:
    return str(parsed.get("sender_type") or "user").lower() not in ("user", "")


def mentions_self(parsed: dict[str, Any], bot: BotIdentity) -> bool:
    mentions = parsed.get("mentions") or []
    if not mentions:
        return False
    for m in mentions:
        if m.get("is_all"):
            return True
        if bot.open_id and m.get("open_id") == bot.open_id:
            return True
        if bot.user_id and m.get("user_id") == bot.user_id:
            return True
        if bot.name and m.get("name") == bot.name:
            return True
    # bot 身份完全未知 → 退化：有 @ 即视为可能 @ 机器人（避免完全失声）
    return not bot.known


def _effective_group_rule(parsed: dict[str, Any], settings: Any) -> dict[str, Any]:
    """合并默认与按 chat 覆盖，返回 {policy, allowlist, blacklist, require_mention}。"""
    rule = {
        "policy": settings.group_policy,
        "allowlist": settings.allowed_users,
        "blacklist": settings.blocked_users,
        "require_mention": settings.require_mention,
    }
    override = (settings.group_rules or {}).get(parsed.get("chat_id", ""))
    if override:
        rule.update({k: v for k, v in override.items() if v is not None})
    return rule


def decide(parsed: dict[str, Any], settings: Any, bot: BotIdentity) -> tuple[bool, str]:
    """返回 (是否放行, 原因)。原因仅用于调试日志。"""
    # 1. 自回声
    if is_self_message(parsed, bot):
        return False, "self"

    # 2. 机器人发送者
    if is_bot_sender(parsed):
        if settings.allow_bots == "none":
            return False, "bot-sender"
        if settings.allow_bots == "mentions" and not mentions_self(parsed, bot):
            return False, "bot-sender-no-mention"
        # allow_bots == "all" → 继续后续校验

    ids = sender_ids(parsed)
    is_admin = _hits(ids, settings.admins)
    chat_type = parsed.get("chat_type", "p2p")

    # 3. 私聊
    if chat_type != "group":
        if settings.allowed_users and not (is_admin or _hits(ids, settings.allowed_users)):
            return False, "dm-not-allowlisted"
        return True, "dm"

    # 4. 群聊策略
    rule = _effective_group_rule(parsed, settings)
    policy = rule["policy"]
    if not is_admin:
        if policy == "disabled":
            return False, "group-disabled"
        if policy == "admin_only":
            return False, "group-admin-only"
        if policy == "allowlist" and not _hits(ids, rule["allowlist"]):
            return False, "group-not-allowlisted"
        if policy == "blacklist" and _hits(ids, rule["blacklist"]):
            return False, "group-blacklisted"

    # 5. require_mention（群聊，最后统一）
    if rule["require_mention"] and not mentions_self(parsed, bot):
        return False, "group-no-mention"

    return True, "group"
