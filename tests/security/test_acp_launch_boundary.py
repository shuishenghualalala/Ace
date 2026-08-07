"""H-2 regression: ACP must refuse host spawn when no ProcessLaunch is bound.

Every security-wired conversation compiles a ProcessLaunch in CrewApp.handle. A
Team member's envelope bypasses that, so ``current_process_launch`` resolves to
None inside its runtime. The ACP adapter previously treated ``None`` as "host
allowed" while every other exec path (execute_captured) refused — so a managed
conversation could still spawn on the host through ACP. It must fail closed,
matching execute_captured.
"""

from __future__ import annotations

import pytest

from crew.security.launch import current_process_launch


@pytest.mark.asyncio
async def test_acp_refuses_host_spawn_when_launch_missing() -> None:
    from crew.agent.external import acp_adapter
    from crew.agent.external.acp_adapter import AcpAdapterError

    token = current_process_launch.set(None)
    try:
        # The refusal happens before the config is read, so a bare object suffices.
        agen = acp_adapter.stream_acp_events("irrelevant", object())  # type: ignore[arg-type]
        with pytest.raises(AcpAdapterError, match="缺少安全启动上下文"):
            async for _ in agen:
                pass
    finally:
        current_process_launch.reset(token)
