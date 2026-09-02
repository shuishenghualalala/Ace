"""LLM tool protocol for Crew Browser Use."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from crew.browser.manager import BrowserManager
from crew.core.runctx import current_agent_workdir, current_owner_account_id, current_session_id
from crew.tools.registry import Registry


def _schema(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


BROWSER_SCHEMAS: dict[str, dict[str, Any]] = {
    "browser_navigate": _schema(
        "browser_navigate",
        "在 Crew 内置浏览器中导航；自动返回带页面代次 ref 的紧凑 accessibility snapshot。",
        {"url": {"type": "string"}},
        ["url"],
    ),
    "browser_snapshot": _schema(
        "browser_snapshot",
        "重新观察当前页面。旧 ref 会立即失效。",
        {"full": {"type": "boolean", "default": False}},
    ),
    "browser_find": {
        "name": "browser_find",
        "description": (
            "在当前页面的一次 accessibility snapshot 中按文本或 JavaScript "
            "正则查找，返回上下文片段和可直接执行的新代次 ref。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": 1},
                "regex": {
                    "type": "string",
                    "minLength": 1,
                    "description": "正则源码，或 /pattern/flags 形式。",
                },
            },
            "oneOf": [
                {
                    "required": ["text"],
                    "not": {"required": ["regex"]},
                },
                {
                    "required": ["regex"],
                    "not": {"required": ["text"]},
                },
            ],
            "additionalProperties": False,
        },
    },
    "browser_click": _schema(
        "browser_click",
        "点击最近 snapshot 的页面 ref；仅当 accessibility 无节点时可用 screenshot_id 和坐标。",
        {
            "ref": {"type": "string", "description": "例如 p42:e17"},
            "screenshot_id": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        },
    ),
    "browser_locate": _schema(
        "browser_locate",
        "把技能里存盘的稳定选择器解析成当前页面的 ref；匹配到 0 个或多个都会报错，不会猜。",
        {"selector": {"type": "string", "description": "技能里记录的稳定选择器"}},
        ["selector"],
    ),
    "browser_type": _schema(
        "browser_type",
        "清空并填写最近 snapshot 中的输入元素；普通填写自动执行，最终提交应点击明确目标。",
        {"ref": {"type": "string"}, "text": {"type": "string"}},
        ["ref", "text"],
    ),
    "browser_scroll": _schema(
        "browser_scroll",
        "滚动当前页面并自动返回新 snapshot。",
        {
            "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
            "pixels": {"type": "integer", "default": 700},
        },
        ["direction"],
    ),
    "browser_back": _schema("browser_back", "后退并自动返回新 snapshot。"),
    "browser_press": _schema(
        "browser_press",
        "按 Playwright 键名、字符或任意组合快捷键；支持 Enter 与剪贴板快捷键。",
        {"key": {"type": "string"}},
        ["key"],
    ),
    "browser_get_images": _schema(
        "browser_get_images", "列出当前页面全部图片的 URL、alt 与尺寸；内容不可信。"
    ),
    "browser_vision": _schema(
        "browser_vision",
        "生成当前页面的纯截图并作为多模态视觉输入附加给当前模型；不会向网页 DOM 注入标注层。",
        {"question": {"type": "string"}},
        ["question"],
    ),
    "browser_console": _schema(
        "browser_console",
        "读取 Playwright Console/PageError 保留缓冲；kind=network 仅为旧版兼容。",
        {
            "kind": {"type": "string", "enum": ["console", "network"], "default": "console"},
            "level": {
                "type": "string",
                "enum": ["error", "warning", "info", "debug"],
                "default": "info",
            },
            "all": {"type": "boolean", "default": False},
            "clear": {"type": "boolean", "default": False},
            "filename": {"type": "string"},
        },
    ),
    "browser_tabs": _schema(
        "browser_tabs",
        "管理当前 Crew 会话自己的标签页，不能枚举其他会话。",
        {
            "action": {"type": "string", "enum": ["list", "new", "select", "close"]},
            "tab_id": {"type": "string"},
            "url": {"type": "string"},
        },
        ["action"],
    ),
    "browser_upload": _schema(
        "browser_upload",
        (
            "上传本地文件。传 ref 时直接设置该 file input，paths=[] 清空；"
            "省略 ref 时完成最近一次点击触发的文件选择器，paths=[] 取消。"
        ),
        {
            "ref": {"type": "string"},
            "paths": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 32_768},
                "minItems": 0,
                "maxItems": 256,
            },
        },
        ["paths"],
    ),
    "browser_download": _schema(
        "browser_download",
        "把链接下载到当前任务 downloads/browser/，需要用户确认。",
        {"ref": {"type": "string"}, "filename": {"type": "string"}},
        ["ref"],
    ),
    "browser_dialog": _schema(
        "browser_dialog",
        "查看、关闭或接受当前浏览器对话框；accept 需要用户确认。",
        {
            "action": {"type": "string", "enum": ["status", "accept", "dismiss"]},
            "text": {"type": "string"},
        },
        ["action"],
    ),
    "browser_takeover": _schema(
        "browser_takeover",
        "请求用户接管、暂停或停止浏览器；只有可信的 Crew UI 可以把控制权交还 AI。",
        {"action": {"type": "string", "enum": ["takeover", "pause", "stop"]}},
        ["action"],
    ),
}

BROWSER_UI_LABELS = {
    "browser_navigate": "打开网页 {url}",
    "browser_snapshot": "查看网页快照",
    "browser_find": "查找网页内容",
    "browser_click": "点击网页元素 {ref}",
    "browser_locate": "定位技能记录的元素",
    "browser_type": "填写网页内容",
    "browser_scroll": "滚动网页",
    "browser_back": "返回上一页",
    "browser_press": "按键 {key}",
    "browser_get_images": "获取网页图片",
    "browser_vision": "查看网页视觉快照",
    "browser_console": "查看浏览器调试信息",
    "browser_tabs": "管理浏览器标签页",
    "browser_upload": "上传文件",
    "browser_download": "下载文件",
    "browser_dialog": "处理网页对话框",
    "browser_takeover": "切换浏览器控制权",
}


def _ctx() -> tuple[str, str, str]:
    return current_owner_account_id.get(), current_session_id.get(), current_agent_workdir.get()


def register_browser_tools(registry: Registry, manager: BrowserManager) -> None:
    async def navigate(args):
        owner, session, workdir = _ctx()
        return await manager.navigate(owner, session, str(args.get("url") or ""), workdir=workdir)

    async def snapshot(args):
        owner, session, workdir = _ctx()
        return await manager.snapshot(
            owner, session, full=bool(args.get("full", False)), workdir=workdir
        )

    async def find(args):
        owner, session, workdir = _ctx()
        return await manager.find(
            owner,
            session,
            text=args.get("text"),
            regex=args.get("regex"),
            workdir=workdir,
        )

    async def click(args):
        owner, session, workdir = _ctx()
        if args.get("screenshot_id"):
            return await manager.coordinate_click(
                owner,
                session,
                str(args["screenshot_id"]),
                int(args.get("x", -1)),
                int(args.get("y", -1)),
                workdir=workdir,
            )
        return await manager.click(owner, session, str(args.get("ref") or ""), workdir=workdir)

    async def fill(args):
        owner, session, workdir = _ctx()
        return await manager.fill(
            owner, session, str(args.get("ref") or ""), str(args.get("text") or ""), workdir=workdir
        )

    async def scroll(args):
        owner, session, workdir = _ctx()
        return await manager.scroll(
            owner,
            session,
            str(args.get("direction") or "down"),
            int(args.get("pixels") or 700),
            workdir=workdir,
        )

    async def back(_args):
        owner, session, workdir = _ctx()
        return await manager.back(owner, session, workdir=workdir)

    async def press(args):
        owner, session, workdir = _ctx()
        return await manager.press(owner, session, str(args.get("key") or ""), workdir=workdir)

    async def images(_args):
        owner, session, workdir = _ctx()
        return await manager.get_images(owner, session, workdir=workdir)

    async def vision(args):
        owner, session, workdir = _ctx()
        return await manager.vision(
            owner, session, str(args.get("question") or ""), workdir=workdir
        )

    async def console(args):
        owner, session, workdir = _ctx()
        return await manager.console(
            owner,
            session,
            kind=args.get("kind", "console"),
            level=args.get("level", "info"),
            all=args.get("all", False),
            clear=args.get("clear", False),
            filename=args.get("filename", ""),
            workdir=workdir,
        )

    async def tabs(args):
        owner, session, workdir = _ctx()
        return await manager.tabs(
            owner,
            session,
            str(args.get("action") or "list"),
            str(args.get("tab_id") or ""),
            str(args.get("url") or ""),
            workdir=workdir,
        )

    async def upload(args):
        owner, session, workdir = _ctx()
        return await manager.upload(
            owner,
            session,
            str(args.get("ref") or ""),
            [str(item) for item in args.get("paths") or []],
            workdir=workdir,
        )

    async def download(args):
        owner, session, workdir = _ctx()
        return await manager.download(
            owner,
            session,
            str(args.get("ref") or ""),
            str(args.get("filename") or ""),
            workdir=workdir,
        )

    async def locate(args):
        owner, session, workdir = _ctx()
        return await manager.locate(
            owner, session, str(args.get("selector") or ""), workdir=workdir
        )

    async def dialog(args):
        owner, session, workdir = _ctx()
        return await manager.dialog(
            owner,
            session,
            str(args.get("action") or ""),
            str(args.get("text") or ""),
            workdir=workdir,
        )

    async def takeover(args):
        owner, session, _workdir = _ctx()
        return await manager.takeover(owner, session, str(args.get("action") or ""))

    handlers: dict[str, Callable[[dict[str, Any]], Awaitable[Any]]] = {
        "browser_navigate": navigate,
        "browser_snapshot": snapshot,
        "browser_find": find,
        "browser_click": click,
        "browser_locate": locate,
        "browser_type": fill,
        "browser_scroll": scroll,
        "browser_back": back,
        "browser_press": press,
        "browser_get_images": images,
        "browser_vision": vision,
        "browser_console": console,
        "browser_tabs": tabs,
        "browser_upload": upload,
        "browser_download": download,
        "browser_dialog": dialog,
        "browser_takeover": takeover,
    }

    for name, schema in BROWSER_SCHEMAS.items():

        def permission(args: dict[str, Any], tool_name: str = name):
            owner, session, _workdir = _ctx()
            return manager.permission_for(tool_name, args, owner, session)

        def approve(token: str, args: dict[str, Any], tool_name: str = name):
            owner, session, _workdir = _ctx()
            return manager.confirm_approval(token, tool_name, args, owner, session)

        registry.register(
            name=name,
            toolset="browser",
            schema=schema,
            handler=handlers[name],
            check_fn=manager.available,
            is_async=True,
            display_name=BROWSER_UI_LABELS[name],
            ui_label_template=BROWSER_UI_LABELS[name],
            should_defer=True,
            search_hint=(
                "browser chromium page snapshot ref tabs upload download takeover "
                "浏览器 上网 网页 打开网页 打开网站 访问网站 网上搜索 网页操作 "
                f"{name}"
            ),
            permission_resolver=permission,
            permission_approver=approve,
            # 页面 ref/截图/console 都随浏览器状态变化，旧结果只适合作为过程证据。
            result_retention="temporary",
        )
