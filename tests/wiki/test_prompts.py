"""Wiki prompt 内容回归测试。"""

from __future__ import annotations

from crew.wiki.prompts import (
    WIKI_AGENT_SYSTEM_PROMPT,
    WIKI_LIST_SOURCES_PROMPT,
    WIKI_PARSE_SOURCE_PROMPT,
)
from crew.agent.subagent.registry import SubagentRegistry


def test_agent_system_prompt_keeps_attachment_access_scoped_to_wiki_tools():
    assert "附件安全" in WIKI_AGENT_SYSTEM_PROMPT
    assert "terminal" not in WIKI_AGENT_SYSTEM_PROMPT
    assert "不得搜索或读取用户未提供的其他本地路径" in WIKI_AGENT_SYSTEM_PROMPT
    assert "wiki_capture_attachment" in WIKI_AGENT_SYSTEM_PROMPT


def test_list_sources_prompt_directs_to_composer_when_missing():
    assert "Wiki Composer" in WIKI_LIST_SOURCES_PROMPT
    assert "不得使用通用文件搜索" in WIKI_LIST_SOURCES_PROMPT


def test_wiki_agent_prompt_routes_queries_without_forcing_orient():
    """简单查询应直接检索，深度研究和写入才需要 orient。"""
    assert "简单查询" in WIKI_AGENT_SYSTEM_PROMPT
    assert "不必先 `wiki_orient`" in WIKI_AGENT_SYSTEM_PROMPT
    assert "分析研究" in WIKI_AGENT_SYSTEM_PROMPT
    assert "先 `wiki_orient`" in WIKI_AGENT_SYSTEM_PROMPT


def test_wiki_ingest_requires_confirmation_when_auto_apply_disabled():
    """关闭 auto_apply 时仍必须在 plan 后停下等待确认。"""
    assert "`auto_apply=false`" in WIKI_AGENT_SYSTEM_PROMPT
    assert "不得在同一轮自行调用 `wiki_apply_ingest`" in WIKI_AGENT_SYSTEM_PROMPT


def test_default_upload_plans_deep_structure_after_searchable_source_page():
    assert "附件会被自动捕获到 default 知识库" in WIKI_AGENT_SYSTEM_PROMPT
    assert "自动对所有附件做深度整理" in WIKI_AGENT_SYSTEM_PROMPT
    assert 'wiki_list_sources(view="inbox")' in WIKI_AGENT_SYSTEM_PROMPT
    assert "wiki_plan_ingest" in WIKI_AGENT_SYSTEM_PROMPT
    assert "全文 Source 页面" in WIKI_PARSE_SOURCE_PROMPT
    assert "图片" in WIKI_PARSE_SOURCE_PROMPT
    assert "视频" in WIKI_PARSE_SOURCE_PROMPT


def test_wiki_prompt_only_documents_exposed_ingest_workflow():
    """已移除的内部工具不得继续出现在 Wiki 预设工具列表中。"""
    definition = SubagentRegistry().get("Wiki")
    assert definition is not None
    removed = {
        "wiki_check_duplicate",
        "wiki_check_drift",
        "wiki_ingest",
        "wiki_compile",
        "wiki_save_parsed_markdown",
        "wiki_update_index",
        "wiki_append_log",
        "wiki_query",
        "wiki_explore",
        "wiki_init",
        "wiki_source_status",
        "wiki_describe_image",
        "wiki_describe_video",
        "wiki_migrate_layout",
        "wiki_archive_page",
    }
    # Wiki 预设不再维护静态 tools 白名单（None），工具范围由统一策略计算。
    assert removed.isdisjoint(definition.tools or [])
    assert "wiki_ingest(source_id)" not in WIKI_LIST_SOURCES_PROMPT
    assert "wiki_ingest(source_id)" not in WIKI_PARSE_SOURCE_PROMPT
    assert "wiki_plan_ingest" in WIKI_LIST_SOURCES_PROMPT
    assert "wiki_plan_ingest" in WIKI_PARSE_SOURCE_PROMPT


def test_wiki_agent_prompt_defines_completion_and_failure_behavior():
    """Agent 应有清晰停止条件，且不得伪造工具执行结果。"""
    assert "完成标准" in WIKI_AGENT_SYSTEM_PROMPT
    assert "证据足够即停止" in WIKI_AGENT_SYSTEM_PROMPT
    assert "不要伪造成功" in WIKI_AGENT_SYSTEM_PROMPT
