/**
 * HTML 转义工具（仅 ASCII 高频字符）。
 *
 * 比 DOMPurify / he 等库轻量，适合 renderer 渲染 user-supplied 文本时使用。
 * 可在 main / preload / renderer / tests 任意侧 import（无 DOM / Node 依赖）。
 *
 * 注意：这是字面字符替换，**不**解析 HTML 结构，不能防止属性上下文里的 XSS；
 * 任何插值到 `href` / `src` / `style` 的值仍需 URL / CSS 编码。
 */

export function escapeHtml(text: string): string {
  return text.replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] ?? c));
}
