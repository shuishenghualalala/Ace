import { Menu, nativeImage, Tray } from 'electron';
import * as path from 'path';
import type { TrayNotificationSummary, TrayStatus } from '../shared/types';

/** 托盘菜单展示的未读通知条数上限。 */
export const TRAY_MENU_MAX_ITEMS = 5;

/** 托盘菜单单行标签长度上限，超出截断并补省略号。 */
const TRAY_MENU_LABEL_MAX = 40;

const STATUS_ASSETS: Record<TrayStatus, string> = {
  default: 'default.png',
  working: 'working.png',
  notification: 'notification.png',
  done: 'done.png',
  rest: 'rest.png',
};

const STATUS_LABELS: Record<TrayStatus, string> = {
  default: 'Crew · 等待指令',
  working: 'Crew · 工作中',
  notification: 'Crew · 有通知',
  done: 'Crew · 已完成',
  rest: 'Crew · 休眠',
};

const STATUS_OPTICAL_SCALE: Record<TrayStatus, number> = {
  default: 1,
  working: 1.02,
  notification: 1,
  done: 1.16,
  rest: 1,
};

export function trayIconOpticalScale(status: TrayStatus): number {
  return STATUS_OPTICAL_SCALE[status];
}

export interface TrayServiceOptions {
  assetsDir: string;
  onActivate: () => void;
  onUninstall: () => void;
  onQuit: () => void;
  /** 托盘菜单点击某条通知：主进程唤醒窗口并转发给 Renderer 标记已读 + 跳转。 */
  onNotificationSelected: (id: string) => void;
  /** 托盘菜单「全部标为已读」：主进程唤醒窗口并转发给 Renderer 复用全部已读逻辑。 */
  onNotificationsMarkAllRead: () => void;
}

function truncateMenuLabel(text: string): string {
  return text.length > TRAY_MENU_LABEL_MAX
    ? `${text.slice(0, TRAY_MENU_LABEL_MAX)}…`
    : text;
}

/**
 * 托盘菜单模板：纯函数，便于在无 Electron 环境单测。
 * 有未读时在顶部追加通知区（禁用表头 + 最近未读 + 全部标为已读 + 分隔线），
 * 原有的打开 Crew / 卸载 / 退出保持不变。
 */
export function buildTrayMenuTemplate(
  summary: TrayNotificationSummary,
  handlers: TrayServiceOptions,
): Electron.MenuItemConstructorOptions[] {
  const template: Electron.MenuItemConstructorOptions[] = [];
  if (summary.unreadCount > 0) {
    template.push({ label: `通知 · ${summary.unreadCount} 条未读`, enabled: false });
    for (const item of summary.items.slice(0, TRAY_MENU_MAX_ITEMS)) {
      const label = item.sourceLabel ? `${item.sourceLabel} · ${item.title}` : item.title;
      template.push({
        label: truncateMenuLabel(label),
        click: () => handlers.onNotificationSelected(item.id),
      });
    }
    template.push({ label: '全部标为已读', click: () => handlers.onNotificationsMarkAllRead() });
    template.push({ type: 'separator' });
  }
  template.push(
    { label: '打开 Crew', click: () => handlers.onActivate() },
    { label: '卸载', click: () => handlers.onUninstall() },
    { type: 'separator' },
    { label: '退出', click: () => handlers.onQuit() },
  );
  return template;
}

/** Renderer 上报的托盘通知摘要结构校验（IPC 边界）。 */
export function isTrayNotificationSummary(value: unknown): value is TrayNotificationSummary {
  if (!value || typeof value !== 'object') return false;
  const candidate = value as Partial<TrayNotificationSummary>;
  if (typeof candidate.unreadCount !== 'number'
    || !Number.isFinite(candidate.unreadCount)
    || candidate.unreadCount < 0) return false;
  if (!Array.isArray(candidate.items) || candidate.items.length > TRAY_MENU_MAX_ITEMS) return false;
  return candidate.items.every((item) => {
    if (!item || typeof item !== 'object') return false;
    const entry = item as Partial<{ id: unknown; title: unknown; sourceLabel: unknown }>;
    return typeof entry.id === 'string'
      && typeof entry.title === 'string'
      && (entry.sourceLabel === undefined || typeof entry.sourceLabel === 'string');
  });
}

