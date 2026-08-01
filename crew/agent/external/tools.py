"""Tools that let the built-in Crew agent delegate to external agents."""

from __future__ import annotations

from typing import Any

from crew.agent.executor.base import ExecutionContext
from crew.agent.executor.external import ExternalExecutor
from crew.agent.external.store import ExternalAgentStore
from crew.core.runctx import current_agent_workdir, current_owner_account_id, current_session_id
from crew.tools.registry import Registry, tool_error, tool_result


DELEGATE_EXTERNAL_AGENT_SCHEMA = {
    "name": "delegate_to_external_agent",
    "description": (
        "把一个子任务交给已创建且 Runtime 可用的外部智能体执行。"
        "适合让外部智能体独立分析、编码或给出第二智能体意见，再把结果返回给当前模型综合。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_id": {"type": "string", "description": "external_agent 的 id"},
            "prompt": {"type": "string", "description": "要交给外部智能体的完整任务说明"},
            "cwd": {"type": "string", "description": "可选工作目录，默认当前目录"},
        },
        "required": ["agent_id", "prompt"],
    },
}


def register_external_agent_tools(
    registry: Registry,
    store: ExternalAgentStore,
    *,
    interaction_bridge_getter=None,
) -> None:
    async def handle_delegate(args: dict[str, Any]) -> str:
        agent_id = str(args.get("agent_id") or "").strip()
        prompt = str(args.get("prompt") or "").strip()
        cwd = str(args.get("cwd") or current_agent_workdir.get() or ".").strip() or "."
        if not agent_id:
            return tool_error("agent_id 不能为空")
        if not prompt:
            return tool_error("prompt 不能为空")
        try:
            agent, _runtime = store.agent_with_runtime(
                agent_id,
                owner_account_id=current_owner_account_id.get(),
            )
        except KeyError:
            return tool_error(f"外部智能体不存在: {agent_id}")

        provider = str(agent["provider"]).lower()
        session_id = current_session_id.get() or "delegate_to_external_agent"
        bridge = interaction_bridge_getter() if callable(interaction_bridge_getter) else None
        executor = ExternalExecutor({
            "external_agent_id": agent_id,
            "external_store": store,
            "interaction_bridge": bridge,
            "cwd": cwd,
            "crew_session_id": f"{session_id}::delegate::{agent_id}",
            "display_session_id": session_id,
            "control_session_id": session_id,
            # 临时委派不与外部单 Agent 的长期会话混用。
            "persist_runtime_session": False,
        })
        ctx = ExecutionContext(
            session_id=f"{session_id}::delegate::{agent_id}",
            request_id="delegate_to_external_agent",
            system_prompt="",
            messages=[],
            query=prompt,
            cwd=cwd,
        )
        output = ""
        async for chunk in executor.execute(ctx):
            if chunk.kind == "final":
                output = str(chunk.body.get("text") or "")
            elif chunk.kind == "error":
                return tool_error(str(chunk.body.get("message") or "外部智能体调用失败"))
        return tool_result({
            "agent_id": agent_id,
            "provider": provider,
            "output": output,
        })

    registry.register(
        name="delegate_to_external_agent",
        toolset="external_agent",
        schema=DELEGATE_EXTERNAL_AGENT_SCHEMA,
        handler=handle_delegate,
        is_async=True,
        display_name="委派外部智能体",
        ui_label_template="委派外部智能体 {agent_id}",
        should_defer=True,
        search_hint="delegate external agent local runtime acp cli",
    )
