"""Feishu/Lark platform plugin（WebSocket 长连接，基于 lark-oapi，全功能）。"""

from __future__ import annotations

import os
from typing import Any

from .adapter import FeishuChannel, lark_available
from .config import FeishuSettings


def _env_creds() -> dict[str, Any]:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not (app_id and app_secret):
        return {}
    creds: dict[str, Any] = {"appId": app_id, "appSecret": app_secret}
    if os.getenv("FEISHU_DOMAIN"):
        creds["domain"] = os.getenv("FEISHU_DOMAIN")
    return creds


def _settings(config: Any) -> FeishuSettings:
    # FeishuSettings.from_extra 已自带 FEISHU_* 环境变量回退。
    return FeishuSettings.from_extra(
        dict(getattr(config, "extra", {}) or {}),
        use_env=getattr(config, "env_enabled", True),
    )


def register(ctx) -> None:
    ctx.register_platform(
        name="feishu",
        label="飞书",
        adapter_factory=lambda config: FeishuChannel(config),
        # available 只表达运行依赖是否就绪；凭证完整性由 validate_config 处理，
        # 这样 owner-scoped appId/appSecret 不需要同步到全局进程 env。
        check_fn=lark_available,
        validate_config=lambda cfg: _settings(cfg).configured,
        is_connected=lambda cfg: _settings(cfg).configured,
        required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        env_enablement_fn=_env_creds,
        install_hint="安装 lark-oapi==1.5.3（pip install .[feishu]）并配置 FEISHU_APP_ID / FEISHU_APP_SECRET",
        description=(
            "Feishu/Lark channel via lark-oapi WebSocket 长连接（机器人主动连飞书，无需公网 webhook）。"
            "文本/富文本/图片/文件/音频/视频 收发；访问控制(group_policy 五档 + 白黑名单 + 管理员 + "
            "allow_bots + require_mention)；@机器人 精确判定；线程内回复、markdown 富文本、出站文件、"
            "处理中 reaction、去重持久化。"
        ),
        emoji="📨",
    )
