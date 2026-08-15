"""微信扫码登录入口：`crew weixin-login` 或 `python -m crew.cli.weixin_login`。

复用 iLink QR 流程：请求二维码 → 终端/URL 展示 → 手机扫码确认 → token 持久化到
crew home 的账号文件（{crew_home}/weixin/accounts/{account_id}.json）。adapter 启动时
只要配置了 WEIXIN_ACCOUNT_ID，就会自动从账号文件加载 token。
"""

from __future__ import annotations

if __name__ == "__main__":
    from crew.process_hardening import harden_main_process

    harden_main_process("cli-weixin-login")

import asyncio
import sys


async def run_weixin_login(timeout_seconds: int = 480) -> int:
    from plugins.platforms.weixin import ilink
    from plugins.platforms.weixin.config import WeixinSettings

    if not ilink.check_weixin_requirements():
        print("错误: 缺少 aiohttp / cryptography 依赖，请先执行: pip install .[weixin]")
        return 1

    settings = WeixinSettings.from_extra({})
    print(f"账号凭证目录: {settings.accounts_dir()}")
    creds = await ilink.qr_login(settings.accounts_dir(), timeout_seconds=timeout_seconds)
    if not creds:
        print("登录失败或超时。")
        return 1

    account_id = creds["account_id"]
    print("\n登录成功。请在配置里启用 weixin 渠道并设置：")
    print("  channels.weixin.enabled = true")
    print(f"  channels.weixin.accountId = {account_id}")
    print("  （token 已持久化到账号文件，无需手工填写；也可用 WEIXIN_ACCOUNT_ID 环境变量）")
    return 0


def main() -> None:
    timeout = 480
    if len(sys.argv) > 2 and sys.argv[1] == "--timeout":
        try:
            timeout = int(sys.argv[2])
        except ValueError:
            pass
    try:
        raise SystemExit(asyncio.run(run_weixin_login(timeout_seconds=timeout)))
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
