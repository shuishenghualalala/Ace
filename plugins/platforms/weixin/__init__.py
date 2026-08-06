"""微信（个人号 iLink）平台插件（长轮询收消息，基于 aiohttp + cryptography）。"""

from __future__ import annotations

import os
from typing import Any

from . import ilink
from .adapter import WeixinChannel
from .config import WeixinSettings


def _env_creds() -> dict[str, Any]:
    account_id = os.getenv("WEIXIN_ACCOUNT_ID", "").strip()
    token = os.getenv("WEIXIN_TOKEN", "").strip()
    if not (account_id and token):
        return {}
    return {"accountId": account_id, "token": token}


def _settings(config: Any) -> WeixinSettings:
    return WeixinSettings.from_extra(
        dict(getattr(config, "extra", {}) or {}),
        use_env=getattr(config, "env_enabled", True),
    )


def _configured(config: Any) -> bool:
    """凭证是否齐备：account_id 必需；token 可来自 extra/env 或已持久化账号文件。"""
    settings = _settings(config)
    if not settings.account_id:
        return False
    if settings.token or str(getattr(config, "token", None) or "").strip():
        return True
    persisted = ilink.load_account(settings.accounts_dir(), settings.account_id)
    return bool(persisted and persisted.get("token"))


def register(ctx) -> None:
    ctx.register_platform(
        name="weixin",
        label="微信",
        adapter_factory=lambda config: WeixinChannel(config),
        check_fn=ilink.check_weixin_requirements,
        validate_config=_configured,
        is_connected=_configured,
        required_env=["WEIXIN_ACCOUNT_ID", "WEIXIN_TOKEN"],
        env_enablement_fn=_env_creds,
        install_hint="安装 aiohttp + cryptography（pip install .[weixin]）并运行 crew weixin-login 扫码登录",
        description=(
            "个人微信（腾讯 iLink Bot API）长轮询渠道，无需公网 webhook。"
            "文本/图片/文件/语音 收发；访问控制（dm_policy 三档 + group_policy 三档 + 白名单）；"
            "context_token 会话续传；长文本分块；出站文件；去重持久化；cron 投递。"
            "注意：iLink 为机器人身份，普通微信群事件大多不推送，仅私聊稳定可用。"
        ),
        emoji="💬",
    )
