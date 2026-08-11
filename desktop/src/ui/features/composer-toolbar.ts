/**
 * Composer 工具栏：Craft/Plan/Ask 下拉 + 模型/Skills/安全模式 浮层（移开鼠标关闭）。
 */

import {
  backendApi,
  type ExternalAgent,
  type ExternalRuntime,
  type ExternalTeam,
  type Skill,
} from '../backend-client';
import { setRuntimeStyle } from '../components/runtime-style';
import { MONOCHROME_ICON_CLASS } from '../components/icon';
import { getSkills, onSkillsChange } from './skill-store';
import { queryPrimaryComposer } from './composer-scope';
import { $, $$, ensureSessionBook, escapeHtml, notify, patchBook, state, type ComposerMode } from '../state';
import {
  canSwitchComposerWorkspace,
  composerWorkspaceId,
  createWorkspaceFromFolderPicker,
  ensureComposerDraftSession,
  getSessionAgentDisplay,
  setComposerTargetWorkspace,
  visibleProjectWorkspaces,
  workspaceForSessionDispatch,
  workspaceLabel,
} from './workspaces';
import { composerModelOptions, setSessionModel, activeComposerModelId, resolveComposerModelLabel } from './session-model';
import { syncModelUi } from './model-picker';
import {
  loadExternalConversationCatalog,
  useAgent,
  useTeam,
  type ExternalConversationCatalog,
} from './agents-page';
import {
  EXTERNAL_AGENTS_DISABLED_MESSAGE,
  externalAgentsEnabled,
} from './external-agents-feature';
import { showConfirmDialog } from '../ui-feedback';
import { sessionStore } from '../stores/session-store';
import { startModelTour } from './model-tour';
import {
  FULL_ACCESS_CONFIRMATION,
  SECURITY_MODE_OPTIONS,
  currentSecurityMode,
  modeLabel,
  selectNextConversationMode,
  type ConversationSecurityMode,
} from './security-approval';

type ComposerEntry = ComposerMode | 'external';

const CRAFT_OPTIONS: { value: ComposerEntry; label: string; desc: string }[] = [
  { value: 'craft', label: '智能体', desc: '默认单 Agent 创作' },
  { value: 'plan', label: '计划模式', desc: '先出方案 · 审批后再执行' },
  { value: 'external', label: '外援', desc: '选择已接入的智能体或团队' },
  // { value: 'ask', label: '问答', desc: '交给 Team 协作回答' },  // 「问答」模式暂时下线；ask 模式位保留，恢复时取消注释即可
];

function visibleCraftOptions(): typeof CRAFT_OPTIONS {
  return externalAgentsEnabled()
    ? CRAFT_OPTIONS
    : CRAFT_OPTIONS.filter((option) => option.value !== 'external');
}

let modelPopoverOpen = false;
let skillsPopoverOpen = false;
let craftPopoverOpen = false;
let workspacePopoverOpen = false;
let securityModePopoverOpen = false;
let toolbarController: AbortController | null = null;
let toolbarBoundTrigger: HTMLElement | null = null;

type PopoverPlacement = 'auto' | 'down' | 'right';

function mountFloatingPopover(
  anchor: HTMLElement,
  popover: HTMLElement,
  width = 300,
  align: 'start' | 'end' = 'start',
  placement: PopoverPlacement = 'auto',
  maxHeight?: number,
): void {
  popover.classList.add('composer-floating-popover');
  popover.dataset.placement = placement;
  const actualWidth = Math.min(width, Math.max(0, window.innerWidth - 16));
  setRuntimeStyle(popover, 'width', `${actualWidth}px`);
  document.body.appendChild(popover);
  window.dispatchEvent(new CustomEvent('composer:popover-opened'));
  scheduleFloatingPopoverPosition(anchor, popover, actualWidth, align, placement, maxHeight);
}

/**
 * 浮层定位：right 用于外援等右侧子菜单，横向空间不足时回退 auto；
 * down=空间允许时向下，否则自动翻到上方；auto=空间允许优先向上，否则向下。
 */
