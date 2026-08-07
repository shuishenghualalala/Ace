/**
 * 会话管理弹窗：批量选择 / 全选 / 批量删除
 *
 * 列表按侧栏同样分为「项目（各工作空间）」与「对话（默认工作空间）」；
 * 各分组支持折叠与分组内全选。
 *
 * 状态：state.selectedSessions / state.manageMode
 * HTML：assets/index.html #session-manage-modal
 * CSS：assets/styles/ui-preview.css
 */

import { backendApi } from '../backend-client';
import { escapeHtml, notify, removeSelectedSession, removeSessionState, removeSubscribedSession, setActiveSessionId, setSelectedSession, setSelectedSessions, state } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import { sessionsForManageList } from './workspaces';
import { refreshAllSessions, type OpenSessionFn } from './workspaces';

let openSession: OpenSessionFn = async () => {};

export function setOpenSessionCallback(fn: OpenSessionFn): void {
  openSession = fn;
}

function getManageList(): HTMLElement | null {
  return document.getElementById('manage-history-list');
}

/** 是否在管理弹窗里显示归档会话。打开弹窗时默认 false（与主列表一致），
 *  勾选「显示归档」后拉取 include_archived=true 的列表，便于查看与单条恢复。 */
let showArchived = false;

/** 弹窗内各分组折叠状态（sectionId → collapsed）。 */
const manageCollapsedSections = new Set<string>();

const SECTION_CARET = `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 18 6-6-6-6"/></svg>`;

