/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, sessionStore } from '../../src/ui/stores/stores';
import { backendApi } from '../../src/ui/backend-client';
import { chatOutbox } from '../../src/ui/features/chat-outbox';
import { renderWorkspaceHistory } from '../../src/ui/features/workspaces';
import { deleteSession, openSessionActionsMenu } from '../../src/ui/features/session-actions';
import { showContextMenu } from '../../src/ui/lib/context-menu';
import * as feedback from '../../src/ui/ui-feedback';
import { mountHistoryList } from './helpers/history-list';

vi.mock('../../src/ui/lib/context-menu', () => ({
  showContextMenu: vi.fn(),
  dismissContextMenu: vi.fn(),
}));

beforeEach(() => {
  __resetAllStoresForTest();
  vi.clearAllMocks();
});

describe('侧栏会话操作入口', () => {
  it('会话行渲染 ⋯ 菜单按钮', () => {
    sessionStore.set({
      sessions: [
        { id: 's1', title: '测试', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' },
      ],
    });
    mountHistoryList();
    renderWorkspaceHistory(() => {});
    const menu = document.querySelector('[data-session-menu="s1"]');
    expect(menu).toBeTruthy();
    expect(menu?.classList.contains('mw-session-history__menu')).toBe(true);
  });

  it('openSessionActionsMenu 调用 showContextMenu', () => {
    sessionStore.set({
      sessions: [
        { id: 's1', title: '测试', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' },
      ],
    });
    const anchor = document.createElement('button');
    openSessionActionsMenu('s1', anchor, async () => {});
    expect(showContextMenu).toHaveBeenCalledTimes(1);
    const items = vi.mocked(showContextMenu).mock.calls[0][1];
    expect(items.map((i) => i.id)).toEqual(['pin', 'rename', 'export', 'archive', 'delete']);
  });

  it('withdraws a pending send before deleting its session', async () => {
    const confirmed = vi.spyOn(feedback, 'showConfirmDialog').mockResolvedValue(true);
    const withdrawal = Promise.withResolvers<void>();
    const withdraw = vi.spyOn(chatOutbox, 'withdraw').mockReturnValueOnce(withdrawal.promise);
    const deleteBackend = vi.spyOn(backendApi, 'deleteSession').mockRejectedValueOnce(new Error('test stops after deletion call'));
    try {
      const pending = deleteSession('s1', vi.fn());
      await vi.waitFor(() => expect(withdraw).toHaveBeenCalledWith('s1'));
      expect(deleteBackend).not.toHaveBeenCalled();
      withdrawal.resolve();
      await pending;
      expect(deleteBackend).toHaveBeenCalledWith('s1');
    } finally {
      confirmed.mockRestore();
      withdraw.mockRestore();
      deleteBackend.mockRestore();
    }
  });
});
