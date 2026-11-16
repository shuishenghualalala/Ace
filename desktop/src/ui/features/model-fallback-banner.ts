/**
 * 「演示模式」顶部 banner：当前会话实际生效的 Provider 是 FakeProvider 时常驻显示。
 *
 * 背景：后端在模型无 API Key 时静默回退 FakeProvider（回声演示），仅以 status_event
 * 在回合开头提示一次，回合结束即消失——用户会误以为模型在正常回复。本 banner 把
 * 这个状态做成持久可见，并给出「去配置模型」入口。
 *
 * 判定在后端完成：crew/app.py 装配用的回退链（owner 默认模型 → 会话绑定 → 全局
 * active → FakeProvider）经 _resolve_session_provider_profile 抽出共用，结果以
 * demo_mode 字段随 GET/PUT /api/session/{id}/model 下发。前端只消费该字段，不再
 * 从 state.config 重推——前端看不到 per-owner overlay 等因素，自行推导会误报/漏报。
 * 绑定未加载（如草稿会话）时按不显示处理，宁缺毋误报。
 *
 * 数据全部来自内存中的会话模型绑定，无额外请求；由
 * chat-controller.updateComposerControls 每次 renderChat 时调用，幂等。
 */

import { isExternalTeamSession, sessionDemoMode } from './session-model';
import { openModelPane } from './model-tour';

const BANNER_ID = 'model-fallback-banner';

/** 纯判定函数，导出供单元测试。 */
export function deriveModelFallbackVisible(
  demoMode: boolean | null | undefined,
  isExternal: boolean,
): boolean {
  if (isExternal) return false;
  return demoMode === true;
}

/** 幂等渲染：每次 renderChat 经 updateComposerControls 调用。 */
export function renderModelFallbackBanner(): void {
  const container = document.querySelector('.chat-composer');
  if (!container) return;
  const visible = deriveModelFallbackVisible(sessionDemoMode(), isExternalTeamSession());
  let banner = document.getElementById(BANNER_ID);
  if (!visible) {
    banner?.remove();
    return;
  }
  if (!banner) banner = createBanner(container);
  banner.classList.add('show');
}

// 绑定（重）加载 / 切换模型后 demo_mode 才可能变化；renderChat 之外的时机靠事件补齐，
// 与 composer-context-ring / inspector 监听同一事件的模式保持一致。
if (typeof window !== 'undefined') {
  window.addEventListener('session:model-changed', () => {
    renderModelFallbackBanner();
  });
}

function createBanner(container: Element): HTMLElement {
  const el = document.createElement('div');
  el.id = BANNER_ID;
  // 复用 security-banner 的既有样式（设计令牌一致，无新增 CSS）
  el.className = 'security-banner is-warn';
  el.setAttribute('role', 'status');
  el.setAttribute('aria-live', 'polite');
  el.innerHTML =
    '<span class="security-banner__icon">!</span>' +
    '<span class="security-banner__content">' +
    '<strong class="security-banner__title">演示模式：不会调用真实模型</strong>' +
    '<span class="security-banner__text">当前模型未配置 API Key，回复由 FakeProvider 回声生成。</span>' +
    '</span>' +
    '<span class="security-banner__actions">' +
    '<button class="security-banner__btn" data-action="configure">去配置模型</button>' +
    '</span>';
  const reference = container.querySelector('.composer-edit-banner');
  container.insertBefore(el, reference);
  el.addEventListener('click', (event) => {
    if ((event.target as HTMLElement).dataset.action === 'configure') openModelPane();
  });
  return el;
}
