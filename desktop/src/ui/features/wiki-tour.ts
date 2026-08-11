/**
 * Wiki 界面导览（spotlight tour）：遮罩暗化全屏，逐个高亮界面控件并附气泡介绍。
 *
 * 入口：首次进入 Wiki 页自动启动一次（localStorage 标记，见 maybeStartWikiTourOnce），
 * 之后可通过页头「?」按钮随时重新启动（startWikiTour）。
 * 卡片视觉复用模型与外援导览的 mw-tour-card，直接把每个功能的位置指给用户看。
 * 目标元素不存在时（如未登录态缺少对话栏）自动跳过该步。
 */

import { setRuntimeStyle } from '../components/runtime-style';

const TOUR_SEEN_KEY = 'crew.desktop.wikiTourSeen.v1';
const HIGHLIGHT_PADDING = 6;
const TOOLTIP_GAP = 12;
const VIEWPORT_MARGIN = 12;

interface TourStep {
  /** 目标元素选择器（在 wiki-page.ts renderShell 的 DOM 里查询）。 */
  selector: string;
  title: string;
  desc: string;
}

/** 使用引导只讲两件事：有疑问问左侧智能体；完整教程在「教程」知识库。 */
const TOUR_STEPS: TourStep[] = [
  {
    selector: '[data-wiki-agent-panel]',
    title: '有疑问？先问这里的智能体',
    desc: '左侧对话区的 Wiki Agent 就是你的使用助手：功能怎么用、操作出了什么问题、想让它帮你整理内容……直接用大白话问它，什么都能解答。把 AI 当助手来沟通，是最快的用法。',
  },
  {
    selector: '#wiki-kb-select',
    title: '完整教程在「教程」知识库',
    desc: '在这里切换到「教程」知识库，里面有详细的使用指南和案例，边用边查——用 Wiki 本身来学 Wiki。找不到也没关系，直接问智能体。',
  },
];

let container: HTMLElement | null = null;
let steps: Array<TourStep & { rect: DOMRect }> = [];
let current = 0;

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function resolveSteps(): Array<TourStep & { rect: DOMRect }> {
  const resolved: Array<TourStep & { rect: DOMRect }> = [];
  for (const step of TOUR_STEPS) {
    const el = document.querySelector(step.selector);
    if (!el) continue;
    const rect = el.getBoundingClientRect();
    // 目标不可见（塌陷/隐藏）时跳过，避免高亮一个 0 尺寸的空气。
    if (rect.width < 4 || rect.height < 4) continue;
    resolved.push({ ...step, rect });
  }
  return resolved;
}

function closeWikiTour(): void {
  if (!container) return;
  document.removeEventListener('keydown', onKeydown);
  window.removeEventListener('resize', onResize);
  container.remove();
  container = null;
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Escape') closeWikiTour();
  if (e.key === 'ArrowRight') showStep(current + 1);
  if (e.key === 'ArrowLeft') showStep(current - 1);
}

/** 视口变化后目标位置失效，重新解析各步目标并回到当前步。 */
function onResize(): void {
  const fresh = resolveSteps();
  if (fresh.length === 0) {
    closeWikiTour();
    return;
  }
  steps = fresh;
  showStep(Math.min(current, steps.length - 1));
}

