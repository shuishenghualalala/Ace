import { EventEmitter } from 'node:events';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  connectOverCdp: vi.fn(),
  setFocusEmulation: vi.fn(async () => undefined),
  mount: vi.fn(),
  unmount: vi.fn(),
  disposeHost: vi.fn(),
}));

vi.mock('../../src/main/browser/automation-host', () => ({
  AutomationHost: class {
    mount = mocks.mount;
    unmount = mocks.unmount;
    dispose = mocks.disposeHost;
  },
}));

vi.mock('../../src/main/browser/playwright-compat', () => ({
  connectOverCdp: mocks.connectOverCdp,
  setFocusEmulation: mocks.setFocusEmulation,
}));

import { PlaywrightEngine } from '../../src/main/browser/playwright-engine';

class FakeDebugger extends EventEmitter {
  attached = false;

  isAttached(): boolean {
    return this.attached;
  }

  attach(): void {
    this.attached = true;
  }

  detach(): void {
    this.attached = false;
    this.emit('detach', {}, 'test detach');
  }

  async sendCommand(method: string): Promise<unknown> {
    if (method === 'Target.getTargetInfo') {
      return {
        targetInfo: {
          targetId: 'engine-target',
          type: 'page',
          url: 'https://engine.test/',
        },
      };
    }
    return {};
  }
}

function fakeView(): { view: any; debug: FakeDebugger } {
  const debug = new FakeDebugger();
  return {
    debug,
    view: {
      setBounds: vi.fn(),
      setVisible: vi.fn(),
      webContents: {
        debugger: debug,
        getUserAgent: () =>
          'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
          + 'AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36',
        isDestroyed: () => false,
      },
    },
  };
}

function connectThroughTransport(browser: unknown) {
  return async (transport: {
    onmessage?: (message: any) => void;
    send(message: object): void;
  }): Promise<unknown> => {
    let sequence = 0;
    const pending = new Map<number, {
      resolve(value: unknown): void;
      reject(error: Error): void;
    }>();
    transport.onmessage = (message: any) => {
      if (typeof message?.id !== 'number') return;
      const waiter = pending.get(message.id);
      if (!waiter) return;
      pending.delete(message.id);
      if (message.error) waiter.reject(new Error(message.error.message));
      else waiter.resolve(message.result);
    };
    const send = async (method: string, params?: Record<string, unknown>): Promise<unknown> => {
      const id = ++sequence;
      const result = new Promise<unknown>((resolve, reject) => {
        pending.set(id, { resolve, reject });
      });
      transport.send({ id, method, params });
      return await result;
    };
    await send('Browser.getVersion');
    await send('Target.setAutoAttach', {
      autoAttach: true,
      waitForDebuggerOnStart: true,
      flatten: true,
    });
    return browser;
  };
}

function fakeContext(targetId: string): {
  context: EventEmitter & {
    pages: ReturnType<typeof vi.fn>;
    newCDPSession: ReturnType<typeof vi.fn>;
  };
  makePage: () => EventEmitter & {
    isClosed: ReturnType<typeof vi.fn>;
    context: ReturnType<typeof vi.fn>;
  };
  setPages: (next: unknown[]) => void;
} {
  let pages: unknown[] = [];
  const context = Object.assign(new EventEmitter(), {
    pages: vi.fn(() => pages),
    newCDPSession: vi.fn(async () => ({
      send: vi.fn(async () => ({
        targetInfo: { targetId },
      })),
      detach: vi.fn(async () => undefined),
    })),
  });
  const makePage = () => Object.assign(new EventEmitter(), {
    isClosed: vi.fn(() => false),
    context: vi.fn(() => context),
  });
  const browser = { contexts: vi.fn(() => [context]) };
  mocks.connectOverCdp.mockImplementationOnce(connectThroughTransport(browser) as never);
  return {
    context,
    makePage,
    setPages: (next: unknown[]) => {
      pages = next;
    },
  };
}

