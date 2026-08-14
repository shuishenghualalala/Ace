/**
 * 模型选择器：Composer 会话级模型 + 全局 config 加载。
 * openModelSelectPopover 为通用模型选择浮层：主对话 Composer 与 Wiki 右栏 Composer 共用。
 */

import { backendApi } from '../backend-client';
import { setRuntimeStyle } from '../components/runtime-style';
import { $, $$, escapeHtml, state } from '../state';
import {
  activeComposerModelId,
  composerModelOptions,
  resolveComposerModelLabel,
  sessionDisplayModelLabel,
  setSessionModel,
  syncSessionModelUi,
  type ComposerModelOption,
} from './session-model';
import { syncExternalAgentsFeatureUi } from './external-agents-feature';
import { maybeStartModelTourOnce } from './model-tour';
import { syncSecurityModuleFeatureUi } from './security-mode';

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
  /** 模型目录；缺省为 config 里的 Crew 模型。主对话传 composerModelOptions(sid) 以覆盖 external runtime 目录。 */
  models?: ComposerModelOption[];
  /** 提供时在浮层头部渲染「打开模型配置引导」按钮（主对话）。 */
  onModelTour?: () => void;
}

const MODEL_CHECK_ICON = `<svg class="composer-select-item__check" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>`;

/** config 里的 Crew 模型 → 浮层条目（与 composerModelOptions 的 Crew 分支同一口径）。 */
function defaultModelOptions(): ComposerModelOption[] {
  return (state.config?.models || []).map((model) => ({
    id: model.id,
    label: model.name || model.model || model.id,
    description: model.has_key ? (model.model || model.id) : '未配置 API Key',
    // 保持 Crew 原行为：无 Key 模型仍可点击，由既有后端校验负责反馈。
    selectable: true,
    warning: !model.has_key,
  }));
}

/**
 * 打开模型选择浮层（composer-select-popover 视觉与 composer-toolbar 各浮层一致）。
 * 自管理 outside-click / Escape 关闭（capture 阶段，避免与页内既有 document 监听器互相干扰）；
 * 返回手动关闭函数（幂等）。
 */
export function openModelSelectPopover(opts: ModelSelectPopoverOptions): () => void {
  const { anchor, activeId, onPick } = opts;
  const models = opts.models ?? defaultModelOptions();
  const width = opts.width ?? 320;

  const popover = document.createElement('div');
  if (opts.id) popover.id = opts.id;
  // --model 修饰类承载布局样式（composer-context.css），主对话与 Wiki 实例同一浮层同一外观。
  popover.className = 'composer-floating-popover composer-select-popover composer-select-popover--model';
  setRuntimeStyle(popover, 'width', `${width}px`);
  popover.setAttribute('role', 'listbox');
  popover.setAttribute('aria-label', '选择模型');
  const modelTourHeader = opts.onModelTour
    ? `
    <div class="composer-select-popover__header">
      <span>选择模型</span>
      <button type="button" class="composer-select-popover__help" data-model-tour-open aria-label="打开模型配置引导" title="打开模型配置引导">?</button>
    </div>`
    : '';
  popover.innerHTML = models.length
    ? `
      ${modelTourHeader}
      <div class="composer-select-popover__section">可用模型</div>
      <div class="composer-select-popover__list">
        ${models
          .map(
            (m) => `
          <button type="button" class="composer-select-item composer-select-item--model${m.id === activeId ? ' is-selected' : ''}" data-model-id="${escapeHtml(m.id)}" title="${escapeHtml(m.description)}" aria-label="${escapeHtml(`${m.label}${m.description ? `，${m.description}` : ''}`)}"${m.selectable ? '' : ' disabled'}>
            <span class="composer-select-item__body composer-select-item__body--model">
              <span class="composer-select-item__title">${escapeHtml(m.label)}${m.default ? ' · 默认' : ''}</span>
              ${m.warning ? `<span class="composer-select-item__meta composer-select-item__meta--warn">${escapeHtml(m.description)}</span>` : ''}
            </span>
            ${m.id === activeId ? MODEL_CHECK_ICON : '<span class="composer-select-item__spacer"></span>'}
          </button>
        `,
          )
          .join('')}
      </div>
    `
    : `${modelTourHeader}<div class="composer-select-popover__empty">暂无模型，请前往配置页</div>`;

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

  popover.querySelector<HTMLElement>('[data-model-tour-open]')?.addEventListener('click', (event) => {
    event.stopPropagation();
    close();
    opts.onModelTour?.();
  });

  popover.querySelectorAll('[data-model-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-model-id');
      if (id) onPick(id);
      close();
    });
  });

  return close;
}

