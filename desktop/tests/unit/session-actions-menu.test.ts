/**
 * @vitest-environment happy-dom
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, sessionStore } from '../../src/ui/stores/stores';
import { renderWorkspaceHistory } from '../../src/ui/features/workspaces';
import { openSessionActionsMenu } from '../../src/ui/features/session-actions';
import { showContextMenu } from '../../src/ui/lib/context-menu';

vi.mock('../../src/ui/lib/context-menu', () => ({
  showContextMenu: vi.fn(),
  dismissContextMenu: vi.fn(),
}));

function mountHistoryList(): HTMLElement {
  const list = document.createElement('div');
  list.id = 'history-list';
  document.body.innerHTML = '';
  document.body.appendChild(list);
  return list;
}

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
    expect(menu?.classList.contains('history-item-menu-btn')).toBe(true);
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
});
