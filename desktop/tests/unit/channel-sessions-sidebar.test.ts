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

let view: SessionHistoryView | null = null;

function mountView(openSession: (sessionId: string) => void = () => {}): HTMLElement {
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
