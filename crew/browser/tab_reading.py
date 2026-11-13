"""只读提取浏览器标签页正文：供 BrowserManager.read_tab_content 使用。

本模块只持有与**页面内容**相关的原语：页面内提取脚本（只读 JS）、正文上限、
Host eval 返回值的解析。标签页定位、模式校验与执行通道都归 BrowserManager。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from crew.browser.driver import BrowserDriverError

log = logging.getLogger(__name__)

# 页面内提取脚本（只读）：优先 article/main 正文，退回 body.innerText；
# 在页面里先截断到 8000 字符，避免超长正文挤占 Host RPC 通道。
PAGE_TEXT_SCRIPT = """() => {
  const title = String(document.title || "");
  const url = String(location.href || "");
  const pickText = (el) => (el && el.innerText ? String(el.innerText) : "");
  let text = pickText(document.querySelector("article"))
    || pickText(document.querySelector("main"))
    || pickText(document.body);
  text = text.replace(/\\r/g, "").replace(/[ \\t]+\\n/g, "\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
  return { title, url, text: text.slice(0, 8000) };
}"""

# 页面内截断与 PAGE_TEXT_SCRIPT 保持一致；调用方可要得更少，不能更多。
PAGE_TEXT_LIMIT = 8000

# Desktop Composer emits this explicit token; arbitrary URLs are not treated as
# permission to read a tab.
BROWSER_TAB_REFERENCE_RE = re.compile(
    r"(?:^|\s)@browser_tab:(?P<tab_id>[A-Za-z0-9_-]+)"
)
BROWSER_TAB_CONTEXT_LIMIT = 4000


def parse_page_text_result(result: dict, limit: int) -> dict[str, str]:
    """把 Host 只读 eval 的返回解析为 {title, url, text}；无效结果抛 BrowserDriverError。"""
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        raise BrowserDriverError("浏览器返回了无效的读取结果")
    value = data.get("value")
    if not isinstance(value, dict):
        # 结构化克隆在某些页面类型上可能缺失；serialized 是 Host 保证的 JSON 文本。
        serialized = data.get("serialized")
        try:
            value = json.loads(serialized) if isinstance(serialized, str) else None
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise BrowserDriverError("浏览器返回了无效的页面内容")
    return {
        "title": str(value.get("title") or "").strip(),
        "url": str(value.get("url") or "").strip(),
        "text": str(value.get("text") or "")[:limit],
    }


async def resolve_browser_tab_references(
    query: str,
    *,
    manager: Any,
    owner_account_id: str,
    session_id: str,
) -> list[dict[str, str]]:
    """Resolve explicit browser-tab tokens to bounded read-only snapshots."""
    tab_ids: list[str] = []
    for match in BROWSER_TAB_REFERENCE_RE.finditer(str(query or "")):
        tab_id = match.group("tab_id")
        if tab_id not in tab_ids:
            tab_ids.append(tab_id)
    if not tab_ids:
        return []
    if manager is None:
        return [{"tab_id": tab_id, "error": "Browser Use 未启用"} for tab_id in tab_ids]

    refs: list[dict[str, str]] = []
    for tab_id in tab_ids:
        try:
            content = await manager.read_tab_content(
                owner_account_id,
                session_id,
                tab_id,
                max_chars=BROWSER_TAB_CONTEXT_LIMIT,
            )
        except BrowserDriverError as error:
            refs.append({"tab_id": tab_id, "error": str(error)[:200]})
            continue
        except Exception as error:  # noqa: BLE001 - one stale tab must not block send
            log.warning("读取浏览器标签页引用失败 tab=%s: %s", tab_id, error)
            refs.append({"tab_id": tab_id, "error": f"读取失败: {error}"[:200]})
            continue
        refs.append({"tab_id": tab_id, **content})
    return refs


def format_browser_tab_references(refs: object) -> str:
    """Format browser snapshots as one model-visible context fragment."""
    if not isinstance(refs, list) or not refs:
        return ""
    lines = [
        "# 用户引用的浏览器标签页",
        "用户在消息中通过 @browser_tab 显式引用了以下标签页，正文为发送时的只读快照：",
    ]
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        tab_id = str(ref.get("tab_id") or "")
        error = str(ref.get("error") or "").strip()
        if error:
            lines.append(f"\n## 标签页 {tab_id}\n（浏览器标签页内容不可用：{error}）")
            continue
        title = str(ref.get("title") or "").strip() or "(无标题)"
        url = str(ref.get("url") or "").strip()
        text = str(ref.get("text") or "").strip() or "(页面正文为空)"
        header = f"\n## {title}"
        if url:
            header += f"\nURL: {url}"
        lines.append(f"{header}\n{text}")
    return "\n".join(lines)
