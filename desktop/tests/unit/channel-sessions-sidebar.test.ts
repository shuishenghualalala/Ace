/**
 * @vitest-environment happy-dom
 *
 * 渠道会话侧栏：分组渲染、空态隐藏、点击打开会话。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, sessionStore, workspaceStore } from '../../src/ui/stores/stores';
import { createSessionHistoryView, type SessionHistoryView } from '../../src/ui/features/session-history-view';
import { isChannelSessionId } from '../../src/ui/features/channel-sessions';
import type { SessionRow } from '../../src/ui/state';
import type { Workspace } from '../../src/ui/backend-client';

const CHANNELS_SECTION = '[data-session-section="Crew.historyChannelsCollapsed"]';

function channelSession(id: string, platform = 'feishu'): SessionRow {
  return {
    id,
    title: '渠道对话',
    workspaceId: 'default',
    updatedAt: 2000,
    preview: '',
    badge: platform,
    channelPlatform: platform,
  };
}

function historySession(id: string, workspaceId: string, options: { archived?: boolean; updatedAt?: number } = {}): SessionRow {
  return {
    id,
    title: id,
    workspaceId,
    updatedAt: options.updatedAt ?? 2000,
    preview: '',
    badge: '',
    archived: options.archived,
  };
}

function workspace(id: string, name: string, hidden = false): Workspace {
  return { id, name, description: '', instructions: '', root_path: '', hidden } as unknown as Workspace;
}

let view: SessionHistoryView | null = null;

function mountView(
  openSession: (sessionId: string) => void = () => {},
  renderStudioHistory: () => void = () => view?.render(),
): HTMLElement {
  const host = document.createElement('div');
  document.body.innerHTML = '';
  document.body.append(host);
  view = createSessionHistoryView(host, {
    openSession,
    createSession: vi.fn(),
    createWorkspace: vi.fn(),
    manageHistory: vi.fn(),
    openWorkspace: vi.fn(),
    refreshSessions: async () => undefined,
    retrySessions: async () => undefined,
    retryWorkspaces: async () => undefined,
    getLoadErrors: () => ({ sessions: null, workspaces: null }),
    renderStudioHistory,
  });
  return host;
}

beforeEach(() => {
  __resetAllStoresForTest();
  localStorage.clear();
});

afterEach(() => {
  view?.dispose();
  view = null;
});

describe('isChannelSessionId', () => {
  it('识别 agent:main: 前缀', () => {
    expect(isChannelSessionId('agent:main:feishu:dm:u1')).toBe(true);
    expect(isChannelSessionId('agent:main:testchat:dm:u1')).toBe(true);
    expect(isChannelSessionId('testchat:acct:u1')).toBe(false);
    expect(isChannelSessionId('sess-1')).toBe(false);
  });
});

describe('渠道侧栏分区', () => {
  it('渠道分区头保留 dataset、ARIA 与折叠态切换', () => {
    workspaceStore.set({
      channelSessionGroups: [
        { platform: 'feishu', label: '飞书', sessions: [channelSession('agent:main:feishu:dm:u1')] },
      ],
    });
    localStorage.setItem('Crew.historyChannelsCollapsed', 'true');
    mountView();

    const header = document.querySelector<HTMLButtonElement>(
      `${CHANNELS_SECTION} [data-section-toggle]`,
    );
    expect(header).not.toBeNull();
    expect(header!.dataset.sectionToggle).toBe('Crew.historyChannelsCollapsed');
    expect(header!.getAttribute('aria-expanded')).toBe('false');
    expect(header!.querySelector('use')?.getAttribute('href')).toBe('#icon-chevron-down');
    // 折叠时不渲染分区内容
    expect(document.querySelector(`${CHANNELS_SECTION} .mw-session-history__section-content`)).toBeNull();

    header!.click();
    const updated = document.querySelector<HTMLButtonElement>(
      `${CHANNELS_SECTION} [data-section-toggle]`,
    );
    expect(updated!.getAttribute('aria-expanded')).toBe('true');
    expect(document.querySelector(`${CHANNELS_SECTION} .mw-session-history__section-content`)).not.toBeNull();
  });

  it('无渠道分组时不渲染渠道分区', () => {
    sessionStore.set({ sessions: [{ id: 's1', title: 's1', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' }] });
    mountView();
    expect(document.querySelector(CHANNELS_SECTION)).toBeNull();
    expect(document.querySelector('[data-session-group="conversation:default"]')).not.toBeNull();
  });

  it('对大历史目录按工作空间归组并保留归档与默认对话边界', () => {
    workspaceStore.set({ workspaces: [workspace('ws-a', '项目 A'), workspace('ws-b', '项目 B'), workspace('ws-hidden', '隐藏项目', true)] });
    sessionStore.set({ sessions: [
      historySession('a-new', 'ws-a', { updatedAt: 3000 }),
      historySession('a-old', 'ws-a', { updatedAt: 1000 }),
      historySession('b-only', 'ws-b'),
      historySession('default-only', 'default'),
      historySession('a-archived', 'ws-a', { archived: true }),
      historySession('hidden-only', 'ws-hidden'),
    ] });
    mountView();

    const ids = (selector: string) => Array.from(document.querySelectorAll<HTMLElement>(selector))
      .map((element) => element.dataset.sessionId);
    expect(ids('[data-session-group="workspace:ws-a"] [data-session-id]')).toEqual(['a-new', 'a-old']);
    expect(ids('[data-session-group="workspace:ws-b"] [data-session-id]')).toEqual(['b-only']);
    expect(ids('[data-session-group="conversation:default"] [data-session-id]')).toEqual(['default-only']);
    expect(ids('[data-session-group="archived"] [data-session-id]')).toEqual(['a-archived']);
    expect(document.querySelector('[data-session-id="hidden-only"]')).toBeNull();
  });

  it('展开大型会话组时只挂载视口窗口并随滚动切换会话行', async () => {
    workspaceStore.set({ workspaces: [workspace('ws-a', '项目 A')] });
    sessionStore.set({
      sessions: Array.from({ length: 200 }, (_, index) =>
        historySession(`session-${index}`, 'ws-a', { updatedAt: 10_000 - index })),
    });
    const open = vi.fn();
    const host = mountView(open);
    const list = host.querySelector<HTMLElement>('[data-session-history-list]')!;
    Object.defineProperty(list, 'clientHeight', { configurable: true, value: 150 });
    vi.spyOn(list, 'getBoundingClientRect').mockReturnValue({
      x: 0, y: 0, width: 260, height: 150, top: 0, right: 260, bottom: 150, left: 0,
      toJSON: () => ({}),
    } as DOMRect);

    host.querySelector<HTMLButtonElement>('[data-show-all="ws-a"]')!.click();
    const virtualGroup = host.querySelector<HTMLElement>(
      '[data-session-group="workspace:ws-a"] .mw-session-history__items--virtualized',
    )!;
    const setItemsRect = (): void => {
      vi.spyOn(virtualGroup, 'getBoundingClientRect').mockReturnValue({
        x: 0, y: 40 - list.scrollTop, width: 248, height: 200 * 34 - 4,
        top: 40 - list.scrollTop, right: 248, bottom: 40 - list.scrollTop + 200 * 34 - 4, left: 0,
        toJSON: () => ({}),
      } as DOMRect);
    };
    setItemsRect();

    const mountedRows = (): string[] => Array.from(
      virtualGroup.querySelectorAll<HTMLElement>('[data-session-id]'),
    ).map((row) => row.dataset.sessionId!);
    expect(mountedRows().length).toBeLessThan(40);
    expect(mountedRows()).toContain('session-0');
    expect(mountedRows()).not.toContain('session-100');
    expect(virtualGroup.querySelector('.mw-session-history__virtual-spacer')?.getAttribute('style'))
      .toContain('height: 6796px');

    list.scrollTop = 1_700;
    setItemsRect();
    list.dispatchEvent(new Event('scroll'));
    await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));

    expect(mountedRows().length).toBeLessThan(40);
    expect(mountedRows()).not.toContain('session-0');
    expect(mountedRows()).toContain('session-49');
    host.querySelector<HTMLButtonElement>('[data-session-open="session-49"]')!.click();
    expect(open).toHaveBeenCalledExactlyOnceWith('session-49');
  });

  it('有渠道分组时渲染平台文件夹与会话行', () => {
    workspaceStore.set({
      channelSessionGroups: [
        { platform: 'feishu', label: '飞书', sessions: [channelSession('agent:main:feishu:dm:u1')] },
      ],
    });
    mountView();
    expect(document.querySelector(CHANNELS_SECTION)).not.toBeNull();
    expect(document.querySelector('[data-session-group="channel:feishu"]')).not.toBeNull();
    expect(document.querySelector('[data-channel-toggle="feishu"]')).not.toBeNull();
    expect(document.querySelector('[data-session-id="agent:main:feishu:dm:u1"]')).not.toBeNull();
  });

  it('渠道会话不出现在默认对话列表', () => {
    sessionStore.set({
      sessions: [
        { id: 's1', title: '桌面', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' },
        channelSession('agent:main:feishu:dm:u1'),
        channelSession('agent:main:testchat:dm:u1', 'testchat'),
      ],
    });
    mountView();
    const convIds = Array.from(
      document.querySelectorAll('[data-session-group="conversation:default"] [data-session-id]'),
    ).map((el) => el.getAttribute('data-session-id'));
    expect(convIds).toEqual(['s1']);
  });

  it('点击渠道会话行触发 openSession', () => {
    workspaceStore.set({
      channelSessionGroups: [
        { platform: 'feishu', label: '飞书', sessions: [channelSession('agent:main:feishu:dm:u1')] },
      ],
    });
    const open = vi.fn();
    mountView(open);
    const row = document.querySelector<HTMLElement>('[data-session-open="agent:main:feishu:dm:u1"]');
    expect(row).not.toBeNull();
    row!.click();
    expect(open).toHaveBeenCalledWith('agent:main:feishu:dm:u1');
  });
});
