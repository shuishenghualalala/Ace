"""web_extract 的 HTML→Markdown 清洗与转换（纯标准库实现）。

剔除不可见/非内容元素后按 GFM 习惯渲染：嵌套深度超限或转换异常时返回
省略标记，绝不让半截标记或不安全的原始 HTML 到达模型。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

MAX_NESTING_DEPTH = 512
MAX_SOURCE_CHARS = 100_000
OMITTED = "[HTML content omitted: unable to convert safely.]"

# 结构化内容之外的元素整棵剔除（turndown 默认会保留它们的文本，这里不保留）。
_DROP_TAGS = {
    "script", "style", "noscript", "template", "iframe", "object", "embed",
    "head", "title", "meta", "link", "base",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_TRANSPARENT_BLOCK_TAGS = {
    "div", "section", "article", "main", "header", "footer", "nav", "aside",
    "figure", "figcaption", "form", "fieldset", "body", "html", "tbody",
    "thead", "tfoot", "caption", "select", "dl", "dt", "dd",
}


@dataclass(frozen=True)
class MarkdownRender:
    text: str
    #: 字符层截断是否发生（调用方据此追加截断 footer）。
    source_truncated: bool


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list["_Node | str"] = field(default_factory=list)
    drop: bool = False


def _is_dropped(node: _Node) -> bool:
    if node.tag in _DROP_TAGS or "hidden" in node.attrs:
        return True
    if node.attrs.get("aria-hidden", "").strip().lower() == "true":
        return True
    if node.tag == "input" and node.attrs.get("type", "").strip().lower() == "hidden":
        return True
    for declaration in node.attrs.get("style", "").split(";"):
        prop, _, value = declaration.partition(":")
        prop = prop.strip().lower()
        value = value.strip().lower()
        if value.endswith("!important"):
            value = value[: -len("!important")].strip()
        if prop == "display" and value == "none":
            return True
        if prop == "visibility" and value in {"hidden", "collapse"}:
            return True
    return False


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("[root]", {})
        self._stack: list[_Node] = [self.root]
        self.exceeded_depth = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.exceeded_depth:
            return
        if len(self._stack) > MAX_NESTING_DEPTH:
            self.exceeded_depth = True
            return
        node = _Node(tag.lower(), {k.lower(): (v or "") for k, v in attrs})
        node.drop = _is_dropped(node)
        self._stack[-1].children.append(node)
        if tag.lower() not in _VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if not self.exceeded_depth and tag.lower() not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.exceeded_depth:
            return
        tag = tag.lower()
        for i in range(len(self._stack) - 1, 0, -1):
            if self._stack[i].tag == tag:
                del self._stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if self.exceeded_depth or not data:
            return
        # 纯排版空白（含换行的缩进）不产生文本；行内空白折叠为单空格。
        if not data.strip():
            if "\n" in data or "\r" in data or "\t" in data:
                return
            data = " "
        self._stack[-1].children.append(data)


def _raw_text(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif not child.drop:
            parts.append(_raw_text(child))
    return "".join(parts)


def _render_children(children: list["_Node | str"]) -> str:
    return "".join(_render_node(child) for child in children)


def _inline(children: list["_Node | str"]) -> str:
    return re.sub(r"\s+", " ", _render_children(children)).strip()


def _rows(node: _Node) -> list[_Node]:
    rows: list[_Node] = []
    for child in node.children:
        if isinstance(child, str) or child.drop:
            continue
        if child.tag == "tr":
            rows.append(child)
        elif child.tag in {"thead", "tbody", "tfoot"}:
            rows.extend(_rows(child))
    return rows


def _table_cell(children: list["_Node | str"]) -> str:
    text = re.sub(r"\s*\n\s*", "<br>", _render_children(children).strip())
    return text.replace("|", "\\|")


def _render_table(node: _Node) -> str:
    lines: list[str] = []
    header_done = False
    for row in _rows(node):
        cells = [
            child
            for child in row.children
            if isinstance(child, _Node) and child.tag in {"td", "th"} and not child.drop
        ]
        if not cells:
            continue
        values = [_table_cell(cell.children) for cell in cells]
        lines.append("| " + " | ".join(values) + " |")
        if not header_done and any(cell.tag == "th" for cell in cells):
            lines.append("|" + "|".join(["---"] * len(values)) + "|")
            header_done = True
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines) + "\n\n"


def _render_list(node: _Node) -> str:
    items = [
        child
        for child in node.children
        if isinstance(child, _Node) and child.tag == "li" and not child.drop
    ]
    lines = []
    for index, item in enumerate(items, 1):
        body = _render_children(item.children).strip()
        # 空列表项（导航装饰、主题切换器占位等）整条丢弃，不产生裸 marker 噪音。
        if not body:
            continue
        body = re.sub(r"\n", "\n  ", body)
        marker = f"{index}." if node.tag == "ol" else "-"
        lines.append(f"{marker} {body}")
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines) + "\n\n"


def _render_node(node: "_Node | str") -> str:
    if isinstance(node, str):
        return node
    if node.drop:
        return ""
    tag = node.tag
    heading = re.fullmatch(r"h([1-6])", tag)
    if heading:
        body = _inline(node.children)
        return f"\n\n{'#' * int(heading.group(1))} {body}\n\n" if body else ""
    if tag in {"p", "address", "details", "summary"}:
        body = _inline(node.children)
        return f"\n\n{body}\n\n" if body else ""
    if tag in _TRANSPARENT_BLOCK_TAGS:
        return _render_children(node.children)
    if tag == "br":
        return "\n"
    if tag == "hr":
        return "\n\n---\n\n"
    if tag == "pre":
        raw = _raw_text(node).strip("\n")
        return f"\n\n```\n{raw}\n```\n\n" if raw.strip() else ""
    if tag in {"strong", "b"}:
        body = _inline(node.children)
        return f"**{body}**" if body else ""
    if tag in {"em", "i"}:
        body = _inline(node.children)
        return f"*{body}*" if body else ""
    if tag in {"del", "s", "strike"}:
        body = _inline(node.children)
        return f"~~{body}~~" if body else ""
    if tag in {"mark"}:
        return _inline(node.children)
    if tag == "code":
        raw = _raw_text(node).strip()
        fence = "``" if "`" in raw else "`"
        return f"{fence}{raw}{fence}" if raw else ""
    if tag == "q":
        body = _inline(node.children)
        return f'"{body}"' if body else ""
    if tag == "a":
        body = _inline(node.children)
        href = node.attrs.get("href", "").strip()
        if href.lower().startswith(("javascript:", "data:")):
            href = ""
        if not body:
            body = href
        if not href:
            return body
        return f"[{body}]({href})"
    if tag == "img":
        src = node.attrs.get("src", "").strip()
        if not src or src.lower().startswith(("javascript:", "data:")):
            return ""
        alt = re.sub(r"\s+", " ", node.attrs.get("alt", "")).strip()
        return f"![{alt}]({src})"
    if tag == "blockquote":
        body = _render_children(node.children).strip()
        if not body:
            return ""
        quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in body.splitlines())
        return f"\n\n{quoted}\n\n"
    if tag in {"ul", "ol"}:
        return _render_list(node)
    if tag == "table":
        return _render_table(node)
    if tag == "li":
        return _render_children(node.children)
    if tag in {"input", "button", "textarea", "option"}:
        return ""
    return _render_children(node.children)


def render_html(source: str, *, max_chars: int = MAX_SOURCE_CHARS) -> MarkdownRender:
    """清洗并转换 HTML；深度超限或解析异常时返回省略标记。"""
    source_truncated = len(source) > max_chars
    source = source[:max_chars]
    builder = _TreeBuilder()
    try:
        builder.feed(source)
        builder.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不得把半截标记透给模型
        return MarkdownRender(text=OMITTED, source_truncated=source_truncated)
    if builder.exceeded_depth:
        return MarkdownRender(text=OMITTED, source_truncated=source_truncated)
    text = _render_children(builder.root.children)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return MarkdownRender(text=text.strip(), source_truncated=source_truncated)
