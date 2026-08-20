import { describe, expect, it, vi } from 'vitest';

const { buildFromTemplate, trayInstances } = vi.hoisted(() => ({
  buildFromTemplate: vi.fn(),
  trayInstances: [] as Array<{ menu: unknown }>,
}));

vi.mock('electron', () => ({
  Menu: { buildFromTemplate },
  nativeImage: {
    createFromPath: () => ({ isEmpty: () => true }),
    createEmpty: () => ({ isEmpty: () => true }),
  },
  Tray: class {
    public menu: unknown;
    public constructor() { trayInstances.push(this); }
    public setToolTip(): void {}
    public setContextMenu(menu: unknown): void { this.menu = menu; }
    public on(): void {}
    public destroy(): void {}
  },
}));

import { TrayService } from '../../src/main/tray-service';

describe('nearby tray entry', () => {
  it('adds the same 同伴 action to the cross-platform tray menu', () => {
    const onNearby = vi.fn();
    new TrayService({
      assetsDir: '/tmp',
      onActivate: vi.fn(),
      onNearby,
      onUninstall: vi.fn(),
      onQuit: vi.fn(),
    }).create();
    expect(buildFromTemplate).toHaveBeenCalled();
    const template = buildFromTemplate.mock.calls.at(-1)?.[0] as Array<{ label?: string; click?: () => void }>;
    const item = template.find((entry) => entry.label === '同伴');
    expect(item).toBeDefined();
    item?.click?.();
    expect(onNearby).toHaveBeenCalledTimes(1);
    expect(trayInstances.length).toBeGreaterThan(0);
  });
});
