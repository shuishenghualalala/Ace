/** Ace 首次使用导览：覆盖主导航、对话输入区和设置入口。 */

import { setRuntimeStyle } from '../components/runtime-style';
import { authStore } from '../stores/auth-store';

const USER_GUIDE_SEEN_KEY_PREFIX = 'Crew.desktop.userGuideSeen.v1';
const ANONYMOUS_USER_BUCKET = '__anonymous__';
const HIGHLIGHT_PADDING = 6;
const TOOLTIP_GAP = 12;
const VIEWPORT_MARGIN = 12;

interface UserGuideStep {
  selector: string;
  title: string;
  desc: string;
}

const USER_GUIDE_STEPS: UserGuideStep[] = [
  {
    selector: '#settings-btn',
    title: '第一步，先配置模型',
    desc: '点击左下角“设置”，进入“模型”后选择“添加模型”，填写模型 ID、接口模型名、Base URL 和 API Key，保存后才能开始对话。',
  },
  {
    selector: '.mw-app-navigation__list',
    title: '左侧是你的工作入口',
    desc: '对话适合直接提问，技能用于复用能力，外援可以派活或组队，Wiki 用来沉淀和检索知识。',
  },
  {
    selector: '#chat-composer-root',
    title: '配置好模型后开始对话',
    desc: '直接在下方输入你要完成的事情。Ace 会根据任务选择合适的工具，并在需要时向你确认关键步骤。',
  },
  {
    selector: '[data-shell-command="help"]',
    title: '以后随时打开帮助',
    desc: '忘记入口或想重新了解功能时，点击左下角的“帮助”，就能再次打开这份用户指引。',
  },
];

let container: HTMLElement | null = null;
let steps: Array<UserGuideStep & { rect: DOMRect }> = [];
let current = 0;
let startTimer: number | null = null;

function userGuideStorageKey(): string {
  const staffCode = authStore.get().userInfo?.staffCode?.trim();
  const bucket = staffCode || ANONYMOUS_USER_BUCKET;
  return `${USER_GUIDE_SEEN_KEY_PREFIX}:${encodeURIComponent(bucket)}`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function resolveSteps(): Array<UserGuideStep & { rect: DOMRect }> {
  const resolved: Array<UserGuideStep & { rect: DOMRect }> = [];
  for (const step of USER_GUIDE_STEPS) {
    const element = document.querySelector<HTMLElement>(step.selector);
    if (!element) continue;
    const rect = element.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 4) continue;
    resolved.push({ ...step, rect });
  }
  return resolved;
}

function closeUserGuide(): void {
  document.removeEventListener('keydown', onKeydown);
  window.removeEventListener('resize', onResize);
  container?.remove();
  container = null;
  steps = [];
}

function finishUserGuide(): void {
  try {
    localStorage.setItem(userGuideStorageKey(), '1');
  } catch {
    // 存储不可用时，本次关闭仍然有效。
  }
  closeUserGuide();
}

function placeTooltip(tooltip: HTMLElement, rect: DOMRect): void {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const tooltipRect = tooltip.getBoundingClientRect();
  const tooltipWidth = tooltipRect.width || 320;
  const tooltipHeight = tooltipRect.height || 160;

  let left = rect.left + rect.width / 2 - tooltipWidth / 2;
  let top = rect.bottom + TOOLTIP_GAP;
  if (top + tooltipHeight > viewportHeight - VIEWPORT_MARGIN) {
    top = rect.top - TOOLTIP_GAP - tooltipHeight;
  }
  if (rect.left > viewportWidth * 0.6 && rect.left - TOOLTIP_GAP - tooltipWidth > VIEWPORT_MARGIN) {
    left = rect.left - TOOLTIP_GAP - tooltipWidth;
    top = rect.top + rect.height / 2 - tooltipHeight / 2;
  }

  setRuntimeStyle(tooltip, 'left', `${Math.round(clamp(left, VIEWPORT_MARGIN, viewportWidth - tooltipWidth - VIEWPORT_MARGIN))}px`);
  setRuntimeStyle(tooltip, 'top', `${Math.round(clamp(top, VIEWPORT_MARGIN, viewportHeight - tooltipHeight - VIEWPORT_MARGIN))}px`);
}

function placeWelcomeTooltip(tooltip: HTMLElement): void {
  const viewportWidth = window.innerWidth;
  const viewportHeight = window.innerHeight;
  const tooltipRect = tooltip.getBoundingClientRect();
  const tooltipWidth = tooltipRect.width || 320;
  const tooltipHeight = tooltipRect.height || 176;
  setRuntimeStyle(tooltip, 'left', `${Math.round(Math.max(VIEWPORT_MARGIN, viewportWidth - tooltipWidth - 24))}px`);
  setRuntimeStyle(tooltip, 'top', `${Math.round(Math.max(VIEWPORT_MARGIN, viewportHeight - tooltipHeight - 24))}px`);
}

