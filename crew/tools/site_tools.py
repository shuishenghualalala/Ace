"""用户明确要求后，将当前 Workspace 中的 App 发布到 Desktop 灵感。"""

from __future__ import annotations

from crew.core.runctx import (
    current_agent_workdir,
    current_owner_account_id,
    current_session_id,
    current_workspace_id,
)
from crew.tools.registry import Registry, tool_result


PUBLISH_SITE_SCHEMA = {
    "name": "publish_site",
    "description": (
        "仅当用户明确要求部署或发布灵感 App 时调用。"
        "将当前 Workspace 内已完成的 App 构建为 Ace Desktop 本地灵感；不会上传网络。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "source_path": {"type": "string", "description": "App 项目目录，绝对路径或相对当前 Workspace 的路径"},
            "name": {"type": "string", "description": "灵感展示名称"},
            "description": {"type": "string", "description": "灵感卡片中的简短说明"},
            "build_command": {"type": "string", "description": "可选构建命令；默认读取 package.json 的 build script"},
            "output_directory": {"type": "string", "description": "可选构建输出目录；默认 dist，纯 HTML 默认当前目录"},
            "site_id": {"type": "string", "description": "更新既有灵感时传入；新灵感留空"},
        },
        "required": ["source_path", "name"],
    },
}


def register_site_tools(registry: Registry, manager) -> None:
    async def publish_site(args):
        owner = current_owner_account_id.get().strip()
        workspace_id = current_workspace_id.get().strip() or "default"
        session_id = current_session_id.get().strip()
        workdir = current_agent_workdir.get().strip()
        if not owner or not workdir:
            raise ValueError("当前会话缺少用户或 Workspace 工作目录")
        result = await manager.publish(
            owner=owner, workspace_id=workspace_id, session_id=session_id,
            workspace_root=workdir, source_path=str(args.get("source_path") or ""),
            name=str(args.get("name") or ""), build_command=str(args.get("build_command") or ""),
            output_directory=str(args.get("output_directory") or ""), site_id=str(args.get("site_id") or ""),
            description=str(args.get("description") or ""),
        )
        site = result["site"]
        release = result["release"]
        return tool_result(
            ok=True, site_id=site["id"], release_id=release["id"], name=site["name"],
            message="已发布到 Ace Desktop 灵感。",
            surface={
                "kind": "inspiration", "mode": "site", "inspirationId": site["id"],
                "siteId": site["id"], "sessionId": session_id, "title": site["name"],
                "status": "ready", "revisionId": release["id"],
            },
        )

    registry.register(
        name="publish_site", toolset="sites", schema=PUBLISH_SITE_SCHEMA,
        handler=publish_site, display_name="发布灵感", ui_label_template="发布灵感 {name}",
        always_load=True, search_hint="部署站点 发布网站 local site publish deploy website",
        is_async=True,
    )
