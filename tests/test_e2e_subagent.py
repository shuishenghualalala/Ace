# ruff: noqa: E402 -- e2e test configures environment/path before application imports
"""端到端测试：验证主 agent 在真实 LLM 驱动下正确调用 subagent 工具。

依赖真实 LLM（默认 deepseek，可用 CREW_MODEL_PROFILE 切换）。
每个用例使用独立 session，避免相互污染。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

# 端到端/真实 LLM：默认不跑，用 `pytest -m e2e` 单独运行。
pytestmark = pytest.mark.e2e

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from crew.app import build_app, CrewApp
from crew.core.envelope import Envelope, ResponseChunk
from crew.core.runctx import current_user_type
from crew.state.config import Config, load_config

from tests._e2e_helpers import collect_chunks


def _build_config(tmp_path: Path) -> Config:
    """基于 config.yaml 创建临时配置，避免污染真实数据库与 crew_home。"""
    cfg = load_config()
    cfg.db_path = str(tmp_path / "crew.db")
    cfg.memory_db_path = str(tmp_path / "memory.db")
    cfg.crew_home = str(tmp_path / ".crew")
    cfg.log_file = str(tmp_path / ".crew" / "logs" / "crew.log")
    cfg.llm_trace = True
    # 控制成本：子任务通常不需要太多迭代
    cfg.max_iterations = min(cfg.max_iterations, 30)
    cfg.subagent_timeout_seconds = min(cfg.subagent_timeout_seconds, 240.0)
    return cfg


@pytest.fixture
async def app(tmp_path_factory):
    """每个测试函数使用独立的临时目录与配置。"""
    tmp = tmp_path_factory.mktemp("subagent_e2e")
    cfg = _build_config(tmp)
    _app = build_app(config=cfg, enable_team=False)
    if not _app.config.has_llm_key:
        pytest.skip(f"模型 {_app.config.active_model_id} 未配置 API Key，跳过真实 LLM E2E")
    yield _app


def _tool_calls(chunks: list[ResponseChunk]) -> list[dict]:
    """提取 tool start 事件。"""
    return [c.body for c in chunks if c.kind == "tool" and c.body.get("phase") == "start"]


def _final_text(chunks: list[ResponseChunk]) -> str:
    """提取 final 文本。"""
    for c in chunks:
        if c.kind == "final":
            return c.body.get("text", "")
    return ""


def _errors(chunks: list[ResponseChunk]) -> list[str]:
    """提取 error 消息。"""
    return [c.body.get("message", "") for c in chunks if c.kind == "error"]


# ---- 测试 1: run_agent Explore 查询代码库 ----
async def test_e2e_run_agent_explore(app: CrewApp):
    """主 agent 调用 Explore 子 agent 查询日志模块。"""
    session_id = f"e2e_run_agent_explore_{int(time.time())}"
    envelope = Envelope.of(
        "请调用 Explore 子智能体（quick 彻底程度），在 crew/state/ 目录附近帮我查一下本项目的日志模块是怎么设计的，"
        "给出关键文件路径和函数名。",
        session_id=session_id,
    )
    chunks = await collect_chunks(app, envelope, timeout=240.0)

    errors = _errors(chunks)
    assert not errors, f"出现 error: {errors}"

    tools = _tool_calls(chunks)
    tool_names = [t.get("name") for t in tools]
    assert "run_agent" in tool_names, f"期望主 agent 调用 run_agent，实际调用: {tool_names}"

    # 解析 run_agent 的返回结果，确认子 agent 已完成
    run_agent_results = []
    for c in chunks:
        if c.kind == "tool" and c.body.get("phase") == "result" and c.body.get("name") == "run_agent":
            run_agent_results.append(c.body.get("detail", ""))
    assert run_agent_results, "未收到 run_agent 的 result 事件"

    final = _final_text(chunks)
    assert final, "final 文本为空"
    # 子 agent 应至少找到 logging 相关文件并返回
    assert "logging" in final.lower() or "log" in final.lower(), f"最终回答未提及日志: {final[:300]}"

    print(f"[PASS] run_agent Explore: tools={tool_names}, final={final[:120]}...")


# ---- 测试 2: delegate_task 批量并行 ----
async def test_e2e_delegate_task_batch(app: CrewApp):
    """主 agent 用 delegate_task 并行分析两个文件。"""
    session_id = f"e2e_delegate_batch_{int(time.time())}"
    readme_path = Path(ROOT) / "README.md"
    pyproject_path = Path(ROOT) / "pyproject.toml"

    prompt = (
        "请使用 delegate_task 同时委派两个子任务："
        f"1) 读取 {readme_path} 并总结前三段内容；"
        f"2) 读取 {pyproject_path} 并列出主要依赖。"
        "要求两个子任务并行执行，最后汇总告诉我。"
    )
    envelope = Envelope.of(prompt, session_id=session_id)
    chunks = await collect_chunks(app, envelope, timeout=300.0)

    errors = _errors(chunks)
    assert not errors, f"出现 error: {errors}"

    tools = _tool_calls(chunks)
    tool_names = [t.get("name") for t in tools]
    assert "delegate_task" in tool_names, f"期望调用 delegate_task，实际调用: {tool_names}"

    final = _final_text(chunks)
    assert final, "final 文本为空"
    # 批量结果应包含 README 和依赖信息
    has_readme = "crew" in final.lower() or "多智能体" in final or "README" in final
    has_deps = "openai" in final.lower() or "fastapi" in final.lower() or "依赖" in final
    assert has_readme or has_deps, f"最终回答未体现两个文件分析: {final[:400]}"

    print(f"[PASS] delegate_task batch: tools={tool_names}, final={final[:120]}...")


# ---- 测试 3: 后台 run_agent + collect_subagent ----
async def test_e2e_background_run_agent_collect(app: CrewApp):
    """主 agent 启动后台子 agent，随后 collect 取回结果。"""
    session_id = f"e2e_bg_collect_{int(time.time())}"
    # 第一轮：启动后台任务（用 Explore 做轻量查询，避免 verification 跑全量测试过慢）
    envelope1 = Envelope.of(
        "请调用 Explore 子智能体在后台查询本项目的日志模块设计，并返回 task_id。",
        session_id=session_id,
    )
    chunks1 = await collect_chunks(app, envelope1, timeout=180.0)

    errors = _errors(chunks1)
    assert not errors, f"启动后台任务出错: {errors}"

    tools = _tool_calls(chunks1)
    tool_names = [t.get("name") for t in tools]
    assert "run_agent" in tool_names, f"期望调用 run_agent，实际调用: {tool_names}"

    # 从 tool result 里解析 task_id
    task_id = ""
    for c in chunks1:
        if c.kind == "tool" and c.body.get("phase") == "result" and c.body.get("name") == "run_agent":
            import json
            try:
                payload = json.loads(c.body.get("detail", "{}"))
                task_id = payload.get("task_id", "")
            except Exception:
                pass
    assert task_id, f"未从 run_agent 结果中解析出 task_id: {[c.body for c in chunks1 if c.kind == 'tool']}"

    # 第二轮：collect 取结果
    envelope2 = Envelope.of(
        f"请用 collect_subagent 取回 task_id {task_id} 的结果，告诉我查询到了什么。",
        session_id=session_id,
    )
    chunks2 = await collect_chunks(app, envelope2, timeout=180.0)

    errors2 = _errors(chunks2)
    assert not errors2, f"collect 出错: {errors2}"

    tools2 = _tool_calls(chunks2)
    assert "collect_subagent" in [t.get("name") for t in tools2], "期望主 agent 调用 collect_subagent"

    final = _final_text(chunks2)
    assert final, "collect 后 final 文本为空"
    assert "logging" in final.lower() or "log" in final.lower() or "文件" in final, (
        f"collect 结果未体现查询内容: {final[:300]}"
    )

    print(f"[PASS] background run_agent + collect: task_id={task_id}, final={final[:120]}...")


# ---- 测试 4: 外部用户权限封顶 ----
async def test_e2e_external_parent_caps_child_tools(app: CrewApp):
    """外部用户主 agent 创建的子 agent 不能拿到 terminal 等工具。"""
    cfg = app.config
    cfg.access_control.user_type = "external"
    cfg.access_control.external = {
        "enabled_toolsets": ["file"],  # 仅允许 file 工具集
        "disabled_toolsets": [],
        "enabled_plugins": [],
        "disabled_plugins": ["*"],
        "enabled_skills": [],
        "disabled_skills": ["*"],
    }
    # 重新构建 app 以应用 access_control
    app2 = build_app(config=cfg, enable_team=False)

    tok = current_user_type.set("external")
    try:
        child = app2._make_subagent({
            "system_prompt": "test",
            "toolsets": None,
            "tools": None,
            "model": "inherit",
            "max_iterations": 5,
        })
    finally:
        current_user_type.reset(tok)
    assert "terminal" not in child.tool_filter, "外部用户的子 agent 不应拿到 terminal"
    assert "delegate_task" not in child.tool_filter, "子 agent 不应拿到 delegate_task"
    print("[PASS] external parent caps child tools")


# ---- 测试 5: 短超时子任务返回 timeout ----
async def test_e2e_delegate_task_timeout(app: CrewApp):
    """真实 LLM 下用极短 timeout 触发 timeout 状态（可能 flaky，允许 retry）。"""
    cfg = app.config
    cfg.subagent_timeout_seconds = 1.0  # 1 秒必然超时
    app2 = build_app(config=cfg, enable_team=False)

    session_id = f"e2e_timeout_{int(time.time())}"
    envelope = Envelope.of(
        "请用 delegate_task 委派一个子任务：'详细分析 README.md 的每一行'。这个任务应该在 1 秒内超时。",
        session_id=session_id,
    )
    chunks = await collect_chunks(app2, envelope, timeout=30.0)

    # 主 agent 可能直接回答，也可能调用 delegate_task 后超时
    tools = _tool_calls(chunks)
    if "delegate_task" in [t.get("name") for t in tools]:
        # 查找 timeout 关键词
        all_text = " ".join(str(c.body) for c in chunks)
        assert "timeout" in all_text.lower() or "超时" in all_text, (
            f"delegate_task 未返回 timeout: {all_text[:500]}"
        )
    else:
        pytest.skip("模型未调用 delegate_task，无法验证超时")

    print("[PASS] delegate_task timeout")


# ---- 测试 6: run_agent Plan preset ----
async def test_e2e_run_agent_plan(app: CrewApp):
    """主 agent 调用 Plan preset 做简单架构规划。"""
    session_id = f"e2e_plan_{int(time.time())}"
    envelope = Envelope.of(
        "请调用 Plan 子智能体，为本项目设计一个轻量化的子智能体模块扩展方案，"
        "列出关键文件和接口。",
        session_id=session_id,
    )
    chunks = await collect_chunks(app, envelope, timeout=300.0)

    errors = _errors(chunks)
    assert not errors, f"出现 error: {errors}"

    tools = _tool_calls(chunks)
    tool_names = [t.get("name") for t in tools]
    assert "run_agent" in tool_names, f"期望调用 run_agent，实际调用: {tool_names}"

    final = _final_text(chunks)
    assert final, "final 文本为空"
    assert len(final) > 50, f"Plan 结果过短，可能未真正规划: {final[:300]}"

    print(f"[PASS] run_agent Plan: final={final[:120]}...")
