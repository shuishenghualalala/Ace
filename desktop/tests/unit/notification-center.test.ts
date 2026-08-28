// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BackendNotification } from '../../src/ui/backend-client';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  unreadCount: vi.fn(),
  markRead: vi.fn(),
  markAllRead: vi.fn(),
  clear: vi.fn(),
  openSessionInChat: vi.fn(),
}));

vi.mock('../../src/ui/backend-client', () => ({
  notificationApi: {
    list: (opts?: unknown) => mocks.list(opts),
    unreadCount: () => mocks.unreadCount(),
    markRead: (id: string) => mocks.markRead(id),
    markAllRead: () => mocks.markAllRead(),
    clear: () => mocks.clear(),
  },
}));

vi.mock('../../src/ui/features/chat-controller', () => ({
  openSessionInChat: (sessionId: string) => mocks.openSessionInChat(sessionId),
}));

import {
  bindNotificationCenter,
  handleNotificationPush,
  resetNotificationCenterForTest,
} from '../../src/ui/features/notification-center';

function installDom(): void {
  document.body.innerHTML = `
    <button id="notification-bell-btn" type="button">
      <span id="notification-badge" hidden></span>
    </button>
    <div id="confirm-dialog" hidden>
      <span id="confirm-title"></span>
      <span id="confirm-message"></span>
      <button id="confirm-ok" type="button"></button>
      <button id="confirm-cancel" type="button"></button>
    </div>
  `;
}

function sample(overrides: Partial<BackendNotification> = {}): BackendNotification {
  return {
    id: 'n1',
    source: 'cron',
    kind: 'cron_run_failed',
    title: '定时任务执行失败',
    body: '日报生成失败',
    payload: { session_id: 's1' },
    created_at: Date.now() / 1000 - 300,
    read_at: null,
    ...overrides,
  };
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe('notification center', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetNotificationCenterForTest();
    installDom();
    mocks.unreadCount.mockResolvedValue({ unread_count: 0 });
    mocks.list.mockResolvedValue({ notifications: [], unread_count: 0 });
    mocks.markRead.mockResolvedValue({ ok: true });
    mocks.markAllRead.mockResolvedValue({ ok: true });
    mocks.clear.mockResolvedValue({ ok: true });
    mocks.openSessionInChat.mockResolvedValue(undefined);
  });

  it('启动时拉取未读数：>0 显示角标，>99 封顶为 99+', async () => {
    const badge = document.getElementById('notification-badge') as HTMLElement;
    mocks.unreadCount.mockResolvedValue({ unread_count: 3 });
    bindNotificationCenter();
    await flush();
    expect(badge.hidden).toBe(false);
    expect(badge.textContent).toBe('3');

    resetNotificationCenterForTest();
    installDom();
    mocks.unreadCount.mockResolvedValue({ unread_count: 120 });
    const badge2 = document.getElementById('notification-badge') as HTMLElement;
    bindNotificationCenter();
    await flush();
    expect(badge2.hidden).toBe(false);
    expect(badge2.textContent).toBe('99+');
  });

  it('未读数为 0 时角标保持隐藏', async () => {
    const badge = document.getElementById('notification-badge') as HTMLElement;
    bindNotificationCenter();
    await flush();
    expect(badge.hidden).toBe(true);
    expect(badge.textContent).toBe('0');
  });

  it('WS 推送：角标 +1 并弹出 toast 轻提示', async () => {
    const badge = document.getElementById('notification-badge') as HTMLElement;
    bindNotificationCenter();
    await flush();
    handleNotificationPush(sample({ title: '同伴发来新消息' }));
    expect(badge.hidden).toBe(false);
    expect(badge.textContent).toBe('1');
    expect(document.body.textContent).toContain('同伴发来新消息');
    // 同 id 重复推送不重复计数
    handleNotificationPush(sample());
    expect(badge.textContent).toBe('1');
  });

  it('点击铃铛打开面板并渲染列表：来源中文标签、未读高亮、相对时间', async () => {
    mocks.list.mockResolvedValue({
      notifications: [sample(), sample({ id: 'n2', source: 'approval', title: '有待审批', read_at: 1 })],
      unread_count: 1,
    });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();

    const panel = document.getElementById('notification-panel') as HTMLElement;
    expect(panel.hidden).toBe(false);
    const items = panel.querySelectorAll('.mw-notification-item');
    expect(items.length).toBe(2);
    expect(items[0].classList.contains('is-unread')).toBe(true);
    expect(items[1].classList.contains('is-unread')).toBe(false);
    expect(panel.textContent).toContain('定时任务');
    expect(panel.textContent).toContain('审批');
    expect(panel.textContent).toContain('分钟前');
  });

  it('空列表显示空状态文案', async () => {
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    const panel = document.getElementById('notification-panel') as HTMLElement;
    expect(panel.textContent).toContain('暂无通知');
  });

  it('点击条目：标记已读 + 角标同步 + 按 payload.session_id 跳转', async () => {
    mocks.unreadCount.mockResolvedValue({ unread_count: 1 });
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();

    (document.querySelector('.mw-notification-item') as HTMLButtonElement).click();
    await flush();
    expect(mocks.markRead).toHaveBeenCalledWith('n1');
    expect(mocks.openSessionInChat).toHaveBeenCalledWith('s1');
    const badge = document.getElementById('notification-badge') as HTMLElement;
    expect(badge.hidden).toBe(true);
    // 跳转后面板已关闭
    expect((document.getElementById('notification-panel') as HTMLElement).hidden).toBe(true);
  });

  it('审批类通知（无 session_id）：标记已读并唤醒审批面板轮询', async () => {
    const approvalEvent = vi.fn();
    window.addEventListener('security:approval-pending', approvalEvent);
    mocks.list.mockResolvedValue({
      notifications: [sample({ source: 'approval', payload: null })],
      unread_count: 1,
    });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    (document.querySelector('.mw-notification-item') as HTMLButtonElement).click();
    await flush();
    expect(mocks.markRead).toHaveBeenCalledWith('n1');
    expect(mocks.openSessionInChat).not.toHaveBeenCalled();
    expect(approvalEvent).toHaveBeenCalled();
    window.removeEventListener('security:approval-pending', approvalEvent);
  });

  it('全部已读：调 read-all 并清空角标与未读高亮', async () => {
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();

    const actions = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.mw-notification-panel__action'),
    );
    actions.find((button) => button.textContent === '全部已读')?.click();
    await flush();
    expect(mocks.markAllRead).toHaveBeenCalled();
    expect(document.querySelector('.mw-notification-item.is-unread')).toBeNull();
    expect((document.getElementById('notification-badge') as HTMLElement).hidden).toBe(true);
  });

  it('清空：确认后调 DELETE 并清空列表', async () => {
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();

    const actions = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.mw-notification-panel__action'),
    );
    actions.find((button) => button.textContent === '清空')?.click();
    await flush();
    // 确认弹窗出现，确认后才真正清空
    expect((document.getElementById('confirm-dialog') as HTMLElement).hidden).toBe(false);
    expect(mocks.clear).not.toHaveBeenCalled();
    (document.getElementById('confirm-ok') as HTMLButtonElement).click();
    await flush();
    expect(mocks.clear).toHaveBeenCalled();
    expect(document.querySelector('.mw-notification-item')).toBeNull();
    expect(document.getElementById('notification-panel')?.textContent).toContain('暂无通知');
  });
});
