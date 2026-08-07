/**
 * 首次模型配置导览。
 *
 * 只在当前账号没有自建模型时自动出现一次；视觉与 Wiki、外援导览共用
 * mw-tour-card Spotlight 样式。步骤只负责打开现有设置入口，不会代替用户填写或保存凭据。
 */

import type { BackendConfig } from '../backend-client';
import { clearRuntimeStyle, setRuntimeStyle } from '../components/runtime-style';

const MODEL_TOUR_SEEN_KEY = 'crew.desktop.modelTourSeen.v1';
const HIGHLIGHT_PADDING = 6;
const TOOLTIP_GAP = 12;
const VIEWPORT_MARGIN = 12;

interface ModelTourStep {
  selector: string;
  title: string;
  desc: string;
  highlightPadding?: number;
  highlightRadius?: number;
  prepare?: () => void;
}

const openSettings = (): void => {
  const modal = document.getElementById('settings-modal');
  if (!modal?.classList.contains('show')) {
    document.getElementById('settings-btn')?.click();
  }
};

const openModelPane = (): void => {
  openSettings();
  const modelNav = document.querySelector<HTMLElement>('[data-settings-pane="model"]');
  if (!modelNav?.classList.contains('is-active')) modelNav?.click();
};

const openModelForm = (): void => {
  openModelPane();
  const overlay = document.getElementById('model-connect-overlay');
  if (overlay?.hidden !== false) document.getElementById('cfg-model-add')?.click();
};

const MODEL_TOUR_STEPS: ModelTourStep[] = [
  {
    selector: '#settings-btn',
    title: '先配置一个自己的模型',
    desc: '当前还没有你的模型配置。先从左下角进入设置；引导不会读取或代填你的 API Key。',
  },
  {
    selector: '[data-settings-pane="model"]',
    title: '进入模型设置',
    desc: '在设置左侧选择“模型”，这里可以查看、添加和维护模型配置。',
    highlightPadding: 2,
    highlightRadius: 14,
    prepare: openSettings,
  },
  {
    selector: '#cfg-model-add',
    title: '添加模型',
    desc: '点击“添加模型”，准备填写模型 ID、接口模型名、Base URL 和 API Key。',
    prepare: openModelPane,
  },
  {
    selector: '#cfg-model-id',
    title: '填写连接信息并保存',
    desc: '从模型 ID 开始，按开放平台提供的信息依次填写；API Key 会由后端保存，不会写入浏览器存储。完成引导后表单会继续保留。',
    prepare: openModelForm,
  },
];

let container: HTMLElement | null = null;
let current = 0;
let layoutFrame: number | null = null;
let startTimer: number | null = null;

export function hasUserConfiguredModel(config: BackendConfig | null | undefined): boolean {
  const profiles = config?.model_profiles ?? config?.models ?? [];
  return profiles.some((profile) => profile.builtin === false && profile.has_key);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), Math.max(min, max));
}

function placeTooltip(tooltip: HTMLElement, rect: DOMRect): void {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const tipRect = tooltip.getBoundingClientRect();
  const tipWidth = tipRect.width || 300;
  const tipHeight = tipRect.height || 150;

  let left = rect.left + rect.width / 2 - tipWidth / 2;
  let top = rect.bottom + TOOLTIP_GAP;
  if (top + tipHeight > vh - VIEWPORT_MARGIN) {
    top = rect.top - TOOLTIP_GAP - tipHeight;
  }
  if (rect.left > vw * 0.6 && rect.left - TOOLTIP_GAP - tipWidth > VIEWPORT_MARGIN) {
    left = rect.left - TOOLTIP_GAP - tipWidth;
    top = rect.top + rect.height / 2 - tipHeight / 2;
  }

  setRuntimeStyle(tooltip, 'left', `${Math.round(clamp(left, VIEWPORT_MARGIN, vw - tipWidth - VIEWPORT_MARGIN))}px`);
  setRuntimeStyle(tooltip, 'top', `${Math.round(clamp(top, VIEWPORT_MARGIN, vh - tipHeight - VIEWPORT_MARGIN))}px`);
}

function cancelLayout(): void {
  if (layoutFrame == null) return;
  window.cancelAnimationFrame(layoutFrame);
  layoutFrame = null;
}

function closeModelTour(): void {
  cancelLayout();
  document.removeEventListener('keydown', onKeydown);
  window.removeEventListener('resize', onResize);
  container?.remove();
  container = null;
}

