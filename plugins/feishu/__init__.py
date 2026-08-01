"""Feishu/Lark document tools for Crew.

Crew has Feishu as gateway/platform code plus Feishu document tools. Crew
does not have platform plugins yet, so this plugin ports the document/drive
tool surface as standalone tools.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _api_base() -> str:
    domain = os.getenv("FEISHU_DOMAIN", "feishu").strip().lower()
    if domain == "lark":
        return "https://open.larksuite.com"
    return "https://open.feishu.cn"


def _has_credentials() -> bool:
    return bool(os.getenv("FEISHU_APP_ID") and os.getenv("FEISHU_APP_SECRET"))


def _json_result(data: Any = None, **kwargs: Any) -> str:
    payload = data if data is not None else kwargs
    return json.dumps(payload, ensure_ascii=False)


def _json_error(message: str, **extra: Any) -> str:
    payload = {"error": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _request_json(method: str, path: str, *, token: str | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = _api_base() + path
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - explicit integration tool
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            parsed = {"raw": raw.decode("utf-8", errors="replace")}
        return {"code": exc.code, "msg": str(exc), "data": parsed}
    return json.loads(raw.decode("utf-8", errors="replace"))


def _tenant_access_token() -> tuple[str | None, str | None]:
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        return None, "FEISHU_APP_ID and FEISHU_APP_SECRET are required"
    payload = _request_json(
        "POST",
        "/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    if payload.get("code") != 0:
        return None, f"tenant token failed: code={payload.get('code')} msg={payload.get('msg')}"
    token = payload.get("tenant_access_token")
    if not token:
        return None, "tenant token missing in response"
    return str(token), None


FEISHU_DOC_READ_SCHEMA = {
    "name": "feishu_doc_read",
    "description": "Read the raw text content of a Feishu/Lark Docx document.",
    "parameters": {
        "type": "object",
        "properties": {
            "doc_token": {"type": "string", "description": "Docx document token."},
        },
        "required": ["doc_token"],
    },
}

FEISHU_DRIVE_LIST_COMMENTS_SCHEMA = {
    "name": "feishu_drive_list_comments",
    "description": "List comments on a Feishu/Lark document or drive file.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {"type": "string", "description": "Document/file token."},
            "file_type": {"type": "string", "description": "File type, default docx."},
            "is_whole": {"type": "boolean", "description": "Only list whole-document comments."},
            "page_size": {"type": "integer", "description": "Page size, max 100."},
            "page_token": {"type": "string", "description": "Pagination token."},
        },
        "required": ["file_token"],
    },
}

FEISHU_DRIVE_LIST_REPLIES_SCHEMA = {
    "name": "feishu_drive_list_comment_replies",
    "description": "List replies in a Feishu/Lark document comment thread.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {"type": "string", "description": "Document/file token."},
            "comment_id": {"type": "string", "description": "Comment thread ID."},
            "file_type": {"type": "string", "description": "File type, default docx."},
            "page_size": {"type": "integer", "description": "Page size, max 100."},
            "page_token": {"type": "string", "description": "Pagination token."},
        },
        "required": ["file_token", "comment_id"],
    },
}

FEISHU_DRIVE_REPLY_SCHEMA = {
    "name": "feishu_drive_reply_comment",
    "description": "Reply to a Feishu/Lark document comment thread.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {"type": "string", "description": "Document/file token."},
            "comment_id": {"type": "string", "description": "Comment thread ID."},
            "content": {"type": "string", "description": "Reply text."},
            "file_type": {"type": "string", "description": "File type, default docx."},
        },
        "required": ["file_token", "comment_id", "content"],
    },
}

FEISHU_DRIVE_ADD_COMMENT_SCHEMA = {
    "name": "feishu_drive_add_comment",
    "description": "Add a whole-document comment to a Feishu/Lark document or drive file.",
    "parameters": {
        "type": "object",
        "properties": {
            "file_token": {"type": "string", "description": "Document/file token."},
            "content": {"type": "string", "description": "Comment text."},
            "file_type": {"type": "string", "description": "File type, default docx."},
        },
        "required": ["file_token", "content"],
    },
}


def _handle_doc_read(args: dict[str, Any], **_: Any) -> str:
    doc_token = str(args.get("doc_token") or "").strip()
    if not doc_token:
        return _json_error("doc_token is required")
    token, error = _tenant_access_token()
    if error:
        return _json_error(error)
    payload = _request_json(
        "GET",
        f"/open-apis/docx/v1/documents/{urllib.parse.quote(doc_token)}/raw_content",
        token=token,
    )
    if payload.get("code") != 0:
        return _json_error("read document failed", code=payload.get("code"), msg=payload.get("msg"))
    return _json_result(success=True, content=(payload.get("data") or {}).get("content", ""))


def _drive_comments_path(file_token: str, suffix: str = "") -> str:
    encoded = urllib.parse.quote(file_token)
    return f"/open-apis/drive/v1/files/{encoded}/comments{suffix}"


def _query(params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v not in {None, ""}}
    return "?" + urllib.parse.urlencode(clean) if clean else ""


def _handle_list_comments(args: dict[str, Any], **_: Any) -> str:
    file_token = str(args.get("file_token") or "").strip()
    if not file_token:
        return _json_error("file_token is required")
    token, error = _tenant_access_token()
    if error:
        return _json_error(error)
    query = _query({
        "file_type": args.get("file_type") or "docx",
        "user_id_type": "open_id",
        "page_size": int(args.get("page_size") or 100),
        "page_token": args.get("page_token") or "",
        "is_whole": "true" if args.get("is_whole") else "",
    })
    payload = _request_json("GET", _drive_comments_path(file_token) + query, token=token)
    if payload.get("code") != 0:
        return _json_error("list comments failed", code=payload.get("code"), msg=payload.get("msg"))
    return _json_result(success=True, data=payload.get("data") or {})


def _handle_list_replies(args: dict[str, Any], **_: Any) -> str:
    file_token = str(args.get("file_token") or "").strip()
    comment_id = str(args.get("comment_id") or "").strip()
    if not file_token or not comment_id:
        return _json_error("file_token and comment_id are required")
    token, error = _tenant_access_token()
    if error:
        return _json_error(error)
    query = _query({
        "file_type": args.get("file_type") or "docx",
        "user_id_type": "open_id",
        "page_size": int(args.get("page_size") or 100),
        "page_token": args.get("page_token") or "",
    })
    suffix = f"/{urllib.parse.quote(comment_id)}/replies"
    payload = _request_json("GET", _drive_comments_path(file_token, suffix) + query, token=token)
    if payload.get("code") != 0:
        return _json_error("list replies failed", code=payload.get("code"), msg=payload.get("msg"))
    return _json_result(success=True, data=payload.get("data") or {})


def _handle_reply_comment(args: dict[str, Any], **_: Any) -> str:
    file_token = str(args.get("file_token") or "").strip()
    comment_id = str(args.get("comment_id") or "").strip()
    content = str(args.get("content") or "").strip()
    if not file_token or not comment_id or not content:
        return _json_error("file_token, comment_id and content are required")
    token, error = _tenant_access_token()
    if error:
        return _json_error(error)
    file_type = args.get("file_type") or "docx"
    suffix = f"/{urllib.parse.quote(comment_id)}/replies"
    # 飞书回复评论 body：content.elements.text_run；file_type 走 query。
    # 见 open.feishu.cn drive-v1/file-comment-reply。
    body = {"content": {"elements": [{"type": "text_run", "text_run": {"text": content}}]}}
    payload = _request_json("POST", _drive_comments_path(file_token, suffix) + _query({"file_type": file_type}),
                            token=token, body=body)
    if payload.get("code") != 0:
        return _json_error("reply comment failed", code=payload.get("code"), msg=payload.get("msg"))
    return _json_result(success=True, data=payload.get("data") or {})


def _handle_add_comment(args: dict[str, Any], **_: Any) -> str:
    file_token = str(args.get("file_token") or "").strip()
    content = str(args.get("content") or "").strip()
    if not file_token or not content:
        return _json_error("file_token and content are required")
    token, error = _tenant_access_token()
    if error:
        return _json_error(error)
    file_type = args.get("file_type") or "docx"
    # 飞书添加全文评论 POST .../comments：reply_list.replies[].content.elements.text_run；file_type 走 query。
    # 见 open.feishu.cn drive-v1/file-comment/create。
    body = {"reply_list": {"replies": [
        {"content": {"elements": [{"type": "text_run", "text_run": {"text": content}}]}}
    ]}}
    payload = _request_json("POST", _drive_comments_path(file_token) + _query({"file_type": file_type}),
                            token=token, body=body)
    if payload.get("code") != 0:
        return _json_error("add comment failed", code=payload.get("code"), msg=payload.get("msg"))
    return _json_result(success=True, data=payload.get("data") or {})


def register(ctx) -> None:
    ctx.register_tool(
        name="feishu_doc_read",
        toolset="feishu_doc",
        schema=FEISHU_DOC_READ_SCHEMA,
        handler=_handle_doc_read,
        check_fn=_has_credentials,
        requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        description="Read Feishu/Lark document content.",
    )
    ctx.register_tool(
        name="feishu_drive_list_comments",
        toolset="feishu_drive",
        schema=FEISHU_DRIVE_LIST_COMMENTS_SCHEMA,
        handler=_handle_list_comments,
        check_fn=_has_credentials,
        requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        description="List Feishu/Lark document comments.",
    )
    ctx.register_tool(
        name="feishu_drive_list_comment_replies",
        toolset="feishu_drive",
        schema=FEISHU_DRIVE_LIST_REPLIES_SCHEMA,
        handler=_handle_list_replies,
        check_fn=_has_credentials,
        requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        description="List Feishu/Lark comment replies.",
    )
    ctx.register_tool(
        name="feishu_drive_reply_comment",
        toolset="feishu_drive",
        schema=FEISHU_DRIVE_REPLY_SCHEMA,
        handler=_handle_reply_comment,
        check_fn=_has_credentials,
        requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        description="Reply to a Feishu/Lark document comment.",
    )
    ctx.register_tool(
        name="feishu_drive_add_comment",
        toolset="feishu_drive",
        schema=FEISHU_DRIVE_ADD_COMMENT_SCHEMA,
        handler=_handle_add_comment,
        check_fn=_has_credentials,
        requires_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
        description="Add a Feishu/Lark document comment.",
    )