function formatManageTime(updatedAt: number): string {
  const diff = Date.now() - updatedAt;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}小时前`;
  return `${Math.floor(diff / 86_400_000)}天前`;
}

function manageSessionRowHtml(session: (typeof state.sessions)[0]): string {
  const isSelected = !!state.selectedSessions[session.id];
  const archivedClass = session.archived ? ' manage-history-item--archived' : '';
  const time = formatManageTime(session.updatedAt);
  const restoreBtn = session.archived
    ? `<button type="button" class="manage-history-restore" data-restore-session="${escapeHtml(session.id)}" title="取消归档，恢复到主列表">恢复</button>`
    : '';
  return `
    <label class="manage-history-item ${isSelected ? 'selected' : ''}${archivedClass}" data-session-id="${escapeHtml(session.id)}">
      <input type="checkbox" class="manage-history-checkbox" ${isSelected ? 'checked' : ''} />
      <div class="manage-history-copy" title="${escapeHtml(session.title)}">
        <div class="manage-history-title">${escapeHtml(session.title)}${session.archived ? '<span class="manage-history-archived-badge">已归档</span>' : ''}${session.pinned ? '<span class="manage-history-pinned-badge">置顶</span>' : ''}</div>
        <div class="manage-history-preview">${escapeHtml(session.preview)}</div>
      </div>
      <span class="manage-history-time">${escapeHtml(time)}</span>
      ${restoreBtn}
    </label>
  `;
}

function sectionSelectionState(sessions: (typeof state.sessions)[0][]): {
  allSelected: boolean;
  someSelected: boolean;
} {
  if (sessions.length === 0) return { allSelected: false, someSelected: false };
  let selected = 0;
  for (const s of sessions) {
    if (state.selectedSessions[s.id]) selected += 1;
  }
  return {
    allSelected: selected === sessions.length,
    someSelected: selected > 0 && selected < sessions.length,
  };
}

function manageSectionHtml(
  sectionId: string,
  label: string,
  sessions: (typeof state.sessions)[0][],
): string {
  if (sessions.length === 0) return '';
  const sorted = [...sessions].sort((a, b) => b.updatedAt - a.updatedAt);
  const collapsed = manageCollapsedSections.has(sectionId);
  const { allSelected } = sectionSelectionState(sorted);
  return `
    <section class="manage-history-section${collapsed ? ' is-collapsed' : ''}" data-manage-section="${escapeHtml(sectionId)}">
      <div class="manage-history-section-head">
        <button
          type="button"
          class="manage-section-toggle${collapsed ? ' collapsed' : ''}"
          data-manage-section-toggle="${escapeHtml(sectionId)}"
          aria-expanded="${collapsed ? 'false' : 'true'}"
          aria-label="${escapeHtml(collapsed ? `展开${label}` : `折叠${label}`)}"
        >
          <span class="manage-section-caret" aria-hidden="true">${SECTION_CARET}</span>
        </button>
        <label class="manage-section-select" title="全选本分组">
          <input
            type="checkbox"
            class="manage-section-checkbox"
            data-manage-section-select="${escapeHtml(sectionId)}"
            ${allSelected ? 'checked' : ''}
          />
          <span class="manage-history-section-label">${escapeHtml(label)}</span>
        </label>
        <span class="manage-section-count">${sorted.length}</span>
      </div>
      <div class="manage-history-section-items">
        ${sorted.map((s) => manageSessionRowHtml(s)).join('')}
      </div>
    </section>
  `;
}

function bindManageListEvents(list: HTMLElement): void {
  list.querySelectorAll<HTMLElement>('.manage-history-item').forEach((item) => {
    const sessionId = item.getAttribute('data-session-id') || '';
    const checkbox = item.querySelector<HTMLInputElement>('.manage-history-checkbox');
    item.addEventListener('click', (e) => {
      if ((e.target as HTMLElement).closest('[data-restore-session]')) return;
      if (e.target === checkbox) return;
      if (checkbox) checkbox.checked = !checkbox.checked;
      toggleSelection(sessionId);
    });
    checkbox?.addEventListener('change', () => toggleSelection(sessionId));
  });

  list.querySelectorAll<HTMLButtonElement>('[data-manage-section-toggle]').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const sectionId = btn.getAttribute('data-manage-section-toggle') || '';
      if (!sectionId) return;
      if (manageCollapsedSections.has(sectionId)) manageCollapsedSections.delete(sectionId);
      else manageCollapsedSections.add(sectionId);
      renderManageList();
    });
  });

  list.querySelectorAll<HTMLInputElement>('[data-manage-section-select]').forEach((checkbox) => {
    checkbox.addEventListener('change', (e) => {
      e.stopPropagation();
      const sectionId = checkbox.getAttribute('data-manage-section-select') || '';
      if (!sectionId) return;
      toggleSectionSelection(sectionId, checkbox.checked);
    });
  });

  list.querySelectorAll<HTMLButtonElement>('[data-restore-session]').forEach((btn) => {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const id = btn.getAttribute('data-restore-session') || '';
      if (!id) return;
      btn.disabled = true;
      try {
        await backendApi.archiveSession(id, false);
        await reloadManageList();
        await refreshAllSessions();
        notify('已恢复到主列表');
      } catch (err) {
        notify(`恢复失败：${(err as Error).message || '未知错误'}`);
        btn.disabled = false;
      }
    });
  });
}

function sessionsInSection(sectionId: string): (typeof state.sessions)[0][] {
  const sessions = sessionsForManageList();
  if (sectionId === 'default') {
    return sessions.filter((s) => s.workspaceId === 'default');
  }
  return sessions.filter((s) => s.workspaceId === sectionId);
}

function toggleSectionSelection(sectionId: string, selected: boolean): void {
  const ids = sessionsInSection(sectionId).map((s) => s.id);
  if (ids.length === 0) return;
  const next = { ...state.selectedSessions };
  for (const id of ids) {
    if (selected) next[id] = true;
    else delete next[id];
  }
  setSelectedSessions(next);
  updateManageUI();
}

/** 重新拉取列表（按 showArchived 决定是否含归档会话）并重渲染。 */
async function reloadManageList(): Promise<void> {
  try {
    const rows = await backendApi.sessions(undefined, { includeArchived: showArchived });
    state.backendSessions = rows;
    const { syncSessionsFromBackend } = await import('./workspaces');
    syncSessionsFromBackend(rows);
  } catch {
    /* keep local */
  }
  renderManageList();
}

function renderManageList(): void {
  const list = getManageList();
  if (!list) return;

  const sessions = sessionsForManageList();
  if (sessions.length === 0) {
    list.innerHTML = '<div class="manage-history-empty">暂无历史对话</div>';
    updateManageUI();
    return;
  }

  const workspaces = state.workspaces.length ? state.workspaces : [];
  const projects = workspaces.filter((ws) => ws.id !== 'default' && !ws.hidden);
  const projectSections = projects
    .map((ws) => manageSectionHtml(ws.id, ws.name, sessions.filter((s) => s.workspaceId === ws.id)))
    .filter(Boolean);
  const conversationSection = manageSectionHtml(
    'default',
    '对话',
    sessions.filter((s) => s.workspaceId === 'default'),
  );

  const sections = [...projectSections, conversationSection].filter(Boolean);
  list.innerHTML = sections.length
    ? sections.join('')
    : '<div class="manage-history-empty">暂无历史对话</div>';

  bindManageListEvents(list);
  updateManageUI();
}

function updateManageUI(): void {
  const deleteBtn = document.getElementById('manage-delete-selected') as HTMLButtonElement | null;
  const countEl = document.getElementById('manage-selected-count');
  const selectAllCheckbox = document.getElementById('manage-select-all') as HTMLInputElement | null;

  const total = sessionsForManageList().length;
  const selectedCount = Object.values(state.selectedSessions).filter(Boolean).length;

  if (countEl) countEl.textContent = String(selectedCount);
  if (deleteBtn) deleteBtn.disabled = selectedCount === 0;
  if (selectAllCheckbox) {
    selectAllCheckbox.checked = total > 0 && selectedCount === total;
    selectAllCheckbox.indeterminate = selectedCount > 0 && selectedCount < total;
  }

  document.querySelectorAll<HTMLElement>('.manage-history-item').forEach((item) => {
    const sid = item.getAttribute('data-session-id') || '';
    const isSelected = !!state.selectedSessions[sid];
    item.classList.toggle('selected', isSelected);
    const cb = item.querySelector<HTMLInputElement>('.manage-history-checkbox');
    if (cb && cb.checked !== isSelected) cb.checked = isSelected;
  });

  document.querySelectorAll<HTMLInputElement>('.manage-section-checkbox').forEach((cb) => {
    const sectionId = cb.getAttribute('data-manage-section-select') || '';
    if (!sectionId) return;
    const { allSelected, someSelected } = sectionSelectionState(sessionsInSection(sectionId));
    cb.checked = allSelected;
    cb.indeterminate = someSelected;
  });
}

export function toggleSelection(sessionId: string): void {
  if (!sessionId) return;
  if (state.selectedSessions[sessionId]) {
    removeSelectedSession(sessionId);
  } else {
    setSelectedSession(sessionId, true);
  }
  updateManageUI();
}

export function toggleSelectAll(): void {
  const total = sessionsForManageList().length;
  const selectedCount = Object.values(state.selectedSessions).filter(Boolean).length;
  if (total === 0) return;
  if (selectedCount === total) {
    setSelectedSessions({});
  } else {
    const all: Record<string, boolean> = {};
    sessionsForManageList().forEach((s) => {
      all[s.id] = true;
    });
    setSelectedSessions(all);
  }
  renderManageList();
}

export function openSessionManage(): void {
  state.manageMode = true;
  state.selectedSessions = {};
  manageCollapsedSections.clear();
  showArchived = false;
  const cb = document.getElementById('manage-show-archived') as HTMLInputElement | null;
  if (cb) cb.checked = false;
  const modal = document.getElementById('session-manage-modal');
  modal?.classList.add('show');
  if (modal) modal.hidden = false;
  void reloadManageList();
}

function closeSessionManage(): void {
  state.manageMode = false;
  state.selectedSessions = {};
  manageCollapsedSections.clear();
  showArchived = false;
  const cb = document.getElementById('manage-show-archived') as HTMLInputElement | null;
  if (cb) cb.checked = false;
  const modal = document.getElementById('session-manage-modal');
  modal?.classList.remove('show');
  if (modal) modal.hidden = true;
}

async function deleteSelectedSessions(): Promise<void> {
  const ids = Object.entries(state.selectedSessions)
    .filter(([, v]) => v)
    .map(([k]) => k);
  if (ids.length === 0) return;

  const confirmed = await showConfirmDialog({
    title: '批量删除历史对话',
    message: `确定要删除选中的 ${ids.length} 个历史对话吗？该操作不可撤销。`,
    confirmText: '批量删除',
    cancelText: '取消',
  });
  if (!confirmed) return;

  const results = await Promise.allSettled(ids.map((id) => backendApi.deleteSession(id)));
  const ok: string[] = [];
  const failed: Array<{ id: string; reason: string }> = [];
  results.forEach((r, i) => {
    if (r.status === 'fulfilled') {
      ok.push(ids[i]);
    } else {
      failed.push({ id: ids[i], reason: (r.reason as Error)?.message || '未知错误' });
    }
  });

  ok.forEach((id) => {
    removeSessionState(id);
    state.socket?.unsubscribe([id]);
    removeSubscribedSession(id);
    if (state.activeSessionId === id) {
      setActiveSessionId(null);
    }
  });

  await refreshAllSessions();

  const nextSelected: Record<string, boolean> = {};
  const okSet = new Set(ok);
  for (const [id, v] of Object.entries(state.selectedSessions)) {
    if (!okSet.has(id)) nextSelected[id] = v;
  }
  setSelectedSessions(nextSelected);

  renderManageList();
  void openSession(
    state.activeSessionId
      || state.sessions.find((session) => !session.archived)?.id
      || '',
  );

  if (failed.length === 0) {
    notify(`已删除 ${ok.length} 个会话`);
  } else {
    const failList = failed.map((f) => f.id).join('\n');
    notify(`删除完成：成功 ${ok.length}，失败 ${failed.length}（${failList}）`);
  }

  if (!(ok.length > 0 && ids.length === failed.length)) {
    closeSessionManage();
  }
}

export function bindSessionManageUi(): void {
  document.getElementById('session-manage-close')?.addEventListener('click', closeSessionManage);
  document.getElementById('session-manage-done')?.addEventListener('click', closeSessionManage);
  document.getElementById('session-manage-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeSessionManage();
  });

  document.getElementById('manage-select-all')?.addEventListener('change', toggleSelectAll);
  document.getElementById('manage-show-archived')?.addEventListener('change', async (e) => {
    showArchived = (e.target as HTMLInputElement).checked;
    setSelectedSessions({});
    await reloadManageList();
  });
  document.getElementById('manage-delete-selected')?.addEventListener('click', () => {
    void deleteSelectedSessions();
  });
}