function scheduleFloatingPopoverPosition(
  anchor: HTMLElement,
  popover: HTMLElement,
  width: number,
  align: 'start' | 'end' = 'start',
  placement: PopoverPlacement = 'auto',
  maxHeight = Number.POSITIVE_INFINITY,
): void {
  requestAnimationFrame(() => {
    if (!anchor.isConnected || !popover.isConnected) return;
    const rect = anchor.getBoundingClientRect();
    const ph = Math.min(popover.offsetHeight || 180, maxHeight);
    const gap = 6;
    if (placement === 'right') {
      const availableRight = Math.max(0, window.innerWidth - rect.right - gap - 8);
      const availableLeft = Math.max(0, rect.left - gap - 8);
      const openRight = availableRight >= Math.min(width, 220) || availableRight >= availableLeft;
      const sideSpace = openRight ? availableRight : availableLeft;
      if (sideSpace >= Math.min(width, 220)) {
        const nestedWidth = Math.min(width, sideSpace);
        const availableHeight = Math.min(Math.max(0, window.innerHeight - 16), maxHeight);
        const nestedHeight = Math.min(popover.offsetHeight || ph, availableHeight);
        const left = openRight ? rect.right + gap : rect.left - nestedWidth - gap;
        const top = Math.max(8, Math.min(
          rect.bottom - nestedHeight,
          window.innerHeight - nestedHeight - 8,
        ));
        setRuntimeStyle(popover, 'width', `${nestedWidth}px`);
        setRuntimeStyle(popover, 'maxHeight', `${availableHeight}px`);
        setRuntimeStyle(popover, 'left', `${Math.max(8, left)}px`);
        setRuntimeStyle(popover, 'top', `${top}px`);
        popover.dataset.resolvedPlacement = openRight ? 'right-up' : 'left-up';
        return;
      }
    }
    let left = align === 'end' ? rect.right - width : rect.left;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    setRuntimeStyle(popover, 'left', `${left}px`);
    if (placement === 'down') {
      const availableBelow = Math.max(0, window.innerHeight - rect.bottom - gap - 8);
      const availableAbove = Math.max(0, rect.top - gap - 8);
      const openDown = availableBelow >= ph || availableBelow >= availableAbove;
      const available = openDown ? availableBelow : availableAbove;
      const effectiveHeight = Math.min(ph, available);
      popover.dataset.resolvedPlacement = openDown ? 'down' : 'up';
      setRuntimeStyle(
        popover,
        'top',
        openDown ? `${rect.bottom + gap}px` : `${Math.max(8, rect.top - gap - effectiveHeight)}px`,
      );
      setRuntimeStyle(popover, 'maxHeight', `${Math.min(available, maxHeight)}px`);
      return;
    }
    const availableAbove = Math.max(0, rect.top - gap - 8);
    const availableBelow = Math.max(0, window.innerHeight - rect.bottom - gap - 8);
    const openUp = availableAbove >= ph || (availableBelow < ph && availableAbove >= availableBelow);
    const available = openUp ? availableAbove : availableBelow;
    const effectiveHeight = Math.min(ph, available);
    popover.dataset.resolvedPlacement = openUp ? 'up' : 'down';
    setRuntimeStyle(
      popover,
      'top',
      openUp ? `${Math.max(8, rect.top - gap - effectiveHeight)}px` : `${rect.bottom + gap}px`,
    );
    setRuntimeStyle(popover, 'maxHeight', `${Math.min(available, maxHeight)}px`);
  });
}

function closeCraftPopover(): void {
  craftPopoverOpen = false;
  $('#chat-craft-popover')?.remove();
  $('#chat-craft-btn')?.classList.remove('is-open');
}

function closeModelPopover(): void {
  modelPopoverOpen = false;
  $('#chat-model-inline-popover')?.remove();
  $('#chat-model-picker-inline-btn')?.classList.remove('is-open');
}

function closeSecurityModeInline(): void {
  securityModePopoverOpen = false;
  $('#chat-security-mode-inline-popover')?.remove();
  $('#chat-security-mode-btn')?.classList.remove('is-open');
  $('#chat-security-mode-btn')?.setAttribute('aria-expanded', 'false');
}

function closeSkillsPopover(): void {
  skillsPopoverOpen = false;
  $('#chat-skills-inline-popover')?.remove();
  $('#chat-skills-btn')?.classList.remove('is-open');
}

let externalPopoverOpen = false;
function closeExternalPopover(): void {
  externalPopoverOpen = false;
  $('#chat-external-inline-popover')?.remove();
  const btn = $('#chat-craft-btn');
  btn?.classList.remove('is-open');
  btn?.setAttribute('aria-expanded', 'false');
}

function closeWorkspacePopover(): void {
  workspacePopoverOpen = false;
  $('#chat-workspace-popover')?.remove();
  const btn = $('#chat-workspace-btn');
  btn?.classList.remove('is-open');
  btn?.setAttribute('aria-expanded', 'false');
}

function syncComposerWorkspaceRowVisibility(): void {
  const row = document.getElementById('chat-workspace-row');
  if (!row) return;
  const show = canSwitchComposerWorkspace();
  const host = row.parentElement;
  row.hidden = !show;
  if (host?.matches('[data-composer-context-target="project"]')) host.hidden = !show;
  if (!show) closeWorkspacePopover();
}

function syncComposerWorkspaceLabel(): void {
  const btn = document.getElementById('chat-workspace-btn') as HTMLButtonElement | null;
  const label = document.getElementById('chat-workspace-btn-label');
  syncComposerWorkspaceRowVisibility();
  if (!label || !btn) return;
  if (!canSwitchComposerWorkspace()) return;

  const id = composerWorkspaceId();
  const isDefault = id === 'default';
  label.textContent = isDefault ? '不在项目中工作' : workspaceLabel(id);
  btn.title = isDefault ? '选择项目' : `项目：${label.textContent}`;
  btn.classList.toggle('is-named', !isDefault);
  btn.classList.remove('is-locked');
}

function filterWorkspacePopoverList(popover: HTMLElement, query: string): void {
  const q = query.trim().toLowerCase();
  popover.querySelectorAll<HTMLElement>('[data-workspace-project]').forEach((row) => {
    const name = row.querySelector('.composer-select-item__title')?.textContent?.toLowerCase() ?? '';
    row.hidden = Boolean(q && !name.includes(q));
  });
}

