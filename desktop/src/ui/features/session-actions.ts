/**
 * 侧栏 / 设置页共用的会话操作：置顶、归档、重命名、导出、删除。
 */

import { backendApi } from '../backend-client';
import { showContextMenu } from '../lib/context-menu';
import { exportSessionToJson } from '../lib/session-export';
import { notify, removeSessionState, setActiveSessionId, state } from '../state';
import { showConfirmDialog } from '../ui-feedback';
import type { RefreshSessionsFn } from './workspaces';
import { renderChat } from './chat-controller';

async function refreshLibraryPaneIfOpen(): Promise<void> {
  const { refreshSettingsLibraryIfVisible } = await import('./settings-library');
  await refreshSettingsLibraryIfVisible();
}

export type SessionActionAnchor = HTMLElement;

/** 在锚点旁展示会话操作菜单（替代 window.prompt）。 */
export function openSessionActionsMenu(
  sessionId: string,
  anchor: SessionActionAnchor,
  refresh: RefreshSessionsFn,
): void {
  const row = state.sessions.find((s) => s.id === sessionId);
  const pinned = !!row?.pinned;
  const archived = !!row?.archived;
  const title = row?.title ?? '';

  showContextMenu(anchor, [
    {
      id: 'pin',
      label: pinned ? '取消置顶' : '置顶',
      onSelect: () => void togglePin(sessionId, !pinned, refresh),
    },
    {
      id: 'rename',
      label: '重命名',
      onSelect: () => void renameSession(sessionId, title, refresh),
    },
    {
      id: 'export',
      label: '导出会话',
      onSelect: () => void exportSessionToJson(sessionId, title),
    },
    {
      id: 'archive',
      label: archived ? '取消归档' : '归档',
      onSelect: () => void setArchived(sessionId, !archived, refresh),
    },
    {
      id: 'delete',
      label: '删除',
      danger: true,
      onSelect: () => void deleteSession(sessionId, refresh),
    },
  ]);
}

export async function togglePin(sessionId: string, pinned: boolean, refresh: RefreshSessionsFn): Promise<void> {
  try {
    await backendApi.pinSession(sessionId, pinned);
    await refresh();
    notify(pinned ? '已置顶' : '已取消置顶');
  } catch {
    notify(pinned ? '置顶失败' : '取消置顶失败');
  }
}

export async function renameSession(
  sessionId: string,
  currentTitle: string,
  refresh: RefreshSessionsFn,
): Promise<void> {
  const next = window.prompt('新标题', currentTitle);
  if (!next?.trim()) return;
  try {
    await backendApi.renameSession(sessionId, next.trim());
    await refresh();
    notify('已重命名');
  } catch {
    notify('重命名失败');
  }
}

export async function setArchived(
  sessionId: string,
  archived: boolean,
  refresh: RefreshSessionsFn,
): Promise<void> {
  if (archived) {
    const ok = await showConfirmDialog({
      title: '归档会话',
      message: '归档后将从侧栏主列表隐藏，可在「设置 → 项目与会话 → 归档会话」中查看、恢复或导出。',
      confirmText: '归档',
      cancelText: '取消',
    });
    if (!ok) return;
  }
  try {
    await backendApi.archiveSession(sessionId, archived);
    if (archived && state.activeSessionId === sessionId) {
      setActiveSessionId(null);
    }
    await refresh();
    await refreshLibraryPaneIfOpen();
    notify(archived ? '已归档' : '已恢复到主列表');
  } catch {
    notify(archived ? '归档失败' : '恢复失败');
  }
}

export async function deleteSession(sessionId: string, refresh: RefreshSessionsFn): Promise<void> {
  const ok = await showConfirmDialog({
    title: '删除会话',
    message: '确认删除该会话？此操作不可撤销。',
    confirmText: '删除',
    cancelText: '取消',
  });
  if (!ok) return;
  try {
    await backendApi.deleteSession(sessionId);
    removeSessionState(sessionId);
    if (state.activeSessionId === sessionId) {
      setActiveSessionId(null);
      renderChat();
    }
    await refresh();
    await refreshLibraryPaneIfOpen();
    notify('已删除会话');
  } catch (err) {
    notify((err as Error)?.message || '删除失败');
  }
}
