/**
 * Work 高价值桌面通知：仅通知批准/关键事件；DND 时间窗 + 来源开关过滤；
 * 点击打开对应事项/模式；平台不支持或权限未授予时安静降级。
 * 纯 Renderer 实现，复用 Web Notification 平台能力，不新增 IPC。
 */

import { workApi } from '../../backend-client';
import type { WorkDashboard, WorkItem } from '../../backend-client';
import { loadWorkDashboard, loadWorkItems, workStore } from '../../stores/work-store';
import { markSystemTrayNotification } from '../system-tray';

/** 通知设置快照（来自 /api/work/settings）。 */
export interface NotificationSettings {
  dnd_enabled: boolean;
  dnd_start: string | null; // "HH:MM"
  dnd_end: string | null;
  source_notifications: Record<string, boolean>;
}

const DEFAULT_SETTINGS: NotificationSettings = {
  dnd_enabled: false,
  dnd_start: null,
  dnd_end: null,
  source_notifications: {},
};

/** 拉取当前通知设置。 */
export async function loadNotificationSettings(): Promise<NotificationSettings> {
  const raw = await workApi.getSettings();
  return {
    dnd_enabled: Boolean(raw.dnd_enabled),
    dnd_start: (raw.dnd_start as string | null) ?? null,
    dnd_end: (raw.dnd_end as string | null) ?? null,
    source_notifications: (raw.source_notifications as Record<string, boolean>) ?? {},
  };
}

/** 更新 DND / 来源通知设置。 */
export async function saveNotificationSettings(settings: NotificationSettings): Promise<void> {
  await workApi.putSettings({
    dnd_enabled: settings.dnd_enabled,
    dnd_start: settings.dnd_start,
    dnd_end: settings.dnd_end,
    source_notifications: settings.source_notifications,
  });
}

/** 当前时间是否落在 DND 时间窗内（支持跨午夜，如 22:00→07:00）。 */
export function isWithinDndWindow(
  dndStart: string | null,
  dndEnd: string | null,
  now: Date = new Date(),
): boolean {
  if (!dndStart || !dndEnd) return false;
  const cur = now.getHours() * 60 + now.getMinutes();
  const [sh, sm] = dndStart.split(':').map(Number);
  const [eh, em] = dndEnd.split(':').map(Number);
  const start = sh * 60 + sm;
  const end = eh * 60 + em;
  if (start === end) return false;
  if (start < end) return cur >= start && cur < end;
  // 跨午夜：end < start
  return cur >= start || cur < end;
}

/** 综合判断：DND + 来源开关。返回是否应发出通知。 */
export function shouldNotify(
  settings: NotificationSettings,
  sourceKey: string,
  now: Date = new Date(),
): boolean {
  if (isWithinDndWindow(settings.dnd_start, settings.dnd_end, now)) return false;
  const sourceFlag = settings.source_notifications[sourceKey];
  // 默认允许（未显式关闭则通知），除非显式设为 false。
  return sourceFlag !== false;
}

/** 平台是否支持桌面通知。 */
export function notificationSupported(): boolean {
  return typeof Notification !== 'undefined';
}

/** 当前通知权限是否已授予。 */
export function notificationPermission(): NotificationPermission {
  if (!notificationSupported()) return 'denied';
  return Notification.permission;
}

/** 点击通知时的回调（由 app 层注入：打开事项 / 切换模式）。 */
type ItemOpener = (itemId: string) => void;
let openItem: ItemOpener | null = null;

/** 注册点击通知后打开事项的回调。 */
export function setNotificationClickHandler(handler: ItemOpener | null): void {
  openItem = handler;
}

function dashboardItems(dashboard: WorkDashboard | null, key: string): WorkItem[] {
  const value = dashboard?.brief?.content[key];
  return Array.isArray(value) ? value as WorkItem[] : [];
}

/** 消费 Gateway 的 WorkItem 失效事件，重算列表/简报并只通知新增高价值事项。 */
export async function handleWorkItemEvent(): Promise<void> {
  const before = workStore.get().dashboard;
  const workspaceId = workStore.get().selectedWorkspaceId;
  await workApi.refreshDashboard(workspaceId);
  await Promise.all([loadWorkItems(), loadWorkDashboard()]);
  if (!before) return;
  const after = workStore.get().dashboard;
  const settings = await loadNotificationSettings();
  for (const [key, title] of [['overdue_items', '事项已逾期'], ['pending_confirmations', '事项等待确认']] as const) {
    const previousIds = new Set(dashboardItems(before, key).map((item) => item.item_id));
    for (const item of dashboardItems(after, key)) {
      if (!previousIds.has(item.item_id)) showNotification(title, item.title, item.item_id, settings);
    }
  }
}

/**
 * 发出一条高价值通知。平台不支持或权限未授予时安静降级。
 * 仅通知批准/关键事件——调用方自行判断事件是否属于高价值。
 */
export function showNotification(
  title: string,
  body: string,
  itemId: string | null,
  settings: NotificationSettings = DEFAULT_SETTINGS,
  sourceKey = 'default',
): boolean {
  if (!notificationSupported()) return false;
  if (notificationPermission() !== 'granted') return false;
  if (!shouldNotify(settings, sourceKey)) return false;
  const n = new Notification(title, { body, tag: itemId ?? title });
  markSystemTrayNotification();
  n.addEventListener('click', () => {
    if (itemId) openItem?.(itemId);
  });
  return true;
}

export { DEFAULT_SETTINGS };
