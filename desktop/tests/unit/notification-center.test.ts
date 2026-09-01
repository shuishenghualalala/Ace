// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BackendNotification } from '../../src/ui/backend-client';

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  unreadCount: vi.fn(),
  markRead: vi.fn(),
  markAllRead: vi.fn(),
  openSessionInChat: vi.fn(),
  markSystemTrayNotification: vi.fn(),
  traySetNotifications: vi.fn(),
  activeSessionId: null as string | null,
}));

vi.mock('../../src/ui/backend-client', () => ({
  notificationApi: {
    list: (opts?: unknown) => mocks.list(opts),
    unreadCount: () => mocks.unreadCount(),
    markRead: (id: string) => mocks.markRead(id),
    markAllRead: () => mocks.markAllRead(),
  },
}));

vi.mock('../../src/ui/features/chat-controller', () => ({
  openSessionInChat: (sessionId: string) => mocks.openSessionInChat(sessionId),
}));

vi.mock('../../src/ui/features/system-tray', () => ({
  markSystemTrayNotification: () => mocks.markSystemTrayNotification(),
}));

vi.mock('../../src/ui/stores/session-store', () => ({
  sessionStore: { get: () => ({ activeSessionId: mocks.activeSessionId }) },
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

type TraySelectedHandler = (id: string) => void;
type TrayMarkAllHandler = () => void;

/** 安装 window.Crew 托盘桥桩，返回捕获到的托盘事件回调。 */
function installTrayBridge(): { selected: { current: TraySelectedHandler | null }; markAll: { current: TrayMarkAllHandler | null } } {
  const selected: { current: TraySelectedHandler | null } = { current: null };
  const markAll: { current: TrayMarkAllHandler | null } = { current: null };
  (window as unknown as { Crew: unknown }).Crew = {
    traySetNotifications: (summary: unknown) => mocks.traySetNotifications(summary),
    onTrayNotificationSelected: (cb: TraySelectedHandler) => {
      selected.current = cb;
      return () => { selected.current = null; };
    },
    onTrayNotificationsMarkAllRead: (cb: TrayMarkAllHandler) => {
      markAll.current = cb;
      return () => { markAll.current = null; };
    },
  };
  return { selected, markAll };
}

function removeTrayBridge(): void {
  delete (window as unknown as { Crew?: unknown }).Crew;
}

/** mocks.traySetNotifications 最近一次收到的摘要。 */
function lastTraySummary(): { unreadCount: number; items: Array<{ id: string; title: string; sourceLabel?: string }> } {
  const calls = mocks.traySetNotifications.mock.calls;
  return calls[calls.length - 1][0];
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
    mocks.openSessionInChat.mockResolvedValue(undefined);
    mocks.traySetNotifications.mockResolvedValue({ ok: true });
    mocks.activeSessionId = null;
    removeTrayBridge();
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

  it('点击铃铛打开面板并渲染未读列表：只拉未读、来源中文标签、相对时间', async () => {
    mocks.list.mockResolvedValue({
      notifications: [sample(), sample({ id: 'n2', source: 'approval', title: '有待审批' })],
      unread_count: 2,
    });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();

    // 面板只拉未读条目（后端 unread_only 过滤）
    expect(mocks.list).toHaveBeenCalledWith({ limit: 50, offset: 0, unreadOnly: true });
    const panel = document.getElementById('notification-panel') as HTMLElement;
    expect(panel.hidden).toBe(false);
    const items = panel.querySelectorAll('.mw-notification-item');
    expect(items.length).toBe(2);
    expect(items[0].classList.contains('is-unread')).toBe(true);
    expect(panel.textContent).toContain('定时任务');
    expect(panel.textContent).toContain('审批');
    expect(panel.textContent).toContain('分钟前');
  });

  it('面板只渲染未读：已读条目在刷新后不出现', async () => {
    const unread = sample();
    const read = sample({ id: 'n-read', title: '已读旧通知', read_at: 1 });
    // 模拟后端 unread_only 过滤：面板拉未读时只返回未读条目
    mocks.list.mockImplementation((opts?: { unreadOnly?: boolean }) =>
      Promise.resolve(
        opts?.unreadOnly
          ? { notifications: [unread], unread_count: 1 }
          : { notifications: [unread, read], unread_count: 1 },
      ),
    );
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    const panel = document.getElementById('notification-panel') as HTMLElement;
    expect(panel.textContent).toContain('定时任务执行失败');
    expect(panel.textContent).not.toContain('已读旧通知');
  });

  it('面板头部只有「全部已读」，没有「清空」按钮', async () => {
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    const actions = Array.from(
      document.querySelectorAll<HTMLButtonElement>('.mw-notification-panel__action'),
    ).map((button) => button.textContent);
    expect(actions).toEqual(['全部已读']);
  });

  it('空列表显示空状态文案', async () => {
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    const panel = document.getElementById('notification-panel') as HTMLElement;
    expect(panel.textContent).toContain('没有未读通知');
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

  it('全部已读：调 read-all 并清空角标与面板列表', async () => {
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
    expect(document.querySelector('.mw-notification-item')).toBeNull();
    expect(document.getElementById('notification-panel')?.textContent).toContain('没有未读通知');
    expect((document.getElementById('notification-badge') as HTMLElement).hidden).toBe(true);
  });

  it('WS 推送：面板开着时新通知插入列表顶部', async () => {
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();

    handleNotificationPush(sample({ id: 'n-new', title: '有一个问题等待你回答', source: 'followup' }));
    const items = document.querySelectorAll<HTMLElement>('.mw-notification-item');
    expect(items.length).toBe(2);
    expect(items[0].dataset.notificationId).toBe('n-new');
    expect(items[0].textContent).toContain('追问');
  });

  it('启动时向托盘推送初始未读摘要（最近 5 条未读）', async () => {
    installTrayBridge();
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    expect(mocks.list).toHaveBeenCalledWith({ limit: 5, offset: 0, unreadOnly: true });
    expect(lastTraySummary()).toEqual({
      unreadCount: 1,
      items: [{ id: 'n1', title: '定时任务执行失败', sourceLabel: '定时任务' }],
    });
  });

  it('WS 推送：托盘摘要同步 +1，并切换托盘图标为通知态', async () => {
    installTrayBridge();
    bindNotificationCenter();
    await flush();
    mocks.traySetNotifications.mockClear();

    handleNotificationPush(sample({ title: '同伴发来新消息', source: 'companion' }));
    expect(mocks.markSystemTrayNotification).toHaveBeenCalled();
    expect(lastTraySummary()).toEqual({
      unreadCount: 1,
      items: [{ id: 'n1', title: '同伴发来新消息', sourceLabel: '同伴' }],
    });
    // 重复推送不重复同步
    mocks.traySetNotifications.mockClear();
    handleNotificationPush(sample({ title: '同伴发来新消息', source: 'companion' }));
    expect(mocks.traySetNotifications).not.toHaveBeenCalled();
  });

  it('托盘点击通知：标记已读 + 按 payload.session_id 跳转 + 托盘摘要清空', async () => {
    const bridge = installTrayBridge();
    mocks.unreadCount.mockResolvedValue({ unread_count: 1 });
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    expect(bridge.selected.current).not.toBeNull();
    mocks.traySetNotifications.mockClear();

    bridge.selected.current?.('n1');
    await flush();
    expect(mocks.markRead).toHaveBeenCalledWith('n1');
    expect(mocks.openSessionInChat).toHaveBeenCalledWith('s1');
    expect(lastTraySummary()).toEqual({ unreadCount: 0, items: [] });
    expect((document.getElementById('notification-badge') as HTMLElement).hidden).toBe(true);
  });

  it('托盘点击本地不存在的通知：拉列表兜底后照常已读 + 跳转', async () => {
    const bridge = installTrayBridge();
    bindNotificationCenter();
    await flush();
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });

    bridge.selected.current?.('n1');
    await flush();
    expect(mocks.list).toHaveBeenCalledWith({ limit: 50, offset: 0, unreadOnly: true });
    expect(mocks.markRead).toHaveBeenCalledWith('n1');
    expect(mocks.openSessionInChat).toHaveBeenCalledWith('s1');
  });

  it('托盘「全部标为已读」：复用全部已读逻辑并同步托盘摘要', async () => {
    const bridge = installTrayBridge();
    mocks.unreadCount.mockResolvedValue({ unread_count: 1 });
    mocks.list.mockResolvedValue({ notifications: [sample()], unread_count: 1 });
    bindNotificationCenter();
    await flush();
    mocks.traySetNotifications.mockClear();

    bridge.markAll.current?.();
    await flush();
    expect(mocks.markAllRead).toHaveBeenCalled();
    expect(lastTraySummary()).toEqual({ unreadCount: 0, items: [] });
  });

  it('托盘选中通知后，该条目立即从打开的面板中移除', async () => {
    const bridge = installTrayBridge();
    mocks.unreadCount.mockResolvedValue({ unread_count: 2 });
    mocks.list.mockResolvedValue({
      notifications: [sample(), sample({ id: 'n2', title: '有一个计划等待你批准', source: 'plan' })],
      unread_count: 2,
    });
    bindNotificationCenter();
    await flush();
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    expect(document.querySelectorAll('.mw-notification-item').length).toBe(2);

    bridge.selected.current?.('n1');
    await flush();
    expect(mocks.markRead).toHaveBeenCalledWith('n1');
    const items = document.querySelectorAll<HTMLElement>('.mw-notification-item');
    expect(items.length).toBe(1);
    expect(items[0].dataset.notificationId).toBe('n2');
    // 面板保持打开，仅移除已读条目
    expect((document.getElementById('notification-panel') as HTMLElement).hidden).toBe(false);
  });

  it('无托盘桥（preview/测试环境）时通知流程不受影响', async () => {
    bindNotificationCenter();
    await flush();
    handleNotificationPush(sample());
    expect((document.getElementById('notification-badge') as HTMLElement).textContent).toBe('1');
  });

  it('当前打开会话的推送：抑制角标/toast/托盘，静默置为已读', async () => {
    installTrayBridge();
    mocks.activeSessionId = 's1';
    bindNotificationCenter();
    await flush();
    mocks.traySetNotifications.mockClear();

    handleNotificationPush(sample({ title: '后台任务已完成' }));
    await flush();
    const badge = document.getElementById('notification-badge') as HTMLElement;
    expect(badge.hidden).toBe(true);
    expect(badge.textContent).toBe('0');
    expect(mocks.markRead).toHaveBeenCalledWith('n1');
    expect(document.body.textContent).not.toContain('后台任务已完成');
    expect(mocks.traySetNotifications).not.toHaveBeenCalled();
    expect(mocks.markSystemTrayNotification).not.toHaveBeenCalled();

    // 面板开着时也不插入未读列表
    (document.getElementById('notification-bell-btn') as HTMLButtonElement).click();
    await flush();
    mocks.markRead.mockClear();
    handleNotificationPush(sample({ id: 'n2', title: '后台任务已完成' }));
    await flush();
    expect(document.querySelectorAll('.mw-notification-item').length).toBe(0);
    expect(mocks.markRead).toHaveBeenCalledWith('n2');
  });

  it('其他会话的推送：保持原有角标 +1 + toast + 托盘同步', async () => {
    installTrayBridge();
    mocks.activeSessionId = 's-other';
    bindNotificationCenter();
    await flush();
    mocks.traySetNotifications.mockClear();

    handleNotificationPush(sample({ title: '后台任务已完成' }));
    const badge = document.getElementById('notification-badge') as HTMLElement;
    expect(badge.hidden).toBe(false);
    expect(badge.textContent).toBe('1');
    expect(document.body.textContent).toContain('后台任务已完成');
    expect(mocks.markRead).not.toHaveBeenCalled();
    expect(mocks.traySetNotifications).toHaveBeenCalled();
    expect(mocks.markSystemTrayNotification).toHaveBeenCalled();
  });

  it('无 session_id 的推送：保持原有角标 +1 + toast + 托盘同步', async () => {
    installTrayBridge();
    mocks.activeSessionId = 's1';
    bindNotificationCenter();
    await flush();
    mocks.traySetNotifications.mockClear();

    handleNotificationPush(sample({ title: '有一个操作等待审批', payload: null }));
    const badge = document.getElementById('notification-badge') as HTMLElement;
    expect(badge.hidden).toBe(false);
    expect(badge.textContent).toBe('1');
    expect(document.body.textContent).toContain('有一个操作等待审批');
    expect(mocks.markRead).not.toHaveBeenCalled();
    expect(mocks.traySetNotifications).toHaveBeenCalled();
    expect(mocks.markSystemTrayNotification).toHaveBeenCalled();
  });
});
