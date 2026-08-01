/**
 * 历史面板折叠 / 展开
 *
 * 折叠按钮固定在顶栏；缩进时在按钮右侧显示「新建对话」快捷入口。
 */

import { $, saveToStorage, state } from '../state';

const STORAGE_KEY = 'crew.historyCollapsed';

function syncCollapsedChrome(collapsed: boolean): void {
  const btn = $('#history-panel-toggle');
  if (btn) {
    btn.setAttribute('aria-expanded', String(!collapsed));
    btn.title = collapsed ? '展开历史对话' : '折叠历史对话';
    btn.setAttribute('aria-label', btn.title);
  }
  const quickNew = $('#history-collapsed-new-chat') as HTMLButtonElement | null;
  if (quickNew) quickNew.hidden = !collapsed;
}

function applyCollapsed(collapsed: boolean): void {
  const dock = $('#history-workspace-dock');
  const shell = $('#history-workspace-shell');
  shell?.classList.toggle('history-panel-collapsed', collapsed);
  dock?.classList.toggle('history-panel-collapsed', collapsed);
  dock?.classList.toggle('is-collapsed-rail', collapsed);
  document.body.classList.toggle('history-panel-collapsed', collapsed);
  syncCollapsedChrome(collapsed);
}

export function applyHistoryCollapsed(): void {
  const stored = localStorage.getItem(STORAGE_KEY) === 'true';
  state.historyCollapsed = stored;
  applyCollapsed(stored);
}

export function bindHistoryPanelToggle(): void {
  applyHistoryCollapsed();
  const btn = $('#history-panel-toggle');
  btn?.addEventListener('click', () => {
    state.historyCollapsed = !state.historyCollapsed;
    saveToStorage(STORAGE_KEY, state.historyCollapsed);
    applyCollapsed(state.historyCollapsed);
  });
  $('#history-collapsed-new-chat')?.addEventListener('click', () => {
    $('#new-chat-sidebar-btn')?.click();
  });
}