function showStep(index: number): void {
  if (!container || steps.length === 0) return;
  if (index < 0) return;
  if (index >= steps.length) {
    finishUserGuide();
    return;
  }
  current = index;
  const step = steps[current];
  const highlight = container.querySelector<HTMLElement>('.wiki-tour__highlight');
  const tooltip = container.querySelector<HTMLElement>('.wiki-tour__tooltip');
  if (!highlight || !tooltip) return;

  setRuntimeStyle(highlight, 'left', `${Math.round(step.rect.left - HIGHLIGHT_PADDING)}px`);
  setRuntimeStyle(highlight, 'top', `${Math.round(step.rect.top - HIGHLIGHT_PADDING)}px`);
  setRuntimeStyle(highlight, 'width', `${Math.round(step.rect.width + HIGHLIGHT_PADDING * 2)}px`);
  setRuntimeStyle(highlight, 'height', `${Math.round(step.rect.height + HIGHLIGHT_PADDING * 2)}px`);
  tooltip.querySelector<HTMLElement>('.wiki-tour__title')!.textContent = step.title;
  tooltip.querySelector<HTMLElement>('.wiki-tour__desc')!.textContent = step.desc;
  tooltip.querySelector<HTMLElement>('.wiki-tour__progress')!.textContent = `${current + 1} / ${steps.length}`;

  const previous = tooltip.querySelector<HTMLButtonElement>('[data-tour-prev]')!;
  const next = tooltip.querySelector<HTMLButtonElement>('[data-tour-next]')!;
  previous.hidden = current === 0;
  next.textContent = current === steps.length - 1 ? '完成' : '下一步';
  placeTooltip(tooltip, step.rect);
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') finishUserGuide();
  if (event.key === 'ArrowRight') showStep(current + 1);
  if (event.key === 'ArrowLeft') showStep(current - 1);
}

function onResize(): void {
  if (!container) return;
  if (container.dataset.guideMode === 'welcome') {
    const tooltip = container.querySelector<HTMLElement>('.wiki-tour__tooltip');
    if (tooltip) placeWelcomeTooltip(tooltip);
    return;
  }
  const fresh = resolveSteps();
  if (fresh.length === 0) {
    closeUserGuide();
    return;
  }
  steps = fresh;
  showStep(Math.min(current, steps.length - 1));
}

function createTourContainer(): HTMLElement {
  const next = document.createElement('div');
  next.className = 'wiki-tour user-guide';
  next.innerHTML = `
    <div class="wiki-tour__mask"></div>
    <div class="wiki-tour__highlight"></div>
    <aside class="wiki-tour__tooltip mw-tour-card" role="dialog" aria-label="Ace 用户指引">
      <div class="mw-tour-card__top"><span class="mw-tour-card__spark" aria-hidden="true"></span><span class="wiki-tour__progress"></span></div>
      <strong class="wiki-tour__title"></strong>
      <p class="wiki-tour__desc"></p>
      <div class="mw-tour-card__actions">
        <button type="button" class="mw-tour-card__quiet" data-tour-skip>跳过</button>
        <div class="mw-tour-card__steps">
          <button type="button" class="mw-tour-card__secondary" data-tour-prev>上一步</button>
          <button type="button" class="mw-tour-card__primary" data-tour-next>下一步</button>
        </div>
      </div>
    </aside>`;
  next.querySelector('[data-tour-skip]')?.addEventListener('click', finishUserGuide);
  next.querySelector('[data-tour-prev]')?.addEventListener('click', () => showStep(current - 1));
  next.querySelector('[data-tour-next]')?.addEventListener('click', () => showStep(current + 1));
  return next;
}

function showWelcome(): void {
  container = document.createElement('div');
  container.className = 'wiki-tour user-guide';
  container.dataset.guideMode = 'welcome';
  container.innerHTML = `
    <aside class="wiki-tour__tooltip mw-tour-card" role="dialog" aria-label="Ace 用户指引">
      <div class="mw-tour-card__top"><span class="mw-tour-card__spark" aria-hidden="true"></span><span>Ace 小向导</span></div>
      <strong>第一次使用 Ace？</strong>
      <p>用 30 秒认识对话、技能、外援、Wiki 和设置入口。</p>
      <div class="mw-tour-card__actions mw-tour-card__actions--welcome">
        <button type="button" class="mw-tour-card__secondary" data-tour-skip>稍后再说</button>
        <button type="button" class="mw-tour-card__primary" data-tour-start>开始看看</button>
      </div>
    </aside>`;
  container.querySelector('[data-tour-skip]')?.addEventListener('click', finishUserGuide);
  container.querySelector('[data-tour-start]')?.addEventListener('click', startUserGuide);
  document.body.appendChild(container);
  const tooltip = container.querySelector<HTMLElement>('.wiki-tour__tooltip');
  if (tooltip) placeWelcomeTooltip(tooltip);
}

export function startUserGuide(): void {
  if (startTimer !== null) {
    window.clearTimeout(startTimer);
    startTimer = null;
  }
  closeUserGuide();
  steps = resolveSteps();
  if (steps.length === 0) return;
  container = createTourContainer();
  container.dataset.guideMode = 'tour';
  document.addEventListener('keydown', onKeydown);
  window.addEventListener('resize', onResize);
  document.body.appendChild(container);
  showStep(0);
}

export function maybeStartUserGuideOnce(): void {
  if (container || startTimer !== null) return;
  try {
    if (localStorage.getItem(userGuideStorageKey())) return;
  } catch {
    // 存储不可用时仍允许本次启动导览。
  }
  startTimer = window.setTimeout(() => {
    startTimer = null;
    if (!container) showWelcome();
  }, 450);
}

export function disposeUserGuide(): void {
  if (startTimer !== null) {
    window.clearTimeout(startTimer);
    startTimer = null;
  }
  closeUserGuide();
}
