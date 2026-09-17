// @vitest-environment happy-dom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const recoveryMocks = vi.hoisted(() => ({
  loadConfig: vi.fn(async () => {}),
}));

vi.mock('../../src/ui/features/model-picker', () => ({
  loadConfig: recoveryMocks.loadConfig,
}));

type StatusPayload = {
  connected: boolean;
  logPath?: string;
  components?: Record<string, { status: string; message?: string }>;
  failureKind?: 'unreachable' | 'timeout' | 'auth_failed' | 'unknown';
  since?: number;
};

type StatusCb = (s: StatusPayload) => void;

const BANNER_ID = 'backend-status-banner';

function banner(): HTMLElement {
  return document.getElementById(BANNER_ID) as HTMLElement;
}

function bannerTitle(): string {
  return banner().querySelector('.backend-banner__title')!.textContent ?? '';
}

function bannerText(): string {
  return banner().querySelector('.backend-banner__text')!.textContent ?? '';
}

function clickAction(action: string): void {
  banner().querySelector(`.backend-banner__btn[data-action="${action}"]`)!
    .dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

describe('backend-status-guard 状态横幅', () => {
  let statusCb: StatusCb | null = null;
  let retryCalled = 0;
  let initialStatus: StatusPayload;

  beforeEach(() => {
    vi.resetModules();
    vi.useFakeTimers();
    statusCb = null;
    retryCalled = 0;
    initialStatus = { connected: false };
    recoveryMocks.loadConfig.mockClear();
    document.body.innerHTML = `
      <main class="main-content">
        <div class="top-bar"></div>
        <section id="chat-tab" class="tab-pane active"></section>
      </main>`;
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        onBackendStatus: (cb: StatusCb) => {
          statusCb = cb;
          return () => {
            statusCb = null;
          };
        },
        getBackendStatus: async () => initialStatus,
        retryGateway: () => {
          retryCalled += 1;
        },
        openPath: vi.fn(),
      },
    });
  });

  async function loadGuard(): Promise<{
    initBackendStatusGuard: () => void;
    disposeBackendStatusGuard: () => void;
  }> {
    const mod = await import('../../src/ui/features/backend-status-guard');
    mod.initBackendStatusGuard();
    return mod;
  }

  it('init 后在页面顶部展示启动中横幅', async () => {
    await loadGuard();
    expect(statusCb).toBeTruthy();
    const el = banner();
    expect(el.classList.contains('show')).toBe(true);
    expect(el.classList.contains('is-info')).toBe(true);
    expect(bannerTitle()).toBe('正在启动后端');
    expect(bannerText()).toContain('准备中');
    // 挂载在 main-content 顶部
    expect(document.querySelector('.main-content')!.firstElementChild).toBe(el);
  });

  it('timeout 分支展示「后端繁忙」并按 since 秒级刷新', async () => {
    await loadGuard();
    const since = Date.now() - 3000;
    statusCb!({ connected: false, failureKind: 'timeout', since });

    expect(banner().classList.contains('is-warn')).toBe(true);
    expect(bannerTitle()).toBe('后端繁忙');
    expect(bannerText()).toContain('已等待 3 秒');

    vi.advanceTimersByTime(2000);
    expect(bannerText()).toContain('已等待 5 秒');
  });

  it('unreachable 分支提示正在自动重启', async () => {
    await loadGuard();
    statusCb!({ connected: false, failureKind: 'unreachable', since: Date.now() });

    expect(banner().classList.contains('is-warn')).toBe(true);
    expect(bannerTitle()).toBe('后端无响应');
    expect(bannerText()).toContain('自动重启');
  });

  it('auth_failed 与 unknown 分支提示后端连接异常', async () => {
    await loadGuard();
    statusCb!({ connected: false, failureKind: 'auth_failed', since: Date.now() });
    expect(banner().classList.contains('is-danger')).toBe(true);
    expect(bannerTitle()).toBe('后端连接异常');

    statusCb!({ connected: false, failureKind: 'unknown', since: Date.now() });
    expect(banner().classList.contains('is-danger')).toBe(true);
    expect(bannerTitle()).toBe('后端连接异常');
  });

  it('未带 failureKind 的断连保持启动中文案', async () => {
    await loadGuard();
    statusCb!({ connected: false });
    expect(bannerTitle()).toBe('正在启动后端');
  });

  it('查看日志按钮调用 openPath 打开日志路径', async () => {
    await loadGuard();
    statusCb!({ connected: false, logPath: '/tmp/gateway.log' });

    clickAction('log');
    expect(window.Crew.openPath).toHaveBeenCalledWith('/tmp/gateway.log');
  });

  it('hint: 前缀的日志路径直接展示排查命令，不调用 openPath', async () => {
    await loadGuard();
    statusCb!({ connected: false, logPath: 'hint:journalctl -u crew-gateway --no-pager' });

    clickAction('log');
    expect(window.Crew.openPath).not.toHaveBeenCalled();
    expect(bannerText()).toContain('journalctl -u crew-gateway');
  });

  it('重试按钮调用 retryGateway', async () => {
    await loadGuard();
    statusCb!({ connected: false, failureKind: 'unreachable', since: Date.now() });

    clickAction('retry');
    expect(retryCalled).toBe(1);
  });

  it('connected 后横幅消失，并补拉配置恢复假阴性', async () => {
    await loadGuard();
    statusCb!({ connected: false, failureKind: 'timeout', since: Date.now() });
    expect(banner().classList.contains('show')).toBe(true);

    statusCb!({ connected: true });
    expect(banner().classList.contains('show')).toBe(false);

    await vi.waitFor(() => expect(recoveryMocks.loadConfig).toHaveBeenCalledOnce());
  });

  it('快照兜底：就绪事件早于订阅时 init 后隐藏横幅', async () => {
    initialStatus = { connected: true, logPath: '/tmp/gw.log' };
    await loadGuard();
    await vi.waitFor(() => {
      expect(banner().classList.contains('show')).toBe(false);
    });
  });

  it('connected 时组件 failed 只弹一次非阻断 toast', async () => {
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

    expect(banner().classList.contains('show')).toBe(false);
    expect(Array.from(document.querySelectorAll('.ui-toast')).map((el) => el.textContent))
      .toEqual(['定时任务启动失败，请查看 Gateway 日志']);
  });

  it('connected 时对通用启动失败弹出非阻断提示', async () => {
    await loadGuard();

    statusCb!({
      connected: true,
      components: {
        startup: { status: 'failed', message: '运行环境组件初始化失败，请查看 Gateway 日志' },
      },
    });
    await vi.advanceTimersByTimeAsync(20);

    expect(banner().classList.contains('show')).toBe(false);
    expect(document.querySelector('.ui-toast')?.textContent)
      .toBe('运行环境组件初始化失败，请查看 Gateway 日志');
  });

  it('dispose 移除横幅并解绑订阅', async () => {
    const mod = await loadGuard();
    expect(banner().classList.contains('show')).toBe(true);

    mod.disposeBackendStatusGuard();
    expect(document.getElementById(BANNER_ID)).toBeNull();
    expect(statusCb).toBeNull();
  });

  afterEach(() => {
    vi.useRealTimers();
  });
});
