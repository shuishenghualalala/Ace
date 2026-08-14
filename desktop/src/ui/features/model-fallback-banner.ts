/**
 * 「演示模式」顶部 banner：当前会话实际生效的 Provider 是 FakeProvider 时常驻显示。
 *
 * 背景：后端在模型无 API Key 时静默回退 FakeProvider（回声演示），仅以 status_event
 * 在回合开头提示一次，回合结束即消失——用户会误以为模型在正常回复。本 banner 把
 * 这个状态做成持久可见，并给出「去配置模型」入口。
 *
 * 判定与后端 crew/app.py 的回退逻辑对齐：
 *   会话绑定模型有 Key            → 真实 provider，隐藏
 *   会话模型无 Key、全局 active 有 Key → 回退到 active，隐藏
 *   两者都无 Key                  → FakeProvider，显示
 * 外部 Runtime / Team 会话的模型不在 config 列表里（自行管理），无法判定时不显示，
 * 宁缺毋误报。
 *
 * 数据全部来自内存中的 state.config / 会话模型绑定，无额外请求；由
 * chat-controller.updateComposerControls 每次 renderChat 时调用，幂等。
 */

import { state } from '../state';
import type { BackendConfig } from '../backend-client';
import { activeComposerModelId, isExternalTeamSession } from './session-model';
import { openModelPane } from './model-tour';

const BANNER_ID = 'model-fallback-banner';

/** 纯判定函数，导出供单元测试。 */
export function deriveModelFallbackVisible(
  config: Pick<BackendConfig, 'has_key' | 'active_model_id'> & {
    models?: Array<{ id: string; has_key: boolean }>;
    model_profiles?: Array<{ id: string; has_key: boolean }>;
  } | null | undefined,
  sessionModelId: string,
  isExternal: boolean,
): boolean {
  if (!config || isExternal) return false;
  if (config.has_key) return false;
  const activeId = config.active_model_id || '';
  if (!sessionModelId || sessionModelId === activeId) return true;
  const profiles = [...(config.models ?? []), ...(config.model_profiles ?? [])];
  const sessionProfile = profiles.find((m) => m.id === sessionModelId);
  // 找不到的模型 id（外部 Runtime 自带模型）无法判定，不显示；
  // 找得到且有 Key → 真实 provider；无 Key → 回退全局，全局也无 Key → 演示模式。
  return sessionProfile ? !sessionProfile.has_key : false;
}

/** 幂等渲染：每次 renderChat 经 updateComposerControls 调用。 */
export function renderModelFallbackBanner(): void {
  const container = document.querySelector('.chat-composer');
  if (!container) return;
  const visible = deriveModelFallbackVisible(
    state.config,
    activeComposerModelId(),
    isExternalTeamSession(),
  );
  let banner = document.getElementById(BANNER_ID);
  if (!visible) {
    banner?.remove();
    return;
  }
  if (!banner) banner = createBanner(container);
  banner.classList.add('show');
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