function renderWorkspacePopover(): void {
  ensureComposerDraftSession();
  if (!canSwitchComposerWorkspace()) return;

  closeAllPopovers();
  const anchor = $('#chat-workspace-btn');
  if (!anchor) return;

  const current = composerWorkspaceId();
  const projects = visibleProjectWorkspaces();

  const popover = document.createElement('div');
  popover.id = 'chat-workspace-popover';
  popover.innerHTML = `
    <div class="composer-select-popover__search">
      <svg class="mw-icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><use href="#icon-search"></use></svg>
      <input type="search" class="chat-popover-search-input" placeholder="搜索项目" id="chat-workspace-search-input" autocomplete="off" />
    </div>
    <div class="composer-select-popover__list" role="listbox" aria-label="项目">
      ${projects
        .map((ws) => {
          const selected = ws.id === current;
          return `
        <button type="button" class="composer-select-item${selected ? ' is-selected' : ''}" data-workspace-id="${escapeHtml(ws.id)}" data-workspace-project>
          <span class="composer-select-item__plain-icon">${WORKSPACE_FOLDER_ICON}</span>
          <span class="composer-select-item__body">
            <span class="composer-select-item__title">${escapeHtml(ws.name)}</span>
          </span>
          ${selected ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
        </button>
      `;
        })
        .join('')}
      ${projects.length === 0 ? '<div class="composer-select-popover__empty">暂无项目，可打开本地文件夹创建</div>' : ''}
      <div class="composer-select-popover__divider" role="separator"></div>
      <button type="button" class="composer-select-item" data-workspace-open-local>
        <span class="composer-select-item__plain-icon"><svg class="mw-icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><use href="#icon-plus"></use></svg></span>
        <span class="composer-select-item__body">
          <span class="composer-select-item__title">新建项目</span>
        </span>
        <span class="composer-select-item__spacer"></span>
      </button>
      <button type="button" class="composer-select-item${current === 'default' ? ' is-selected' : ''}" data-workspace-id="default">
        <span class="composer-select-item__plain-icon"><svg class="mw-icon" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true"><use href="#icon-close"></use></svg></span>
        <span class="composer-select-item__body">
          <span class="composer-select-item__title">不在项目中工作</span>
        </span>
        ${current === 'default' ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
      </button>
    </div>
  `;

  mountSelectPopover(anchor, popover, 300, 'end');
  workspacePopoverOpen = true;
  anchor.classList.add('is-open');
  anchor.setAttribute('aria-expanded', 'true');

  const searchInput = $('#chat-workspace-search-input') as HTMLInputElement | null;
  searchInput?.addEventListener('input', () => filterWorkspacePopoverList(popover, searchInput.value));
  searchInput?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeWorkspacePopover();
  });
  requestAnimationFrame(() => searchInput?.focus());

  popover.querySelector('[data-workspace-open-local]')?.addEventListener('click', () => {
    closeWorkspacePopover();
    void createWorkspaceFromFolderPicker(() => {}).then(() => {
      if (!canSwitchComposerWorkspace()) return;
      setComposerTargetWorkspace(state.currentWorkspaceId);
      syncComposerWorkspaceLabel();
      window.dispatchEvent(new CustomEvent('workspace:context-changed'));
    });
  });

  popover.querySelectorAll('[data-workspace-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const wsId = btn.getAttribute('data-workspace-id') || 'default';
      setComposerTargetWorkspace(wsId);
      syncComposerWorkspaceLabel();
      closeWorkspacePopover();
      window.dispatchEvent(new CustomEvent('workspace:context-changed'));
    });
  });
}

function closeAllPopovers(): void {
  closeCraftPopover();
  closeModelPopover();
  closeSecurityModeInline();
  closeSkillsPopover();
  closeExternalPopover();
  closeWorkspacePopover();
}

export function applyComposerMode(mode: ComposerMode): void {
  const previousMode = state.composerMode;
  const sid = state.activeSessionId;
  if (mode === 'plan' && sid) {
    const book = ensureSessionBook(sid);
    if (!book.planActive) {
      void state.socket?.planEnter(sid).then((sent) => {
        if (sent) patchBook(sid, { planActive: true, pendingPlan: null });
      });
    }
  } else if (previousMode === 'plan' && mode !== 'plan' && sid) {
    void state.socket?.planExit(sid);
    patchBook(sid, { planActive: false, pendingPlan: null });
  }
  state.composerMode = mode;
  syncCraftLabel();
  window.dispatchEvent(new CustomEvent('craft:mode-change', { detail: mode }));
}

export function syncCraftLabel(): void {
  const label = $('#chat-craft-btn-label');
  const btn = $('#chat-craft-btn') as HTMLElement | null;
  const selection = $('#chat-craft-inline');
  const clear = $('#chat-craft-clear') as HTMLButtonElement | null;
  if (!label) return;
  const externalDisplay = getSessionAgentDisplay(state.activeSessionId);
  const externalKind = externalDisplay?.agentBinding?.kind;
  const externalName = String(externalDisplay?.agentLabel?.name || '').trim();
  let title = 'Craft · Plan · Ask';
  // 选中外援后，按钮直接显示外援名，不再带模式前缀
  if (
    externalName
    && (externalKind === 'external_agent' || externalKind === 'external_team')
  ) {
    label.textContent = externalName;
    title = `外援：${externalName}`;
  } else {
    const current = CRAFT_OPTIONS.find((o) => o.value === state.composerMode);
    label.textContent = current?.label ?? '智能体';
  }
  if (btn) {
    btn.title = title;
  }
  const activeEntry = activeComposerEntry();
  const selected = activeEntry !== 'craft';
  selection?.classList.toggle('is-selected', selected);
  if (clear) {
    clear.hidden = !selected || activeEntry === 'external';
    clear.title = `清除${label.textContent || '对话模式'}`;
    clear.setAttribute('aria-label', clear.title);
  }
}

