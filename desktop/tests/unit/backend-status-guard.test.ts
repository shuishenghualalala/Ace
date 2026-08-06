// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const recoveryMocks = vi.hoisted(() => ({
  loadConfig: vi.fn(async () => {}),
}));

vi.mock('../../src/ui/features/model-picker', () => ({
  loadConfig: recoveryMocks.loadConfig,
}));

type StatusCb = (s: {
  connected: boolean;
  logPath?: string;
  components?: Record<string, { status: string; message?: string }>;
}) => void;

describe('backend-status-guard slow overlay', () => {
  let statusCb: StatusCb | null = null;
  let retryCalled = 0;
  let initialStatus: StatusCb extends (s: infer S) => void ? S : never;

  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    statusCb = null;
    retryCalled = 0;
    initialStatus = { connected: false };
    recoveryMocks.loadConfig.mockClear();
    document.body.innerHTML = `
      <div id="backend-loading-overlay">
        <p id="backend-loading-elapsed"></p>
        <div id="backend-loading-actions"></div>
        <button id="backend-loading-log"></button>
        <button id="backend-loading-retry"></button>
        <button id="backend-loading-dismiss"></button>
      </div>`;
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        onBackendStatus: (cb: StatusCb) => {
          statusCb = cb;
          return () => {};
        },
        getBackendStatus: async () => initialStatus,
        retryGateway: () => {
          retryCalled += 1;
        },
        openPath: vi.fn(),
      },
    });
  });

  async function loadGuard(): Promise<void> {
    const mod = await import('../../src/ui/features/backend-status-guard');
    mod.initBackendStatusGuard();
  }

  it('shows slow actions only after the 20s threshold; dismiss hides them', async () => {
    await loadGuard();
    expect(statusCb).toBeTruthy();
    statusCb!({ connected: false, logPath: '/tmp/gw.log' });

    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('none');

    vi.advanceTimersByTime(21_000);
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('');
    expect(document.getElementById('backend-loading-elapsed')!.textContent).toContain('仍在准备中');

    document.getElementById('backend-loading-dismiss')!.click();
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('none');
  });

  it('retry calls the gateway and resets the slow clock', async () => {
    await loadGuard();
    statusCb!({ connected: false });
    vi.advanceTimersByTime(21_000);
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('');

    document.getElementById('backend-loading-retry')!.click();
    expect(retryCalled).toBe(1);
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('none');

    vi.advanceTimersByTime(10_000);
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('none');
    vi.advanceTimersByTime(12_000);
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('');
  });

  it('connected hides the overlay and stops the slow timer', async () => {
    await loadGuard();
    statusCb!({ connected: false });
    vi.advanceTimersByTime(21_000);
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('');

    statusCb!({ connected: true });
    expect(document.getElementById('backend-loading-overlay')!.style.display).toBe('none');
    expect(document.getElementById('backend-loading-actions')!.style.display).toBe('none');
  });

  it('hydrates the current status when the ready event was sent before subscription', async () => {
    initialStatus = { connected: true, logPath: '/tmp/gw.log' };
    await loadGuard();
    await vi.waitFor(() => {
      expect(document.getElementById('backend-loading-overlay')!.style.display).toBe('none');
    });
  });

  it('reloads model and feature config when the Gateway becomes available again', async () => {
    await loadGuard();
    statusCb!({ connected: false });
    statusCb!({ connected: true });

    await vi.waitFor(() => expect(recoveryMocks.loadConfig).toHaveBeenCalledOnce());
  });

  it('keeps the Gateway usable and shows one non-blocking cron failure notice', async () => {
    await loadGuard();
    const failed = {
      connected: true,
      components: {
        cron: { status: 'failed', message: '定时任务启动失败，请查看 Gateway 日志' },
      },
    };

    statusCb!(failed);
    statusCb!(failed);
    await vi.advanceTimersByTimeAsync(20);

    expect(document.getElementById('backend-loading-overlay')!.style.display).toBe('none');
    expect(Array.from(document.querySelectorAll('.ui-toast')).map((el) => el.textContent))
      .toEqual(['定时任务启动失败，请查看 Gateway 日志']);
  });

  it('shows a non-blocking notice for a general deferred startup failure', async () => {
    await loadGuard();

    statusCb!({
      connected: true,
      components: {
        startup: { status: 'failed', message: '运行环境组件初始化失败，请查看 Gateway 日志' },
      },
    });
    await vi.advanceTimersByTimeAsync(20);

    expect(document.getElementById('backend-loading-overlay')!.style.display).toBe('none');
    expect(document.querySelector('.ui-toast')?.textContent)
      .toBe('运行环境组件初始化失败，请查看 Gateway 日志');
  });

  afterEach(() => {
    vi.useRealTimers();
  });
});
