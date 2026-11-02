"""单 Agent 内核：对话循环 + 工具编排。用于 run_conversation。"""

__all__ = ["SingleAgent", "build_system_prompt"]


def __getattr__(name: str):
    if name == "SingleAgent":
        from crew.agent.runtime import SingleAgent
        return SingleAgent
    if name == "build_system_prompt":
        from crew.agent.prompt_builder import build_system_prompt
        return build_system_prompt
    raise AttributeError(name)
