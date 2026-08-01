/** Agent 使用内置浏览器时自动展开侧边浏览器面板的判定（纯函数，便于单测）。
 *
 * 语义：用户在对话中明确要求（或模型决定）用浏览器干活时，把侧边浏览器工作台
 * 自动展示出来——浏览器自动化涉及不可信页面与登录态，可视性本身是安全体验。
 * 每个 request（一轮任务）至多自动展开一次：用户本轮手动关闭后不再反复抢布局。
 */

export interface BrowserAutoOpenInput {
  /** chunk kind（只有 'tool' 才可能触发）。 */
  kind: string;
  /** tool chunk 的工具名；其它 kind 传 undefined。 */
  toolName?: string | undefined;
  /** chunk 所属会话。 */
  sessionId: string;
  /** 前端当前正在查看的会话。 */
  activeSessionId: string;
  /** 本轮 request id（无法识别轮次时不自动展开，退化为不打扰）。 */
  requestId: string | null;
  /** 该会话上次已自动展开过的 request id。 */
  lastOpenedRequestId?: string | undefined;
}

/** 返回 true 表示应自动展开一次浏览器工作台。 */
export function shouldAutoOpenBrowserWorkbench(input: BrowserAutoOpenInput): boolean {
  if (input.kind !== 'tool' || input.toolName !== 'browser_use') return false;
  // 后台会话（cron / 渠道 / 非当前查看）不打扰当前界面
  if (!input.sessionId || input.sessionId !== input.activeSessionId) return false;
  if (!input.requestId) return false;
  return input.lastOpenedRequestId !== input.requestId;
}
