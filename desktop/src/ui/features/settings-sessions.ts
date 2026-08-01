/**
 * 设置 → 归档会话：列表、恢复/导出/删除，点击卡片预览历史。
 */

import { backendApi } from '../backend-client';
import { exportSessionToJson } from '../lib/session-export';
import { escapeHtml, notify, state } from '../state';
import { deleteSession, setArchived } from './session-actions';
import { openSessionPreviewModal } from './session-preview-modal';
import { syncLibrarySectionCounts } from './settings-library';
import { refreshAllSessions, syncSessionsFromBackend, loadWorkspaces } from './workspaces';

function sessionsRoot(): HTMLElement | null {
  return document.getElementById('settings-library-sessions') ?? document.getElementById('settings-sessions-root');
}

function formatArchivedTime(updatedAtMs: number): string {
  const diff = Date.now() - updatedAtMs;
  if (diff < 86_400_000) return `${Math.max(1, Math.floor(diff / 3_600_000))} 小时前`;
  if (diff < 30 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return new Date(updatedAtMs).toLocaleDateString();
}

function workspaceLabelForSession(workspaceId: string | undefined): string {
  if (!workspaceId || workspaceId === 'default') return '通用对话';
  return state.workspaces.find((w) => w.id === workspaceId)?.name || workspaceId;
}

/** 渲染设置页「归档会话」面板。 */
export async function renderSettingsSessionsPage(): Promise<void> {
  const root = sessionsRoot();
  if (!root) return;

  root.innerHTML = '<p class="set-v2-row__desc">加载中…</p>';

  try {
    await loadWorkspaces();
    const rows = await backendApi.sessions(undefined, { includeArchived: true });
    syncSessionsFromBackend(rows);
    const archived = rows.filter((r) => r.archived === true);

    if (!archived.length) {
      root.innerHTML =
        '<p class="library-empty-hint">暂无归档会话。侧栏会话行点击 ⋯ 可归档；归档后会从主列表隐藏。</p>';
      syncLibrarySectionCounts({ sessions: 0 });
      return;
    }

    root.innerHTML = archived
      .sort((a, b) => b.updated_at - a.updated_at)
      .map((s) => {
        const title = s.title?.trim() || '新会话';
        const ws = workspaceLabelForSession(s.workspace_id);
        const time = formatArchivedTime(s.updated_at * 1000);
        return `
          <div class="set-v2-row settings-session-row settings-session-row--clickable" data-session-id="${escapeHtml(s.session_id)}" tabindex="0" role="button" aria-label="预览会话 ${escapeHtml(title)}">
            <div class="set-v2-row__copy settings-session-row__main">
              <div class="set-v2-row__label">${escapeHtml(title)}</div>
              <div class="set-v2-row__desc">
                <span class="settings-session-ws-badge">${escapeHtml(ws)}</span>
                <span class="settings-session-meta-sep">·</span>
                <span>${escapeHtml(time)}</span>
                <span class="settings-session-preview-hint">点击查看对话</span>
              </div>
            </div>
            <div class="set-v2-row__control settings-session-actions">
              <button type="button" class="set-v2-btn" data-session-unarchive>恢复</button>
              <button type="button" class="set-v2-btn" data-session-export>导出</button>
              <button type="button" class="set-v2-btn set-v2-btn--danger" data-session-delete>删除</button>
            </div>
          </div>
        `;
      })
      .join('');

    syncLibrarySectionCounts({ sessions: archived.length });

    root.querySelectorAll('.settings-session-row').forEach((rowEl) => {
      const id = rowEl.getAttribute('data-session-id') || '';
      const title =
        archived.find((s) => s.session_id === id)?.title?.trim() || '新会话';

      const openPreview = (): void => {
        void openSessionPreviewModal(id, title);
      };

      rowEl.querySelector('.settings-session-row__main')?.addEventListener('click', openPreview);
      rowEl.addEventListener('keydown', ((e: KeyboardEvent) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          openPreview();
        }
      }) as EventListener);

      rowEl.querySelector('[data-session-unarchive]')?.addEventListener('click', (e) => {
        e.stopPropagation();
        void setArchived(id, false, refreshAllSessions).then(() => void renderSettingsSessionsPage());
      });
      rowEl.querySelector('[data-session-export]')?.addEventListener('click', (e) => {
        e.stopPropagation();
        void exportSessionToJson(id, title);
      });
      rowEl.querySelector('[data-session-delete]')?.addEventListener('click', (e) => {
        e.stopPropagation();
        void deleteSession(id, refreshAllSessions).then(() => void renderSettingsSessionsPage());
      });
    });
  } catch (err) {
    root.innerHTML = `<p class="set-v2-row__desc">加载失败：${escapeHtml((err as Error).message || '未知错误')}</p>`;
    notify('加载归档会话失败');
  }
}
