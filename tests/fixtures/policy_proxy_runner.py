"""拉起真实的 LoopbackPolicyProxy 并把它的 URL 打到 stdout。

给 desktop/scripts/ax-probe.ts 用：宿主强制要求浏览器走 Crew 的网络策略代理
（browser-host.ts 的 `parseProxy` 对空代理直接抛 `proxy_required`），所以量测
AX 快照时必须先有一个真代理。用真的这一个而不是随便找个代理，顺带把
「私网默认被拒、加白名单才通」这条策略也一起验了。

用法：
    python3 -m tests.fixtures.policy_proxy_runner [--allow-host 127.0.0.1 ...]

打印一行代理 URL（含 basic auth 凭据）后常驻，Ctrl+C 退出。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib

from crew.browser.security import BrowserNetworkPolicy, LoopbackPolicyProxy
from crew.browser.types import BrowserConfig


async def main() -> None:
    parser = argparse.ArgumentParser(description="Crew 网络策略代理（量测用）")
    parser.add_argument(
        "--allow-host",
        action="append",
        default=[],
        metavar="HOST",
        help="允许访问的私网主机，可重复。不给则维持默认的「私网全拒」。",
    )
    args = parser.parse_args()

    policy = BrowserNetworkPolicy(
        BrowserConfig(allowed_private_hosts=list(args.allow_host))
    )
    proxy = LoopbackPolicyProxy(policy)
    await proxy.start()
    print(proxy.url, flush=True)

    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        await asyncio.Event().wait()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
