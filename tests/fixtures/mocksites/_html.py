"""仿真站点共用的 HTML 拼装工具。

刻意保持零依赖：这些站点既被 pytest 用作 fixture，也会被人手工拉起来对着
内置浏览器做端到端验证，不该为此引入模板引擎。
"""

from __future__ import annotations

import html


def esc(value: object) -> str:
    """转义为 HTML 文本节点。仿真数据里含 `<` `&` 的字段必须走这里。"""
    return html.escape(str(value), quote=True)


def page(title: str, body: str, *, nav: str = "") -> bytes:
    """套一层最小页面骨架。

    刻意不引入任何 CSS 框架：无障碍树只看语义，样式越少越贴近我们真正要测的
    东西（role 与 accessible name），也让页面在快照里更容易人工核对。
    """
    nav_html = f"<nav>{nav}</nav>" if nav else ""
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        f"<title>{esc(title)}</title>\n"
        "</head>\n"
        "<body>\n"
        f"{nav_html}\n"
        f"<main>\n{body}\n</main>\n"
        "</body>\n"
        "</html>\n"
    ).encode("utf-8")
