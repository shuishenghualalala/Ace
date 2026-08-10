"""注册 Wiki 工具。

工具 handler 通过 ``current_session_id`` / ``current_owner_account_id`` ContextVar
取当前会话与所有者，与 Plan 模式工具一致。
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from crew.core.runctx import (
    current_attachment_files,
    current_attachment_paths,
    current_owner_account_id,
    current_push_fn,
    current_request_id,
    current_session_id,
)
from crew.tools.registry import Registry, tool_error, tool_result

from .compiler import WikiCompiler
from .config import WikiConfig
from .manager import WikiSessionManager
from .parser import (
    MissingDependencyError,
    fetch_url_to_markdown,
    guess_mime_type,
    parse_document_from_bytes,
    validate_parsed_text,
)
from .sources import (
    adapter_status,
    all_adapter_statuses,
    classify_file,
    classify_url,
    fetch_youtube_transcript,
)
from .store._ids import filename_from_title
from .prompts import (
    WIKI_APPLY_INGEST_PROMPT,
    WIKI_BATCH_INGEST_PROMPT,
    WIKI_CREATE_KB_PROMPT,
    WIKI_DELETE_KB_PROMPT,
    WIKI_DELETE_SOURCE_PROMPT,
    WIKI_DIGEST_PROMPT,
    WIKI_FETCH_URL_PROMPT,
    WIKI_REFRESH_SOURCE_PROMPT,
    WIKI_LINT_PROMPT,
    WIKI_LIST_INBOX_PROMPT,
    WIKI_LIST_KBS_PROMPT,
    WIKI_LIST_SOURCES_PROMPT,
    WIKI_ORIENT_PROMPT,
    WIKI_PARSE_SOURCE_PROMPT,
    WIKI_PLAN_INGEST_PROMPT,
    WIKI_READ_PROMPT,
    WIKI_SEARCH_PROMPT,
    WIKI_UPDATE_PAGE_PROMPT,
)
from .query import WikiQuerier
from .store import WikiStore

# Wiki 工具按读写职责拆分为只读与管理工具集。
#
# 设计原则：
# 1. 回答用户问题前，必须优先使用 wiki_search / wiki_read 检索知识库。
# 2. 禁止暴露 glob / grep 这类通用文件搜索工具，避免模型绕过知识库去扫本地文件。
# 3. 附件只能经 wiki_capture_attachment 读取当前 owner 本轮上传目录。
# 4. wiki_list_sources 用于在 ingest 前列出当前知识库的 raw sources。

# 共享 schema 片段：kb_id 参数（仅跨库操作时显式传入；常规操作省略，跟随当前活跃知识库。
# 描述必须抑制模型按惯性填字面量 "default"——显式值优先级最高，填错会整条入库链错库）
_KB_ID_PARAM: dict[str, dict[str, str]] = {
    "kb_id": {
        "type": "string",
        "description": "目标知识库 ID。仅当用户明确要求操作其他知识库时才传入；常规操作必须省略此参数，系统会自动使用当前活跃知识库（active_kb_id），不要自行猜测或填写",
    },
}

WIKI_READ_TOOLSET = "wiki.read"
WIKI_MANAGE_TOOLSET = "wiki.manage"

WIKI_READ_TOOLS = [
    "wiki_orient",
    "wiki_search",
    "wiki_read",
    "wiki_list_sources",
    "wiki_list_kbs",
    "wiki_list_inbox",
]

WIKI_MANAGE_TOOLS = [
    "wiki_batch_ingest",
    "wiki_lint",
    "wiki_create_kb",
    "wiki_delete_kb",
    "wiki_delete_source",
    "wiki_parse_source",
    "wiki_update_page",
    "wiki_plan_ingest",
    "wiki_apply_ingest",
    "wiki_fetch_url",
    "wiki_refresh_source",
    "wiki_digest",
    "wiki_capture_attachment",
    "wiki_capture_text",
    "wiki_capture_session",
    "wiki_create_page",
    "wiki_delete_pages",
    "wiki_rename_page",
]

_WIKI_ORIENT_SCHEMA = {
    "name": "wiki_orient",
    "description": WIKI_ORIENT_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            **_KB_ID_PARAM,
        },
        "required": [],
    },
}

_WIKI_BATCH_INGEST_SCHEMA = {
    "name": "wiki_batch_ingest",
    "description": WIKI_BATCH_INGEST_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "source_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选；指定要处理的 RawSource ID，省略时处理当前 KB 的 parsed sources",
            },
            "cursor": {
                "type": "integer",
                "description": "批次起始游标，默认 0；继续时传上次返回的 next_cursor",
                "default": 0,
            },
            "batch_size": {
                "type": "integer",
                "description": "本次最多处理数量，范围 1-5，默认 5",
                "default": 5,
            },
            "confirmation_id": {
                "type": "string",
                "description": "auto_apply=false 时确认整批计划的一次性 ID",
            },
            **_KB_ID_PARAM,
        },
        "required": [],
    },
}

_WIKI_SEARCH_SCHEMA = {
    "name": "wiki_search",
    "description": WIKI_SEARCH_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词",
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量上限",
                "default": 5,
            },
            "expand_neighbors": {
                "type": "boolean",
                "description": "是否扩展命中页面的一跳关联页面，默认 true",
                "default": True,
            },
            "include_context": {
                "type": "boolean",
                "description": "是否生成用于回答的相关正文与 Claim/Evidence 上下文，默认 true",
                "default": True,
            },
            **_KB_ID_PARAM,
        },
        "required": ["query"],
    },
}

_WIKI_READ_SCHEMA = {
    "name": "wiki_read",
    "description": WIKI_READ_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "页面 ID",
            },
            "include_neighbors": {
                "type": "boolean",
                "description": "是否同时返回关联页面，默认 false",
                "default": False,
            },
            "neighbor_limit": {
                "type": "integer",
                "description": "最多返回多少个关联页面，范围 1-20，默认 5",
                "default": 5,
            },
            **_KB_ID_PARAM,
        },
        "required": ["page_id"],
    },
}

_WIKI_LINT_SCHEMA = {
    "name": "wiki_lint",
    "description": WIKI_LINT_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "deep": {
                "type": "boolean",
                "description": "是否进行 LLM 语义检查（矛盾、概念缺口）。默认 false，只做程序化检查（断链、孤立页面、格式、时效性）。",
                "default": False,
            },
            "plan_fixes": {
                "type": "boolean",
                "description": "生成可自动修复项的计划和一次性确认卡",
                "default": False,
            },
            "apply_fixes": {
                "type": "boolean",
                "description": "使用 confirmation_id 应用上一轮安全修复计划",
                "default": False,
            },
            "confirmation_id": {"type": "string"},
            **_KB_ID_PARAM,
        },
        "required": [],
    },
}

_WIKI_CREATE_KB_SCHEMA = {
    "name": "wiki_create_kb",
    "description": WIKI_CREATE_KB_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "kb_id": {
                "type": "string",
                "description": "知识库 ID，支持中文、字母、数字、下划线和连字符",
            },
            "name": {
                "type": "string",
                "description": "知识库显示名称，默认为 kb_id",
            },
        },
        "required": ["kb_id"],
    },
}

_WIKI_DELETE_KB_SCHEMA = {
    "name": "wiki_delete_kb",
    "description": WIKI_DELETE_KB_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "kb_id": {
                "type": "string",
                "description": "要删除的知识库 ID",
            },
            "confirmation_id": {"type": "string", "description": "用户确认卡返回的一次性确认 ID"},
        },
        "required": ["kb_id"],
    },
}

_WIKI_DELETE_SOURCE_SCHEMA = {
    "name": "wiki_delete_source",
    "description": WIKI_DELETE_SOURCE_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "要删除的 raw source ID",
            },
            "confirmation_id": {"type": "string", "description": "用户确认卡返回的一次性确认 ID"},
            **_KB_ID_PARAM,
        },
        "required": ["source_id"],
    },
}

_WIKI_PARSE_SOURCE_SCHEMA = {
    "name": "wiki_parse_source",
    "description": WIKI_PARSE_SOURCE_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "要解析或重新解析的 raw source ID",
            },
            "prompt": {
                "type": "string",
                "description": "图片或视频理解的自定义提示词（可选）",
            },
            "confirmation_id": {
                "type": "string",
                "description": "视频外传预检返回的一次性确认 ID",
            },
            **_KB_ID_PARAM,
        },
        "required": ["source_id"],
    },
}

_WIKI_LIST_SOURCES_SCHEMA = {
    "name": "wiki_list_sources",
    "description": WIKI_LIST_SOURCES_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "enum": ["all", "parsed", "failed", "pending"],
                "description": "过滤 parse_status，默认 all",
                "default": "all",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回多少条，默认 50",
                "default": 50,
            },
            "include_history": {
                "type": "boolean",
                "description": "是否包含已被新版本取代的历史来源，默认 false（仅当前版本）",
                "default": False,
            },
            **_KB_ID_PARAM,
        },
        "required": [],
    },
}

_WIKI_LIST_KBS_SCHEMA = {
    "name": "wiki_list_kbs",
    "description": WIKI_LIST_KBS_PROMPT,
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_WIKI_LIST_INBOX_SCHEMA = {
    "name": "wiki_list_inbox",
    "description": WIKI_LIST_INBOX_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "最多返回多少条，默认 50",
                "default": 50,
            },
            **_KB_ID_PARAM,
        },
        "required": [],
    },
}

_WIKI_UPDATE_PAGE_SCHEMA = {
    "name": "wiki_update_page",
    "description": WIKI_UPDATE_PAGE_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "要更新的页面 ID",
            },
            "content": {
                "type": "string",
                "description": "页面 Markdown 内容（可选，传入则完全替换）",
            },
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target_page_id": {"type": "string"},
                        "relation": {"type": "string"},
                    },
                    "required": ["target_page_id", "relation"],
                },
                "description": "基于稳定页面 ID 的有类型关系（可选，传入则覆盖）",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "标签列表（可选，传入则覆盖）",
            },
            **_KB_ID_PARAM,
        },
        "required": ["page_id"],
    },
}

_WIKI_PLAN_INGEST_SCHEMA = {
    "name": "wiki_plan_ingest",
    "description": WIKI_PLAN_INGEST_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "要分析的 raw source ID",
            },
            "chunk_size": {
                "type": "integer",
                "description": "长文档分块分析的字符阈值（可选）。未指定时使用系统默认值。",
            },
            "use_chunking": {
                "type": "boolean",
                "description": "是否强制启用/禁用长文档分块分析（可选）。未指定时按文档长度自动判断。",
            },
            **_KB_ID_PARAM,
        },
        "required": ["source_id"],
    },
}

_WIKI_APPLY_INGEST_SCHEMA = {
    "name": "wiki_apply_ingest",
    "description": WIKI_APPLY_INGEST_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "要写入的 raw source ID",
            },
            "approved_titles": {
                "type": "array",
                "items": {"type": "string"},
                "description": "（可选）用户确认要写入的页面标题列表；未指定时应用整个 plan",
            },
            "confirmation_id": {
                "type": "string",
                "description": "wiki_plan_ingest 返回的一次性确认 ID",
            },
            "chunk_size": {
                "type": "integer",
                "description": "长文档分块分析的字符阈值（可选）。仅在未找到已有 plan、回退到完整 ingest 时生效。",
            },
            "use_chunking": {
                "type": "boolean",
                "description": "是否强制启用/禁用长文档分块分析（可选）。仅在未找到已有 plan、回退到完整 ingest 时生效。",
            },
            **_KB_ID_PARAM,
        },
        "required": ["source_id"],
    },
}

_WIKI_FETCH_URL_SCHEMA = {
    "name": "wiki_fetch_url",
    "description": WIKI_FETCH_URL_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要抓取的网页 URL",
            },
            "title": {
                "type": "string",
                "description": "可选标题，未提供时自动从 URL 提取",
            },
            **_KB_ID_PARAM,
        },
        "required": ["url"],
    },
}

_WIKI_REFRESH_SOURCE_SCHEMA = {
    "name": "wiki_refresh_source",
    "description": WIKI_REFRESH_SOURCE_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "source_id": {
                "type": "string",
                "description": "要重新抓取的 URL RawSource ID",
            },
            **_KB_ID_PARAM,
        },
        "required": ["source_id"],
    },
}

_WIKI_DIGEST_SCHEMA = {
    "name": "wiki_digest",
    "description": WIKI_DIGEST_PROMPT,
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "需要比较或综合的主题"},
            "mode": {
                "type": "string",
                "enum": ["auto", "comparison", "synthesis"],
                "description": "报告类型；默认 auto",
            },
            **_KB_ID_PARAM,
        },
        "required": ["topic"],
    },
}

_WIKI_CAPTURE_ATTACHMENT_SCHEMA = {
    "name": "wiki_capture_attachment",
    "description": "把当前用户通过对话上传的附件复制为不可变 Wiki RawSource。只接受用户 uploads 目录中的附件路径。用户消息中已带附件且需要入库（或基于该附件整理知识）时，第一步就对每个附件调用本工具；附件路径取用户消息开头「附件『文件名』位于: ...」给出的路径，不要猜测，也不要让用户重新上传。",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "本轮附件提示中给出的绝对路径"},
            "title": {"type": "string", "description": "可选的人类可读标题"},
            **_KB_ID_PARAM,
        },
        "required": ["path"],
    },
}

_WIKI_CAPTURE_TEXT_SCHEMA = {
    "name": "wiki_capture_text",
    "description": "把用户粘贴或明确提供的文本保存为不可变 Wiki RawSource，并生成 parsed markdown。",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "材料标题"},
            "content": {"type": "string", "description": "要沉淀的原始文本"},
            "source_platform": {
                "type": "string",
                "description": "可选来源平台，如 wechat、zhihu、x、xiaohongshu",
            },
            **_KB_ID_PARAM,
        },
        "required": ["title", "content"],
    },
}

_WIKI_CAPTURE_SESSION_SCHEMA = {
    "name": "wiki_capture_session",
    "description": "把当前会话或当前用户拥有的指定会话整理为不可变 Wiki RawSource。",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string", "description": "可选；默认当前会话"},
            "title": {"type": "string", "description": "可选材料标题"},
            **_KB_ID_PARAM,
        },
        "required": [],
    },
}

_WIKI_CREATE_PAGE_SCHEMA = {
    "name": "wiki_create_page",
    "description": "在目标知识库新建页面；自动维护导航、日志、搜索索引和摘要状态。",
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "content": {"type": "string"},
            "page_type": {
                "type": "string",
                "enum": ["entity", "topic", "source", "comparison", "synthesis"],
            },
            "tags": {"type": "array", "items": {"type": "string"}},
            "relations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "target_page_id": {"type": "string"},
                        "relation": {"type": "string"},
                    },
                    "required": ["target_page_id", "relation"],
                },
            },
            **_KB_ID_PARAM,
        },
        "required": ["title", "content"],
    },
}

_WIKI_DELETE_PAGES_SCHEMA = {
    "name": "wiki_delete_pages",
    "description": "预检或执行批量删除页面。首次调用不传 confirmation_id，只返回影响和一次性确认 ID。",
    "parameters": {
        "type": "object",
        "properties": {
            "page_ids": {"type": "array", "items": {"type": "string"}},
            "confirmation_id": {"type": "string"},
            **_KB_ID_PARAM,
        },
        "required": ["page_ids"],
    },
}

_WIKI_RENAME_PAGE_SCHEMA = {
    "name": "wiki_rename_page",
    "description": "重命名页面并修复正文 wikilink；结构化关系使用页面 ID，无需改写。",
    "parameters": {
        "type": "object",
        "properties": {
            "page_id": {"type": "string"},
            "new_title": {"type": "string"},
            **_KB_ID_PARAM,
        },
        "required": ["page_id", "new_title"],
    },
}

def register_wiki_tools(
    registry: Registry,
    store: WikiStore,
    compiler: WikiCompiler,
    querier: WikiQuerier,
    manager: WikiSessionManager,
    config: WikiConfig | None = None,
    session_store: Any = None,
) -> None:
    """把 Wiki 工具注册到 registry（toolset='wiki'）。"""

    def _owner() -> str:
        return current_owner_account_id.get() or ""

    def _kb_id(args: dict[str, Any] | None = None) -> str:
        """读取目标知识库：优先使用用户显式传入的 kb_id，否则使用当前活跃知识库。"""
        explicit = str((args or {}).get("kb_id") or "").strip()
        if explicit:
            return explicit
        resolved = manager.get_kb_id(
            current_session_id.get(),
            owner_account_id=_owner(),
        )
        return resolved.strip() if isinstance(resolved, str) and resolved.strip() else "default"

    def _kb_id_for_source(args: dict[str, Any], source_id: str) -> str:
        """source 级操作的目标知识库：显式 kb_id > source 实际所在知识库 > 当前活跃知识库。

        capture 之后的 parse/plan/apply 等步骤必须跟随 source 归属，避免会话活跃 KB
        在流程中途被重置（客户端未携带 wiki_kb_id 时 runtime 会回落 default）导致
        source 与 wiki 页面落到不同知识库。
        """
        explicit = str((args or {}).get("kb_id") or "").strip()
        if explicit:
            return explicit
        try:
            located = store.find_source_kb(source_id, owner_account_id=_owner())
        except Exception:  # noqa: BLE001
            located = None
        if located:
            return located
        return _kb_id(args)

    def _build_progress_callback() -> Callable[[str, int, dict[str, Any]], Awaitable[None]] | None:
        """构建一个向前端推送 wiki_ingest 进度的回调（非 gateway 场景返回 None）。"""
        push = current_push_fn.get()
        sid = current_session_id.get()
        req_id = current_request_id.get()
        if push is None or not sid:
            return None

        async def _progress(stage: str, step: int, payload: dict[str, Any]) -> None:
            try:
                await push(sid, {
                    "kind": "tool_progress",
                    "request_id": req_id,
                    "tool_name": "wiki_ingest",
                    "stage": stage,
                    "step": step,
                    "payload": payload,
                })
            except Exception:  # noqa: BLE001
                pass

        return _progress

    def _mark_changed(kb_id: str, change_type: str, **payload: Any) -> None:
        manager.add_pending_change(
            current_session_id.get(),
            {"kb_id": kb_id, "change_type": change_type, **payload},
            owner_account_id=_owner(),
        )

    def _finish_write(kb_id: str, message: str, change_type: str, **payload: Any) -> None:
        compiler.finalize_write(message, owner_account_id=_owner(), kb_id=kb_id)
        _mark_changed(kb_id, change_type, **payload)

    def _save_parsed_source(
        raw: Any,
        content: str,
        kb_id: str,
        *,
        log_message: str,
    ) -> tuple[str, Any | None, Any | None]:
        """统一保存解析文本，并在发布 Source 页面前完成内容去重。"""
        text = validate_parsed_text(content, str(raw.title or raw.id))
        parsed_path = str(
            store.save_parsed_markdown(
                raw.id,
                text,
                owner_account_id=_owner(),
                kb_id=kb_id,
            )
        )
        raw.parsed_path = parsed_path
        raw.parse_status = "parsed"
        raw.parse_error = None
        saved_raw = store.load_raw(raw.id, owner_account_id=_owner(), kb_id=kb_id)
        if saved_raw is not None:
            raw.content_sha256 = saved_raw.content_sha256
        duplicate = store.check_source_duplicate(
            raw,
            owner_account_id=_owner(),
            kb_id=kb_id,
        )
        from .schemas import RawSource

        if not isinstance(duplicate, RawSource):
            duplicate = None
        raw.is_duplicate = duplicate is not None
        store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
        if duplicate is not None:
            _finish_write(
                kb_id,
                f"解析 source {raw.id}，内容与 {duplicate.id} 重复，跳过 Source 页面发布",
                "source_duplicate",
                source_ids=[raw.id],
            )
            return parsed_path, None, duplicate
        page = compiler.publish_source_page(
            raw.id,
            owner_account_id=_owner(),
            kb_id=kb_id,
        )
        _finish_write(
            kb_id,
            log_message,
            "source_parsed",
            source_ids=[raw.id],
            page_ids=[page.id],
        )
        return parsed_path, page, None

    def _issue_confirmation(
        *,
        action: str,
        kb_id: str,
        payload: dict[str, Any],
        summary: str,
        impact: dict[str, Any],
    ) -> str:
        confirmation = manager.issue_confirmation(
            current_session_id.get(),
            action=action,
            kb_id=kb_id,
            payload=payload,
            summary=summary,
            impact=impact,
            owner_account_id=_owner(),
        )
        if not isinstance(confirmation, dict):
            return tool_result(
                requires_confirmation=True,
                action=action,
                kb_id=kb_id,
                summary=summary,
                impact=impact,
            )
        return tool_result(**confirmation)

    def _consume_confirmation(args: dict[str, Any], *, action: str, kb_id: str) -> dict[str, Any] | None:
        confirmation_id = str(args.get("confirmation_id") or "").strip()
        if not confirmation_id:
            return None
        return manager.consume_confirmation(
            current_session_id.get(),
            confirmation_id,
            action=action,
            kb_id=kb_id,
            owner_account_id=_owner(),
        )

    def _capture_bytes(path: str, title: str, kb_id: str) -> str:
        from pathlib import Path
        import shutil
        import time
        import uuid

        from crew.gateway.context import _get_upload_dir
        from crew.wiki.multimodal import is_image_mime, is_video_mime
        from .schemas import RawSource

        candidate = Path(path).expanduser().resolve()
        uploads_root = _get_upload_dir(_owner()).expanduser().resolve()
        try:
            candidate.relative_to(uploads_root)
        except ValueError:
            return tool_error("附件路径不属于当前用户 uploads 目录")
        allowed = {Path(item).expanduser().resolve() for item in current_attachment_paths.get()}
        if candidate not in allowed:
            return tool_error("附件不属于当前用户回合，拒绝读取")
        if not candidate.is_file():
            return tool_error("附件文件不存在")

        original_names = {
            Path(item_path).expanduser().resolve(): item_name.strip()
            for item_path, item_name in current_attachment_files.get()
            if item_path.strip() and item_name.strip()
        }
        original_name = original_names.get(candidate, "")
        source_id = f"upload_{uuid.uuid4().hex[:12]}"
        filename = title.strip() or original_name or candidate.name
        # MIME 优先来自原始附件名；仅读取小段文件头用于识别被错误命名的 PDF，
        # 避免大文件在 capture 阶段被重复完整读取。
        with candidate.open("rb") as captured:
            header = captured.read(16)
        mime = guess_mime_type(original_name or candidate.name, header)
        source_kind = classify_file(original_name or candidate.name, mime)
        source_dir = store._source_dir(source_kind, _owner(), kb_id)
        suffix = candidate.suffix.lower() or ".bin"
        safe_stem = filename_from_title(Path(original_name or candidate.name).stem)
        original_path = source_dir / f"{source_id}-{safe_stem}{suffix}"
        shutil.copy2(candidate, original_path)
        source_type = "image" if is_image_mime(mime) else "video" if is_video_mime(mime) else "upload"
        raw = RawSource(
            id=source_id,
            title=filename,
            source_type=source_type,
            parsed_path="",
            original_path=str(original_path),
            file_type=mime,
            size=original_path.stat().st_size,
            created_at=time.time(),
            session_id=current_session_id.get(),
            source_kind=source_kind,
            source_platform="local",
            adapter_name="builtin-file",
            original_ref=original_name or candidate.name,
        )
        store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
        _finish_write(kb_id, f"捕获附件 RawSource {source_id} ({filename})", "source_captured", source_ids=[source_id])
        return tool_result(
            source=raw.to_dict(),
            message="附件已保存为不可变 RawSource；下一步调用 wiki_parse_source 完成质量检查并发布全文 Source 页面。",
        )

    def _handle_capture_attachment(args: dict[str, Any]) -> str:
        path = str(args.get("path") or "").strip()
        if not path:
            return tool_error("缺少附件 path")
        return _capture_bytes(path, str(args.get("title") or ""), _kb_id(args))

    def _capture_text(
        title: str,
        content: str,
        source_type: str,
        kb_id: str,
        session_id: str = "",
        source_platform: str = "",
    ) -> str:
        import time
        import uuid

        from .schemas import RawSource

        if not title.strip() or not content.strip():
            return tool_error("标题和内容不能为空")
        source_id = f"{source_type}_{uuid.uuid4().hex[:12]}"
        material_kind = (
            "session"
            if source_type == "session"
            else "article"
            if source_platform in {"web", "wechat", "zhihu", "x", "xiaohongshu"}
            else "note"
        )
        raw = RawSource(
            id=source_id,
            title=title.strip(),
            source_type=source_type,  # type: ignore[arg-type]
            parsed_path="",
            file_type="text/markdown",
            size=len(content.encode("utf-8")),
            created_at=time.time(),
            session_id=session_id or None,
            source_kind=material_kind,
            source_platform="crew" if source_type == "session" else source_platform,
            adapter_name="builtin-session" if source_type == "session" else "builtin-text",
        )
        store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
        parsed_path, page, duplicate = _save_parsed_source(
            raw,
            content,
            kb_id,
            log_message=f"捕获文本并发布全文 Source 页面 {source_id} ({title.strip()})",
        )
        if duplicate is not None:
            return tool_result(
                source=raw.to_dict(),
                parsed_path=parsed_path,
                duplicate=True,
                duplicate_of=duplicate.id,
                message="文本已保存为 RawSource，但内容与已有来源重复，未发布重复 Source 页面。",
            )
        return tool_result(
            source=raw.to_dict(),
            source_page=page.to_dict(brief=True),
            message="文本已保存为不可变 RawSource，全文 Source 页面已发布并可搜索。",
        )

    def _handle_capture_text(args: dict[str, Any]) -> str:
        return _capture_text(
            str(args.get("title") or ""),
            str(args.get("content") or ""),
            "paste",
            _kb_id(args),
            current_session_id.get(),
            str(args.get("source_platform") or ""),
        )

    def _handle_capture_session(args: dict[str, Any]) -> str:
        if session_store is None:
            return tool_error("会话存储未初始化")
        sid = str(args.get("session_id") or current_session_id.get()).strip()
        history = session_store.load(sid, owner_account_id=_owner())
        if not history:
            return tool_error("会话不存在或没有可沉淀内容")
        lines: list[str] = []
        for message in history:
            if bool(getattr(message, "is_meta", False)):
                continue
            content = str(getattr(message, "content", "") or "").strip()
            if not content:
                continue
            role = getattr(message, "role", "message")
            role = getattr(role, "value", role)
            lines.append(f"## {role}\n\n{content}")
        if not lines:
            return tool_error("会话没有可沉淀的可见消息")
        title = str(args.get("title") or f"会话 {sid}")
        return _capture_text(title, "\n\n".join(lines), "session", _kb_id(args), sid)

    async def _handle_orient(args: dict[str, Any]) -> str:
        orientation = await compiler.orient(owner_account_id=_owner(), kb_id=_kb_id(args))
        data = orientation.to_dict()
        data["source_adapters"] = [item.to_dict() for item in all_adapter_statuses()]
        return tool_result(orientation=data)

    async def _handle_batch_ingest(args: dict[str, Any]) -> str:
        kb_id = _kb_id(args)
        source_ids_raw = args.get("source_ids")
        source_ids = None
        if isinstance(source_ids_raw, list):
            source_ids = [
                str(source_id).strip()
                for source_id in source_ids_raw
                if str(source_id).strip()
            ]
        cursor = max(0, int(args.get("cursor") or 0))
        batch_size = max(1, min(int(args.get("batch_size") or 5), 5))
        confirmation_id = str(args.get("confirmation_id") or "").strip()

        if confirmation_id:
            confirmed = _consume_confirmation(
                args,
                action="apply_batch_ingest",
                kb_id=kb_id,
            )
            if confirmed is None:
                return tool_error("缺少有效的批量 ingest 确认")
            confirmed_ids = [
                str(value)
                for value in (confirmed.get("source_ids") or [])
                if str(value).strip()
            ]
            result = await compiler.batch_ingest(
                source_ids=confirmed_ids,
                cursor=0,
                batch_size=min(len(confirmed_ids), 5) or 1,
                apply=True,
                use_existing_plans=True,
                owner_account_id=_owner(),
                kb_id=kb_id,
            )
            _mark_changed(
                kb_id,
                "batch_ingest_applied",
                source_ids=result["succeeded"],
                page_ids=result["page_ids"],
            )
            return tool_result(**result, auto_applied=False, confirmed=True)

        auto_apply = (config or WikiConfig()).ingest.auto_apply
        result = await compiler.batch_ingest(
            source_ids=source_ids,
            cursor=cursor,
            batch_size=batch_size,
            apply=auto_apply,
            owner_account_id=_owner(),
            kb_id=kb_id,
        )
        if auto_apply:
            _mark_changed(
                kb_id,
                "batch_ingest_applied",
                source_ids=result["succeeded"],
                page_ids=result["page_ids"],
            )
            return tool_result(**result, auto_applied=True)
        planned_ids = list(result["succeeded"])
        confirmation = manager.issue_confirmation(
            current_session_id.get(),
            action="apply_batch_ingest",
            kb_id=kb_id,
            payload={"source_ids": planned_ids},
            summary=f"应用 {len(planned_ids)} 份素材的 Wiki 批量计划",
            impact={
                "source_ids": planned_ids,
                "skipped": result["skipped"],
                "failed": result["failed"],
            },
            owner_account_id=_owner(),
        )
        return tool_result(**result, auto_applied=False, **confirmation)

    def _handle_check_duplicate(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")
        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")
        dup = store.check_source_duplicate(raw, owner_account_id=_owner(), kb_id=kb_id)
        if dup is None:
            return tool_result(duplicate=False, message="未检测到重复 source")
        return tool_result(
            duplicate=True,
            existing_source_id=dup.id,
            existing_title=dup.title,
            message=f"检测到重复 source: {dup.id} ({dup.title})",
        )

    def _handle_check_drift(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")
        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")
        drifted = store.check_source_drift(raw, owner_account_id=_owner(), kb_id=kb_id)
        return tool_result(
            drift=len(drifted) > 0,
            drift_count=len(drifted),
            drifted_sources=[{"source_id": d.id, "title": d.title, "content_sha256": d.content_sha256} for d in drifted],
            message=f"发现 {len(drifted)} 个历史漂移版本" if drifted else "未发现漂移",
        )


    def _handle_search(args: dict[str, Any]) -> str:
        query = str(args.get("query", ""))
        top_k = int(args.get("top_k", 5))
        expand_neighbors = bool(args.get("expand_neighbors", True))
        include_context = bool(args.get("include_context", True))
        if not query:
            return tool_error("缺少 query")
        result = querier.search(
            query,
            top_k=top_k,
            owner_account_id=_owner(),
            kb_id=_kb_id(args),
            expand_neighbors=expand_neighbors,
            include_context=include_context,
        )
        cards = result["pages"]
        sid = current_session_id.get()
        if sid:
            manager.add_pending_cards(sid, cards, owner_account_id=_owner())
        return tool_result(**result)

    def _handle_read(args: dict[str, Any]) -> str:
        page_id = str(args.get("page_id", ""))
        if not page_id:
            return tool_error("缺少 page_id")
        page = store.get(page_id, owner_account_id=_owner(), kb_id=_kb_id(args))
        if page is None:
            return tool_error(f"页面不存在: {page_id}")
        if not bool(args.get("include_neighbors", False)):
            return tool_result(page=page.to_dict())
        neighbor_limit = max(1, min(int(args.get("neighbor_limit", 5)), 20))
        neighbors = store.get_neighbors(
            page_id,
            owner_account_id=_owner(),
            kb_id=_kb_id(args),
        )[:neighbor_limit]
        return tool_result(
            page=page.to_dict(),
            neighbors=[n.to_dict() for n in neighbors],
            neighbor_count=len(neighbors),
        )

    async def _handle_lint(args: dict[str, Any]) -> str:
        deep = bool(args.get("deep", False))
        kb_id = _kb_id(args)
        if bool(args.get("apply_fixes")):
            confirmed = _consume_confirmation(args, action="lint_apply", kb_id=kb_id)
            if confirmed is None:
                return tool_error("缺少有效的 lint 修复确认；请重新生成修复计划")
            completed: list[str] = []
            failed: list[dict[str, str]] = []
            for fix in confirmed.get("fixes") or []:
                page_id = str(fix.get("page_id") or "")
                page = store.get(page_id, owner_account_id=_owner(), kb_id=kb_id)
                if page is None:
                    failed.append({"page_id": page_id, "error": "页面不存在"})
                    continue
                expected_title = str(fix.get("title") or "")
                if page.title != expected_title:
                    failed.append({"page_id": page_id, "error": "页面标题已变化，请重新 lint"})
                    continue
                page.content = f"# {page.title}\n\n{page.content.lstrip()}"
                if store.update(page, owner_account_id=_owner(), kb_id=kb_id) is None:
                    failed.append({"page_id": page_id, "error": "写入失败"})
                else:
                    completed.append(page_id)
            if completed:
                _finish_write(
                    kb_id,
                    f"应用 lint 安全修复: {', '.join(completed)}",
                    "lint_fixed",
                    page_ids=completed,
                )
            return tool_result(completed=completed, failed=failed)

        issues = await compiler.lint(owner_account_id=_owner(), kb_id=kb_id, deep=deep)
        if not bool(args.get("plan_fixes")):
            return tool_result(issues=issues)
        fixes: list[dict[str, str]] = []
        seen: set[str] = set()
        for issue in issues:
            page_id = str(issue.get("page_id") or "")
            if issue.get("kind") != "format_violation" or not page_id or page_id in seen:
                continue
            page = store.get(page_id, owner_account_id=_owner(), kb_id=kb_id)
            if page is not None:
                fixes.append({"page_id": page.id, "title": page.title, "action": "prepend_title_heading"})
                seen.add(page_id)
        if not fixes:
            return tool_result(issues=issues, repair_plan=[], message="没有可安全自动修复的项目")
        confirmation = manager.issue_confirmation(
            current_session_id.get(),
            action="lint_apply",
            kb_id=kb_id,
            payload={"fixes": fixes},
            summary=f"应用 {len(fixes)} 项 Wiki lint 安全修复",
            impact={"pages": fixes, "other_issues_require_manual_review": len(issues) - len(fixes)},
            owner_account_id=_owner(),
        )
        return tool_result(issues=issues, repair_plan=fixes, **confirmation)

    def _handle_create_kb(args: dict[str, Any]) -> str:
        kb_id = str(args.get("kb_id", "")).strip()
        name = str(args.get("name", kb_id) or kb_id).strip()
        if not kb_id:
            return tool_error("缺少 kb_id")
        try:
            kb = store.create_kb(kb_id, name=name, owner_account_id=_owner())
        except ValueError as exc:
            return tool_error(str(exc))
        _finish_write(kb_id, f"创建知识库 {kb_id}", "kb_created", kb_ids=[kb_id])
        return tool_result(kb=kb.to_dict())

    def _handle_delete_kb(args: dict[str, Any]) -> str:
        kb_id = str(args.get("kb_id", "")).strip()
        if not kb_id:
            return tool_error("缺少 kb_id")
        if kb_id == "default":
            return tool_error("禁止删除 default 知识库")

        owner = _owner()
        pages = store.list_all(owner_account_id=owner, kb_id=kb_id, limit=10000)
        raws = store.list_raws(owner_account_id=owner, kb_id=kb_id)

        confirmed = _consume_confirmation(args, action="delete_kb", kb_id=kb_id)
        if confirmed is None:
            return _issue_confirmation(
                action="delete_kb",
                kb_id=kb_id,
                payload={"kb_id": kb_id},
                summary=f"删除知识库 {kb_id}",
                impact={
                    "pages": len(pages),
                    "raw_sources": len(raws),
                    "cannot_undo": True,
                },
            )

        try:
            store.append_log([f"删除知识库 {kb_id}"], owner_account_id=owner, kb_id=kb_id)
            ok = store.delete_kb(kb_id, owner_account_id=owner)
        except ValueError as exc:
            return tool_error(str(exc))
        if not ok:
            return tool_error(f"知识库不存在: {kb_id}")
        _mark_changed(kb_id, "kb_deleted", kb_ids=[kb_id])
        return tool_result(message=f"已删除知识库: {kb_id}")

    def _handle_delete_source(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")
        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")

        linked_pages = [
            page for page in store.list_all(owner_account_id=_owner(), kb_id=kb_id, limit=10000)
            if source_id in page.sources
        ]

        confirmed = _consume_confirmation(args, action="delete_source", kb_id=kb_id)
        if confirmed is None:
            return _issue_confirmation(
                action="delete_source",
                kb_id=kb_id,
                payload={"source_id": source_id},
                summary=f"删除 RawSource {source_id} 及关联页面",
                impact={
                    "linked_pages": len(linked_pages),
                    "linked_page_titles": [p.title for p in linked_pages[:20]],
                    "cannot_undo": True,
                },
            )
        if str(confirmed.get("source_id") or "") != source_id:
            return tool_error("确认内容与当前 source 参数不一致，请重新生成确认卡")

        ok = store.delete_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if not ok:
            return tool_error(f"删除 raw source 失败: {source_id}")
        _finish_write(
            kb_id,
            f"删除 raw source {source_id} 及其关联页面",
            "source_deleted",
            source_ids=[source_id],
        )
        return tool_result(message=f"已删除 raw source 及其关联页面: {source_id}")

    def _handle_describe_image(args: dict[str, Any]) -> str:
        if config is None or not config.multimodal.enabled:
            return tool_error("Wiki 多模态功能未启用")

        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")

        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")
        if raw.source_type != "image":
            return tool_error(f"Source 类型不是图片: {raw.source_type}")
        if not raw.original_path:
            return tool_error("Raw source 缺少原文件路径")

        from .multimodal import MediaUnderstandingError, describe_image

        prompt = str(args.get("prompt") or config.multimodal.prompt_image or "")
        try:
            description = describe_image(raw.original_path, prompt or None)
        except MediaUnderstandingError as exc:
            return tool_error(str(exc))
        try:
            parsed_path, page, duplicate = _save_parsed_source(
                raw,
                description,
                kb_id,
                log_message=f"理解图片并发布全文 Source 页面 {source_id}",
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"图片描述保存失败: {exc}")
        if duplicate is not None:
            return tool_result(
                description=description,
                parsed_path=parsed_path,
                duplicate=True,
                duplicate_of=duplicate.id,
            )
        return tool_result(
            description=description,
            parsed_path=parsed_path,
            source_page=page.to_dict(brief=True),
        )

    async def _handle_describe_video(args: dict[str, Any]) -> str:
        if config is None or not config.multimodal.enabled:
            return tool_error("Wiki 多模态功能未启用")

        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")

        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")
        if raw.source_type != "video":
            return tool_error(f"Source 类型不是视频: {raw.source_type}")
        if not raw.original_path:
            return tool_error("Raw source 缺少原文件路径")

        from .multimodal import MediaUnderstandingError, describe_video

        confirmed = _consume_confirmation(args, action="describe_video", kb_id=kb_id)
        if confirmed is None:
            return _issue_confirmation(
                action="describe_video",
                kb_id=kb_id,
                payload={"source_id": source_id},
                summary=f"将视频 {raw.title} 上传到外部云端进行理解",
                impact={"external_service": "configured_media_provider", "privacy_risk": True},
            )
        if str(confirmed.get("source_id") or "") != source_id:
            return tool_error("确认内容与当前视频参数不一致，请重新生成确认卡")
        prompt = str(args.get("prompt") or config.multimodal.prompt_video or "")
        try:
            description = describe_video(raw.original_path, prompt or None, confirm_upload=True)
        except MediaUnderstandingError as exc:
            if exc.needs_confirmation:
                return tool_result(
                    needs_confirmation=True,
                    message=str(exc),
                    security_notice=(
                        "视频理解需要将视频上传到已配置的外部媒体分析服务。"
                        "请确认视频不包含个人隐私、敏感信息或受保护内容，并取得用户明确同意后，"
                        "再次调用 wiki_parse_source(source_id, confirmation_id=...)。"
                    ),
                )
            return tool_error(str(exc))
        try:
            parsed_path, page, duplicate = _save_parsed_source(
                raw,
                description,
                kb_id,
                log_message=f"理解视频并发布全文 Source 页面 {source_id}",
            )
        except Exception as exc:  # noqa: BLE001
            return tool_error(f"视频描述保存失败: {exc}")
        if duplicate is not None:
            return tool_result(
                description=description,
                parsed_path=parsed_path,
                duplicate=True,
                duplicate_of=duplicate.id,
            )
        return tool_result(
            description=description,
            parsed_path=parsed_path,
            source_page=page.to_dict(brief=True),
        )

    async def _handle_parse_source(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")

        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")
        if raw.source_type == "image":
            return _handle_describe_image(args)
        if raw.source_type == "video":
            return await _handle_describe_video(args)
        if not raw.original_path:
            return tool_error(f"Raw source 缺少原文件路径: {source_id}")

        from pathlib import Path

        original_path = Path(raw.original_path)
        if not original_path.is_file():
            return tool_error(f"原文件不存在: {raw.original_path}")

        try:
            content = original_path.read_bytes()
            # ``title`` 仅用于展示，格式必须以不可变原文件及其内容为准。
            text = await asyncio.to_thread(parse_document_from_bytes, content, original_path.name)
        except MissingDependencyError as exc:
            raw.parse_status = "failed"
            raw.parse_error = f"缺少依赖: {exc}"
            store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
            return tool_error(
                f"解析失败（缺少依赖）: {exc}。请安装依赖后重试：{exc.install_command}"
            )
        except Exception as exc:  # noqa: BLE001
            error_msg = f"解析失败: {exc}"
            raw.parse_status = "failed"
            raw.parse_error = error_msg
            store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
            return tool_error(f"{error_msg}。请保留当前 RawSource，修复解析依赖或重新上传受支持格式后重试。")

        raw.parse_error = None
        try:
            parsed_path, page, duplicate = _save_parsed_source(
                raw,
                text,
                kb_id,
                log_message=f"解析并发布全文 Source 页面 {source_id}",
            )
        except Exception as exc:  # noqa: BLE001
            raw.parse_status = "failed"
            raw.parse_error = f"全文 Source 页面创建失败: {exc}"
            store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
            return tool_error(raw.parse_error)
        if duplicate is not None:
            return tool_result(
                source_id=source_id,
                parsed_path=parsed_path,
                parse_status="parsed",
                duplicate=True,
                duplicate_of=duplicate.id,
                message="解析成功，但内容与已有来源重复，未发布重复 Source 页面，也无需继续深度入库。",
            )
        return tool_result(
            source_id=source_id,
            parsed_path=parsed_path,
            parse_status="parsed",
            source_page=page.to_dict(brief=True),
            message=(
                "解析与质量检查成功，全文 Source 页面已发布并可搜索。"
                "默认上传流程下一步调用 wiki_orient 和 wiki_plan_ingest 生成深度整理计划；"
                "plan 工具会根据 wiki.ingest.auto_apply 自动应用或返回待确认计划。"
            ),
        )

    def _handle_list_sources(args: dict[str, Any]) -> str:
        status = str(args.get("status") or "all").strip().lower()
        include_history = bool(args.get("include_history", False))
        limit = max(1, int(args.get("limit", 50)))
        raws = store.list_raws(owner_account_id=_owner(), kb_id=_kb_id(args))
        # 默认只列当前版本；被取代的旧版本需显式 include_history=true 才返回，
        # 避免旧版本在列表中与当前版本混淆。
        if not include_history:
            raws = [r for r in raws if r.is_current]
        if status != "all":
            raws = [r for r in raws if (r.parse_status or "pending") == status]
        raws = sorted(raws, key=lambda r: r.created_at, reverse=True)[:limit]
        return tool_result(
            sources=[
                {
                    "source_id": r.id,
                    "title": r.title,
                    "source_type": r.source_type,
                    "parse_status": r.parse_status or "pending",
                    "file_type": r.file_type,
                    "size": r.size,
                    "created_at": r.created_at,
                    "is_current": r.is_current,
                    "superseded_by": r.superseded_by,
                    "drift_from": r.drift_from,
                    "last_refresh_at": r.last_refresh_at or None,
                    "last_refresh_error": r.last_refresh_error,
                }
                for r in raws
            ],
            count=len(raws),
            kb_id=_kb_id(args),
        )

    def _handle_list_kbs(args: dict[str, Any]) -> str:
        kbs = store.list_kbs(owner_account_id=_owner())
        return tool_result(
            kbs=[
                {
                    "kb_id": kb.id,
                    "name": kb.name,
                    "page_count": kb.summary.page_count,
                    "source_count": kb.summary.source_count,
                    "vault_path": kb.vault_path,
                }
                for kb in kbs
            ],
            count=len(kbs),
        )

    def _handle_list_inbox(args: dict[str, Any]) -> str:
        """列出已解析且系统建议深度整理、但尚未 ingest 的素材。"""
        limit = max(1, int(args.get("limit", 50)))
        kb_id = _kb_id(args)
        raws = store.list_raws(owner_account_id=_owner(), kb_id=kb_id)
        inbox = [
            raw
            for raw in raws
            if raw.is_current
            and (raw.parse_status or "pending") == "parsed"
            and raw.ingest_recommend
            and raw.ingest_status in ("pending", "recommended", "failed")
        ]
        inbox = sorted(inbox, key=lambda r: r.created_at, reverse=True)[:limit]
        return tool_result(
            sources=[
                {
                    "source_id": r.id,
                    "title": r.title,
                    "source_type": r.source_type,
                    "doc_type": r.doc_type,
                    "summary": r.summary,
                    "tags": r.tags,
                    "ingest_recommend": r.ingest_recommend,
                    "ingest_reason": r.ingest_reason,
                    "ingest_status": r.ingest_status,
                    "created_at": r.created_at,
                }
                for r in inbox
            ],
            count=len(inbox),
            kb_id=kb_id,
        )

    def _handle_create_page(args: dict[str, Any]) -> str:
        from .schemas import WikiPage, WikiRelation

        title = str(args.get("title") or "").strip()
        content = str(args.get("content") or "").strip()
        kb_id = _kb_id(args)
        if not title or not content:
            return tool_error("页面标题和内容不能为空")
        existing = store.get_by_title(title, owner_account_id=_owner(), kb_id=kb_id)
        if existing is not None:
            return tool_error(f"同名页面已存在: {existing.id}")
        page = WikiPage(
            id="",
            page_type=str(args.get("page_type") or "topic"),  # type: ignore[arg-type]
            title=title,
            content=content,
            file_path="",
            tags=[str(item) for item in args.get("tags") or []],
            relations=[
                WikiRelation.from_dict(item)
                for item in args.get("relations") or []
                if isinstance(item, dict)
            ],
        )
        saved = store.save_page(page, owner_account_id=_owner(), kb_id=kb_id)
        _finish_write(kb_id, f"创建页面 {saved.id} ({saved.title})", "page_created", page_ids=[saved.id])
        return tool_result(page=saved.to_dict())

    def _handle_delete_pages(args: dict[str, Any]) -> str:
        kb_id = _kb_id(args)
        requested = list(dict.fromkeys(str(item).strip() for item in args.get("page_ids") or [] if str(item).strip()))
        if not requested:
            return tool_error("page_ids 不能为空")
        pages = [page for page_id in requested if (page := store.get(page_id, owner_account_id=_owner(), kb_id=kb_id))]
        if not pages:
            return tool_error("没有找到可删除页面")
        target_ids = {page.id for page in pages}
        deleted_titles = {
            value
            for page in pages
            for value in [page.title, *page.aliases]
            if value
        }
        remaining = [
            page
            for page in store.list_all(owner_account_id=_owner(), kb_id=kb_id, limit=10000)
            if page.id not in target_ids
        ]
        inbound_reference_pages = sum(
            1
            for page in remaining
            if any(value in deleted_titles for value in page.related)
            or any(relation.target_page_id in target_ids for relation in page.relations)
            or any(f"[[{value}]]" in page.content for value in deleted_titles)
        )
        confirmed = _consume_confirmation(args, action="delete_pages", kb_id=kb_id)
        if confirmed is None:
            return _issue_confirmation(
                action="delete_pages",
                kb_id=kb_id,
                payload={"page_ids": [page.id for page in pages]},
                summary=f"删除 {len(pages)} 个 Wiki 页面",
                impact={
                    "pages": [{"page_id": page.id, "title": page.title} for page in pages],
                    "inbound_reference_pages": inbound_reference_pages,
                    "cannot_undo": True,
                },
            )
        approved = [str(item) for item in confirmed.get("page_ids") or []]
        if set(approved) != {page.id for page in pages}:
            return tool_error("确认内容与当前删除参数不一致，请重新生成确认卡")
        deleted: list[str] = []
        failed: list[str] = []
        for page_id in approved:
            (deleted if store.delete(page_id, owner_account_id=_owner(), kb_id=kb_id) else failed).append(page_id)
        updated_references: list[str] = []
        if deleted:
            for other in remaining:
                changed = False
                related = [value for value in other.related if value not in deleted_titles]
                if related != other.related:
                    other.related = related
                    changed = True
                relations = [
                    relation
                    for relation in other.relations
                    if relation.target_page_id not in target_ids
                ]
                if len(relations) != len(other.relations):
                    other.relations = relations
                    changed = True
                for title in deleted_titles:
                    link = f"[[{title}]]"
                    if link in other.content:
                        other.content = other.content.replace(link, title)
                        changed = True
                if changed and store.update(other, owner_account_id=_owner(), kb_id=kb_id):
                    updated_references.append(other.id)
            _finish_write(
                kb_id,
                f"删除页面: {', '.join(deleted)}",
                "pages_deleted",
                page_ids=[*deleted, *updated_references],
            )
        return tool_result(
            deleted=deleted,
            failed=failed,
            updated_references=len(updated_references),
        )

    def _handle_rename_page(args: dict[str, Any]) -> str:
        page_id = str(args.get("page_id") or "").strip()
        new_title = str(args.get("new_title") or "").strip()
        kb_id = _kb_id(args)
        if not page_id or not new_title:
            return tool_error("page_id 和 new_title 不能为空")
        page = store.get(page_id, owner_account_id=_owner(), kb_id=kb_id)
        if page is None:
            return tool_error(f"页面不存在: {page_id}")
        duplicate = store.get_by_title(new_title, owner_account_id=_owner(), kb_id=kb_id)
        if duplicate is not None and duplicate.id != page.id:
            return tool_error(f"目标标题已存在: {duplicate.id}")
        old_title = page.title
        page.title = new_title
        if old_title not in page.aliases:
            page.aliases.append(old_title)
        if store.update(page, owner_account_id=_owner(), kb_id=kb_id) is None:
            return tool_error("重命名页面失败")
        changed_ids = [page.id]
        old_link = f"[[{old_title}]]"
        new_link = f"[[{new_title}]]"
        for other in store.list_all(owner_account_id=_owner(), kb_id=kb_id, limit=10000):
            if other.id == page.id:
                continue
            changed = False
            if old_link in other.content:
                other.content = other.content.replace(old_link, new_link)
                changed = True
            if changed and store.update(other, owner_account_id=_owner(), kb_id=kb_id):
                changed_ids.append(other.id)
        _finish_write(kb_id, f"页面重命名 {old_title} → {new_title}", "page_renamed", page_ids=changed_ids)
        return tool_result(page=page.to_dict(), updated_references=len(changed_ids) - 1)

    def _handle_update_page(args: dict[str, Any]) -> str:
        page_id = str(args.get("page_id", "")).strip()
        if not page_id:
            return tool_error("缺少 page_id")

        page = store.get(page_id, owner_account_id=_owner(), kb_id=_kb_id(args))
        if page is None:
            return tool_error(f"页面不存在: {page_id}")

        changed_fields: list[str] = []
        if "content" in args:
            page.content = str(args["content"])
            changed_fields.append("content")
        if "relations" in args:
            from .schemas import WikiRelation

            page.relations = [
                WikiRelation.from_dict(item)
                for item in args["relations"]
                if isinstance(item, dict)
            ]
            page.related = []
            changed_fields.append("relations")
        if "tags" in args:
            page.tags = [str(t) for t in args["tags"] if t is not None]
            changed_fields.append("tags")

        updated = store.update(page, owner_account_id=_owner(), kb_id=_kb_id(args))
        if updated is None:
            return tool_error(f"更新页面失败: {page_id}")
        if changed_fields:
            kb_id = _kb_id(args)
            _finish_write(
                kb_id,
                f"更新页面 {page_id} ({page.title})，字段: {', '.join(changed_fields)}",
                "page_updated",
                page_ids=[page_id],
            )
        return tool_result(page=updated.to_dict())

    async def _apply_ingest_plan(
        source_id: str,
        kb_id: str,
        *,
        approved_titles: list[str] | None = None,
        chunk_size: int | None = None,
        use_chunking: bool | None = None,
    ) -> Any:
        progress = _build_progress_callback()
        result = await compiler.apply_ingest(
            source_id,
            owner_account_id=_owner(),
            kb_id=kb_id,
            approved_titles=approved_titles or None,
            progress=progress,
            chunk_size=chunk_size,
            use_chunking=use_chunking,
        )
        # compiler.apply_ingest 已负责 index、log、摘要失效；工具层只推送变更事件，
        # 避免同一次 apply 重建两次索引并写入两条操作日志。
        if result.pages:
            _mark_changed(
                kb_id,
                "ingest_applied",
                page_ids=[p.id for p in result.pages],
                source_ids=[source_id],
            )
        return result

    async def _handle_plan_ingest(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")

        chunk_size = args.get("chunk_size")
        if chunk_size is not None:
            chunk_size = int(chunk_size)
        use_chunking = args.get("use_chunking")
        if use_chunking is not None:
            use_chunking = bool(use_chunking)

        kb_id = _kb_id_for_source(args, source_id)
        result = await compiler.plan_ingest(
            source_id,
            owner_account_id=_owner(),
            kb_id=kb_id,
            chunk_size=chunk_size,
            use_chunking=use_chunking,
        )
        analysis_failed = any(
            str(issue).startswith("LLM 分析失败:")
            for issue in result.issues
        )
        if analysis_failed:
            # parsed Markdown 和全文 Source 已经独立完成；模型容量不足时不签发一个
            # 只有 source 页的误导性 apply 确认，交还 Agent 稍后重试分析。
            return tool_result(
                **result.to_dict(brief=True),
                analysis_status="failed",
                retryable=True,
                message="LLM 分析未完成；已解析 Markdown 与 Source 页面仍保留，可切换模型或稍后重试。",
            )
        duplicate_skipped = not result.planned_pages and any(
            "重复 source" in str(issue) for issue in result.issues
        )
        if duplicate_skipped:
            return tool_result(
                **result.to_dict(brief=True),
                auto_applied=False,
                skipped=True,
                message="来源内容与已有 RawSource 相同，已跳过知识编译。",
            )
        if (config or WikiConfig()).ingest.auto_apply:
            applied = await _apply_ingest_plan(
                source_id,
                kb_id,
                chunk_size=chunk_size,
                use_chunking=use_chunking,
            )
            return tool_result(
                **result.to_dict(brief=True),
                auto_applied=True,
                applied_pages=[page.to_dict(brief=True) for page in applied.pages],
                apply_issues=applied.issues,
                message="深度整理计划已按 wiki.ingest.auto_apply=true 自动应用。",
            )
        confirmation = manager.issue_confirmation(
            current_session_id.get(),
            action="apply_ingest",
            kb_id=kb_id,
            payload={
                "source_id": source_id,
                "source_content_sha256": result.source_content_sha256,
                "plan_fingerprint": result.plan_fingerprint,
                "planned_titles": [p.title for p in result.planned_pages],
            },
            summary=f"应用 source {source_id} 的 Wiki 变更计划",
            impact={
                "create": result.total_new,
                "update": result.total_update,
                "contest": result.total_contested,
                "issues": result.issues,
            },
            owner_account_id=_owner(),
        )
        # 计划预览不返回 source 全文，避免大文档撑满 Agent 上下文。
        return tool_result(**result.to_dict(brief=True), **confirmation)

    async def _handle_apply_ingest(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")
        kb_id = _kb_id_for_source(args, source_id)
        confirmed = _consume_confirmation(args, action="apply_ingest", kb_id=kb_id)
        if confirmed is None or str(confirmed.get("source_id") or "") != source_id:
            return tool_error("缺少有效的 ingest 确认；请重新调用 wiki_plan_ingest 并等待用户确认")

        # 计划指纹绑定：确认卡记录的是用户当时看到的计划指纹。
        # 若确认前系统又为同一 source 生成新计划，磁盘 plan 指纹会变化，旧确认作废。
        confirmed_fingerprint = str(confirmed.get("plan_fingerprint") or "")
        confirmed_hash = str(confirmed.get("source_content_sha256") or "")
        confirmed_titles = set(
            str(t) for t in (confirmed.get("planned_titles") or []) if str(t).strip()
        )
        disk_plan = compiler.load_plan(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if disk_plan is None:
            return tool_error("未找到 ingest 计划；请重新调用 wiki_plan_ingest")
        if not confirmed_fingerprint or confirmed_fingerprint != disk_plan.plan_fingerprint:
            return tool_error(
                "确认的计划已过期（计划已被重新生成）；请重新调用 wiki_plan_ingest 并等待用户确认"
            )
        if confirmed_hash and disk_plan.source_content_sha256 != confirmed_hash:
            return tool_error(
                "确认的计划与当前 source 内容版本不一致；请重新调用 wiki_plan_ingest"
            )

        approved_titles = args.get("approved_titles")
        if approved_titles is not None:
            approved_titles = [str(t) for t in approved_titles if t is not None]
            # 用户挑选的标题必须来自确认时展示的计划，禁止借旧确认写入计划外页面。
            approved_set = {t.strip() for t in approved_titles if t.strip()}
            extra = approved_set - confirmed_titles
            if extra:
                return tool_error(
                    f"approved_titles 含有计划外的页面: {', '.join(sorted(extra))}；"
                    "请重新调用 wiki_plan_ingest"
                )

        chunk_size = args.get("chunk_size")
        if chunk_size is not None:
            chunk_size = int(chunk_size)
        use_chunking = args.get("use_chunking")
        if use_chunking is not None:
            use_chunking = bool(use_chunking)

        result = await _apply_ingest_plan(
            source_id,
            kb_id,
            approved_titles=approved_titles or None,
            chunk_size=chunk_size,
            use_chunking=use_chunking,
        )
        return tool_result(
            source_id=result.source_id,
            pages=[p.to_dict() for p in result.pages],
            issues=result.issues,
        )

    def _capture_url(
        url: str,
        title: str,
        kb_id: str,
        *,
        refresh_from: Any | None = None,
    ) -> str:
        import hashlib
        import time
        import uuid

        from .schemas import RawSource

        source_kind, platform = classify_url(url)
        status = adapter_status(platform)

        def _failed_extraction(
            state: str,
            detail: str,
            adapter_data: dict[str, Any],
        ) -> str:
            if refresh_from is not None:
                # 不可变旧版本：刷新失败不改其 parse_status/extraction_state，
                # 旧版本继续 parsed/available。只记录刷新尝试结果，避免破坏已可用版本。
                refresh_from.last_refresh_at = time.time()
                refresh_from.last_refresh_error = detail
                store.save_raw(refresh_from, owner_account_id=_owner(), kb_id=kb_id)
                return tool_result(
                    extracted=False,
                    changed=False,
                    source_id=refresh_from.id,
                    adapter=adapter_data,
                    message="刷新失败，旧版本仍可用；详情见 last_refresh_error。",
                )
            source_id = f"url_{uuid.uuid4().hex[:12]}"
            failed = RawSource(
                id=source_id,
                title=title or url,
                source_type="url",
                parsed_path="",
                source_url=url,
                file_type="text/html",
                parse_status="failed",
                parse_error=detail,
                created_at=time.time(),
                source_kind=source_kind,
                source_platform=platform,
                adapter_name=status.adapter_name,
                original_ref=url,
                extraction_state=state,  # type: ignore[arg-type]
            )
            store.save_raw(failed, owner_account_id=_owner(), kb_id=kb_id)
            _finish_write(
                kb_id,
                f"记录 URL 提取失败 {source_id} ({url})",
                "source_extraction_failed",
                source_ids=[source_id],
            )
            return tool_result(
                extracted=False,
                source_id=source_id,
                adapter=adapter_data,
            )

        if status.state not in {"available"}:
            return _failed_extraction(
                status.state,
                status.detail,
                status.to_dict(),
            )

        markdown_text = ""
        final_url = url
        video_id = ""
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                if platform == "youtube":
                    markdown_text, video_id = fetch_youtube_transcript(url)
                else:
                    markdown_text, final_url = fetch_url_to_markdown(url)
                markdown_text = validate_parsed_text(markdown_text, url)
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        if last_error is not None:
            detail = f"自动提取失败: {last_error}"
            return _failed_extraction(
                "runtime_failed",
                detail,
                {
                    **status.to_dict(),
                    "state": "runtime_failed",
                    "detail": detail,
                    "recovery_action": "已自动重试一次；请改用手动粘贴入口",
                },
            )
        if not markdown_text.strip():
            detail = "自动提取未获得有效正文"
            return _failed_extraction(
                "empty_result",
                detail,
                {
                    **status.to_dict(),
                    "state": "empty_result",
                    "detail": detail,
                },
            )

        content_hash = hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()
        if refresh_from is not None and refresh_from.content_sha256 == content_hash:
            # 内容未变化：保留当前不可变版本，仅刷新时间戳并清空历史刷新错误。
            refresh_from.extraction_state = "available"
            refresh_from.parse_error = None
            refresh_from.last_refresh_at = time.time()
            refresh_from.last_refresh_error = None
            store.save_raw(refresh_from, owner_account_id=_owner(), kb_id=kb_id)
            return tool_result(
                extracted=True,
                changed=False,
                source_id=refresh_from.id,
                content_sha256=content_hash,
                adapter=status.to_dict(),
                message="来源内容未变化，保留当前不可变版本。",
            )

        source_id = f"url_{uuid.uuid4().hex[:12]}"
        if not title:
            # 从 URL 路径提取标题
            from urllib.parse import urlparse

            parsed = urlparse(final_url)
            path_parts = [p for p in parsed.path.strip("/").split("/") if p]
            title = path_parts[-1] if path_parts else final_url

        # 创建 RawSource
        raw = RawSource(
            id=source_id,
            title=title,
            source_type="url",
            parsed_path="",
            source_url=final_url,
            file_type="text/markdown" if platform == "youtube" else "text/html",
            parse_status="pending",
            size=len(markdown_text.encode("utf-8")),
            created_at=time.time(),
            content_sha256=content_hash,
            drift_from=refresh_from.id if refresh_from is not None else None,
            source_kind=source_kind,
            source_platform=platform,
            adapter_name=status.adapter_name,
            original_ref=final_url,
            extraction_state="available",
        )
        store.save_raw(raw, owner_account_id=_owner(), kb_id=kb_id)
        # 刷新出内容变化的新版本：旧版本被取代，默认检索与入库只认当前版本，
        # 防止旧版本 Source 页继续参与搜索与综合。
        if refresh_from is not None:
            refresh_from.superseded_by = raw.id
            refresh_from.last_refresh_at = time.time()
            refresh_from.last_refresh_error = None
            store.save_raw(refresh_from, owner_account_id=_owner(), kb_id=kb_id)
        parsed_path, page, duplicate = _save_parsed_source(
            raw,
            markdown_text,
            kb_id,
            log_message=f"抓取 URL 并发布全文 Source 页面 {source_id} ({title})",
        )
        if duplicate is not None:
            return tool_result(
                extracted=True,
                changed=refresh_from is not None,
                source_id=source_id,
                title=title,
                url=final_url,
                parsed_path=parsed_path,
                duplicate=True,
                duplicate_of=duplicate.id,
                adapter=status.to_dict(),
                message="URL 内容已保存，但与已有来源重复，未发布重复 Source 页面。",
            )

        return tool_result(
            extracted=True,
            changed=refresh_from is not None,
            source_id=source_id,
            title=title,
            url=final_url,
            video_id=video_id,
            source_kind=source_kind,
            source_platform=platform,
            adapter=status.to_dict(),
            content_length=len(markdown_text),
            source_page=page.to_dict(brief=True),
            message="URL 内容已抓取、通过质量检查并发布为可搜索的全文 Source 页面。",
        )

    def _handle_fetch_url(args: dict[str, Any]) -> str:
        url = str(args.get("url", "")).strip()
        if not url:
            return tool_error("缺少 url")
        return _capture_url(
            url,
            str(args.get("title", "")).strip(),
            _kb_id(args),
        )

    def _handle_refresh_source(args: dict[str, Any]) -> str:
        source_id = str(args.get("source_id", "")).strip()
        if not source_id:
            return tool_error("缺少 source_id")
        kb_id = _kb_id_for_source(args, source_id)
        raw = store.load_raw(source_id, owner_account_id=_owner(), kb_id=kb_id)
        if raw is None:
            return tool_error(f"Raw source 不存在: {source_id}")
        if raw.source_type != "url" or not raw.source_url:
            return tool_error("只有带 source_url 的 URL RawSource 可以刷新")
        return _capture_url(raw.source_url, raw.title, kb_id, refresh_from=raw)

    async def _handle_digest(args: dict[str, Any]) -> str:
        topic = str(args.get("topic") or "").strip()
        if not topic:
            return tool_error("缺少 topic")
        kb_id = _kb_id(args)
        try:
            page = await compiler.digest(
                topic,
                mode=str(args.get("mode") or "auto"),
                owner_account_id=_owner(),
                kb_id=kb_id,
            )
        except ValueError as exc:
            return tool_error(str(exc))
        _mark_changed(kb_id, "page_digested", page_ids=[page.id])
        return tool_result(
            page=page.to_dict(),
            vault_path=str(store.get_vault_path(_owner(), kb_id)),
        )

    _TOOLS: list[tuple[Any, Any, bool, str, str, str, str]] = [
        (_WIKI_ORIENT_SCHEMA, _handle_orient, True, "🧭", "了解 Wiki", "了解 Wiki 知识库", "wiki orient overview schema index status understand"),
        (_WIKI_BATCH_INGEST_SCHEMA, _handle_batch_ingest, True, "📚", "批量整理 Wiki", "批量整理 Wiki", "wiki batch ingest five sources"),
        (_WIKI_SEARCH_SCHEMA, _handle_search, False, "🗂️", "搜索 Wiki", "搜索 Wiki", "wiki search keyword pages find"),
        (_WIKI_READ_SCHEMA, _handle_read, False, "📄", "读取 Wiki 页面", "读取 Wiki 页面", "wiki read page content view"),
        (_WIKI_LINT_SCHEMA, _handle_lint, True, "🧹", "检查 Wiki", "检查 Wiki", "wiki lint check quality issues broken links orphan"),
        (_WIKI_CREATE_KB_SCHEMA, _handle_create_kb, False, "📚", "创建知识库", "创建知识库 {kb_id}", "wiki create knowledge base new kb"),
        (_WIKI_DELETE_KB_SCHEMA, _handle_delete_kb, False, "🗑️", "删除知识库", "删除知识库 {kb_id}", "wiki delete knowledge base remove kb"),
        (_WIKI_DELETE_SOURCE_SCHEMA, _handle_delete_source, False, "🗑️", "删除 Raw Source", "删除 Raw Source {source_id}", "wiki delete raw source remove"),
        (_WIKI_PARSE_SOURCE_SCHEMA, _handle_parse_source, True, "🔧", "重新解析 Raw Source", "重新解析 {source_id}", "wiki parse source reparse document extract text"),
        (_WIKI_LIST_SOURCES_SCHEMA, _handle_list_sources, False, "📋", "列出 Raw Sources", "列出 Raw Sources", "wiki list sources raw files pending parsed failed"),
        (_WIKI_LIST_KBS_SCHEMA, _handle_list_kbs, False, "📚", "列出知识库", "列出知识库", "wiki list knowledge bases kbs"),
        (_WIKI_LIST_INBOX_SCHEMA, _handle_list_inbox, False, "📥", "列出待整理素材", "列出待整理素材", "wiki list inbox pending sources recommend ingest"),
        (_WIKI_UPDATE_PAGE_SCHEMA, _handle_update_page, False, "✏️", "更新 Wiki 页面", "更新页面 {page_id}", "wiki update page edit content tags related aliases"),
        (_WIKI_PLAN_INGEST_SCHEMA, _handle_plan_ingest, True, "📋", "计划 Wiki 变更", "计划变更 {source_id}", "wiki plan ingest preview changes proposed pages"),
        (_WIKI_APPLY_INGEST_SCHEMA, _handle_apply_ingest, True, "✅", "执行 Wiki 变更", "执行变更 {source_id}", "wiki apply ingest write pages confirm plan"),
        (_WIKI_FETCH_URL_SCHEMA, _handle_fetch_url, False, "🌐", "抓取网页", "抓取网页 {url}", "wiki fetch url webpage scrape crawl import"),
        (_WIKI_REFRESH_SOURCE_SCHEMA, _handle_refresh_source, False, "🔄", "刷新网页来源", "刷新来源 {source_id}", "wiki refresh url source drift version"),
        (_WIKI_DIGEST_SCHEMA, _handle_digest, True, "🧠", "生成跨来源报告", "综合 {topic}", "wiki digest synthesis comparison multi source"),
        (_WIKI_CAPTURE_ATTACHMENT_SCHEMA, _handle_capture_attachment, False, "📎", "捕获 Wiki 附件", "捕获附件 {path}", "wiki capture attachment file import upload"),
        (_WIKI_CAPTURE_TEXT_SCHEMA, _handle_capture_text, False, "📝", "捕获 Wiki 文本", "捕获文本 {title}", "wiki capture text snippet import note"),
        (_WIKI_CAPTURE_SESSION_SCHEMA, _handle_capture_session, False, "💬", "沉淀会话", "沉淀会话 {session_id}", "wiki capture session conversation import chat"),
        (_WIKI_CREATE_PAGE_SCHEMA, _handle_create_page, False, "📄", "创建 Wiki 页面", "创建页面 {title}", "wiki create page new manual write"),
        (_WIKI_DELETE_PAGES_SCHEMA, _handle_delete_pages, False, "🗑️", "删除 Wiki 页面", "删除 Wiki 页面", "wiki delete pages remove"),
        (_WIKI_RENAME_PAGE_SCHEMA, _handle_rename_page, False, "✏️", "重命名 Wiki 页面", "重命名 {page_id}", "wiki rename page title change"),
    ]
    read_tools = set(WIKI_READ_TOOLS)
    for schema, handler, is_async, emoji, display_name, ui_label, search_hint in _TOOLS:
        toolset = WIKI_READ_TOOLSET if schema["name"] in read_tools else WIKI_MANAGE_TOOLSET
        registry.register(
            name=schema["name"],
            toolset=toolset,
            schema=schema,
            handler=handler,
            is_async=is_async,
            emoji=emoji,
            display_name=display_name,
            ui_label_template=ui_label,
            search_hint=search_hint,
        )