function finishModelTour(): void {
  try {
    localStorage.setItem(MODEL_TOUR_SEEN_KEY, '1');
  } catch {
    // localStorage 不可用时，本次关闭仍然有效。
  }
  closeModelTour();
}

function layoutStep(step: ModelTourStep): void {
  if (!container) return;
  const target = document.querySelector<HTMLElement>(step.selector);
  const highlight = container.querySelector<HTMLElement>('.wiki-tour__highlight');
  const tooltip = container.querySelector<HTMLElement>('.wiki-tour__tooltip');
  if (!target || !highlight || !tooltip) {
    closeModelTour();
    return;
  }
  const rect = target.getBoundingClientRect();
  if (rect.width < 4 || rect.height < 4) {
    closeModelTour();
    return;
  }

  const padding = step.highlightPadding ?? HIGHLIGHT_PADDING;
  highlight.hidden = false;
  tooltip.hidden = false;
  setRuntimeStyle(highlight, 'left', `${Math.round(rect.left - padding)}px`);
  setRuntimeStyle(highlight, 'top', `${Math.round(rect.top - padding)}px`);
  setRuntimeStyle(highlight, 'width', `${Math.round(rect.width + padding * 2)}px`);
  setRuntimeStyle(highlight, 'height', `${Math.round(rect.height + padding * 2)}px`);
  if (step.highlightRadius == null) clearRuntimeStyle(highlight, 'borderRadius');
  else setRuntimeStyle(highlight, 'borderRadius', `${step.highlightRadius}px`);

  tooltip.querySelector('.wiki-tour__title')!.textContent = step.title;
  tooltip.querySelector('.wiki-tour__desc')!.textContent = step.desc;
  tooltip.querySelector('.wiki-tour__progress')!.textContent = `${current + 1} / ${MODEL_TOUR_STEPS.length}`;
  const previous = tooltip.querySelector<HTMLButtonElement>('[data-tour-prev]')!;
  const next = tooltip.querySelector<HTMLButtonElement>('[data-tour-next]')!;
  previous.hidden = current === 0;
  next.textContent = current === MODEL_TOUR_STEPS.length - 1 ? '完成' : '下一步';
  placeTooltip(tooltip, rect);
}

function showStep(index: number): void {
  if (!container || index < 0) return;
  if (index >= MODEL_TOUR_STEPS.length) {
    finishModelTour();
    return;
  }
  current = index;
  const step = MODEL_TOUR_STEPS[current];
  step.prepare?.();
  cancelLayout();
  // 设置弹窗和模型表单在 click 后同步挂载；连续两帧再测量可避免首帧尺寸尚未稳定。
  layoutFrame = window.requestAnimationFrame(() => {
    layoutFrame = window.requestAnimationFrame(() => {
      layoutFrame = null;
      layoutStep(step);
    });
  });
}

function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Escape') finishModelTour();
  if (event.key === 'ArrowRight') showStep(current + 1);
  if (event.key === 'ArrowLeft') showStep(current - 1);
}

function onResize(): void {
  showStep(current);
}

export function startModelTour(): void {
  closeModelTour();
  if (!document.querySelector('#settings-btn')) return;

  container = document.createElement('div');
  container.className = 'wiki-tour model-tour';
  container.innerHTML = `
    <div class="wiki-tour__mask"></div>
    <div class="wiki-tour__highlight" hidden></div>
    <aside class="wiki-tour__tooltip mw-tour-card" role="dialog" aria-label="模型配置导览" hidden>
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
  container.querySelector('[data-tour-skip]')?.addEventListener('click', finishModelTour);
  container.querySelector('[data-tour-prev]')?.addEventListener('click', () => showStep(current - 1));
  container.querySelector('[data-tour-next]')?.addEventListener('click', () => showStep(current + 1));
  document.addEventListener('keydown', onKeydown);
  window.addEventListener('resize', onResize);
  document.body.appendChild(container);
  showStep(0);
}

export function maybeStartModelTourOnce(config: BackendConfig | null | undefined): void {
  if (!config || hasUserConfiguredModel(config) || container || startTimer != null) return;
  try {
    if (localStorage.getItem(MODEL_TOUR_SEEN_KEY)) return;
  } catch {
    // localStorage 不可用不阻断导览。
  }
  startTimer = window.setTimeout(() => {
    startTimer = null;
    if (!hasUserConfiguredModel(config)) startModelTour();
  }, 450);
}
