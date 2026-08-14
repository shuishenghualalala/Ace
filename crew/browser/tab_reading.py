"""只读提取浏览器标签页正文：read-tab 端点与 @browser_tab 引用解析共用。

manager.py 由并行改动占用，且其公开方法只支持「AI 模式 + 活动标签页 + 变更语义」
的 evaluate；这里以独立函数复用 manager 持有的 owner 连接信息（runtime_key /
profile_dir / policy proxy）与 driver 的 targeted execute 通道，对**指定**标签页
执行一段只读 JS，不新增 manager 方法、不改任何浏览器状态。

Host 侧仍有自己的强制约束：人工接管/暂停模式（control_mode_blocked）、待处理
对话框等会拒绝求值——这些错误原样向上抛，由调用方决定呈现方式。
"""

from __future__ import annotations

import json
from typing import Any

from crew.browser.driver import BrowserDriverError
from crew.state.logging import get_logger

log = get_logger("browser.tab_reading")

# 页面内提取脚本（只读）：优先 article/main 正文，退回 body.innerText；
# 在页面里先截断到 8000 字符，避免超长正文挤占 Host RPC 通道。
_EXTRACT_EXPRESSION = """() => {
  const title = String(document.title || "");
  const url = String(location.href || "");
  const pickText = (el) => (el && el.innerText ? String(el.innerText) : "");
  let text = pickText(document.querySelector("article"))
    || pickText(document.querySelector("main"))
    || pickText(document.body);
  text = text.replace(/\\r/g, "").replace(/[ \\t]+\\n/g, "\\n").replace(/\\n{3,}/g, "\\n\\n").trim();
  return { title, url, text: text.slice(0, 8000) };
}"""

# 页面内截断与 _EXTRACT_EXPRESSION 保持一致；调用方可要得更少，不能更多。
_PAGE_EXTRACTION_LIMIT = 8000


def _truncate(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit]


async def read_tab_content(
    manager: Any,
    owner_id: str,
    session_id: str,
    tab_id: str,
    *,
    max_chars: int = _PAGE_EXTRACTION_LIMIT,
) -> dict[str, str]:
    """读取指定标签页的 {title, url, text}；任何失败抛 BrowserDriverError。

    只读观察：不加 owner.lock（Host 按 owner 串行执行 RPC，读不与其他动作互相
    破坏），不触碰 session 观察状态，也不触发 ref 失效之外的任何 manager 簿记。
    """
    limit = max(1, min(int(max_chars), _PAGE_EXTRACTION_LIMIT))
    # 与 manager.state() 一致：只 peek 已存在的 owner，不为一次只读访问
    # 创建 owner / 启动 policy proxy。
    owner = getattr(manager, "_owners", {}).get(str(owner_id or ""))
    if owner is None:
        raise BrowserDriverError("当前账号没有浏览器会话")
    session = owner.sessions.get(str(session_id or ""))
    if session is None:
        raise BrowserDriverError("当前会话没有浏览器标签页")
    tab = session.tabs.get(str(tab_id or ""))
    if tab is None:
        raise BrowserDriverError("标签页不存在或已关闭")
    if session.mode != "ai":
        # Host 同样拒绝（control_mode_blocked）；这里提前给出面向用户的明确原因。
        raise BrowserDriverError("人工接管或暂停期间不可读取标签页内容；请先交还 AI")
    if not tab.target_id:
        raise BrowserDriverError("标签页尚未就绪，缺少不可伪造的 targetId")
    result = await manager.driver.execute_targeted(
        owner.runtime_key,
        owner.profile_dir,
        "eval",
        [_EXTRACT_EXPRESSION],
        target_id=tab.target_id,
        timeout=float(manager.config.command_timeout_seconds),
        proxy_url=owner.proxy.url if owner.proxy else "",
        mutating=False,
    )
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
        "text": _truncate(str(value.get("text") or ""), limit),
    }
