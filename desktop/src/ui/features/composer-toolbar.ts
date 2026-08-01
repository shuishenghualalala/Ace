/**
 * Composer 工具栏：Craft/Plan/Ask 下拉 + 外援 + 模型/Skills 浮层（移开鼠标关闭）。
 */

import {
  type ExternalAgent,
  type ExternalRuntime,
  type ExternalTeam,
  type Skill,
} from '../backend-client';
import { getSkills, onSkillsChange } from './skill-store';
import { $, $$, ensureSessionBook, escapeHtml, notify, patchBook, state, type ComposerMode } from '../state';
import {
  canSwitchComposerWorkspace,
  composerWorkspaceId,
  createWorkspaceFromFolderPicker,
  ensureComposerDraftSession,
  getSessionAgentDisplay,
  setComposerTargetWorkspace,
  visibleProjectWorkspaces,
  workspaceLabel,
} from './workspaces';
import { composerModelOptions, setSessionModel, activeComposerModelId, resolveComposerModelLabel } from './session-model';
import { syncModelUi } from './model-picker';
import { startModelTour } from './model-tour';
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

type ComposerEntry = ComposerMode | 'external';

const CRAFT_OPTIONS: { value: ComposerEntry; label: string; desc: string }[] = [
  { value: 'craft', label: '智能体', desc: '默认单 Agent 创作' },
  { value: 'plan', label: '计划模式', desc: '先出方案 · 审批后再执行' },
  { value: 'external', label: '外援', desc: '选择已接入的智能体或团队' },
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
function mountFloatingPopover(
  anchor: HTMLElement,
  popover: HTMLElement,
  width = 300,
  align: 'start' | 'end' = 'start',
): void {
  popover.classList.add('composer-floating-popover');
  const availableWidth = Math.max(0, window.innerWidth - 16);
  const actualWidth = Math.min(width, availableWidth);
  popover.style.width = `${actualWidth}px`;
  document.body.appendChild(popover);
  scheduleFloatingPopoverPosition(anchor, popover, actualWidth, align);
}

function scheduleFloatingPopoverPosition(
  anchor: HTMLElement,
  popover: HTMLElement,
  width: number,
  align: 'start' | 'end' = 'start',
): void {
  requestAnimationFrame(() => {
    if (!anchor.isConnected || !popover.isConnected) return;
    const rect = anchor.getBoundingClientRect();
    const popoverHeight = popover.offsetHeight || 180;
    const viewportBottom = Math.max(8, window.innerHeight - popoverHeight - 8);
    const openUp = rect.top > popoverHeight + 12;
    const desiredTop = openUp ? rect.top - popoverHeight - 6 : rect.bottom + 6;
    const top = Math.max(8, Math.min(desiredTop, viewportBottom));
    let left = align === 'end' ? rect.right - width : rect.left;
    left = Math.max(8, Math.min(left, window.innerWidth - width - 8));
    popover.style.left = `${left}px`;
    popover.style.top = `${top}px`;
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
  row.hidden = !show;
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
  label.textContent = isDefault ? '工作空间' : workspaceLabel(id);
  btn.title = isDefault ? '选择工作空间' : `工作空间：${label.textContent}`;
  btn.classList.toggle('is-named', !isDefault);
  btn.classList.remove('is-locked');
}

function filterWorkspacePopoverList(popover: HTMLElement, query: string): void {
  const q = query.trim().toLowerCase();
  const section = popover.querySelector<HTMLElement>('[data-workspace-section="projects"]');
  const openRow = popover.querySelector<HTMLElement>('[data-workspace-open-local]');
  if (section) section.hidden = Boolean(q);
  if (openRow) openRow.style.display = q ? 'none' : '';
  popover.querySelectorAll<HTMLElement>('[data-workspace-filterable]').forEach((row) => {
    if (row.hasAttribute('data-workspace-open-local')) return;
    const name = row.querySelector('.composer-select-item__title')?.textContent?.toLowerCase() ?? '';
    const path = row.querySelector('.composer-select-item__desc')?.textContent?.toLowerCase() ?? '';
    row.style.display = !q || name.includes(q) || path.includes(q) ? '' : 'none';
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
      <input type="search" class="chat-popover-search-input" placeholder="搜索工作空间" id="chat-workspace-search-input" autocomplete="off" />
    </div>
    <button type="button" class="composer-select-item composer-select-item--nav" data-workspace-open-local data-workspace-filterable>
      <span class="composer-select-item__icon composer-select-item__icon--tone-4">${WORKSPACE_OPEN_ICON}</span>
      <span class="composer-select-item__body">
        <span class="composer-select-item__title">打开本地工作空间</span>
        <span class="composer-select-item__desc">选择文件夹并绑定为项目</span>
      </span>
      <span class="composer-select-item__arrow" aria-hidden="true">›</span>
    </button>
    <div class="composer-select-popover__divider" role="separator"></div>
    <div class="composer-select-popover__section" data-workspace-section="projects">我的工作空间</div>
    <div class="composer-select-popover__list" role="listbox" aria-label="工作空间">
      <button type="button" class="composer-select-item${current === 'default' ? ' is-selected' : ''}" data-workspace-id="default" data-workspace-filterable>
        <span class="composer-select-item__icon composer-select-item__icon--tone-0">${WORKSPACE_GLOBE_ICON}</span>
        <span class="composer-select-item__body">
          <span class="composer-select-item__title">从新工作空间开始</span>
          <span class="composer-select-item__desc">不绑定项目目录的通用对话</span>
        </span>
        ${current === 'default' ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
      </button>
      ${projects
        .map((ws) => {
          const sub = ws.root_path?.trim() ?? '';
          const selected = ws.id === current;
          const tone = workspaceToneIndex(ws.id);
          return `
        <button type="button" class="composer-select-item${selected ? ' is-selected' : ''}" data-workspace-id="${escapeHtml(ws.id)}" data-workspace-filterable>
          <span class="composer-select-item__icon composer-select-item__icon--tone-${tone}">${WORKSPACE_FOLDER_ICON}</span>
          <span class="composer-select-item__body">
            <span class="composer-select-item__title">${escapeHtml(ws.name)}</span>
            ${sub ? `<span class="composer-select-item__desc">${escapeHtml(sub)}</span>` : '<span class="composer-select-item__desc">本地项目工作空间</span>'}
          </span>
          ${selected ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
        </button>
      `;
        })
        .join('')}
      ${projects.length === 0 ? '<div class="composer-select-popover__empty">暂无项目，可打开本地文件夹创建</div>' : ''}
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
  if (!label) return;
  const externalDisplay = getSessionAgentDisplay(state.activeSessionId);
  const externalKind = externalDisplay?.agentBinding?.kind;
  const externalName = String(externalDisplay?.agentLabel?.name || '').trim();
  let title = 'Craft · Plan · Ask';
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
): void {
  popover.classList.add('composer-floating-popover', 'composer-select-popover');
  mountFloatingPopover(anchor, popover, width, align);
}

function selectChevron(): string {
  return `<svg class="composer-select-item__check" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 6 9 17l-5-5"/></svg>`;
}

function renderComposerModeSwitch(activeMode: ComposerEntry): string {
  return `
    <div class="composer-mode-switch" role="tablist" aria-label="对话模式">
      ${visibleCraftOptions().map((o) => `
        <button type="button" class="composer-mode-switch__item${o.value === activeMode ? ' is-active' : ''}" data-composer-mode-switch="${o.value}" role="tab" aria-selected="${o.value === activeMode ? 'true' : 'false'}">
          ${escapeHtml(o.label)}
        </button>
      `).join('')}
    </div>
  `;
}

const WORKSPACE_FOLDER_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`;
const WORKSPACE_GLOBE_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`;
const WORKSPACE_OPEN_ICON = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>`;

function workspaceToneIndex(id: string): number {
  let hash = 0;
  for (let i = 0; i < id.length; i += 1) hash = (hash + id.charCodeAt(i) * (i + 1)) % 5;
  return hash;
}

function renderCraftPopover(): void {
  closeAllPopovers();
  const anchor = $('#chat-craft-btn');
  if (!anchor) return;

  const popover = document.createElement('div');
  popover.id = 'chat-craft-popover';
  const craftIcon = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3Z"/></svg>`;
  const planIcon = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1"/></svg>`;
  const askIcon = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  const externalIcon = `<svg class="composer-external-agent-logo" viewBox="3 3 18 18" aria-hidden="true"><path d="M5.2 13.2c0-4.5 2.9-6.9 6.8-6.9 4.5 0 7 2.8 7 6.2 0 3.8-2.5 5.5-7.2 5.5-4.3 0-6.6-1.4-6.6-4.8Z"></path><path d="M9 6.7c.7-1.1 1.7-1.7 3.1-1.7 1.3 0 2.3.5 3 1.5"></path><path d="M9.6 10.8v1.9"></path><path d="M14.4 10.8v1.9"></path><path d="M18.8 8.2h1.5M19.55 7.45v1.5"></path></svg>`;
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
  const header = `
    <div class="composer-model-popover__header">
      <div class="composer-select-popover__section">可用模型</div>
      <button
        type="button"
        class="composer-model-popover__help"
        data-model-tour-open
        title="模型配置引导"
        aria-label="打开模型配置引导"
      >?</button>
    </div>`;
  popover.innerHTML = models.length
    ? `${header}
      <div class="composer-select-popover__list">
        ${models
          .map(
            (m) => `
          <button type="button" class="composer-select-item${m.id === active ? ' is-selected' : ''}" data-model-id="${escapeHtml(m.id)}"${m.selectable ? '' : ' disabled'}>
            <span class="composer-select-item__icon composer-select-item__icon--model">${escapeHtml((m.label || '?').slice(0, 1).toUpperCase())}</span>
            <span class="composer-select-item__body">
              <span class="composer-select-item__title">${escapeHtml(m.label)}${m.default ? ' · 默认' : ''}</span>
              <span class="composer-select-item__desc${m.warning ? ' composer-select-item__desc--warn' : ''}">${escapeHtml(m.description)}</span>
            </span>
            ${m.id === active ? selectChevron() : '<span class="composer-select-item__spacer"></span>'}
          </button>
        `,
          )
          .join('')}
      </div>
    `
    : `${header}<div class="composer-select-popover__empty">暂无模型，请前往配置页</div>`;

  mountSelectPopover(anchor, popover, 320);
  modelPopoverOpen = true;
  anchor.classList.add('is-open');

  popover.querySelector<HTMLElement>('[data-model-tour-open]')?.addEventListener('click', (event) => {
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
  const anchor = $('#chat-skills-btn');
  const input = $('#chat-input') as HTMLTextAreaElement | null;
  if (!anchor) return;

  const skillsCache = await getSkills().catch(() => [] as Skill[]);

  const popover = document.createElement('div');
  popover.id = 'chat-skills-inline-popover';
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

  mountSelectPopover(anchor, popover, 340);
  skillsPopoverOpen = true;
  anchor.classList.add('is-open');

  const searchInput = $('#chat-skills-search-input') as HTMLInputElement | null;
  if (searchInput) {
    searchInput.focus();
    searchInput.addEventListener('input', () => {
      const q = searchInput.value.trim().toLowerCase();
      $$('[data-skill-filterable]').forEach((btn) => {
        const slug = btn.getAttribute('data-skill-slug') || '';
        const title = btn.querySelector('.composer-select-item__title')?.textContent || '';
        const desc = btn.querySelector('.composer-select-item__desc')?.textContent || '';
        const match = slug.toLowerCase().includes(q) || title.toLowerCase().includes(q) || desc.toLowerCase().includes(q);
        (btn as HTMLElement).style.display = match ? 'flex' : 'none';
      });
    });
    searchInput.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closeSkillsPopover();
    });
  }

  $$('.composer-select-item[data-skill-slug]').forEach((btn) => {
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

function bindExternalModeSwitch(popover: HTMLElement): void {
  popover.querySelectorAll<HTMLElement>('[data-composer-mode-switch]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const mode = btn.getAttribute('data-composer-mode-switch') as ComposerEntry | null;
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
  bindExternalModeSwitch(popover);

  let catalog: ExternalConversationCatalog;
  try {
    catalog = await loadExternalConversationCatalog();
  } catch (error) {
    if (!externalPopoverOpen || !popover.isConnected) return;
    popover.innerHTML = `
      ${renderComposerModeSwitch('external')}
      <div class="composer-select-popover__empty">加载外援失败：${escapeHtml((error as Error).message)}</div>
    `;
    bindExternalModeSwitch(popover);
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
        <span class="composer-agent-pixel-icon composer-agent-pixel-icon--tone-${externalAgentTone(agent.id || agent.name)}" aria-hidden="true">${escapeHtml(agent.display_badge || '?')}</span>
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
  bindExternalModeSwitch(popover);
  // 初次定位发生在“正在加载”短内容阶段；目录渲染后高度改变，必须重新贴合锚点。
  scheduleFloatingPopoverPosition(resolvedAnchor, popover, 340);

  const searchInput = popover.querySelector<HTMLInputElement>('#chat-external-search-input');
  searchInput?.addEventListener('input', () => {
    const query = searchInput.value.trim().toLowerCase();
    popover.querySelectorAll<HTMLElement>('[data-external-filterable]').forEach((row) => {
      const name = row.querySelector('.composer-select-item__title')?.textContent || '';
      const description = row.querySelector('.composer-select-item__desc')?.textContent || '';
      row.style.display = !query
        || name.toLowerCase().includes(query)
        || description.toLowerCase().includes(query)
        ? ''
        : 'none';
    });
    popover.querySelectorAll<HTMLElement>('[data-external-section]').forEach((section) => {
      let next = section.nextElementSibling as HTMLElement | null;
      let visible = false;
      while (next && !next.hasAttribute('data-external-section')) {
        if (next.hasAttribute('data-external-filterable') && next.style.display !== 'none') {
          visible = true;
          break;
        }
        next = next.nextElementSibling as HTMLElement | null;
      }
      section.style.display = visible ? '' : 'none';
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

export function bindComposerToolbar(): void {
  $('#chat-craft-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (craftPopoverOpen) closeCraftPopover();
    else renderCraftPopover();
  });

  $('#chat-model-picker-inline-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (modelPopoverOpen) closeModelPopover();
    else renderModelPopover();
  });

  $('#chat-skills-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (skillsPopoverOpen) closeSkillsPopover();
    else void renderSkillsPopover();
  });

  $('#chat-workspace-btn')?.addEventListener('click', (e) => {
    e.stopPropagation();
    if (workspacePopoverOpen) {
      closeWorkspacePopover();
      return;
    }
    // 避免同一次 click 冒泡到 document 监听器后立即被关掉
    window.setTimeout(() => renderWorkspacePopover(), 0);
  });

  document.addEventListener('click', (e) => {
    const t = e.target as HTMLElement;
    if (craftPopoverOpen && !t.closest('#chat-craft-popover') && !t.closest('#chat-craft-btn')) closeCraftPopover();
    if (modelPopoverOpen && !t.closest('#chat-model-inline-popover') && !t.closest('#chat-model-picker-inline-btn')) closeModelPopover();
    if (skillsPopoverOpen && !t.closest('#chat-skills-inline-popover') && !t.closest('#chat-skills-btn')) closeSkillsPopover();
    if (externalPopoverOpen && !t.closest('#chat-external-inline-popover') && !t.closest('#chat-craft-btn')) closeExternalPopover();
    if (workspacePopoverOpen && !t.closest('#chat-workspace-popover') && !t.closest('#chat-workspace-btn')) closeWorkspacePopover();
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeAllPopovers();
  });

  syncComposerModelLabel();
  syncCraftLabel();
  syncComposerWorkspaceLabel();
  window.addEventListener('craft:mode-change', () => syncCraftLabel());
  window.addEventListener('workspace:context-changed', () => syncComposerWorkspaceLabel());
  window.addEventListener('session:changed', () => {
    syncComposerWorkspaceLabel();
    syncCraftLabel();
  });
  window.addEventListener('session:agent-assigned', () => syncCraftLabel());
  window.addEventListener('external-agents:config-change', () => {
    if (!externalAgentsEnabled()) {
      closeExternalPopover();
      closeCraftPopover();
    }
  });
  window.addEventListener('messages:changed', () => syncComposerWorkspaceLabel());
  window.addEventListener('session:model-picker-disabled', closeModelPopover);

  // skill 安装/卸载后关闭 skills 浮层，避免展示旧列表；下次打开会重新拉取。
  onSkillsChange(() => {
    if (skillsPopoverOpen) closeSkillsPopover();
  });
}

export { syncModelUi, syncComposerWorkspaceLabel };