/** 计算气泡位置：优先放目标下方，空间不足放上方；目标在屏幕右侧 1/3 时放左侧。 */
function placeTooltip(tooltip: HTMLElement, rect: DOMRect): void {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const tipRect = tooltip.getBoundingClientRect();
  const tipW = tipRect.width || 300;
  const tipH = tipRect.height || 140;

  let left: number;
  let top: number;

  if (rect.left > vw * 0.6 && rect.left - TOOLTIP_GAP - tipW > VIEWPORT_MARGIN) {
    // 目标靠右（如知识库面板）：气泡放左侧，垂直方向与目标顶部对齐并夹取。
    left = rect.left - TOOLTIP_GAP - tipW;
    top = clamp(rect.top, VIEWPORT_MARGIN, vh - tipH - VIEWPORT_MARGIN);
  } else {
    // 默认放下方，放不下就放上方。
    top = rect.bottom + TOOLTIP_GAP;
    if (top + tipH > vh - VIEWPORT_MARGIN) {
      top = Math.max(rect.top - TOOLTIP_GAP - tipH, VIEWPORT_MARGIN);
    }
    // 水平方向与目标中心对齐，夹取在视口内。
    left = clamp(rect.left + rect.width / 2 - tipW / 2, VIEWPORT_MARGIN, vw - tipW - VIEWPORT_MARGIN);
  }

  setRuntimeStyle(tooltip, 'left', `${Math.round(left)}px`);
  setRuntimeStyle(tooltip, 'top', `${Math.round(top)}px`);
}

function showStep(index: number): void {
  if (!container || steps.length === 0) return;
  if (index < 0) return;
  if (index >= steps.length) {
    closeWikiTour();
    return;
  }
  current = index;
  const step = steps[current];

  const highlight = container.querySelector<HTMLElement>('.wiki-tour__highlight');
  const tooltip = container.querySelector<HTMLElement>('.wiki-tour__tooltip');
  if (!highlight || !tooltip) return;

  setRuntimeStyle(highlight, 'left', `${step.rect.left - HIGHLIGHT_PADDING}px`);
  setRuntimeStyle(highlight, 'top', `${step.rect.top - HIGHLIGHT_PADDING}px`);
  setRuntimeStyle(highlight, 'width', `${step.rect.width + HIGHLIGHT_PADDING * 2}px`);
  setRuntimeStyle(highlight, 'height', `${step.rect.height + HIGHLIGHT_PADDING * 2}px`);

  tooltip.querySelector('.wiki-tour__title')!.textContent = step.title;
  tooltip.querySelector('.wiki-tour__desc')!.textContent = step.desc;
  tooltip.querySelector('.wiki-tour__progress')!.textContent = `${current + 1} / ${steps.length}`;

  const prevBtn = tooltip.querySelector<HTMLButtonElement>('[data-tour-prev]')!;
  const nextBtn = tooltip.querySelector<HTMLButtonElement>('[data-tour-next]')!;
  prevBtn.hidden = current === 0;
  nextBtn.textContent = current === steps.length - 1 ? '完成' : '下一步';

  // 先更新内容再测量定位（内容高度影响放置方向）。
  placeTooltip(tooltip, step.rect);
}

/** 启动界面导览。当前页面找不到任何目标控件时静默不启动。 */
export function startWikiTour(): void {
  closeWikiTour();
  steps = resolveSteps();
  if (steps.length === 0) return;

  container = document.createElement('div');
  container.className = 'wiki-tour';
  container.innerHTML = `
    <div class="wiki-tour__mask"></div>
    <div class="wiki-tour__highlight"></div>
    <aside class="wiki-tour__tooltip mw-tour-card" role="dialog" aria-label="界面导览">
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

  container.querySelector('[data-tour-skip]')!.addEventListener('click', closeWikiTour);
  container.querySelector('[data-tour-prev]')!.addEventListener('click', () => showStep(current - 1));
  container.querySelector('[data-tour-next]')!.addEventListener('click', () => showStep(current + 1));
  // 点遮罩不退出（防误触），只响应 Esc / 跳过 / 完成。
  document.addEventListener('keydown', onKeydown);
  window.addEventListener('resize', onResize);

  document.body.appendChild(container);
  showStep(0);
}

/** 首次进入 Wiki 页时自动启动一次；此后只通过「使用教程」按钮手动启动。 */
export function maybeStartWikiTourOnce(): void {
  try {
    if (localStorage.getItem(TOUR_SEEN_KEY)) return;
    localStorage.setItem(TOUR_SEEN_KEY, '1');
  } catch {
    // localStorage 不可用时静默降级：每次都启动也不阻断页面。
  }
  startWikiTour();
}