/**
 * Desktop tray 的唯一资源入口。
 * 状态图片、macOS 缩放和模板策略集中在这里，避免 main/index.ts 继续膨胀。
 */
export class TrayService {
  private tray: Tray | null = null;
  private status: TrayStatus = 'default';
  private notificationSummary: TrayNotificationSummary = { unreadCount: 0, items: [] };

  public constructor(private readonly options: TrayServiceOptions) {}

  public create(): void {
    if (this.tray) return;
    this.tray = new Tray(this.resolveIcon(this.status));
    this.tray.setToolTip(STATUS_LABELS[this.status]);
    this.rebuildMenu();
    this.tray.on('double-click', () => this.options.onActivate());
    this.tray.on('click', () => this.options.onActivate());
  }

  public setStatus(status: TrayStatus): void {
    this.status = status;
    if (!this.tray) return;
    this.tray.setImage(this.resolveIcon(status));
    this.tray.setToolTip(STATUS_LABELS[status]);
  }

  /** 通知状态变化时由 Renderer 推送摘要，立即重建菜单避免展示过期未读。 */
  public setNotifications(summary: TrayNotificationSummary): void {
    this.notificationSummary = summary;
    this.rebuildMenu();
  }

  public getStatus(): TrayStatus {
    return this.status;
  }

  public dispose(): void {
    this.tray?.destroy();
    this.tray = null;
  }

  private rebuildMenu(): void {
    if (!this.tray) return;
    this.tray.setContextMenu(Menu.buildFromTemplate(
      buildTrayMenuTemplate(this.notificationSummary, this.options),
    ));
  }

  private resolveIcon(status: TrayStatus): Electron.NativeImage {
    const imagePath = path.join(this.options.assetsDir, 'menubar', STATUS_ASSETS[status]);
    const source = nativeImage.createFromPath(imagePath);
    if (source.isEmpty()) return nativeImage.createEmpty();

    if (process.platform !== 'darwin') return source;
    // macOS Retina 菜单栏按 2x 像素密度绘制。先生成 44px 位图，再以
    // scaleFactor=2 注册为 22pt 图像，避免把 22 个物理像素直接放大。
    const targetSize = 44;
    const opticalSize = Math.round(targetSize * trayIconOpticalScale(status));
    const resized = source.resize({ width: opticalSize, height: opticalSize, quality: 'best' });
    // done 素材右侧包含庆祝星光，主体机器人占比比其他状态小。放大后从左侧
    // 保留完整主体，并向上校正；其余状态只做居中的亚像素级光学校正。
    const cropX = status === 'done' ? 0 : Math.floor((opticalSize - targetSize) / 2);
    const cropY = status === 'done'
      ? opticalSize - targetSize
      : Math.floor((opticalSize - targetSize) / 2);
    const retinaBitmap = opticalSize === targetSize
      ? resized
      : resized.crop({ x: cropX, y: cropY, width: targetSize, height: targetSize });
    const image = nativeImage.createFromBuffer(retinaBitmap.toPNG(), { scaleFactor: 2 });
    // default/rest 使用仅保留黑色线稿的透明 PNG，可安全交给 macOS
    // 按系统主题着色；其余三态保留原始彩色情感反馈。
    image.setTemplateImage(status === 'default' || status === 'rest');
    return image;
  }
}

export function isTrayStatus(value: unknown): value is TrayStatus {
  return value === 'default'
    || value === 'working'
    || value === 'notification'
    || value === 'done'
    || value === 'rest';
}
