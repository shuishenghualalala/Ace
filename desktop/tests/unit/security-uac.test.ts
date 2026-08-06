// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest';

const showConfirmDialogMock = vi.hoisted(() => vi.fn(async () => true));
const notifyMock = vi.hoisted(() => vi.fn());

vi.mock('../../src/ui/state', () => ({
  notify: notifyMock,
}));
vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: showConfirmDialogMock,
}));

import { prepareWindowsSecuritySetup } from '../../src/ui/features/security-setup-flow';

describe('Windows security setup UAC flow', () => {
  beforeEach(() => {
    showConfirmDialogMock.mockClear();
    showConfirmDialogMock.mockResolvedValue(true);
    notifyMock.mockClear();
    Object.assign(window, {
      Crew: {
        securityUacStatus: vi.fn(async () => ({ enabled: false })),
        securityEnableUac: vi.fn(async () => ({ ok: true, exitCode: 0, restartRequired: true })),
      },
    });
  });

  it('enables UAC after confirmation and requires a restart before setup continues', async () => {
    await expect(prepareWindowsSecuritySetup()).resolves.toBe('restart-required');

    expect(window.Crew.securityEnableUac).toHaveBeenCalledTimes(1);
    expect(showConfirmDialogMock).toHaveBeenNthCalledWith(1, expect.objectContaining({
      title: '需要启用 Windows 用户账户控制',
      confirmText: '启用 UAC',
    }));
    expect(showConfirmDialogMock).toHaveBeenNthCalledWith(2, expect.objectContaining({
      title: 'UAC 已启用，请重启电脑',
    }));
  });

  it('does not change UAC when the user cancels', async () => {
    showConfirmDialogMock.mockResolvedValueOnce(false);

    await expect(prepareWindowsSecuritySetup()).resolves.toBe('cancelled');
    expect(window.Crew.securityEnableUac).not.toHaveBeenCalled();
  });

  it('keeps the setup blocked when UAC was enabled but the computer was not restarted', async () => {
    Object.assign(window.Crew, {
      securityUacStatus: vi.fn(async () => ({ enabled: true, restartRequired: true })),
    });

    await expect(prepareWindowsSecuritySetup()).resolves.toBe('restart-required');
    expect(window.Crew.securityEnableUac).not.toHaveBeenCalled();
    expect(showConfirmDialogMock).toHaveBeenCalledWith(expect.objectContaining({
      title: 'UAC 已启用，请重启电脑',
    }));
  });
});