// ── Composer 模型 chip 控制器（主对话 / Wiki 问答面板各实例化一个） ──

export interface ComposerModelControl {
  /** 按当前 getSessionId() 重刷 chip 文案。 */
  refresh(): void;
  /** 浮层开着时关闭（幂等）。 */
  close(): void;
  dispose(): void;
}

export interface ComposerModelControlOptions {
  /** 本实例的会话来源（主对话 = 全局活跃会话；Wiki = 内嵌会话）。 */
  getSessionId: () => string | null;
  /** chip 文案元素；缺省在 anchor 内查 .mw-context-chip__label。 */
  labelEl?: HTMLElement | null;
  /** 打开浮层前的协调钩子（主对话用来先关掉其他工具栏浮层）。 */
  onBeforeOpen?: () => void;
  /** 提供时浮层头部带「模型配置引导」入口（主对话）。 */
  onModelTour?: () => void;
  /** 切换模型时透传的 workspace_id；缺省走主 Composer 工作区。 */
  workspaceId?: string;
}

/**
 * Composer 工具栏的模型 chip：点击开合统一模型浮层，选中走会话级 setSessionModel，
 * 文案跟随 session:model-changed（按 sessionId 过滤，多实例互不干扰）。
 */
export function createComposerModelControl(
  anchor: HTMLButtonElement,
  opts: ComposerModelControlOptions,
): ComposerModelControl {
  const controller = new AbortController();
  const labelEl = opts.labelEl ?? anchor.querySelector<HTMLElement>('.mw-context-chip__label');
  let closePopover: (() => void) | null = null;

  const syncLabel = (): void => {
    if (labelEl) labelEl.textContent = sessionDisplayModelLabel(opts.getSessionId()) || '模型';
  };

  const close = (): void => {
    closePopover?.();
  };

  anchor.addEventListener('click', (event) => {
    event.stopPropagation();
    if (closePopover) {
      closePopover();
      return;
    }
    if (anchor.disabled || !state.config) return;
    opts.onBeforeOpen?.();
    const sid = opts.getSessionId();
    anchor.classList.add('is-open');
    anchor.setAttribute('aria-expanded', 'true');
    closePopover = openModelSelectPopover({
      anchor,
      activeId: activeComposerModelId(sid),
      models: composerModelOptions(sid),
      align: 'end',
      width: 300,
      ...(opts.onModelTour ? { onModelTour: opts.onModelTour } : {}),
      onPick: (modelId) => {
        void setSessionModel(modelId, sid ?? undefined, opts.workspaceId);
      },
      onClose: () => {
        closePopover = null;
        anchor.classList.remove('is-open');
        anchor.setAttribute('aria-expanded', 'false');
      },
    });
  }, { signal: controller.signal });

  window.addEventListener('session:model-changed', (ev) => {
    const detail = (ev as CustomEvent<{ sessionId?: string }>).detail;
    if (detail?.sessionId && detail.sessionId === opts.getSessionId()) syncLabel();
  }, { signal: controller.signal });
  syncLabel();

  return {
    refresh: syncLabel,
    close,
    dispose() {
      controller.abort();
      closePopover?.();
    },
  };
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
    syncSecurityModuleFeatureUi();
    maybeStartModelTourOnce(state.config);
    return;
  } catch {
    state.config = null;
    syncExternalAgentsFeatureUi();
    syncSecurityModuleFeatureUi();
  }
}
