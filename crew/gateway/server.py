"""FastAPI + WebSocket 网关服务（外观模块，保持向后兼容）。

实现已按职责拆分：
- app.py        create_app / lifespan / 渠道投递接线 / SPA 托管 / run()
- ws.py         /ws 流式对话
- routers/      REST 端点按域拆分（config / sessions / cron / runtimes / channels / misc / system）
- helpers.py    辅助函数与常量

本模块仅 re-export create_app / run，并保留 `python -m crew.gateway.server` 入口。
辅助函数（session_agent_label / with_session_agent_labels 等）请直接从 crew.gateway.helpers 导入。
"""

from __future__ import annotations

if __name__ == "__main__":
    from pathlib import Path

    import os

    from crew.process_hardening import harden_main_process

    # Match Desktop local mode before imports/config loading can observe the checkout home.
    os.environ.setdefault("CREW_HOME", str(Path.home() / ".Crew"))
    harden_main_process("gateway")

from crew.gateway.app import create_app, run
from crew.security.launch import configure_default_security_state_dir

__all__ = ["create_app", "run"]


if __name__ == "__main__":
    configure_default_security_state_dir()
    run()
