import { beforeEach, describe, expect, it, vi } from 'vitest';

const electron = vi.hoisted(() => ({
  response: 0,
  showMessageBox: vi.fn(),
}));

vi.mock('electron', () => ({
  BrowserWindow: class {},
  dialog: {
    showMessageBox: electron.showMessageBox,
  },
}));

import {
  confirmDangerousAction,
  confirmFullAccessMode,
} from '../../src/main/host-authority-dialog';

describe('full-access host confirmation', () => {
  beforeEach(() => {
    electron.response = 0;
    electron.showMessageBox.mockReset();
    electron.showMessageBox.mockImplementation(async () => ({
      response: electron.response,
      checkboxChecked: false,
    }));
  });

  it('fails closed without a live parent window', async () => {
    await expect(confirmFullAccessMode(null)).resolves.toBe(false);
    await expect(
      confirmFullAccessMode({ isDestroyed: () => true } as never),
    ).resolves.toBe(false);
    expect(electron.showMessageBox).not.toHaveBeenCalled();
  });

  it('accepts only the explicit destructive confirmation button', async () => {
    const parent = { isDestroyed: () => false } as never;
    await expect(confirmFullAccessMode(parent)).resolves.toBe(false);

    electron.response = 1;
    await expect(confirmFullAccessMode(parent)).resolves.toBe(true);
    expect(electron.showMessageBox).toHaveBeenCalledTimes(2);
    expect(electron.showMessageBox.mock.calls[1]?.[1]).toMatchObject({
      defaultId: 0,
      cancelId: 0,
      noLink: true,
    });
  });

  it('requires the same fail-closed native confirmation for dangerous actions', async () => {
    const parent = { isDestroyed: () => false } as never;
    await expect(confirmDangerousAction(parent)).resolves.toBe(false);
    electron.response = 1;
    await expect(confirmDangerousAction(parent)).resolves.toBe(true);
    expect(electron.showMessageBox.mock.calls[1]?.[1]).toMatchObject({
      title: '再次确认高风险操作',
      defaultId: 0,
      cancelId: 0,
    });
  });
});
