import { Menu, nativeImage, Tray } from 'electron';
import * as path from 'path';
import type { TrayStatus } from '../shared/types';

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
  onNearby: () => void;
  onUninstall: () => void;
  onQuit: () => void;
}

/**
 * Desktop tray 的唯一资源入口。
 * 状态图片、macOS 缩放和模板策略集中在这里，避免 main/index.ts 继续膨胀。
 */
export class TrayService {
  private tray: Tray | null = null;
  private status: TrayStatus = 'default';

  public constructor(private readonly options: TrayServiceOptions) {}

  public create(): void {
    if (this.tray) return;
    this.tray = new Tray(this.resolveIcon(this.status));
    this.tray.setToolTip(STATUS_LABELS[this.status]);
    this.tray.setContextMenu(Menu.buildFromTemplate([
      { label: '打开 Crew', click: () => this.options.onActivate() },
      { label: '同伴', click: () => this.options.onNearby() },
      { label: '卸载', click: () => this.options.onUninstall() },
      { type: 'separator' },
      { label: '退出', click: () => this.options.onQuit() },
    ]));
    this.tray.on('double-click', () => this.options.onActivate());
    this.tray.on('click', () => this.options.onActivate());
  }

  public setStatus(status: TrayStatus): void {
    this.status = status;
    if (!this.tray) return;
    this.tray.setImage(this.resolveIcon(status));
    this.tray.setToolTip(STATUS_LABELS[status]);
  }

  public getStatus(): TrayStatus {
    return this.status;
  }

  public dispose(): void {
    this.tray?.destroy();
    this.tray = null;
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
