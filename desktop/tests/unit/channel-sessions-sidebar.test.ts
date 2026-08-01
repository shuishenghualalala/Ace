/**
 * @vitest-environment happy-dom
 *
 * 渠道会话侧栏：分组渲染、空态隐藏、点击打开会话。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetAllStoresForTest, sessionStore, workspaceStore } from '../../src/ui/stores/stores';
import { renderWorkspaceHistory } from '../../src/ui/features/workspaces';
import { isChannelSessionId } from '../../src/ui/features/channel-sessions';
import type { SessionRow } from '../../src/ui/state';

function mountHistoryList(): HTMLElement {
  const list = document.createElement('div');
  list.id = 'history-list';
  document.body.innerHTML = '';
  document.body.appendChild(list);
  return list;
}

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

beforeEach(() => {
  __resetAllStoresForTest();
  localStorage.clear();
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
  it('原生 DOM 分区头保留 dataset、ARIA、折叠态与 SVG 图标', () => {
    localStorage.setItem('crew.historyChannelsCollapsed', 'true');
    mountHistoryList();
    renderWorkspaceHistory(() => {});

    const header = document.querySelector<HTMLButtonElement>(
      '.history-section--channels .history-section-header',
    );
    expect(header).not.toBeNull();
    expect(header!.dataset.sectionToggle).toBe('crew.historyChannelsCollapsed');
    expect(header!.getAttribute('aria-expanded')).toBe('false');
    expect(header!.classList.contains('collapsed')).toBe(true);
    expect(header!.querySelector('.history-section-caret svg path')?.getAttribute('d'))
      .toBe('m9 18 6-6-6-6');

    header!.click();
    const updated = document.querySelector<HTMLButtonElement>(
      '.history-section--channels .history-section-header',
    );
    expect(updated!.getAttribute('aria-expanded')).toBe('true');
    expect(updated!.classList.contains('collapsed')).toBe(false);
  });

  it('无渠道分组时隐藏渠道分区', () => {
    sessionStore.set({ sessions: [{ id: 's1', title: 's1', workspaceId: 'default', updatedAt: 1000, preview: '', badge: '' }] });
    mountHistoryList();
    renderWorkspaceHistory(() => {});
    const section = document.querySelector('.history-section--channels') as HTMLElement;
    expect(section).not.toBeNull();
    expect(section.hidden).toBe(true);
  });

  it('有渠道分组时渲染平台文件夹与会话行', () => {
    workspaceStore.set({
      channelSessionGroups: [
        { platform: 'feishu', label: '飞书', sessions: [channelSession('agent:main:feishu:dm:u1')] },
      ],
    });
    mountHistoryList();
    renderWorkspaceHistory(() => {});
    const section = document.querySelector('.history-section--channels') as HTMLElement;
    expect(section.hidden).toBe(false);
    expect(document.querySelector('[data-channel-id="feishu"]')).not.toBeNull();
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
    mountHistoryList();
    renderWorkspaceHistory(() => {});
    const convIds = Array.from(document.querySelectorAll('.conversations-list [data-session-id]')).map(
      (el) => el.getAttribute('data-session-id'),
    );
    expect(convIds).toEqual(['s1']);
  });

  it('点击渠道会话行触发 openSession', () => {
    workspaceStore.set({
      channelSessionGroups: [
        { platform: 'feishu', label: '飞书', sessions: [channelSession('agent:main:feishu:dm:u1')] },
      ],
    });
    mountHistoryList();
    const open = vi.fn();
    renderWorkspaceHistory(open);
    const row = document.querySelector<HTMLElement>('[data-session-id="agent:main:feishu:dm:u1"]');
    expect(row).not.toBeNull();
    row!.click();
    expect(open).toHaveBeenCalledWith('agent:main:feishu:dm:u1');
  });
});