describe('PlaywrightEngine', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('pageForView 第一次调用会先建立 Playwright，再等待该 view attach-ready', async () => {
    const detach = vi.fn(async () => undefined);
    const context = {
      pages: vi.fn(() => [page]),
      newCDPSession: vi.fn(async () => ({
        send: vi.fn(async () => ({
          targetInfo: { targetId: 'engine-target' },
        })),
        detach,
      })),
      on: vi.fn(),
      once: vi.fn(),
      off: vi.fn(),
    };
    const page = {
      on: vi.fn(),
      off: vi.fn(),
      isClosed: vi.fn(() => false),
      context: vi.fn(() => context),
    };
    const browser = {
      contexts: vi.fn(() => [context]),
    };

    mocks.connectOverCdp.mockImplementationOnce(connectThroughTransport(browser) as never);

    const engine = new PlaywrightEngine();
    const { view, debug } = fakeView();
    engine.registerTab(view);

    await expect(engine.pageForView(view)).resolves.toBe(page);
    expect(mocks.connectOverCdp).toHaveBeenCalledTimes(1);
    expect(debug.attached).toBe(true);
    expect(context.newCDPSession).toHaveBeenCalledWith(page);
    expect(detach).toHaveBeenCalledTimes(1);
    expect(page.on).toHaveBeenCalledWith('dialog', expect.any(Function));
    expect(page.on).toHaveBeenCalledWith('filechooser', expect.any(Function));
    expect(page.on).toHaveBeenCalledWith('close', expect.any(Function));
    expect(mocks.setFocusEmulation).toHaveBeenCalledWith(context, page, true);

    const chooser = { setFiles: vi.fn(), isMultiple: () => true };
    const chooserListener = page.on.mock.calls.find(
      ([event]) => event === 'filechooser',
    )?.[1] as ((value: unknown) => void) | undefined;
    expect(chooserListener).toBeTypeOf('function');
    chooserListener?.(chooser);
    expect(engine.hasPendingFileChooser(view)).toBe(true);
    expect(engine.takePendingFileChooser(view)).toBe(chooser);
    expect(engine.hasPendingFileChooser(view)).toBe(false);
    expect(engine.takePendingFileChooser(view)).toBeNull();

    chooserListener?.(chooser);
    await engine.setAutomationMode(view, false);
    expect(page.off).toHaveBeenCalledWith('filechooser', chooserListener);
    expect(engine.hasPendingFileChooser(view)).toBe(false);

    await engine.setAutomationMode(view, true);
    expect(page.on.mock.calls.filter(([event]) => event === 'filechooser')).toHaveLength(2);
  });

  it('连接完成即给既有 Page 安装 dialog listener，不等待首次 pageForView', async () => {
    const page = {
      on: vi.fn(),
      off: vi.fn(),
      isClosed: vi.fn(() => false),
      context: vi.fn(),
    };
    const { context, setPages } = fakeContext('unregistered-page');
    setPages([page]);
    page.context.mockReturnValue(context);

    const engine = new PlaywrightEngine();
    await expect(engine.context()).resolves.toBe(context);

    expect(page.on).toHaveBeenCalledWith('dialog', expect.any(Function));
    expect(page.on).toHaveBeenCalledWith('close', expect.any(Function));
    await engine.dispose();
  });

  it('已绑定 Page 的重复读取复用同一 Page 生命周期', async () => {
    const { context, makePage, setPages } = fakeContext('engine-target');
    const page = makePage();
    setPages([page]);
    const engine = new PlaywrightEngine();
    const { view } = fakeView();
    engine.registerTab(view);

    await expect(engine.pageForView(view)).resolves.toBe(page);
    setPages([]);
    await expect(engine.pageForView(view, 20)).resolves.toBe(page);

    expect(context.newCDPSession).toHaveBeenCalledOnce();
    await engine.dispose();
  });

  it('native detach 后 close 事件到达前不复用 stale Page', async () => {
    const { context, makePage, setPages } = fakeContext('engine-target');
    const oldPage = makePage();
    const newPage = makePage();
    setPages([oldPage]);
    const engine = new PlaywrightEngine();
    const { view, debug } = fakeView();
    engine.registerTab(view);

    await expect(engine.pageForView(view)).resolves.toBe(oldPage);
    await engine.setAutomationMode(view, false);
    mocks.setFocusEmulation.mockClear();
    debug.detach();
    // Playwright may retain the old Page until its delayed Target detach is
    // processed, while the replacement Page is already visible to the
    // context. Matching targetId alone must not select the retired object.
    setPages([oldPage, newPage]);

    await expect(engine.pageForView(view)).resolves.toBe(newPage);
    expect(mocks.setFocusEmulation).toHaveBeenCalledWith(context, newPage, false);

    await engine.setAutomationMode(view, true);
    const chooser = { setFiles: vi.fn(), isMultiple: () => false };
    newPage.emit('filechooser', chooser);
    expect(engine.takePendingFileChooser(view)).toBe(chooser);
    oldPage.emit('close');
    await expect(engine.pageForView(view)).resolves.toBe(newPage);
    await engine.dispose();
  });

  it('unregister 使正在解析 Page 的旧任务失效，不能在稍后重新写回映射', async () => {
    const { context, makePage, setPages } = fakeContext('engine-target');
    const page = makePage();
    const engine = new PlaywrightEngine();
    const { view } = fakeView();
    engine.registerTab(view);

    const lookup = engine.pageForView(view);
    await new Promise<void>((resolve) => setImmediate(resolve));
    engine.unregisterTab(view);
    setPages([page]);
    context.emit('page', page);

    await expect(lookup).rejects.toThrow(/生命周期已变化/);
    mocks.setFocusEmulation.mockClear();
    await engine.setAutomationMode(view, false);
    expect(mocks.setFocusEmulation).not.toHaveBeenCalled();
    await engine.dispose();
  });

  it('旧 Page 晚到 close 不会清掉 reattach 后新 Page 的 filechooser', async () => {
    const { context, makePage, setPages } = fakeContext('engine-target');
    const oldPage = makePage();
    const newPage = makePage();
    setPages([oldPage]);
    const engine = new PlaywrightEngine();
    const { view } = fakeView();
    engine.registerTab(view);
    await expect(engine.pageForView(view)).resolves.toBe(oldPage);

    engine.unregisterTab(view);
    engine.registerTab(view);
    setPages([newPage]);
    context.emit('page', newPage);
    await expect(engine.pageForView(view)).resolves.toBe(newPage);

    const chooser = { setFiles: vi.fn(), isMultiple: () => false };
    newPage.emit('filechooser', chooser);
    expect(engine.hasPendingFileChooser(view)).toBe(true);
    oldPage.emit('close');
    expect(engine.hasPendingFileChooser(view)).toBe(true);
    expect(engine.takePendingFileChooser(view)).toBe(chooser);
    await engine.dispose();
  });
});
