/**
 * 模型选择器：Composer 会话级模型 + 全局 config 加载。
 * openModelSelectPopover 为通用模型选择浮层：主对话 Composer 与 Wiki 右栏 Composer 共用。
 */

import { backendApi } from '../backend-client';
import { setRuntimeStyle } from '../components/runtime-style';
import { $, $$, escapeHtml, notify, state } from '../state';
import {
  activeComposerModelId,
  resolveComposerModelLabel,
  setSessionModel,
  syncSessionModelUi,
} from './session-model';
import { syncExternalAgentsFeatureUi } from './external-agents-feature';

let dropdownOpen = false;

function closeDropdown(): void {
  dropdownOpen = false;
  $('#chat-model-dropdown')?.remove();
  $('.chat-model-trigger')?.classList.remove('is-open');
}

function renderDropdown(): void {
  closeDropdown();
  const trigger = $('.chat-model-trigger');
  const shell = $('#chat-model-picker-shell');
  if (!trigger || !shell || !state.config) return;

  const models = state.config.models ?? [];
  const active = activeComposerModelId();
  const menu = document.createElement('div');
  menu.id = 'chat-model-dropdown';
  menu.className = 'chat-model-dropdown';
  menu.innerHTML = `
    <div class="chat-model-dropdown__title">选择模型</div>
    <div class="chat-model-dropdown__list">
      ${models.length === 0 ? '<div class="chat-model-option chat-model-option--empty">暂无可用模型</div>' : ''}
      ${models
        .map(
          (m) => `
        <button type="button" class="chat-model-option${m.id === active ? ' is-selected' : ''}" data-model-id="${m.id}">
          <span class="chat-model-option__copy">
            <span class="chat-model-option__name">${m.name || m.model}</span>
            <span class="chat-model-option__meta">${m.model}${m.has_key ? '' : ' · 未配置 Key'}</span>
          </span>
          ${m.id === active ? '<span class="chat-model-option__check">✓</span>' : ''}
        </button>
      `,
        )
        .join('')}
    </div>
  `;
  shell.appendChild(menu);
  dropdownOpen = true;
  trigger.classList.add('is-open');

  $$('.chat-model-option[data-model-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-model-id');
      if (id) void setSessionModel(id);
      closeDropdown();
    });
  });
}

/** @deprecated 设置页不再切换全局模型；保留供兼容，内部走 setSessionModel。 */
export async function switchModel(modelId: string): Promise<void> {
  await setSessionModel(modelId);
}

// ── 通用模型选择浮层 ──

export interface ModelSelectPopoverOptions {
  /** 锚点元素：浮层按其定位，上方空间够则向上弹。 */
  anchor: HTMLElement;
  /** 当前高亮的模型 id。 */
  activeId: string;
  /** 选中回调（浮层随后自动关闭）。 */
  onPick: (modelId: string) => void;
  /** 关闭回调（选中 / outside-click / Escape / 手动 close 均触发）。 */
  onClose?: () => void;
  /** 浮层 id（主对话传固定 id 以兼容既有 document 点击关闭选择器）。 */
  id?: string;
  width?: number;
  align?: 'start' | 'end';
}

const MODEL_CHECK_ICON = `<svg class="composer-select-item__check" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>`;

/**
 * 打开模型选择浮层（composer-select-popover 视觉与 composer-toolbar 各浮层一致）。
 * 自管理 outside-click / Escape 关闭（capture 阶段，避免与页内既有 document 监听器互相干扰）；
 * 返回手动关闭函数（幂等）。
 */
export function openModelSelectPopover(opts: ModelSelectPopoverOptions): () => void {
  const { anchor, activeId, onPick } = opts;
  const models = state.config?.models ?? [];
  const width = opts.width ?? 320;

  const popover = document.createElement('div');
  if (opts.id) popover.id = opts.id;
  popover.className = 'composer-floating-popover composer-select-popover';
  setRuntimeStyle(popover, 'width', `${width}px`);
  popover.setAttribute('role', 'listbox');
  popover.setAttribute('aria-label', '选择模型');
  popover.innerHTML = models.length
    ? `
      <div class="composer-select-popover__section">可用模型</div>
      <div class="composer-select-popover__list">
        ${models
          .map(
            (m) => `
          <button type="button" class="composer-select-item${m.id === activeId ? ' is-selected' : ''}" data-model-id="${escapeHtml(m.id)}">
            <span class="composer-select-item__icon composer-select-item__icon--model">${escapeHtml((m.name || m.model || '?').slice(0, 1).toUpperCase())}</span>
            <span class="composer-select-item__body">
              <span class="composer-select-item__title">${escapeHtml(m.name || m.model)}</span>
              <span class="composer-select-item__desc${m.has_key ? '' : ' composer-select-item__desc--warn'}">${m.has_key ? escapeHtml(m.model || m.id) : '未配置 API Key'}</span>
            </span>
            ${m.id === activeId ? MODEL_CHECK_ICON : '<span class="composer-select-item__spacer"></span>'}
          </button>
        `,
          )
          .join('')}
      </div>
    `
    : '<div class="composer-select-popover__empty">暂无模型，请前往配置页</div>';

  document.body.appendChild(popover);
  const place = (): void => {
    const rect = anchor.getBoundingClientRect();
    const ph = popover.offsetHeight || 180;
    const openUp = rect.top > ph + 12;
    let left = opts.align === 'end' ? rect.right - width : rect.left;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    setRuntimeStyle(popover, 'left', `${left}px`);
    setRuntimeStyle(popover, 'top', openUp ? `${Math.max(8, rect.top - ph - 6)}px` : `${rect.bottom + 6}px`);
  };
  requestAnimationFrame(place);

  let closed = false;
  const close = (): void => {
    if (closed) return;
    closed = true;
    document.removeEventListener('click', onDocClick, true);
    document.removeEventListener('keydown', onKeydown, true);
    popover.remove();
    opts.onClose?.();
  };
  const onDocClick = (e: MouseEvent): void => {
    const t = e.target as Node | null;
    if (t && (popover.contains(t) || anchor.contains(t))) return;
    close();
  };
  const onKeydown = (e: KeyboardEvent): void => {
    if (e.key === 'Escape') close();
  };
  document.addEventListener('click', onDocClick, true);
  document.addEventListener('keydown', onKeydown, true);

  popover.querySelectorAll('[data-model-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-model-id');
      if (id) onPick(id);
      close();
    });
  });

  return close;
}

export function syncModelUi(): void {
  syncSessionModelUi();
}

export function bindModelPicker(): void {
  document.addEventListener('click', (e) => {
    if (!dropdownOpen) return;
    const t = e.target as HTMLElement;
    if (t.closest('#chat-model-picker-shell')) return;
    closeDropdown();
  });

  $$('.chat-model-trigger').forEach((trigger) => {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      if (dropdownOpen) closeDropdown();
      else renderDropdown();
    });
  });
}

export async function loadConfig(): Promise<void> {
  try {
    state.config = await backendApi.config();
    state.configModel = resolveComposerModelLabel();
    syncModelUi();
    syncExternalAgentsFeatureUi();
    return;
  } catch {
    state.config = null;
    syncExternalAgentsFeatureUi();
  }
}
