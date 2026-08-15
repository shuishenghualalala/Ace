/**
 * @vitest-environment happy-dom
 *
 * 阶段 0：状态隔离 + 局部 patch 治本状态图标抽搐。
 * 覆盖 setBusy/setSessionStatus 的短路返回、patchSessionRowStatus 的局部更新、
 * 以及「同值二次写入不重建状态图标节点」这一治本契约。
 * DOM 契约：SessionHistoryView（mw-session-history__*，见 src/ui/features/session-history-view.ts）。
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { __resetAllStoresForTest, sessionStore, workspaceStore } from '../../src/ui/stores/stores';
import { setBusy, setSessionStatus, setActiveSessionId, setExpandedWorkspace, type SessionRow } from '../../src/ui/state';
import { CONVERSATIONS_LIMIT, patchSessionRowStatus, renderWorkspaceHistory } from '../../src/ui/features/workspaces';
import type { Workspace } from '../../src/ui/backend-client';
import { mountHistoryList } from './helpers/history-list';

function makeSession(id: string, workspaceId = 'default'): SessionRow {
  return { id, title: id, workspaceId, updatedAt: 1000, preview: '', badge: '' };
}

function statusOf(list: HTMLElement, sessionId: string): HTMLElement {
  return list.querySelector(`[data-session-id="${sessionId}"] [data-session-status]`) as HTMLElement;
}

beforeEach(() => __resetAllStoresForTest());

describe('setBusy / setSessionStatus 状态隔离短路', () => {
  it('setBusy 相同值二次调用返回 false 且不再触发订阅', () => {
    let notifyCount = 0;
    const unsub = sessionStore.subscribe(() => { notifyCount++; });
    try {
      expect(setBusy('s1', true)).toBe(true);
      const afterFirst = notifyCount;
      expect(setBusy('s1', true)).toBe(false);
      expect(notifyCount).toBe(afterFirst);
      expect(setBusy('s1', false)).toBe(true);
      expect(setBusy('s1', false)).toBe(false); // false→false（含 undefined→false）短路
    } finally {
      unsub();
    }
  });

  it('setSessionStatus 相同值二次调用返回 false 且不再触发订阅', () => {
    let notifyCount = 0;
    const unsub = sessionStore.subscribe(() => { notifyCount++; });
    try {
      expect(setSessionStatus('s1', 'running')).toBe(true);
      const afterFirst = notifyCount;
      expect(setSessionStatus('s1', 'running')).toBe(false);
      expect(notifyCount).toBe(afterFirst);
    } finally {
      unsub();
    }
  });
});

describe('patchSessionRowStatus 局部更新', () => {
  it('只改目标行状态区，不触碰其它行 DOM 身份', () => {
    sessionStore.set({ sessions: [makeSession('s1'), makeSession('s2')] });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const row1 = list.querySelector('[data-session-id="s1"]') as HTMLElement;
    const row2 = list.querySelector('[data-session-id="s2"]') as HTMLElement;
    const row1Title = row1.querySelector('[data-session-title]');
    const status1 = statusOf(list, 's1');
    expect(status1.dataset.status).toBe('idle');
    expect(status1.querySelector('svg')).toBeNull();

    patchSessionRowStatus('s1', 'running');

    expect(status1.dataset.status).toBe('running');
    expect(status1.classList.contains('mw-session-history__status--running')).toBe(true);
    expect(status1.querySelector('svg')).not.toBeNull();
    // row1 节点身份不变，title 子节点身份不变（未整树重建）
    expect(list.querySelector('[data-session-id="s1"]')).toBe(row1);
    expect(row1.querySelector('[data-session-title]')).toBe(row1Title);
    // row2 完全未受影响
    expect(statusOf(list, 's2').dataset.status).toBe('idle');
    expect(list.querySelector('[data-session-id="s2"]')).toBe(row2);
  });

  it('running → idle 移除状态图标与 running 标记', () => {
    sessionStore.set({ sessions: [makeSession('s1')] });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    patchSessionRowStatus('s1', 'running');
    const status = statusOf(list, 's1');
    expect(status.dataset.status).toBe('running');
    expect(status.querySelector('svg')).not.toBeNull();
    patchSessionRowStatus('s1', 'idle');
    expect(status.dataset.status).toBe('idle');
    expect(status.querySelector('svg')).toBeNull();
    expect(status.classList.contains('mw-session-history__status--running')).toBe(false);
  });

  it('未渲染的行 patch 为 no-op，不抛错', () => {
    mountHistoryList();
    expect(() => patchSessionRowStatus('not-rendered', 'running')).not.toThrow();
  });
});

describe('状态图标在流式期间保持稳定（治本契约）', () => {
  it('同值二次 setSessionStatus 不重建状态图标节点', () => {
    sessionStore.set({ sessions: [makeSession('s1')] });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    // 首次 running：通过 sessionStatuses 订阅 patch 出状态图标
    setSessionStatus('s1', 'running');
    const iconBefore = statusOf(list, 's1').querySelector('svg');
    expect(iconBefore).not.toBeNull();
    // 流式期间反复 running → running：短路，订阅不触发，图标节点身份不变
    setSessionStatus('s1', 'running');
    setSessionStatus('s1', 'running');
    expect(statusOf(list, 's1').querySelector('svg')).toBe(iconBefore);
  });
});

describe('行级 reconciler：key 复用 + 重排 + 跨整树存活', () => {
  function makeWs(id: string, name: string): Workspace {
    return { id, name, description: '', instructions: '', root_path: '' } as unknown as Workspace;
  }

  it('重渲后同 sid 行节点身份保持（key 复用，不重建）', () => {
    sessionStore.set({ sessions: [makeSession('s1'), makeSession('s2')] });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const row1 = list.querySelector('[data-session-id="s1"]');
    const row2 = list.querySelector('[data-session-id="s2"]');
    // 再次渲染（无状态变化）
    renderWorkspaceHistory(() => {});
    expect(list.querySelector('[data-session-id="s1"]')).toBe(row1);
    expect(list.querySelector('[data-session-id="s2"]')).toBe(row2);
  });

  it('updatedAt 变化导致重排：行节点身份保持，仅位置变化', () => {
    sessionStore.set({
      sessions: [
        { id: 's1', title: 's1', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' },
        { id: 's2', title: 's2', workspaceId: 'default', updatedAt: 2000, preview: '', badge: '' },
      ],
    });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    // 初始顺序：s2（2000）在前，s1（1000）在后
    const rows = list.querySelectorAll('[data-session-id]');
    expect(rows[0].getAttribute('data-session-id')).toBe('s2');
    expect(rows[1].getAttribute('data-session-id')).toBe('s1');
    const row1Before = list.querySelector('[data-session-id="s1"]');
    const row2Before = list.querySelector('[data-session-id="s2"]');
    // s1 更新更晚 → 上浮到首位
    sessionStore.set({
      sessions: [
        { id: 's1', title: 's1', workspaceId: 'default', updatedAt: 3000, preview: '', badge: '' },
        { id: 's2', title: 's2', workspaceId: 'default', updatedAt: 2000, preview: '', badge: '' },
      ],
    });
    renderWorkspaceHistory(() => {});
    const rowsAfter = list.querySelectorAll('[data-session-id]');
    expect(rowsAfter[0].getAttribute('data-session-id')).toBe('s1');
    expect(rowsAfter[1].getAttribute('data-session-id')).toBe('s2');
    // 节点身份不变（只是被重新 append 移动）
    expect(list.querySelector('[data-session-id="s1"]')).toBe(row1Before);
    expect(list.querySelector('[data-session-id="s2"]')).toBe(row2Before);
  });

  it('后台 running 行在另一会话触发整树重渲后状态图标身份保持（治本核心）', () => {
    sessionStore.set({
      sessions: [
        { id: 'active', title: 'active', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' },
        { id: 'bg', title: 'bg', workspaceId: 'default', updatedAt: 2000, preview: '', badge: '' },
      ],
    });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    // 后台会话 bg 进入 running，状态图标出现
    setSessionStatus('bg', 'running');
    const iconBefore = statusOf(list, 'bg').querySelector('svg');
    expect(iconBefore).not.toBeNull();
    // active 会话回合结束触发整树 renderWorkspaceHistory（模拟 chat-controller 路径）
    sessionStore.set({
      sessions: [
        { id: 'active', title: 'active-renamed', workspaceId: 'default', updatedAt: 3000, preview: '', badge: '' },
        { id: 'bg', title: 'bg', workspaceId: 'default', updatedAt: 2000, preview: '', badge: '' },
      ],
    });
    renderWorkspaceHistory(() => {});
    // bg 的行节点与状态图标元素身份都必须保持，CSS 动画不重启
    const bgRowAfter = list.querySelector('[data-session-id="bg"]') as HTMLElement;
    expect(statusOf(list, 'bg').querySelector('svg')).toBe(iconBefore);
    expect(bgRowAfter.querySelector<HTMLElement>('[data-session-status]')!.dataset.status).toBe('running');
  });

  it('项目块复用：折叠项目 A 不影响项目 B 的行身份', () => {
    workspaceStore.set({ workspaces: [makeWs('wA', 'A'), makeWs('wB', 'B')] });
    sessionStore.set({
      sessions: [
        { id: 'a1', title: 'a1', workspaceId: 'wA', updatedAt: 1000, preview: '', badge: '' },
        { id: 'b1', title: 'b1', workspaceId: 'wB', updatedAt: 1000, preview: '', badge: '' },
      ],
    });
    setExpandedWorkspace('wA', true);
    setExpandedWorkspace('wB', true);
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const bRowBefore = list.querySelector('[data-session-id="b1"]');
    // 折叠 A 后重渲
    setExpandedWorkspace('wA', false);
    renderWorkspaceHistory(() => {});
    expect(list.querySelector('[data-session-id="b1"]')).toBe(bRowBefore);
  });

  it('事件委托：点击行触发 openSession', () => {
    sessionStore.set({ sessions: [makeSession('s1')] });
    let opened: string | null = null;
    const list = mountHistoryList({ openSession: (id) => { opened = id; } });
    renderWorkspaceHistory(() => {});
    const openBtn = list.querySelector<HTMLElement>('[data-session-open="s1"]');
    expect(openBtn).not.toBeNull();
    openBtn!.click();
    expect(opened).toBe('s1');
  });
});

describe('行尾未读 pill', () => {
  it('后台 running → idle 显示未读 pill；打开会话后恢复时间', () => {
    sessionStore.set({
      sessions: [
        { id: 'active', title: 'active', workspaceId: 'default', updatedAt: 2000, preview: '', badge: '' },
        { id: 'bg', title: 'bg', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' },
      ],
      activeSessionId: 'active',
    });
    const list = mountHistoryList({ openSession: (id) => setActiveSessionId(id) });
    renderWorkspaceHistory(() => {});
    setSessionStatus('bg', 'running');
    setSessionStatus('bg', 'idle');
    const bgStatus = statusOf(list, 'bg');
    expect(bgStatus.dataset.status).toBe('unread');
    expect(bgStatus.classList.contains('mw-session-history__status--unread')).toBe(true);

    list.querySelector<HTMLElement>('[data-session-open="bg"]')!.click();
    const bgStatusAfter = statusOf(list, 'bg');
    expect(bgStatusAfter.dataset.status).toBe('idle');
    expect(bgStatusAfter.classList.contains('mw-session-history__status--unread')).toBe(false);
  });
});

describe('对话分区封顶 + 展开显示', () => {
  function makeConversations(n: number): SessionRow[] {
    return Array.from({ length: n }, (_, i) => ({
      id: `c${i}`,
      title: `c${i}`,
      workspaceId: 'default',
      updatedAt: n - i,
      preview: '',
      badge: '',
    }));
  }

  it('超过 CONVERSATIONS_LIMIT 只渲染上限条 + 出现「再显示 N 个会话」', () => {
    // 断言跟着常量走：这里曾硬编码旧上限 50，上限调成 10 后就静默失配。
    // 用 LIMIT+1 而非大倍数：这是「超过上限」的最紧边界，off-by-one 会立刻变红。
    sessionStore.set({ sessions: makeConversations(CONVERSATIONS_LIMIT + 1) });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const rows = list.querySelectorAll('[data-session-id]');
    expect(rows.length).toBe(CONVERSATIONS_LIMIT);
    const expandBtn = list.querySelector('[data-show-all="default"]');
    expect(expandBtn).not.toBeNull();
    expect(expandBtn!.textContent).toBe('再显示 1 个会话');
  });

  it('点击「再显示 N 个会话」后渲染全部，按钮变为「收起」', () => {
    sessionStore.set({ sessions: makeConversations(CONVERSATIONS_LIMIT + 1) });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const expandBtn = list.querySelector<HTMLElement>('[data-show-all="default"]');
    expect(expandBtn).not.toBeNull();
    expandBtn!.click(); // 委托：setWsShowAll('default', true) + 重渲
    const rows = list.querySelectorAll('[data-session-id]');
    expect(rows.length).toBe(CONVERSATIONS_LIMIT + 1);
    expect(list.querySelector('[data-show-all="default"]')!.textContent).toBe('收起');
  });

  it('未超限时不显示展开按钮，全量渲染', () => {
    // 恰好等于上限：代码用的是 `>`，此时不应出现展开按钮。
    sessionStore.set({ sessions: makeConversations(CONVERSATIONS_LIMIT) });
    const list = mountHistoryList();
    renderWorkspaceHistory(() => {});
    const rows = list.querySelectorAll('[data-session-id]');
    expect(rows.length).toBe(CONVERSATIONS_LIMIT);
    expect(list.querySelector('[data-show-all="default"]')).toBeNull();
  });
});
