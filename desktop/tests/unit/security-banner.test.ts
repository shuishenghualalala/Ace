// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const showConfirmDialogMock = vi.hoisted(() => vi.fn(async () => true));

vi.mock('../../src/ui/state', () => ({
  notify: vi.fn(),
  state: { activeSessionId: null, sessions: [] },
}));
vi.mock('../../src/ui/ui-feedback', () => ({
  showConfirmDialog: showConfirmDialogMock,
}));

import {
  deriveState,
  refreshSecurityBanner,
  stopSecurityBannerRefresh,
} from '../../src/ui/features/security-banner';

beforeEach(() => {
  showConfirmDialogMock.mockClear();
  showConfirmDialogMock.mockResolvedValue(true);
  document.body.innerHTML = '<div class="chat-composer"><div class="composer-edit-banner"></div></div>';
  Object.assign(window, {
    Crew: {
      securityCapabilities: vi.fn(async () => ({
        ok: true,
        body: {
          platform: 'windows',
          helper_present: true,
          runtime_stale: true,
          filesystem_sandbox: false,
          managed_network: false,
        },
      })),
    },
  });
});

afterEach(() => {
  stopSecurityBannerRefresh();
  document.body.replaceChildren();
});

describe('security banner deriveState', () => {
  type Caps = Parameters<typeof deriveState>[0];

  it.each([
    {
      name: 'hides on non-win32 platforms',
      caps: { platform: 'darwin', helper_present: true, filesystem_sandbox: true, managed_network: true },
      expected: 'hidden',
    },
    {
      name: 'reports missing when the helper binary is absent',
      caps: { platform: 'win32', helper_present: false },
      expected: 'missing',
    },
    {
      name: 'reports stale when the runtime binary lags behind Rust source',
      caps: { platform: 'win32', helper_present: true, runtime_stale: true, filesystem_sandbox: true, managed_network: true },
      expected: 'stale',
    },
    {
      name: 'reports off when the filesystem sandbox is not enabled',
      caps: { platform: 'win32', helper_present: true, filesystem_sandbox: false, managed_network: false },
      expected: 'off',
    },
    {
      name: 'reports off for the backend Windows platform name when setup is missing',
      caps: { platform: 'windows', helper_present: true, filesystem_sandbox: false, managed_network: false },
      expected: 'off',
    },
    {
      name: 'keeps the install prompt ahead of stale diagnostics when the sandbox is not ready',
      caps: { platform: 'windows', helper_present: true, runtime_stale: true, filesystem_sandbox: false, managed_network: false },
      expected: 'off',
    },
    {
      name: 'reports on when both filesystem and network controls are active',
      caps: { platform: 'win32', helper_present: true, filesystem_sandbox: true, managed_network: true },
      expected: 'on',
    },
    // U3: WFP 缺失时不能显示 on 让用户以为出网受控。
    {
      name: 'reports net-missing when filesystem sandbox is on but WFP network control is absent (U3)',
      caps: { platform: 'win32', helper_present: true, filesystem_sandbox: true, managed_network: false },
      expected: 'net-missing',
    },
    // 兼容老 gateway 未返回 managed_network 字段的情况：保守视为缺失，
    // 与 security-mode.ts 的 formatCapabilitySummary 一致（falsy -> 不完整）。
    {
      name: 'reports net-missing when managed_network is undefined but filesystem sandbox is on',
      caps: { platform: 'win32', helper_present: true, filesystem_sandbox: true },
      expected: 'net-missing',
    },
  ] as Array<{ name: string; caps: Caps; expected: string }>)('$name', ({ caps, expected }) => {
    expect(deriveState(caps)).toBe(expected);
  });

  it('shows the sandbox install prompt above the composer', async () => {
    await refreshSecurityBanner();

    const banner = document.getElementById('security-sandbox-banner');
    expect(banner?.classList.contains('show')).toBe(true);
    expect(banner?.querySelector('.security-banner__title')?.textContent)
      .toBe('请安装安全沙箱');
    expect(banner?.querySelector('.security-banner__text')?.textContent)
      .toContain('限制命令对本机文件和网络的访问');
    expect(banner?.querySelector<HTMLButtonElement>('[data-action="enable"]')?.textContent)
      .toBe('安装安全沙箱');
    expect(banner?.nextElementSibling?.classList.contains('composer-edit-banner')).toBe(true);
  });

  it('uses the in-app confirmation before requesting administrator access', async () => {
    const securitySetup = vi.fn(async () => ({ ok: true, exitCode: 0 }));
    Object.assign(window.Crew, { securitySetup });

    await refreshSecurityBanner();
    document.querySelector<HTMLButtonElement>('[data-action="enable"]')?.click();
    await vi.waitFor(() => expect(showConfirmDialogMock).toHaveBeenCalledWith(expect.objectContaining({
      title: '安装安全沙箱',
      confirmText: '安装并继续',
    })));

    await vi.waitFor(() => expect(securitySetup).toHaveBeenCalledWith({ action: 'install' }));
  });
});