function activeComposerEntry(): ComposerEntry {
  const kind = getSessionAgentDisplay(state.activeSessionId)?.agentBinding?.kind;
  if (externalAgentsEnabled() && (kind === 'external_agent' || kind === 'external_team')) {
    return 'external';
  }
  return state.composerMode;
}

function mountSelectPopover(
  anchor: HTMLElement,
  popover: HTMLElement,
  width = 300,
  align: 'start' | 'end' = 'start',
  placement: PopoverPlacement = 'auto',
  maxHeight?: number,
): void {
  popover.classList.add('composer-floating-popover', 'composer-select-popover');
  mountFloatingPopover(anchor, popover, width, align, placement, maxHeight);
}

function selectChevron(): string {
  return `<svg class="composer-select-item__check" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>`;
}

function renderComposerModeSwitch(activeMode: ComposerEntry): string {
  return `
    <div class="composer-mode-switch" role="tablist" aria-label="对话模式">
      ${visibleCraftOptions().map((o) => `
        <button type="button" class="composer-mode-switch__item${o.value === activeMode ? ' is-active' : ''}" data-expert-mode-switch="${o.value}" role="tab" aria-selected="${o.value === activeMode ? 'true' : 'false'}">
          ${escapeHtml(o.label)}
        </button>
      `).join('')}
    </div>
  `;
}

function spriteIcon(id: string, className?: string): string {
  const viewBox = id === 'skill-badge' ? '0 0 32 32' : '0 0 24 24';
  const classes = ['mw-icon', className].filter(Boolean).join(' ');
  return `<svg class="${classes}" viewBox="${viewBox}" width="18" height="18" aria-hidden="true"><use href="#${id}"></use></svg>`;
}

