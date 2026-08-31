"""只读提取浏览器标签页正文：供 BrowserManager.read_tab_content 使用。

本模块只持有与**页面内容**相关的原语：页面内提取脚本（只读 JS）、正文上限、
Host eval 返回值的解析。标签页定位、模式校验与执行通道都归 BrowserManager。
"""

from __future__ import annotations

import json

from crew.browser.driver import BrowserDriverError

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
