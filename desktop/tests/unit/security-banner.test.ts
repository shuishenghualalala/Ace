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
  it('reports on when macOS Seatbelt and managed networking are active', () => {
    expect(deriveState({ platform: 'darwin', helper_present: true, filesystem_sandbox: true, managed_network: true })).toBe('on');
  });

  it('reports incomplete instead of offering Windows setup on macOS', () => {
    expect(deriveState({ platform: 'darwin', helper_present: true, filesystem_sandbox: false, managed_network: false })).toBe('mac-incomplete');
  });

  it('reports missing when the helper binary is absent', () => {
    expect(deriveState({ platform: 'win32', helper_present: false })).toBe('missing');
  });

  it('does not show a platform-specific setup prompt before detection completes', () => {
    expect(deriveState({ helper_present: false })).toBe('hidden');
  });

  it('reports stale when the runtime binary lags behind Rust source', () => {
    expect(deriveState({ platform: 'win32', helper_present: true, runtime_stale: true, filesystem_sandbox: true, managed_network: true })).toBe('stale');
  });

  it('reports off when the filesystem sandbox is not enabled', () => {
    expect(deriveState({ platform: 'win32', helper_present: true, filesystem_sandbox: false, managed_network: false })).toBe('off');
  });

  it('reports off for the backend Windows platform name when setup is missing', () => {
    expect(deriveState({ platform: 'windows', helper_present: true, filesystem_sandbox: false, managed_network: false })).toBe('off');
  });

  it('asks for a service restart instead of reinstalling when the gateway missed the sandbox state directory', () => {
    expect(deriveState({
      platform: 'windows',
      helper_present: true,
      filesystem_sandbox: false,
      managed_network: false,
      state_dir_configured: false,
    })).toBe('service-restart-required');
  });

  it('keeps the install prompt ahead of stale diagnostics when the sandbox is not ready', () => {
    expect(deriveState({
      platform: 'windows',
      helper_present: true,
      runtime_stale: true,
      filesystem_sandbox: false,
      managed_network: false,
    })).toBe('off');
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

  it('verifies capabilities after setup instead of claiming the sandbox is ready', async () => {
    const securitySetup = vi.fn(async () => ({ ok: true, exitCode: 0 }));
    const securityCapabilities = vi.mocked(window.Crew.securityCapabilities!);
    securityCapabilities
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        body: {
          platform: 'windows',
          helper_present: true,
          filesystem_sandbox: false,
          managed_network: false,
        },
      })
      .mockResolvedValue({
        ok: true,
        status: 200,
        body: {
          platform: 'windows',
          helper_present: true,
          filesystem_sandbox: false,
          managed_network: false,
          state_dir_configured: false,
          detail: '当前 Gateway 未加载安全状态目录，请重启 Crew 后再试',
        },
      });
    Object.assign(window.Crew, { securitySetup });

    await refreshSecurityBanner();
    document.querySelector<HTMLButtonElement>('[data-action="enable"]')?.click();

    await vi.waitFor(() => expect(
      document.querySelector('.security-banner__title')?.textContent,
    ).toBe('安全服务需要重启'));
  });
});