const WORKSPACE_FOLDER_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`;

function renderCraftPopover(): void {
  closeAllPopovers();
  const anchor = $('#chat-craft-btn') as HTMLElement | null;
  if (!anchor) return;

  const popover = document.createElement('div');
  popover.id = 'chat-craft-popover';
  const craftIcon = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>`;
  const planIcon = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>`;
  const askIcon = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  const externalIcon = spriteIcon('icon-agent', MONOCHROME_ICON_CLASS);
  const modeIcons: Record<ComposerEntry, string> = {
    craft: craftIcon,
    plan: planIcon,
    ask: askIcon,
    external: externalIcon,
  };
  const activeEntry = activeComposerEntry();

  popover.innerHTML = `
    <div class="composer-select-popover__list">
      ${visibleCraftOptions().map(
        (o) => `
        <button type="button" class="composer-select-item${o.value === activeEntry ? ' is-selected' : ''}" data-craft-mode="${o.value}">
          <span class="composer-select-item__icon">${modeIcons[o.value]}</span>
          <span class="composer-select-item__body">
            <span class="composer-select-item__title">${escapeHtml(o.label)}</span>
            <span class="composer-select-item__desc">${escapeHtml(o.desc)}</span>
          </span>
          ${o.value === activeEntry
            ? selectChevron()
            : '<span class="composer-select-item__spacer"></span>'}
        </button>
      `,
      ).join('')}
    </div>
  `;

  mountSelectPopover(anchor, popover, 280);
  craftPopoverOpen = true;
  anchor.classList.add('is-open');

  popover.querySelectorAll<HTMLElement>('[data-craft-mode]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      const m = btn.getAttribute('data-craft-mode') as ComposerEntry;
      if (!m) {
        closeCraftPopover();
        return;
      }
      if (m === 'external') {
        e.stopPropagation();
        if (!externalAgentsEnabled()) {
          notify(EXTERNAL_AGENTS_DISABLED_MESSAGE);
          return;
        }
        closeCraftPopover();
        void renderExternalPopover(anchor as HTMLElement);
        return;
      }
      applyComposerMode(m);
      notify(m === 'plan' ? 'Plan：下条消息先出方案' : 'Craft：单 Agent 创作');
      closeCraftPopover();
    });
  });
}

function renderModelPopover(): void {
  closeAllPopovers();
  const anchor = $('#chat-model-picker-inline-btn') as HTMLButtonElement | null;
  if (!anchor || anchor.disabled || !state.config) return;

  const models = composerModelOptions();
  const active = activeComposerModelId();
  const popover = document.createElement('div');
  popover.id = 'chat-model-inline-popover';
  popover.setAttribute('role', 'listbox');
  popover.setAttribute('aria-label', '选择模型');
  const modelTourHelp = `
    <div class="composer-select-popover__header">
      <span>选择模型</span>
      <button type="button" class="composer-select-popover__help" data-model-tour-open aria-label="打开模型配置引导" title="打开模型配置引导">?</button>
    </div>`;
  popover.innerHTML = models.length
    ? `
      ${modelTourHelp}
      <div class="composer-select-popover__section">可用模型</div>
      <div class="composer-select-popover__list">
        ${models
          .map(
            (m) => `
          <button type="button" class="composer-select-item composer-select-item--model${m.id === active ? ' is-selected' : ''}" data-model-id="${escapeHtml(m.id)}" title="${escapeHtml(m.description)}" aria-label="${escapeHtml(`${m.label}${m.description ? `，${m.description}` : ''}`)}"${m.selectable ? '' : ' disabled'}>
            <span class="composer-select-item__body composer-select-item__body--model">
              <span class="composer-select-item__title">${escapeHtml(m.label)}${m.default ? ' · 默认' : ''}</span>
              ${m.warning ? `<span class="composer-select-item__meta composer-select-item__meta--warn">${escapeHtml(m.description)}</span>` : ''}
            </span>
            ${m.id === active ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
          </button>
        `,
          )
          .join('')}
      </div>
    `
    : `${modelTourHelp}<div class="composer-select-popover__empty">暂无模型，请前往配置页</div>`;

  mountSelectPopover(anchor, popover, 300, 'end');
  modelPopoverOpen = true;
  anchor.classList.add('is-open');

  popover.querySelector<HTMLButtonElement>('[data-model-tour-open]')?.addEventListener('click', (event) => {
    event.stopPropagation();
    closeModelPopover();
    startModelTour();
  });

  $$('.composer-select-item[data-model-id]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-model-id');
      if (id) void setSessionModel(id);
      closeModelPopover();
    });
  });
}

async function renderSkillsPopover(): Promise<void> {
  closeAllPopovers();
  const anchor = $('#chat-skills-btn') as HTMLElement | null;
  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');
  if (!anchor) return;

  const popover = document.createElement('div');
  popover.id = 'chat-skills-inline-popover';
  popover.innerHTML = '<div class="composer-select-popover__empty">正在读取技能…</div>';
  mountSelectPopover(anchor, popover, 340, 'start', 'auto', 340);
  skillsPopoverOpen = true;
  anchor.classList.add('is-open');

  const skillsCache = await getSkills().catch(() => [] as Skill[]);
  if (!skillsPopoverOpen || !popover.isConnected) return;
  popover.innerHTML = skillsCache.length
    ? `
      <div class="chat-popover-search composer-select-popover__search">
        <input type="search" class="chat-popover-search-input" placeholder="搜索技能" id="chat-skills-search-input" autocomplete="off" />
      </div>
      <div class="composer-select-popover__list" id="chat-skills-list">
        ${skillsCache
          .map(
            (s, i) => `
        <button type="button" class="composer-select-item" data-skill-slug="${escapeHtml(s.slug)}" data-skill-filterable>
          <span class="composer-select-item__icon composer-select-item__icon--skill composer-select-item__icon--tone-${i % 5}">/${escapeHtml(s.slug.slice(0, 1))}</span>
          <span class="composer-select-item__body">
            <span class="composer-select-item__title">${escapeHtml(s.display_name || s.name || s.slug)}</span>
            <span class="composer-select-item__desc">${escapeHtml(s.description_zh || s.description || '')}</span>
          </span>
          <span class="composer-select-item__spacer"></span>
        </button>
      `,
          )
          .join('')}
      </div>
    `
    : '<div class="composer-select-popover__empty">暂无 Skills</div>';
  scheduleFloatingPopoverPosition(anchor, popover, 340, 'start', 'auto', 340);

  const searchInput = popover.querySelector<HTMLInputElement>('#chat-skills-search-input');
  if (searchInput) {
    searchInput.focus();
    searchInput.addEventListener('input', () => {
      const q = searchInput.value.trim().toLowerCase();
      popover.querySelectorAll<HTMLElement>('[data-skill-filterable]').forEach((btn) => {
        const slug = btn.getAttribute('data-skill-slug') || '';
        const title = btn.querySelector('.composer-select-item__title')?.textContent || '';
        const desc = btn.querySelector('.composer-select-item__desc')?.textContent || '';
        const match = slug.toLowerCase().includes(q) || title.toLowerCase().includes(q) || desc.toLowerCase().includes(q);
        btn.hidden = !match;
      });
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSkillsPopover();
    });
  }

  popover.querySelectorAll<HTMLElement>('.composer-select-item[data-skill-slug]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const slug = btn.getAttribute('data-skill-slug');
      if (!slug || !input) return;
      const prefix = `/${slug} `;
      input.value = input.value.trim() ? `${prefix}${input.value}` : prefix;
      input.dispatchEvent(new Event('input'));
      input.focus();
      closeSkillsPopover();
    });
  });
}

function syncComposerModelChip(): void {
  const label = document.getElementById('chat-model-picker-inline-label');
  if (label) label.textContent = resolveComposerModelLabel() || '模型';
}

/** 安全模式 chip 标签：据当前生效模式（会话绑定优先，否则新对话预设）渲染。 */
function syncSecurityModeChip(): void {
  const label = document.getElementById('chat-security-mode-btn-label');
  if (label) label.textContent = modeLabel(currentSecurityMode());
}

function renderSecurityModePopover(): void {
  closeAllPopovers();
  const anchor = $('#chat-security-mode-btn');
  if (!anchor) return;
  const active = currentSecurityMode();
  const popover = document.createElement('div');
  popover.id = 'chat-security-mode-inline-popover';
  popover.innerHTML = `
    <div class="composer-select-popover__section composer-select-popover__section--title">Crew 应如何批准操作？</div>
    <div class="composer-select-popover__list">
      ${SECURITY_MODE_OPTIONS.map((o) => `
        <button type="button" class="composer-select-item composer-select-item--security${o.value === active ? ' is-selected' : ''}" data-sec-mode="${o.value}">
          <span class="composer-select-item__plain-icon">${spriteIcon(o.value === 'full_access' ? 'icon-warning' : 'icon-security')}</span>
          <span class="composer-select-item__body">
            <span class="composer-select-item__title">${escapeHtml(o.label)}</span>
            <span class="composer-select-item__desc">${escapeHtml(o.desc)}</span>
          </span>
          ${o.value === active ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
        </button>
      `).join('')}
    </div>
  `;
  mountSelectPopover(anchor, popover, 300);
  securityModePopoverOpen = true;
  anchor.classList.add('is-open');
  anchor.setAttribute('aria-expanded', 'true');

  $$('.composer-select-item[data-sec-mode]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const m = btn.getAttribute('data-sec-mode') as ConversationSecurityMode | null;
      if (!m) {
        closeSecurityModeInline();
        return;
      }
      // full_access 仍走二次确认；只有 Gateway ACK 后才提交本地 chip 状态。
      const accepted = await selectNextConversationMode(m, () => showConfirmDialog({
        title: '启用完全访问权限？',
        message: FULL_ACCESS_CONFIRMATION,
        confirmText: '仅对此对话完全放行',
      }));
      if (accepted) syncSecurityModeChip();
      closeSecurityModeInline();
    });
  });
}

function externalRuntimeReady(runtime: ExternalRuntime | undefined): boolean {
  if (!runtime) return false;
  const metadataStatus = typeof runtime.metadata?.availability_status === 'string'
    ? runtime.metadata.availability_status
    : '';
  const status = runtime.availability_status || metadataStatus;
  if (status) return status === 'ready';
  return runtime.available === true || runtime.healthy === true;
}

function externalAgentReady(
  agent: ExternalAgent,
  runtimes: ExternalRuntime[],
): boolean {
  const runtime = runtimes.find((item) => item.id === agent.runtime_id);
  return externalRuntimeReady(runtime) && !['disabled', 'unavailable'].includes(
    String(agent.status || '').trim().toLowerCase(),
  );
}

function externalTeamReady(
  team: ExternalTeam,
  catalog: ExternalConversationCatalog,
): boolean {
  const participantIds = new Set([
    String(team.leader_agent_id || '').trim(),
    ...(team.members || []).map((member) => String(member.agent_id || '').trim()),
  ]);
  participantIds.delete('');
  participantIds.delete('crew::builtin');
  return Array.from(participantIds).every((id) => {
    const agent = catalog.agents.find((item) => item.id === id);
    return Boolean(agent && externalAgentReady(agent, catalog.runtimes));
  });
}

function externalTeamLeaderLabel(
  team: ExternalTeam,
  catalog: ExternalConversationCatalog,
): string {
  const leaderId = String(team.leader_agent_id || '').trim();
  if (!leaderId) return '';
  const leaderName = leaderId === 'crew::builtin'
    ? 'Crew'
    : catalog.agents.find((agent) => agent.id === leaderId)?.name
      || team.members?.find((member) => member.agent_id === leaderId)?.agent_name
      || '';
  if (!leaderName) return '';
  return /\bleader$/i.test(leaderName) ? leaderName : `${leaderName} Leader`;
}

function externalAgentTone(value: string): number {
  let hash = 0;
  for (const char of value) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0;
  return Math.abs(hash) % 6;
}

function bindExternalModeSwitch(
  popover: HTMLElement,
  anchor: HTMLElement,
): void {
  popover.querySelectorAll<HTMLElement>('[data-expert-mode-switch]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const mode = btn.getAttribute('data-expert-mode-switch') as ComposerEntry | null;
      if (!mode || mode === 'external') {
        if (!externalAgentsEnabled()) notify(EXTERNAL_AGENTS_DISABLED_MESSAGE);
        return;
      }
      closeExternalPopover();
      applyComposerMode(mode);
      notify(mode === 'plan' ? 'Plan：下条消息先出方案' : 'Craft：单 Agent 创作');
    });
  });
}

async function renderExternalPopover(anchor?: HTMLElement | null): Promise<void> {
  if (!externalAgentsEnabled()) {
    notify(EXTERNAL_AGENTS_DISABLED_MESSAGE);
    return;
  }
  closeAllPopovers();
  const resolvedAnchor = (anchor || $('#chat-craft-btn')) as HTMLElement | null;
  if (!resolvedAnchor) return;

  const popover = document.createElement('div');
  popover.id = 'chat-external-inline-popover';
  popover.innerHTML = `
    ${renderComposerModeSwitch('external')}
    <div class="composer-select-popover__empty">正在加载外援…</div>
  `;
  mountSelectPopover(resolvedAnchor, popover, 340);
  externalPopoverOpen = true;
  resolvedAnchor.classList.add('is-open');
  resolvedAnchor.setAttribute('aria-expanded', 'true');
  bindExternalModeSwitch(popover, resolvedAnchor);

  let catalog: ExternalConversationCatalog;
  try {
    catalog = await loadExternalConversationCatalog();
  } catch (error) {
    if (!externalPopoverOpen || !popover.isConnected) return;
    popover.innerHTML = `
      ${renderComposerModeSwitch('external')}
      <div class="composer-select-popover__empty">加载外援失败：${escapeHtml((error as Error).message)}</div>
    `;
    bindExternalModeSwitch(popover, resolvedAnchor);
    scheduleFloatingPopoverPosition(resolvedAnchor, popover, 340);
    return;
  }
  if (!externalPopoverOpen || !popover.isConnected) return;
  if (!externalAgentsEnabled()) {
    closeExternalPopover();
    notify(EXTERNAL_AGENTS_DISABLED_MESSAGE);
    return;
  }

  const agentRows = catalog.agents.map((agent) => {
    const ready = externalAgentReady(agent, catalog.runtimes);
    const description = ready
      ? `${agent.provider || 'Agent'} · ${agent.model || '默认模型'}`
      : '暂时不可用，请到外援页面再找找';
    return `
      <button type="button" class="composer-select-item${ready ? '' : ' is-unavailable'}" data-external-agent-id="${escapeHtml(agent.id)}" data-external-filterable aria-disabled="${ready ? 'false' : 'true'}">
        <span class="composer-agent-badge composer-agent-pixel-icon composer-agent-pixel-icon--tone-${externalAgentTone(agent.id || agent.name)}" aria-hidden="true">${escapeHtml(agent.display_badge || '?')}</span>
        <span class="composer-select-item__body">
          <span class="composer-select-item__title">${escapeHtml(agent.name || '未命名外援')}</span>
          <span class="composer-select-item__desc${ready ? '' : ' composer-select-item__desc--warn'}">${escapeHtml(description)}</span>
        </span>
        <span class="composer-select-item__spacer"></span>
      </button>
    `;
  }).join('');

  const teamRows = catalog.teams.map((team) => {
    const ready = externalTeamReady(team, catalog);
    const memberCount = Array.isArray(team.members) ? team.members.length : 0;
    const leaderLabel = externalTeamLeaderLabel(team, catalog);
    const description = ready
      ? `${memberCount} 名成员${leaderLabel ? ` · ${leaderLabel}` : ''}`
      : '团队成员的运行时不可用';
    return `
      <button type="button" class="composer-select-item${ready ? '' : ' is-unavailable'}" data-external-team-id="${escapeHtml(team.id)}" data-external-filterable aria-disabled="${ready ? 'false' : 'true'}">
        <span class="composer-agent-team-logo" aria-hidden="true"><span class="session__team-logo"><i></i><i></i></span></span>
        <span class="composer-select-item__body">
          <span class="composer-select-item__title">${escapeHtml(team.name || '未命名团队')}</span>
          <span class="composer-select-item__desc${ready ? '' : ' composer-select-item__desc--warn'}">${escapeHtml(description)}</span>
        </span>
        <span class="composer-select-item__spacer"></span>
      </button>
    `;
  }).join('');

  const hasCatalog = Boolean(agentRows || teamRows);
  popover.innerHTML = `
    ${renderComposerModeSwitch('external')}
    ${hasCatalog ? `
      <div class="chat-popover-search composer-select-popover__search">
        <input type="search" class="chat-popover-search-input" placeholder="搜索外援" id="chat-external-search-input" autocomplete="off" />
      </div>
      <div class="composer-select-popover__list" id="chat-external-list">
        ${agentRows ? `<div class="composer-select-popover__section" data-external-section="agents">外援</div>${agentRows}` : ''}
        ${teamRows ? `<div class="composer-select-popover__section" data-external-section="teams">外援团队</div>${teamRows}` : ''}
      </div>
    ` : `
      <div class="composer-select-popover__empty">
        <div>暂无可用外援</div>
        <button type="button" class="composer-empty-action" data-open-external-management>前往“外援”页面添加</button>
      </div>
    `}
  `;
  bindExternalModeSwitch(popover, resolvedAnchor);
  // 初次定位发生在“正在加载”短内容阶段；目录渲染后高度改变，必须重新贴合锚点。
  scheduleFloatingPopoverPosition(resolvedAnchor, popover, 340);

  const searchInput = popover.querySelector<HTMLInputElement>('#chat-external-search-input');
  searchInput?.addEventListener('input', () => {
    const query = searchInput.value.trim().toLowerCase();
    popover.querySelectorAll<HTMLElement>('[data-external-filterable]').forEach((row) => {
      const name = row.querySelector('.composer-select-item__title')?.textContent || '';
      const description = row.querySelector('.composer-select-item__desc')?.textContent || '';
      row.hidden = Boolean(query
        && !name.toLowerCase().includes(query)
        && !description.toLowerCase().includes(query));
    });
    popover.querySelectorAll<HTMLElement>('[data-external-section]').forEach((section) => {
      let next = section.nextElementSibling as HTMLElement | null;
      let visible = false;
      while (next && !next.hasAttribute('data-external-section')) {
        if (next.hasAttribute('data-external-filterable') && !next.hidden) {
          visible = true;
          break;
        }
        next = next.nextElementSibling as HTMLElement | null;
      }
      section.hidden = !visible;
    });
  });
  searchInput?.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeExternalPopover();
  });
  requestAnimationFrame(() => searchInput?.focus());

  popover.querySelectorAll<HTMLElement>('[data-external-agent-id]').forEach((row) => {
    row.addEventListener('click', () => {
      const agent = catalog.agents.find((item) => item.id === row.dataset.externalAgentId);
      if (!agent) return;
      if (!externalAgentReady(agent, catalog.runtimes)) {
        notify('这位外援暂时不可用，请前往“外援”页面再找找。');
        return;
      }
      closeExternalPopover();
      void useAgent(agent);
    });
  });
  popover.querySelectorAll<HTMLElement>('[data-external-team-id]').forEach((row) => {
    row.addEventListener('click', () => {
      const team = catalog.teams.find((item) => item.id === row.dataset.externalTeamId);
      if (!team) return;
      if (!externalTeamReady(team, catalog)) {
        notify('该外援团队包含不可用成员，请先检查成员运行时。');
        return;
      }
      closeExternalPopover();
      void useTeam(team);
    });
  });
  popover.querySelector<HTMLElement>('[data-open-external-management]')?.addEventListener('click', () => {
    closeExternalPopover();
    document.querySelector<HTMLElement>('[data-tab="agents"]')?.click();
  });
}

export function syncComposerModelLabel(): void {
  syncModelUi();
  syncComposerModelChip();
}

export function bindComposerToolbar(): () => void {
  const trigger = $('#chat-craft-btn');
  if (toolbarController && toolbarBoundTrigger === trigger && trigger?.isConnected) return () => {};
  if (toolbarController) {
    toolbarController.abort();
    toolbarController = null;
    closeAllPopovers();
  }
  toolbarController = new AbortController();
  toolbarBoundTrigger = trigger;
  const { signal } = toolbarController;

  $('#chat-craft-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (craftPopoverOpen) closeAllPopovers();
    else renderCraftPopover();
  }, { signal });

  $('#chat-craft-clear')?.addEventListener('click', (e) => {
    e.stopPropagation();
    closeAllPopovers();
    applyComposerMode('craft');
    notify('已切回默认主智能体');
  }, { signal });

  $('#chat-model-picker-inline-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (modelPopoverOpen) closeModelPopover();
    else renderModelPopover();
  }, { signal });

  $('#chat-security-mode-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (securityModePopoverOpen) closeSecurityModeInline();
    else renderSecurityModePopover();
  }, { signal });

  $('#chat-skills-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (skillsPopoverOpen) closeSkillsPopover();
    else void renderSkillsPopover();
  }, { signal });

  $('#chat-workspace-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (workspacePopoverOpen) {
      closeWorkspacePopover();
      return;
    }
    // 避免同一次 click 冒泡到 document 监听器后立即被关掉
    window.setTimeout(() => {
      if (!signal.aborted) renderWorkspacePopover();
    }, 0);
  }, { signal });

  document.addEventListener('click', (e) => {
    const t = e.target instanceof Element ? e.target : null;
    if (!t) {
      closeAllPopovers();
      return;
    }
    if (
      craftPopoverOpen
      && !t.closest('#chat-craft-popover')
      && !t.closest('#chat-craft-btn')
    ) closeCraftPopover();
    if (modelPopoverOpen && !t.closest('#chat-model-inline-popover') && !t.closest('#chat-model-picker-inline-btn')) closeModelPopover();
    if (securityModePopoverOpen && !t.closest('#chat-security-mode-inline-popover') && !t.closest('#chat-security-mode-btn')) closeSecurityModeInline();
    if (skillsPopoverOpen && !t.closest('#chat-skills-inline-popover') && !t.closest('#chat-skills-btn')) closeSkillsPopover();
    if (externalPopoverOpen && !t.closest('#chat-external-inline-popover') && !t.closest('#chat-craft-btn')) closeExternalPopover();
    if (workspacePopoverOpen && !t.closest('#chat-workspace-popover') && !t.closest('#chat-workspace-btn')) closeWorkspacePopover();
  }, { signal, capture: true });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllPopovers();
  }, { signal });

  syncComposerModelLabel();
  syncCraftLabel();
  syncComposerWorkspaceLabel();
  syncSecurityModeChip();
  window.addEventListener('craft:mode-change', () => syncCraftLabel(), { signal });
  window.addEventListener('security:mode-change', () => syncSecurityModeChip(), { signal });
  window.addEventListener('workspace:context-changed', () => syncComposerWorkspaceLabel(), { signal });
  window.addEventListener('session:changed', () => {
    syncComposerWorkspaceLabel();
    syncCraftLabel();
  }, { signal });
  window.addEventListener('session:agent-assigned', () => syncCraftLabel(), { signal });
  window.addEventListener('external-agents:config-change', () => {
    if (!externalAgentsEnabled()) {
      closeExternalPopover();
      closeCraftPopover();
    }
  }, { signal });
  window.addEventListener('messages:changed', () => syncComposerWorkspaceLabel(), { signal });
  window.addEventListener('session:model-picker-disabled', closeModelPopover, { signal });

  // skill 安装/卸载后关闭 skills 浮层，避免展示旧列表；下次打开会重新拉取。
  const unsubscribeSkills = onSkillsChange(() => {
    if (skillsPopoverOpen) closeSkillsPopover();
  });
  const unsubscribeSession = sessionStore.subscribe(() => {
    syncComposerWorkspaceLabel();
  });
  return () => {
    toolbarController?.abort();
    toolbarController = null;
    toolbarBoundTrigger = null;
    unsubscribeSkills();
    unsubscribeSession();
    closeAllPopovers();
  };
}

export { syncModelUi, syncComposerWorkspaceLabel };
