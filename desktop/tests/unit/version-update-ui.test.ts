// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { bindVersionUpdateUi, resetVersionUpdateUiForTest } from '../../src/ui/features/version-update';
import type { VersionUpdateDownloadProgressPayload, VersionUpdatePayload } from '../../src/shared/types';

function installDom(): void {
  document.body.innerHTML = `
    <button id="version-update-sidebar-btn" hidden>
      <span id="version-update-percent"></span>
    </button>
    <div id="version-install-modal" style="display: none;">
      <span id="version-install-version"></span>
      <div id="version-install-message"></div>
      <button id="version-install-now" type="button">现在安装</button>
      <button id="version-install-later" type="button">稍后</button>
    </div>
    <div id="force-update-overlay" hidden>
      <p id="force-update-message"></p>
      <div id="force-update-progress"><div id="force-update-progress-fill"></div><div id="force-update-progress-text"></div></div>
      <button id="force-update-exit" type="button">退出</button>
      <button id="force-update-action" type="button">立即更新</button>
    </div>
    <span id="set-version-badge">最新版</span>
  `;
}

describe('version update UI', () => {
  let updateHandler: ((payload: VersionUpdatePayload) => void) | undefined;
  let progressHandler: ((payload: VersionUpdateDownloadProgressPayload) => void) | undefined;
  let startDownload: ReturnType<typeof vi.fn>;
  let pauseDownload: ReturnType<typeof vi.fn>;
  let resumeDownload: ReturnType<typeof vi.fn>;
  let installUpdatePackage: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.clearAllMocks();
    resetVersionUpdateUiForTest();
    installDom();
    startDownload = vi.fn(async () => ({ success: true }));
    pauseDownload = vi.fn(async () => ({ success: true }));
    resumeDownload = vi.fn(async () => ({ success: true }));
    installUpdatePackage = vi.fn(async () => ({ success: true }));
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        onVersionUpdate: vi.fn((cb: (payload: VersionUpdatePayload) => void) => {
          updateHandler = cb;
          return () => undefined;
        }),
        onVersionUpdateDownloadProgress: vi.fn((cb: (payload: VersionUpdateDownloadProgressPayload) => void) => {
          progressHandler = cb;
          return () => undefined;
        }),
        startDownload,
        pauseDownload,
        resumeDownload,
        installUpdatePackage,
        getUpdateState: vi.fn(async () => ({ downloaded: null, forceLock: null })),
        getAppVersion: vi.fn(async () => ({ version: '0.23.57', label: 'Crew Desktop 0.23.57' })),
      },
    });
  });

  it('reminder: 点击下载 → 暂停可点 → 下载完成弹安装确认 → 安装调用 install', async () => {
    const button = document.getElementById('version-update-sidebar-btn') as HTMLButtonElement;
    bindVersionUpdateUi();

    updateHandler?.({
      type: 'reminder',
      title: '提示',
      message: '建议更新',
      version: '0.23.59',
      reportedVersion: 'Crew Desktop 0.23.57',
    });

    expect(button.hidden).toBe(false);
    expect(button.getAttribute('aria-label')).toContain('0.23.59');

    // 开始下载
    button.click();
    await Promise.resolve();
    expect(startDownload).toHaveBeenCalledWith({ version: '0.23.59', type: 'reminder' });
    progressHandler?.({ phase: 'downloading', percent: 42, receivedBytes: 42, totalBytes: 100 });
    expect(button.classList.contains('is-downloading')).toBe(true);
    expect(button.disabled).toBe(false); // 下载中仍可点击（=暂停）

    // 下载中点击 → 暂停
    button.click();
    await Promise.resolve();
    expect(pauseDownload).toHaveBeenCalled();

    // 下载完成 → 弹安装确认
    progressHandler?.({ phase: 'downloaded', percent: 100 });
    await Promise.resolve();
    const modal = document.getElementById('version-install-modal') as HTMLElement;
    expect(modal.classList.contains('show')).toBe(true);

    document.getElementById('version-install-now')?.click();
    await Promise.resolve();
    expect(installUpdatePackage).toHaveBeenCalledWith(); // 无参：主进程从 update-state 读路径
  });

  it('reminder 同版本心跳去重：下载中再次推送不重置 phase', async () => {
    const button = document.getElementById('version-update-sidebar-btn') as HTMLButtonElement;
    bindVersionUpdateUi();

    updateHandler?.({ type: 'reminder', title: '提示', message: 'v1', version: '0.23.59', reportedVersion: 'x' });
    button.click();
    await Promise.resolve();
    progressHandler?.({ phase: 'downloading', percent: 50 });
    expect(button.classList.contains('is-downloading')).toBe(true);

    // 每 5min 心跳再次推送同版本 → 不应回到 available
    updateHandler?.({ type: 'reminder', title: '提示', message: 'v2', version: '0.23.59', reportedVersion: 'x' });
    expect(button.classList.contains('is-downloading')).toBe(true);
  });

  it('force: 盖阻断层，下载完成自动安装（无需用户再点）', async () => {
    bindVersionUpdateUi();
    const overlay = document.getElementById('force-update-overlay') as HTMLElement;
    const button = document.getElementById('version-update-sidebar-btn') as HTMLButtonElement;

    updateHandler?.({ type: 'force', title: '提示', message: '必须更新', version: '0.23.59', reportedVersion: 'x' });
    expect(overlay.hidden).toBe(false); // 阻断层显示
    expect(button.hidden).toBe(true); // reminder 按钮不显示

    // 点击「立即更新」→ 开始下载
    document.getElementById('force-update-action')?.click();
    await Promise.resolve();
    expect(startDownload).toHaveBeenCalledWith({ version: '0.23.59', type: 'force' });

    // 下载完成 → force 自动安装
    progressHandler?.({ phase: 'downloaded', percent: 100 });
    await Promise.resolve();
    expect(installUpdatePackage).toHaveBeenCalled();
  });
});
