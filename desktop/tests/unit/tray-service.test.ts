import { describe, expect, it, vi } from 'vitest';
import type { TrayNotificationSummary } from '../../src/shared/types';
import {
  buildTrayMenuTemplate,
  isTrayNotificationSummary,
  TRAY_MENU_MAX_ITEMS,
  type TrayServiceOptions,
} from '../../src/main/tray-service';

vi.mock('electron', () => ({
  Menu: { buildFromTemplate: vi.fn() },
  nativeImage: {},
  Tray: class {},
}));

function makeHandlers() {
  return {
    assetsDir: '/tmp/assets',
    onActivate: vi.fn(),
    onUninstall: vi.fn(),
    onQuit: vi.fn(),
    onNotificationSelected: vi.fn(),
    onNotificationsMarkAllRead: vi.fn(),
  } satisfies TrayServiceOptions;
}

function labelsOf(template: Electron.MenuItemConstructorOptions[]): Array<string | undefined> {
  return template.map((item) => item.label);
}

describe('buildTrayMenuTemplate', () => {
  it('无未读时只有原有的打开 Crew / 卸载 / 退出', () => {
    const handlers = makeHandlers();
    const template = buildTrayMenuTemplate({ unreadCount: 0, items: [] }, handlers);
    expect(labelsOf(template)).toEqual(['打开 Crew', '卸载', undefined, '退出']);
    expect(template[2]?.type).toBe('separator');
  });

  it('有未读时顶部追加通知区：表头计数 + 条目 + 全部标为已读 + 分隔线', () => {
    const handlers = makeHandlers();
    const summary: TrayNotificationSummary = {
      unreadCount: 3,
      items: [
        { id: 'n1', title: '定时任务执行失败', sourceLabel: '定时任务' },
        { id: 'n2', title: '无来源标题' },
      ],
    };
    const template = buildTrayMenuTemplate(summary, handlers);
    expect(labelsOf(template)).toEqual([
      '通知 · 3 条未读',
      '定时任务 · 定时任务执行失败',
      '无来源标题',
      '全部标为已读',
      undefined,
      '打开 Crew',
      '卸载',
      undefined,
      '退出',
    ]);
    // 表头禁用，不可点击
    expect(template[0]?.enabled).toBe(false);
    expect(template[4]?.type).toBe('separator');
  });

  it('超长标签截断到 40 字符并补省略号', () => {
    const handlers = makeHandlers();
    const longTitle = '长'.repeat(60);
    const template = buildTrayMenuTemplate(
      { unreadCount: 1, items: [{ id: 'n1', title: longTitle }] },
      handlers,
    );
    const label = template[1]?.label ?? '';
    expect(label).toBe(`${'长'.repeat(40)}…`);
  });

  it('条目数量最多取前 5 条', () => {
    const handlers = makeHandlers();
    const items = Array.from({ length: 8 }, (_, i) => ({ id: `n${i}`, title: `通知${i}` }));
    const template = buildTrayMenuTemplate({ unreadCount: 8, items }, handlers);
    const notificationItems = template.slice(1, 1 + TRAY_MENU_MAX_ITEMS);
    expect(notificationItems.length).toBe(TRAY_MENU_MAX_ITEMS);
    expect(template[1 + TRAY_MENU_MAX_ITEMS]?.label).toBe('全部标为已读');
  });

  it('点击条目把通知 id 路由给 onNotificationSelected', () => {
    const handlers = makeHandlers();
    const template = buildTrayMenuTemplate(
      { unreadCount: 1, items: [{ id: 'n42', title: '同伴消息', sourceLabel: '同伴' }] },
      handlers,
    );
    template[1]?.click?.({} as never, {} as never, {} as never);
    expect(handlers.onNotificationSelected).toHaveBeenCalledWith('n42');
  });

  it('点击全部标为已读触发 onNotificationsMarkAllRead', () => {
    const handlers = makeHandlers();
    const template = buildTrayMenuTemplate(
      { unreadCount: 1, items: [{ id: 'n1', title: 't' }] },
      handlers,
    );
    template[2]?.click?.({} as never, {} as never, {} as never);
    expect(handlers.onNotificationsMarkAllRead).toHaveBeenCalled();
  });
});

describe('isTrayNotificationSummary', () => {
  it('接受合法摘要', () => {
    expect(isTrayNotificationSummary({
      unreadCount: 2,
      items: [{ id: 'n1', title: 't', sourceLabel: '定时任务' }, { id: 'n2', title: 'x' }],
    })).toBe(true);
    expect(isTrayNotificationSummary({ unreadCount: 0, items: [] })).toBe(true);
  });

  it('拒绝非法结构', () => {
    expect(isTrayNotificationSummary(null)).toBe(false);
    expect(isTrayNotificationSummary({ unreadCount: -1, items: [] })).toBe(false);
    expect(isTrayNotificationSummary({ unreadCount: Number.NaN, items: [] })).toBe(false);
    expect(isTrayNotificationSummary({ unreadCount: 1 })).toBe(false);
    expect(isTrayNotificationSummary({ unreadCount: 1, items: [{ id: 1, title: 't' }] })).toBe(false);
    expect(isTrayNotificationSummary({ unreadCount: 1, items: [{ id: 'n1' }] })).toBe(false);
    expect(isTrayNotificationSummary({
      unreadCount: 1,
      items: [{ id: 'n1', title: 't', sourceLabel: 3 }],
    })).toBe(false);
    expect(isTrayNotificationSummary({
      unreadCount: 6,
      items: Array.from({ length: 6 }, (_, i) => ({ id: `n${i}`, title: 't' })),
    })).toBe(false);
  });
});
