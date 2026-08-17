import { EventEmitter } from 'node:events';
import { describe, expect, it, vi } from 'vitest';

const nativeImageMock = vi.hoisted(() => ({
  created: [] as Array<{ bitmap: Buffer; width: number; height: number }>,
  createFromBitmap(bitmap: Buffer, options: { width: number; height: number }) {
    const makeImage = (width: number, height: number, pixels: Buffer): any => ({
      isEmpty: () => pixels.length === 0,
      getSize: () => ({ width, height }),
      toBitmap: () => pixels,
      toPNG: () => {
        const png = Buffer.alloc(25);
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png);
        png.writeUInt32BE(width, 16);
        png.writeUInt32BE(height, 20);
        return png;
      },
      toJPEG: () => {
        const jpeg = Buffer.from([
          0xff, 0xd8,
          0xff, 0xc0, 0x00, 0x0b, 0x08,
          height >> 8, height & 0xff,
          width >> 8, width & 0xff,
          0x01, 0x01, 0x11, 0x00,
          0xff, 0xd9,
        ]);
        return jpeg;
      },
      resize: ({ width: nextWidth, height: nextHeight }: { width: number; height: number }) => (
        makeImage(nextWidth, nextHeight, Buffer.alloc(nextWidth * nextHeight * 4))
      ),
    });
    nativeImageMock.created.push({ bitmap: Buffer.from(bitmap), ...options });
    return makeImage(options.width, options.height, bitmap);
  },
}));

vi.mock('electron', () => ({
  nativeImage: { createFromBitmap: nativeImageMock.createFromBitmap },
}));

import {
  ELECTRON_CDP_CAPABILITIES,
  ElectronCdpTransport,
} from '../../src/main/browser/electron-cdp-transport';

/** 最小 webContents.debugger 替身：只需 attach/sendCommand/on/off 四件事。 */
class FakeDebugger extends EventEmitter {
  attached = false;
  detachCount = 0;
  readonly sent: Array<{ method: string; params?: unknown; sessionId?: string }> = [];
  targetId: string;
  frameId = 'frame-1';
  loaderId = 'loader-1';
  viewport = { x: 0, y: 0, width: 1024, height: 720, scale: 1 };
  contentSize = { x: 0, y: 0, width: 1024, height: 720 };
  /** 允许单条命令抛错，验错误如何回到 Playwright。 */
  failOn = '';
  targetInfoGate: Promise<void> | null = null;
  onSend: ((method: string) => void) | null = null;
  readonly commandGates = new Map<string, Promise<void>>();

  constructor(targetId: string) {
    super();
    this.targetId = targetId;
  }

  isAttached(): boolean {
    return this.attached;
  }

  attach(): void {
    this.attached = true;
  }

  detach(): void {
    this.detachCount += 1;
    this.attached = false;
    this.emit('detach', {}, 'target_closed');
  }

  simulateExternalDetach(reason = 'replaced_with_devtools'): void {
    this.attached = false;
    this.emit('detach', {}, reason);
  }

  async sendCommand(method: string, params?: unknown, sessionId?: string): Promise<unknown> {
    this.sent.push({ method, params, sessionId });
    this.onSend?.(method);
    const gate = this.commandGates.get(`${sessionId ?? ''}\u0000${method}`);
    if (gate) await gate;
    if (this.failOn === method) throw new Error(`boom: ${method}`);
    if (method === 'Target.getTargetInfo') {
      await this.targetInfoGate;
      return { targetInfo: { targetId: this.targetId, type: 'page', url: 'https://example.test/' } };
    }
    if (method === 'Page.getFrameTree') {
      return { frameTree: { frame: { id: this.frameId, loaderId: this.loaderId } } };
    }
    if (method === 'Page.getLayoutMetrics') {
      const { x, y, width, height, scale } = this.viewport;
      return {
        cssContentSize: this.contentSize,
        visualViewport: {
          pageX: x,
          pageY: y,
          clientWidth: width,
          clientHeight: height,
          scale,
        },
        cssVisualViewport: {
          pageX: x,
          pageY: y,
          clientWidth: width,
          clientHeight: height,
          scale,
        },
        cssLayoutViewport: {
          pageX: x,
          pageY: y,
          clientWidth: width,
          clientHeight: height,
        },
      };
    }
    if (method === 'Runtime.evaluate') {
      const expression = String((params as { expression?: unknown } | undefined)?.expression ?? '');
      const scroll = /window\.scrollTo\(([-\d.]+),\s*([-\d.]+)\)/.exec(expression);
      if (scroll) {
        this.viewport.x = Math.max(0, Math.min(Number(scroll[1]), this.contentSize.width - this.viewport.width));
        this.viewport.y = Math.max(0, Math.min(Number(scroll[2]), this.contentSize.height - this.viewport.height));
      }
      return { result: { value: { x: this.viewport.x, y: this.viewport.y } } };
    }
    return { ok: method };
  }
}

function fakePng(width = 1024, height = 720): Buffer {
  const png = Buffer.alloc(25);
  Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png);
  png.writeUInt32BE(width, 16);
  png.writeUInt32BE(height, 20);
  return png;
}

function fakeNativeImage(
  width = 1024,
  height = 720,
  empty = false,
): { isEmpty(): boolean; getSize(): { width: number; height: number }; toPNG(): Buffer; toJPEG(quality: number): Buffer } {
  return {
    isEmpty: () => empty,
    getSize: () => ({ width, height }),
    toPNG: () => fakePng(width, height),
    toJPEG: () => Buffer.from([0xff, 0xd8, 0xff, 0xd9]),
  };
}

function fakeBitmapNativeImage(width: number, height: number, pageY: number): any {
  const bitmap = Buffer.alloc(width * height * 4);
  for (let row = 0; row < height; row += 1) {
    bitmap.fill((pageY + row) % 251, row * width * 4, (row + 1) * width * 4);
  }
  return {
    isEmpty: () => false,
    getSize: () => ({ width, height }),
    toBitmap: () => bitmap,
    toPNG: () => fakePng(width, height),
    toJPEG: () => Buffer.from([0xff, 0xd8, 0xff, 0xd9]),
  };
}

function jpegSize(bytes: Buffer): { width: number; height: number } {
  const marker = bytes.indexOf(Buffer.from([0xff, 0xc0]));
  return {
    height: bytes.readUInt16BE(marker + 5),
    width: bytes.readUInt16BE(marker + 7),
  };
}

class FakeDownloadItem extends EventEmitter {
  cancelled = false;
  state = 'progressing';

  constructor(
    private readonly url: string,
    private readonly filename: string,
  ) {
    super();
  }

  getURL(): string {
    return this.url;
  }

  getFilename(): string {
    return this.filename;
  }

  getState(): string {
    return this.state;
  }

  getSavePath(): string {
    return '';
  }

  cancel(): void {
    this.cancelled = true;
    this.state = 'cancelled';
  }
}

function fakeView(targetId: string): {
  view: any;
  debug: FakeDebugger;
  destroy(): void;
  setURL(url: string): void;
} {
  const debug = new FakeDebugger(targetId);
  let destroyed = false;
  let url = 'https://example.test/';
  const view = {
    webContents: {
      debugger: debug,
      isDestroyed: () => destroyed,
      getURL: () => url,
      getUserAgent: () =>
        'Mozilla/5.0 (Crew Test OS) AppleWebKit/537.36 '
        + '(KHTML, like Gecko) Chrome/143.0.7499.40 Safari/537.36',
    },
  };
  return {
    view,
    debug,
    destroy: () => {
      destroyed = true;
    },
    setURL: (next: string) => {
      url = next;
    },
  };
}

/** 收集 transport 发回 Playwright 的消息，并提供按 id 取响应的辅助。 */
function collect(transport: ElectronCdpTransport): {
  messages: any[];
  reply(id: number): any;
  events(method: string): any[];
} {
  const messages: any[] = [];
  transport.onmessage = (m) => messages.push(m);
  return {
    messages,
    reply: (id) => messages.find((m) => m.id === id),
    events: (method) => messages.filter((m) => m.method === method && m.id === undefined),
  };
}

const flush = async (): Promise<void> => {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
  await new Promise<void>((resolve) => setImmediate(resolve));
  for (let i = 0; i < 4; i += 1) await Promise.resolve();
};

describe('ElectronCdpTransport', () => {
  it('Browser.getVersion 本地合成，且 userAgent 不含 Headless', async () => {
    // Playwright 用 userAgent 里有无 Headless 判定 headful；带上会让它误判。
    const transport = new ElectronCdpTransport();
    const { view } = fakeView('T-UA');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Browser.getVersion' });
    await flush();

    const result = sink.reply(1).result;
    expect(result.protocolVersion).toBe('1.3');
    expect(result.userAgent).not.toMatch(/Headless/i);
    expect(result.userAgent).toContain('Crew Test OS');
    expect(result.product).toBe('Chrome/143.0.7499.40');
  });

  it('Browser.getVersion 无 view 时的 fallback UA 仍包含当前 OS 与有效 Chrome 版本', async () => {
    const transport = new ElectronCdpTransport();
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Browser.getVersion' });
    await flush();

    const result = sink.reply(1).result;
    const osPattern = process.platform === 'win32'
      ? /Windows NT/
      : process.platform === 'darwin'
        ? /Macintosh/
        : /Linux/;
    expect(result.userAgent).toMatch(osPattern);
    expect(result.userAgent).not.toContain('undefined');
    expect(result.product).toMatch(/^Chrome\/\d+\.\d+\.\d+\.\d+$/);
  });

  it('setAutoAttach 之前登记的 view，在 setAutoAttach 时才 attach 并广播', async () => {
    // 这个顺序是刻意的：Playwright 必须在 setAutoAttach 的响应之前看到
    // attachedToTarget，否则握手时它认为一个页面都没有。
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    expect(debug.attached).toBe(false);

    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: { autoAttach: true } });
    await flush();

    expect(debug.attached).toBe(true);
    const attached = sink.events('Target.attachedToTarget');
    expect(attached).toHaveLength(1);
    expect(attached[0].params.targetInfo.targetId).toBe('T-1');
    expect(attached[0].params.waitingForDebugger).toBe(false);

    // attachedToTarget 必须排在 setAutoAttach 的响应之前
    const attachIndex = sink.messages.indexOf(attached[0]);
    const replyIndex = sink.messages.indexOf(sink.reply(1));
    expect(attachIndex).toBeLessThan(replyIndex);
  });

  it('setAutoAttach 之后登记的 view 立即 attach', async () => {
    const transport = new ElectronCdpTransport();
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    const { view, debug } = fakeView('T-late');
    transport.addView(view);
    await flush();

    expect(debug.attached).toBe(true);
    expect(sink.events('Target.attachedToTarget')).toHaveLength(1);
  });

  it('translates Electron Page download events and makes early public cancel non-blocking', async () => {
    const transport = new ElectronCdpTransport();
    const sink = collect(transport);
    const { view, debug } = fakeView('T-download');
    transport.addView(view);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    const payload = {
      frameId: 'frame-download',
      guid: 'guid-download',
      url: 'https://example.test/download.bin',
      suggestedFilename: 'download.bin',
    };
    debug.emit('message', {}, 'Page.downloadWillBegin', payload);
    await flush();
    expect(sink.events('Browser.downloadWillBegin')).toEqual([
      expect.objectContaining({ params: payload }),
    ]);

    // Public Download.cancel() can run in the same microtask as the event,
    // before Electron emits will-download. It must acknowledge immediately,
    // then cancel the native item as soon as pairing completes.
    transport.send({
      id: 2,
      method: 'Browser.cancelDownload',
      params: { guid: payload.guid },
    });
    await flush();
    expect(sink.reply(2)?.result).toEqual({});

    const item = new FakeDownloadItem(payload.url, payload.suggestedFilename);
    transport.registerNativeDownload(view, item as any);
    expect(item.cancelled).toBe(true);

    debug.emit('message', {}, 'Page.downloadProgress', {
      guid: payload.guid,
      receivedBytes: 0,
      totalBytes: 1,
      state: 'canceled',
    });
    await flush();
    expect(sink.events('Browser.downloadProgress')).toEqual([
      expect.objectContaining({
        params: expect.objectContaining({
          guid: payload.guid,
          state: 'canceled',
        }),
      }),
    ]);
  });

  it('buffers early modal events until Engine confirms Page listeners are ready', async () => {
    const transport = new ElectronCdpTransport();
    const sink = collect(transport);
    const { view, debug } = fakeView('T-early-modal');
    transport.addView(view, { deferPageEventsUntilReady: true });
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const attached = sink.events('Target.attachedToTarget')[0];
    const sessionId = attached.params.sessionId;

    debug.emit('message', {}, 'Page.javascriptDialogOpening', {
      type: 'confirm',
      message: 'early',
    });
    debug.emit('message', {}, 'Page.javascriptDialogClosed', {
      result: false,
      userInput: '',
    });
    debug.emit('message', {}, 'Runtime.consoleAPICalled', { type: 'log' });
    await flush();

    expect(sink.events('Runtime.consoleAPICalled')).toEqual([
      expect.objectContaining({ sessionId }),
    ]);
    expect(sink.events('Page.javascriptDialogOpening')).toHaveLength(0);
    expect(sink.events('Page.javascriptDialogClosed')).toHaveLength(0);

    transport.markPageEventsReady(view);
    await flush();
    const modalEvents = sink.messages.filter(
      (message) => (
        message.method === 'Page.javascriptDialogOpening'
        || message.method === 'Page.javascriptDialogClosed'
      ),
    );
    expect(modalEvents.map((message) => message.method)).toEqual([
      'Page.javascriptDialogOpening',
      'Page.javascriptDialogClosed',
    ]);
    expect(modalEvents.every((message) => message.sessionId === sessionId)).toBe(true);
  });

  it('streams modal events immediately for a bare Transport client', async () => {
    const transport = new ElectronCdpTransport();
    const sink = collect(transport);
    const { view, debug } = fakeView('T-direct-modal');
    transport.addView(view);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const sessionId = sink.events('Target.attachedToTarget')[0].params.sessionId;

    debug.emit('message', {}, 'Page.javascriptDialogOpening', {
      type: 'confirm',
      message: 'direct',
    });
    await flush();

    expect(sink.events('Page.javascriptDialogOpening')).toEqual([
      expect.objectContaining({ sessionId }),
    ]);
  });

  it('atomically handles chained unpublished dialogs without exposing half-pairs to core', async () => {
    const transport = new ElectronCdpTransport();
    const sink = collect(transport);
    const { view, debug } = fakeView('T-unpublished-dialog');
    transport.addView(view, { deferPageEventsUntilReady: true });
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Page.javascriptDialogOpening', {
      type: 'alert',
      message: 'first',
    });
    let handled = 0;
    debug.onSend = (method) => {
      if (method !== 'Page.handleJavaScriptDialog') return;
      handled += 1;
      debug.emit('message', {}, 'Page.javascriptDialogClosed', {
        result: handled === 1,
        userInput: '',
      });
      if (handled === 1) {
        debug.emit('message', {}, 'Page.javascriptDialogOpening', {
          type: 'confirm',
          message: 'second',
          defaultPrompt: '',
        });
      }
    };

    await expect(transport.handleUnpublishedDialog(view, {
      accept: true,
      expectedType: 'alert',
    })).resolves.toEqual({
      type: 'alert',
      message: 'first',
      defaultValue: '',
      matched: true,
    });
    await expect(transport.handleUnpublishedDialog(view, {
      accept: true,
      expectedType: 'prompt',
    })).resolves.toEqual({
      type: 'confirm',
      message: 'second',
      defaultValue: '',
      matched: false,
    });

    expect(
      debug.sent.filter((command) => command.method === 'Page.handleJavaScriptDialog'),
    ).toEqual([
      expect.objectContaining({ params: { accept: true } }),
      expect.objectContaining({ params: { accept: false } }),
    ]);
    transport.markPageEventsReady(view);
    await flush();
    expect(sink.events('Page.javascriptDialogOpening')).toHaveLength(0);
    expect(sink.events('Page.javascriptDialogClosed')).toHaveLength(0);
  });

  it('显式 opener 的新生 popup 等待 Electron adoption，并合成精确 openerId', async () => {
    const transport = new ElectronCdpTransport();
    const opener = fakeView('T-popup-opener');
    transport.addView(opener.view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    expect(sink.events('Target.attachedToTarget')).toHaveLength(1);

    const popup = fakeView('T-popup-child');
    popup.setURL('');
    transport.addView(popup.view, { opener: opener.view });
    await flush();
    expect(sink.events('Target.attachedToTarget')).toHaveLength(1);

    popup.setURL('https://popup.example/');
    await new Promise((resolve) => setTimeout(resolve, 35));
    await flush();
    const attached = sink.events('Target.attachedToTarget').find(
      (event) => event.params?.targetInfo?.targetId === 'T-popup-child',
    );
    expect(attached?.params.targetInfo.openerId).toBe('T-popup-opener');
    expect(attached?.params.targetInfo.canAccessOpener).toBe(true);
  });

  it('多个 pending view 独立 attach：一个失败不阻止其他 view ready', async () => {
    const transport = new ElectronCdpTransport();
    const bad = fakeView('T-bad');
    const good = fakeView('T-good');
    bad.debug.failOn = 'Target.getTargetInfo';
    transport.addView(bad.view);
    transport.addView(good.view);
    const sink = collect(transport);

    transport.send({ id: 1, method: 'Target.setAutoAttach', params: { autoAttach: true } });
    await flush();

    expect(sink.reply(1).error).toBeUndefined();
    expect(sink.events('Target.attachedToTarget').map((event) => event.params.targetInfo.targetId))
      .toEqual(['T-good']);
    await expect(transport.waitForViewTarget(good.view, 100)).resolves.toBe('T-good');
    await expect(transport.waitForViewTarget(bad.view, 100)).rejects.toThrow(
      /无法收编 Electron 标签页/,
    );
  });

  it('removeView 会取消进行中的 attach，且不会晚到广播 target', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-race');
    let releaseTargetInfo: (() => void) | undefined;
    debug.targetInfoGate = new Promise<void>((resolve) => {
      releaseTargetInfo = resolve;
    });
    transport.addView(view);
    const sink = collect(transport);
    const ready = transport.waitForViewTarget(view, 1000).catch((error: unknown) => error);

    transport.send({ id: 1, method: 'Target.setAutoAttach', params: { autoAttach: true } });
    await flush();
    expect(debug.attached).toBe(true);
    transport.removeView(view);
    releaseTargetInfo?.();
    await flush();

    expect(await ready).toBeInstanceOf(Error);
    expect(sink.events('Target.attachedToTarget')).toHaveLength(0);
    expect(debug.listenerCount('message')).toBe(0);
    expect(debug.listenerCount('detach')).toBe(0);
    expect(debug.detachCount).toBe(1);
  });

  it('removeView 在 setAutoAttach 前取消 pending，不会触碰 debugger', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-pending-remove');
    transport.addView(view);
    const ready = transport.waitForViewTarget(view, 1000).catch((error: unknown) => error);
    transport.removeView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: { autoAttach: true } });
    await flush();

    expect(await ready).toBeInstanceOf(Error);
    expect(debug.attached).toBe(false);
    expect(sink.events('Target.attachedToTarget')).toHaveLength(0);
  });

  it('带 sessionId 的命令路由到对应 view，且不把 sessionId 转发给 debugger', async () => {
    // pw-tab-N 是我们自己合成的 id，Electron 的 debugger 不认识它。
    const transport = new ElectronCdpTransport();
    const a = fakeView('T-A');
    const b = fakeView('T-B');
    transport.addView(a.view);
    transport.addView(b.view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    const sessions = sink.events('Target.attachedToTarget').map((m) => m.params.sessionId);
    transport.send({ id: 2, method: 'Page.navigate', params: { url: 'x' }, sessionId: sessions[1] });
    await flush();

    expect(b.debug.sent.some((c) => c.method === 'Page.navigate' && c.sessionId === undefined)).toBe(true);
    expect(a.debug.sent.some((c) => c.method === 'Page.navigate')).toBe(false);
  });

  it('root/browser Target.getTargetInfo 按 targetId 精确返回，多标签不回退到首项', async () => {
    const transport = new ElectronCdpTransport();
    const first = fakeView('T-first');
    const second = fakeView('T-second');
    transport.addView(first.view);
    transport.addView(second.view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    transport.send({
      id: 2,
      method: 'Target.getTargetInfo',
      params: { targetId: 'T-second' },
    });
    transport.send({ id: 3, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(3).result.sessionId;
    transport.send({
      id: 4,
      method: 'Target.getTargetInfo',
      params: { targetId: 'T-second' },
      sessionId: browserSession,
    });
    transport.send({
      id: 5,
      method: 'Target.getTargetInfo',
      params: { targetId: 'T-missing' },
      sessionId: browserSession,
    });
    await flush();

    expect(sink.reply(2).result.targetInfo.targetId).toBe('T-second');
    expect(sink.reply(4).result.targetInfo.targetId).toBe('T-second');
    expect(sink.reply(5).error.message).toMatch(/未知 target/);
  });

  it('Host-backed createTarget/closeTarget 保留来源并严格发布真实页面生命周期', async () => {
    const transport = new ElectronCdpTransport();
    const source = fakeView('T-lifecycle-source');
    let created: ReturnType<typeof fakeView> | null = null;
    const lifecycle: string[] = [];
    transport.setPageLifecycleHook({
      createPage: async (context) => {
        expect(context.sourceView).toBe(source.view);
        expect(context.deadlineAt).toBe(12_345);
        expect(context.url).toBe('about:blank');
        expect(context.browserContextId).toBe('');
        created = fakeView('T-lifecycle-created');
        transport.addView(created.view);
        const targetId = await transport.waitForViewTarget(created.view, 1_000);
        lifecycle.push(`create:${targetId}`);
        return targetId;
      },
      closePage: async (context) => {
        expect(context.sourceView).toBe(source.view);
        expect(context.targetId).toBe('T-lifecycle-created');
        expect(context.view).toBe(created?.view);
        lifecycle.push(`close:${context.targetId}`);
        transport.removeView(context.view);
        created?.destroy();
      },
    });
    transport.addView(source.view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    await transport.runWithPageLifecycleSource(source.view, 12_345, async () => {
      transport.send({
        id: 2,
        method: 'Target.createTarget',
        params: { url: 'about:blank' },
      });
      await flush();
    });
    expect(sink.reply(2)?.result).toEqual({ targetId: 'T-lifecycle-created' });
    const createdAttach = sink.events('Target.attachedToTarget').find(
      (event) => event.params?.targetInfo?.targetId === 'T-lifecycle-created',
    );
    expect(createdAttach).toBeDefined();
    expect(sink.messages.indexOf(createdAttach)).toBeLessThan(
      sink.messages.indexOf(sink.reply(2)),
    );

    await transport.runWithPageLifecycleSource(source.view, 12_345, async () => {
      transport.send({
        id: 3,
        method: 'Target.closeTarget',
        params: { targetId: 'T-lifecycle-created' },
      });
      await flush();
    });
    expect(sink.reply(3)?.result).toEqual({ success: true });
    const createdDetach = sink.events('Target.detachedFromTarget').find(
      (event) => event.params?.targetId === 'T-lifecycle-created',
    );
    expect(createdDetach).toBeDefined();
    expect(sink.messages.indexOf(createdDetach)).toBeLessThan(
      sink.messages.indexOf(sink.reply(3)),
    );
    expect(lifecycle).toEqual([
      'create:T-lifecycle-created',
      'close:T-lifecycle-created',
    ]);
    expect(ELECTRON_CDP_CAPABILITIES.createPage).toBe(true);
    expect(ELECTRON_CDP_CAPABILITIES.closePage).toBe(true);
  });

  it('createTarget hook 失败可回滚 view，响应报错且不留下可发现孤儿', async () => {
    const transport = new ElectronCdpTransport();
    const source = fakeView('T-create-rollback-source');
    const failed = fakeView('T-create-rollback-failed');
    transport.setPageLifecycleHook({
      createPage: async () => {
        transport.addView(failed.view);
        await transport.waitForViewTarget(failed.view, 1_000);
        transport.removeView(failed.view);
        failed.destroy();
        throw new Error('host initialization failed');
      },
      closePage: async () => {
        throw new Error('unexpected close');
      },
    });
    transport.addView(source.view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    transport.send({
      id: 2,
      method: 'Target.createTarget',
      params: { url: 'about:blank' },
    });
    await flush();

    expect(sink.reply(2)?.error?.message).toContain('host initialization failed');
    transport.send({ id: 3, method: 'Target.getTargets' });
    await flush();
    expect(
      sink.reply(3)?.result?.targetInfos.map(
        (info: { targetId?: string }) => info.targetId,
      ),
    ).toEqual(['T-create-rollback-source']);
  });

  it('OOPIF 子会话：事件带自己的 sessionId，命令按子会话回流到同一 view', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    // Chromium 通过页面会话引入一个子 target
    debug.emit('message', {}, 'Target.attachedToTarget', { sessionId: 'child-9' }, undefined);
    await flush();

    transport.send({ id: 2, method: 'Runtime.evaluate', params: {}, sessionId: 'child-9' });
    await flush();

    const call = debug.sent.find((c) => c.method === 'Runtime.evaluate');
    expect(call?.sessionId).toBe('child-9');
    expect(sink.reply(2).error).toBeUndefined();
  });

  it('OOPIF lifecycle hook 在 attach 对 Playwright 可见前完成，并缓冲早到子事件', async () => {
    let releaseAttach!: () => void;
    const gate = new Promise<void>((resolve) => { releaseAttach = resolve; });
    const lifecycle: string[] = [];
    const transport = new ElectronCdpTransport({
      childSessionLifecycleHook: async (context) => {
        lifecycle.push(`${context.phase}:${context.sessionId}:${context.targetInfo.type}`);
        if (context.phase === 'attached') await gate;
      },
    });
    const { view, debug } = fakeView('T-hook');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-hook',
      targetInfo: { targetId: 'F-hook', type: 'iframe' },
    }, undefined);
    debug.emit('message', {}, 'Runtime.executionContextCreated', {
      context: { id: 7 },
    }, 'child-hook');
    await flush();
    expect(sink.events('Target.attachedToTarget').some(
      (event) => event.params.sessionId === 'child-hook',
    )).toBe(false);
    expect(sink.events('Runtime.executionContextCreated')).toHaveLength(0);

    releaseAttach();
    await flush();
    const childAttachIndex = sink.messages.findIndex(
      (message) => message.method === 'Target.attachedToTarget'
        && message.params?.sessionId === 'child-hook',
    );
    const contextIndex = sink.messages.findIndex(
      (message) => message.method === 'Runtime.executionContextCreated'
        && message.sessionId === 'child-hook',
    );
    expect(lifecycle).toEqual(['attached:child-hook:iframe']);
    expect(childAttachIndex).toBeGreaterThanOrEqual(0);
    expect(contextIndex).toBeGreaterThan(childAttachIndex);
  });

  it('attach barrier 期间替换 lifecycle hook 也不能让子事件越过 attach', async () => {
    let releaseAttach!: () => void;
    const gate = new Promise<void>((resolve) => { releaseAttach = resolve; });
    const transport = new ElectronCdpTransport({
      childSessionLifecycleHook: async (context) => {
        if (context.phase === 'attached') await gate;
      },
    });
    const { view, debug } = fakeView('T-hook-replaced');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-hook-replaced',
      targetInfo: { targetId: 'F-hook-replaced', type: 'iframe' },
    }, undefined);
    await flush();
    transport.setChildSessionLifecycleHook(null);
    debug.emit('message', {}, 'Runtime.executionContextCreated', {
      context: { id: 11 },
    }, 'child-hook-replaced');
    await flush();

    expect(sink.events('Target.attachedToTarget').some(
      (event) => event.params.sessionId === 'child-hook-replaced',
    )).toBe(false);
    expect(sink.events('Runtime.executionContextCreated')).toHaveLength(0);

    releaseAttach();
    await flush();
    const attachIndex = sink.messages.findIndex(
      (message) => message.method === 'Target.attachedToTarget'
        && message.params?.sessionId === 'child-hook-replaced',
    );
    const eventIndex = sink.messages.findIndex(
      (message) => message.method === 'Runtime.executionContextCreated'
        && message.sessionId === 'child-hook-replaced',
    );
    expect(attachIndex).toBeGreaterThanOrEqual(0);
    expect(eventIndex).toBeGreaterThan(attachIndex);
  });

  it('嵌套 OOPIF 在父 attach 屏障后发布，即使期间 lifecycle hook 被移除', async () => {
    let releaseParent!: () => void;
    const parentGate = new Promise<void>((resolve) => { releaseParent = resolve; });
    const transport = new ElectronCdpTransport({
      childSessionLifecycleHook: async (context) => {
        if (context.phase === 'attached' && context.sessionId === 'parent-oopif') {
          await parentGate;
        }
      },
    });
    const { view, debug } = fakeView('T-nested-hook-replaced');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'parent-oopif',
      targetInfo: { targetId: 'F-parent', type: 'iframe' },
    }, undefined);
    await flush();
    transport.setChildSessionLifecycleHook(null);
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'nested-worker',
      targetInfo: { targetId: 'W-nested', type: 'worker' },
    }, 'parent-oopif');
    await flush();

    expect(sink.messages.some(
      (message) => message.method === 'Target.attachedToTarget'
        && message.params?.sessionId === 'parent-oopif',
    )).toBe(false);
    expect(sink.messages.some(
      (message) => message.method === 'Target.attachedToTarget'
        && message.params?.sessionId === 'nested-worker',
    )).toBe(false);

    releaseParent();
    await flush();
    const parentIndex = sink.messages.findIndex(
      (message) => message.method === 'Target.attachedToTarget'
        && message.params?.sessionId === 'parent-oopif',
    );
    const nestedIndex = sink.messages.findIndex(
      (message) => message.method === 'Target.attachedToTarget'
        && message.params?.sessionId === 'nested-worker',
    );
    expect(parentIndex).toBeGreaterThanOrEqual(0);
    expect(nestedIndex).toBeGreaterThan(parentIndex);
    expect(sink.messages[nestedIndex].sessionId).toBe('parent-oopif');
  });

  it('attach 屏障期间移除 hook 后收到 detach，仍严格发布 attach → detach', async () => {
    let releaseAttach!: () => void;
    const gate = new Promise<void>((resolve) => { releaseAttach = resolve; });
    const transport = new ElectronCdpTransport({
      childSessionLifecycleHook: async (context) => {
        if (context.phase === 'attached') await gate;
      },
    });
    const { view, debug } = fakeView('T-attach-detach-order');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'short-lived-child',
      targetInfo: { targetId: 'F-short-lived', type: 'iframe' },
    }, undefined);
    await flush();
    transport.setChildSessionLifecycleHook(null);
    debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'short-lived-child',
      targetId: 'F-short-lived',
    }, undefined);
    await flush();
    expect(sink.messages.some(
      (message) => message.params?.sessionId === 'short-lived-child'
        && (
          message.method === 'Target.attachedToTarget'
          || message.method === 'Target.detachedFromTarget'
        ),
    )).toBe(false);

    releaseAttach();
    await flush();
    const lifecycle = sink.messages
      .filter((message) => message.params?.sessionId === 'short-lived-child')
      .map((message) => message.method);
    expect(lifecycle).toEqual(['Target.attachedToTarget', 'Target.detachedFromTarget']);
  });

  it('OOPIF lifecycle hook 明确失败后仍释放 attach，不锁死 Playwright target graph', async () => {
    const transport = new ElectronCdpTransport({
      childSessionLifecycleHook: async () => {
        throw new Error('recorder install failed');
      },
    });
    const { view, debug } = fakeView('T-hook-failure');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-failed-hook',
      targetInfo: { targetId: 'F-failed-hook', type: 'iframe' },
    }, undefined);
    await flush();

    expect(sink.events('Target.attachedToTarget').some(
      (event) => event.params.sessionId === 'child-failed-hook',
    )).toBe(true);
  });

  it('OOPIF lifecycle hook 永不 settle 时按时 abort 并释放 attach', async () => {
    let observedSignal: AbortSignal | null = null;
    const transport = new ElectronCdpTransport({
      childSessionHookTimeoutMs: 5,
      childSessionLifecycleHook: async (context) => {
        observedSignal = context.signal;
        await new Promise<void>(() => {});
      },
    });
    const { view, debug } = fakeView('T-hook-timeout');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-timeout',
      targetInfo: { targetId: 'F-timeout', type: 'iframe' },
    }, undefined);
    await new Promise((resolve) => setTimeout(resolve, 15));
    await flush();

    expect(observedSignal?.aborted).toBe(true);
    expect(sink.events('Target.attachedToTarget').some(
      (event) => event.params.sessionId === 'child-timeout',
    )).toBe(true);
  });

  it('OOPIF detach 到达即关闭命令路由，清理 hook 完成后再通知 Playwright', async () => {
    let releaseDetach!: () => void;
    const detachGate = new Promise<void>((resolve) => { releaseDetach = resolve; });
    const transport = new ElectronCdpTransport({
      childSessionLifecycleHook: async (context) => {
        if (context.phase === 'detached') await detachGate;
      },
    });
    const { view, debug } = fakeView('T-detach-hook');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-detach-hook',
      targetInfo: { targetId: 'F-detach-hook', type: 'iframe' },
    }, undefined);
    await flush();

    debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'child-detach-hook',
    }, undefined);
    transport.send({
      id: 2,
      method: 'Runtime.evaluate',
      params: {},
      sessionId: 'child-detach-hook',
    });
    await flush();
    expect(sink.reply(2).error?.message).toMatch(/未知 sessionId/);
    expect(sink.events('Target.detachedFromTarget').some(
      (event) => event.params.sessionId === 'child-detach-hook',
    )).toBe(false);

    releaseDetach();
    await flush();
    expect(sink.events('Target.detachedFromTarget').some(
      (event) => event.params.sessionId === 'child-detach-hook',
    )).toBe(true);
  });

  it('子会话 detach 后，发往它的命令报错而不是打到页面会话上', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Target.attachedToTarget', { sessionId: 'child-9' }, undefined);
    debug.emit('message', {}, 'Target.detachedFromTarget', { sessionId: 'child-9' }, undefined);
    await flush();

    transport.send({ id: 2, method: 'Runtime.evaluate', params: {}, sessionId: 'child-9' });
    await flush();
    expect(sink.reply(2).error?.message).toMatch(/未知 sessionId/);
  });

  it('子会话 detach 后晚到的原生事件被丢弃，不会出现在 detached 通知之后', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-late-child-event');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-late-event',
      targetInfo: { targetId: 'F-late-event', type: 'iframe' },
    }, undefined);
    await flush();
    debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'child-late-event',
    }, undefined);
    debug.emit('message', {}, 'Runtime.consoleAPICalled', {
      type: 'log',
    }, 'child-late-event');
    await flush();

    expect(sink.events('Target.detachedFromTarget').some(
      (event) => event.params.sessionId === 'child-late-event',
    )).toBe(true);
    expect(sink.events('Runtime.consoleAPICalled')).toHaveLength(0);
  });

  it('newCDPSession：attachToBrowserTarget → attachToTarget 开出别名会话', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(2).result.sessionId;

    transport.send({
      id: 3,
      method: 'Target.attachToTarget',
      params: { targetId: 'T-1', flatten: true },
      sessionId: browserSession,
    });
    await flush();
    const alias = sink.reply(3).result.sessionId;
    expect(alias).not.toBe(browserSession);

    transport.send({ id: 4, method: 'Accessibility.getFullAXTree', params: {}, sessionId: alias });
    await flush();
    expect(debug.sent.some((c) => c.method === 'Accessibility.getFullAXTree')).toBe(true);
    expect(sink.reply(4).error).toBeUndefined();
  });

  it('newCDPSession detach 会释放别名，之后命令 fail closed', async () => {
    const transport = new ElectronCdpTransport();
    const { view } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(2).result.sessionId;
    transport.send({
      id: 3,
      method: 'Target.attachToTarget',
      params: { targetId: 'T-1' },
      sessionId: browserSession,
    });
    await flush();
    const alias = sink.reply(3).result.sessionId;

    transport.send({
      id: 4,
      method: 'Target.detachFromTarget',
      params: { sessionId: alias },
      sessionId: browserSession,
    });
    await flush();
    expect(sink.reply(4).error).toBeUndefined();

    transport.send({ id: 5, method: 'Runtime.evaluate', params: {}, sessionId: alias });
    await flush();
    expect(sink.reply(5).error.message).toMatch(/未知 sessionId/);
  });

  it('newCDPSession(frame) 精确路由到 OOPIF，并在 frame detach 后关闭别名', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-frame-session');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'oopif-physical',
      targetInfo: {
        targetId: 'F-frame-session',
        type: 'iframe',
        url: 'https://frame.example/',
      },
    }, undefined);
    await flush();

    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(2).result.sessionId;
    transport.send({
      id: 3,
      method: 'Target.attachToTarget',
      params: { targetId: 'F-frame-session', flatten: true },
      sessionId: browserSession,
    });
    await flush();
    const alias = sink.reply(3).result.sessionId;

    transport.send({
      id: 4,
      method: 'Runtime.evaluate',
      params: { expression: 'location.href' },
      sessionId: alias,
    });
    await flush();
    expect(debug.sent.some(
      (command) => command.method === 'Runtime.evaluate'
        && command.sessionId === 'oopif-physical',
    )).toBe(true);
    expect(sink.reply(4).error).toBeUndefined();

    debug.emit('message', {}, 'Runtime.consoleAPICalled', { type: 'log' }, 'oopif-physical');
    await flush();
    const copies = sink.messages.filter(
      (message) => message.method === 'Runtime.consoleAPICalled',
    );
    expect(copies.map((message) => message.sessionId)).toEqual([
      'oopif-physical',
      alias,
    ]);

    debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'oopif-physical',
      targetId: 'F-frame-session',
    }, undefined);
    await flush();
    expect(sink.messages.some(
      (message) => message.sessionId === browserSession
        && message.method === 'Target.detachedFromTarget'
        && message.params?.sessionId === alias
        && message.params?.targetId === 'F-frame-session',
    )).toBe(true);

    transport.send({ id: 5, method: 'Runtime.evaluate', params: {}, sessionId: alias });
    await flush();
    expect(sink.reply(5).error?.message).toMatch(/未知 sessionId/);
  });

  it('别名会话收到页面级事件的副本，但不收子会话事件', async () => {
    // 真实 Chromium 里别名是第二条独立 CDP 会话，页面事件两边都到；
    // 子会话事件自带 sessionId，重复投递会让 Playwright 记两次账。
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    transport.send({
      id: 3, method: 'Target.attachToTarget', params: { targetId: 'T-1' },
      sessionId: sink.reply(2).result.sessionId,
    });
    await flush();
    const alias = sink.reply(3).result.sessionId;

    debug.emit('message', {}, 'Runtime.bindingCalled', { name: 'x' }, undefined);
    await flush();
    const copies = sink.messages.filter((m) => m.method === 'Runtime.bindingCalled');
    expect(copies.map((m) => m.sessionId)).toContain(alias);
    expect(copies).toHaveLength(2);

    // 先按真实协议引入子会话；未知/已 detach 的 child session 事件会被丢弃。
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-1',
      targetInfo: { targetId: 'F-child-1', type: 'iframe' },
    }, undefined);
    await flush();
    // 已登记子会话的事件只投一次
    debug.emit('message', {}, 'Runtime.consoleAPICalled', {}, 'child-1');
    await flush();
    expect(sink.messages.filter((m) => m.method === 'Runtime.consoleAPICalled')).toHaveLength(1);
  });

  it('removeView 广播 detachedFromTarget，并摘掉 debugger 监听', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    transport.removeView(view);
    const detached = sink.events('Target.detachedFromTarget');
    expect(detached).toHaveLength(1);
    expect(detached[0].params.targetId).toBe('T-1');
    expect(debug.listenerCount('message')).toBe(0);
    expect(debug.listenerCount('detach')).toBe(0);
  });

  it('debugger 外部 detach 会清理页面/别名路由并逐层广播关闭', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-detach');
    // 模拟 BrowserHost 已经拥有 debugger；transport 不应在清理时再次 detach。
    debug.attached = true;
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const tabSession = sink.events('Target.attachedToTarget')[0].params.sessionId;
    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(2).result.sessionId;
    transport.send({
      id: 3,
      method: 'Target.attachToTarget',
      params: { targetId: 'T-detach' },
      sessionId: browserSession,
    });
    await flush();
    const alias = sink.reply(3).result.sessionId;

    debug.simulateExternalDetach();
    await flush();

    const rootDetach = sink.events('Target.detachedFromTarget')
      .find((event) => event.params.sessionId === tabSession);
    const aliasDetach = sink.messages.find(
      (event) =>
        event.sessionId === browserSession
        && event.method === 'Target.detachedFromTarget'
        && event.params.sessionId === alias,
    );
    expect(rootDetach?.params.targetId).toBe('T-detach');
    expect(aliasDetach?.params.targetId).toBe('T-detach');
    expect(debug.listenerCount('message')).toBe(0);
    expect(debug.listenerCount('detach')).toBe(0);
    expect(debug.detachCount).toBe(0);

    transport.send({ id: 4, method: 'Runtime.evaluate', params: {}, sessionId: tabSession });
    transport.send({ id: 5, method: 'Runtime.evaluate', params: {}, sessionId: alias });
    await flush();
    expect(sink.reply(4).error.message).toMatch(/未知 sessionId/);
    expect(sink.reply(5).error.message).toMatch(/未知 sessionId/);
  });

  it('仅串行 OOPIF bootstrap 命令，并允许主会话 cancellation/cleanup 越过挂起响应', async () => {
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve;
    });
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-wire-order');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const tab = sink.events('Target.attachedToTarget')[0].params.sessionId;

    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-wire-order',
      targetInfo: { targetId: 'F-wire-order', type: 'iframe' },
    }, undefined);
    await flush();

    debug.commandGates.set('child-wire-order\u0000Page.enable', firstGate);
    const sentBefore = debug.sent.length;
    transport.send({
      id: 2,
      method: 'Page.enable',
      params: {},
      sessionId: 'child-wire-order',
    });
    transport.send({
      id: 3,
      method: 'Runtime.enable',
      params: {},
      sessionId: 'child-wire-order',
    });
    transport.send({
      id: 4,
      method: 'Target.setAutoAttach',
      params: { autoAttach: true },
      sessionId: 'child-wire-order',
    });
    transport.send({
      id: 5,
      method: 'Runtime.runIfWaitingForDebugger',
      params: {},
      sessionId: 'child-wire-order',
    });
    // A response-serialized per-view lane would deadlock here when the first
    // child command is an awaitPromise actionability call. Main-session abort,
    // focus and cleanup commands must remain physically dispatchable.
    transport.send({
      id: 6,
      method: 'Runtime.evaluate',
      params: { expression: 'cancel-or-cleanup' },
      sessionId: tab,
    });
    await flush();

    expect(debug.sent.slice(sentBefore)).toEqual([
      expect.objectContaining({
        method: 'Runtime.evaluate',
        sessionId: undefined,
      }),
      expect.objectContaining({
        method: 'Page.enable',
        sessionId: 'child-wire-order',
      }),
    ]);
    expect(sink.reply(2)).toBeUndefined();
    expect(sink.reply(3)).toBeUndefined();
    expect(sink.reply(4)).toBeUndefined();
    expect(sink.reply(5)).toBeUndefined();
    expect(sink.reply(6)?.error).toBeUndefined();

    releaseFirst();
    await flush();

    expect(debug.sent.slice(sentBefore)).toEqual([
      expect.objectContaining({
        method: 'Runtime.evaluate',
        sessionId: undefined,
      }),
      expect.objectContaining({
        method: 'Page.enable',
        sessionId: 'child-wire-order',
      }),
      expect.objectContaining({
        method: 'Runtime.enable',
        sessionId: 'child-wire-order',
      }),
      expect.objectContaining({
        method: 'Target.setAutoAttach',
        sessionId: 'child-wire-order',
      }),
      expect.objectContaining({
        method: 'Runtime.runIfWaitingForDebugger',
        sessionId: 'child-wire-order',
      }),
    ]);
    const responseOrder = sink.messages
      .filter((message) => [2, 3, 4, 5].includes(message.id))
      .map((message) => message.id);
    expect(responseOrder).toEqual([2, 3, 4, 5]);
  });

  it('Input.* lease 精确覆盖 tab/alias/child，并在 debugger 失败时 finally 释放', async () => {
    const order: string[] = [];
    const transport = new ElectronCdpTransport({
      inputCommandLeaseHook: ({ method, sessionKind }) => {
        order.push(`acquire:${sessionKind}:${method}`);
        return () => {
          order.push(`release:${sessionKind}:${method}`);
        };
      },
    });
    const { view, debug } = fakeView('T-input');
    debug.onSend = (method) => {
      if (method.startsWith('Input.')) order.push(`send:${method}`);
    };
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const tab = sink.events('Target.attachedToTarget')[0].params.sessionId;
    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(2).result.sessionId;
    transport.send({
      id: 3,
      method: 'Target.attachToTarget',
      params: { targetId: 'T-input' },
      sessionId: browserSession,
    });
    await flush();
    const alias = sink.reply(3).result.sessionId;
    debug.emit('message', {}, 'Target.attachedToTarget', { sessionId: 'child-input' }, undefined);
    await flush();

    transport.send({ id: 4, method: 'Input.dispatchMouseEvent', params: {}, sessionId: tab });
    transport.send({ id: 5, method: 'Input.insertText', params: {}, sessionId: alias });
    transport.send({ id: 6, method: 'Input.dispatchKeyEvent', params: {}, sessionId: 'child-input' });
    transport.send({ id: 7, method: 'Runtime.evaluate', params: {}, sessionId: tab });
    await flush();

    expect(order).toEqual([
      'acquire:tab:Input.dispatchMouseEvent',
      'send:Input.dispatchMouseEvent',
      'release:tab:Input.dispatchMouseEvent',
      'acquire:alias:Input.insertText',
      'send:Input.insertText',
      'release:alias:Input.insertText',
      'acquire:child:Input.dispatchKeyEvent',
      'send:Input.dispatchKeyEvent',
      'release:child:Input.dispatchKeyEvent',
    ]);

    debug.failOn = 'Input.dispatchMouseEvent';
    transport.send({ id: 8, method: 'Input.dispatchMouseEvent', params: {}, sessionId: tab });
    await flush();
    expect(sink.reply(8).error.message).toMatch(/boom/);
    expect(order.slice(-3)).toEqual([
      'acquire:tab:Input.dispatchMouseEvent',
      'send:Input.dispatchMouseEvent',
      'release:tab:Input.dispatchMouseEvent',
    ]);
  });

  it('排队中的 child Input 在 OOPIF detach 后重新校验路由并拒绝发送', async () => {
    let releaseFirst!: () => void;
    const firstGate = new Promise<void>((resolve) => { releaseFirst = resolve; });
    const transport = new ElectronCdpTransport({
      inputCommandLeaseHook: async ({ method }) => {
        if (method === 'Input.dispatchMouseEvent') await firstGate;
      },
    });
    const { view, debug } = fakeView('T-stale-child-input');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const tab = sink.events('Target.attachedToTarget')[0].params.sessionId;
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-stale-input',
      targetInfo: { targetId: 'F-stale-input', type: 'iframe' },
    }, undefined);
    await flush();

    transport.send({
      id: 2,
      method: 'Input.dispatchMouseEvent',
      params: {},
      sessionId: tab,
    });
    await flush();
    transport.send({
      id: 3,
      method: 'Input.dispatchKeyEvent',
      params: {},
      sessionId: 'child-stale-input',
    });
    debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'child-stale-input',
    }, undefined);
    releaseFirst();
    await flush();

    expect(sink.reply(2).error).toBeUndefined();
    expect(sink.reply(3).error?.message).toMatch(/等待输入调度时失效/);
    expect(debug.sent.some(
      (command) => command.method === 'Input.dispatchKeyEvent'
        && command.sessionId === 'child-stale-input',
    )).toBe(false);
  });

  it('debugger 抛错转成 CDP error 回给 Playwright，不冒泡', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

    debug.failOn = 'Page.navigate';
    transport.send({ id: 2, method: 'Page.navigate', params: {}, sessionId: session });
    await flush();

    expect(sink.reply(2).error.message).toMatch(/boom: Page.navigate/);
  });

  it('顶层 viewport 截图走 hidden Electron capturePage 并返回 CDP data', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-native-screenshot');
    const calls: unknown[] = [];
    view.webContents.capturePage = async (...args: unknown[]) => {
      calls.push(args);
      return {
        isEmpty: () => false,
        getSize: () => ({ width: 1024, height: 720 }),
        toPNG: () => fakePng(),
      };
    };
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

    transport.send({
      id: 2,
      method: 'Page.captureScreenshot',
      params: {
        format: 'png',
        clip: { x: 0, y: 0, width: 1024, height: 720, scale: 1 },
        captureBeyondViewport: false,
      },
      sessionId: session,
    });
    await flush();

    expect(sink.reply(2).result).toEqual({ data: fakePng().toString('base64') });
    expect(calls).toEqual([[{ x: 0, y: 0, width: 1024, height: 720 }, { stayHidden: true }]]);
    expect(debug.sent.some((command) => command.method === 'Page.captureScreenshot')).toBe(false);
  });

  it('顶层 fullPage JPEG 在 hidden Electron 中分段合成真实完整高度并恢复滚动', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-native-full-page-jpeg');
    debug.viewport = { x: 0, y: 123, width: 1024, height: 700, scale: 1 };
    debug.contentSize = { x: 0, y: 0, width: 1024, height: 1900 };
    const calls: unknown[][] = [];
    view.webContents.capturePage = async (...args: unknown[]) => {
      calls.push(args);
      return fakeBitmapNativeImage(1024, 700, debug.viewport.y);
    };
    nativeImageMock.created.length = 0;
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

    transport.send({
      id: 2,
      method: 'Page.captureScreenshot',
      params: {
        format: 'jpeg',
        quality: 71,
        clip: { x: 0, y: 0, width: 1024, height: 1900, scale: 1 },
        captureBeyondViewport: true,
      },
      sessionId: session,
    });
    await flush();

    const jpeg = Buffer.from(sink.reply(2).result.data, 'base64');
    expect(jpeg.subarray(0, 2)).toEqual(Buffer.from([0xff, 0xd8]));
    expect(jpeg.subarray(-2)).toEqual(Buffer.from([0xff, 0xd9]));
    expect(jpegSize(jpeg)).toEqual({ width: 1024, height: 1900 });
    const stitched = nativeImageMock.created.at(-1)!;
    expect({ width: stitched.width, height: stitched.height }).toEqual({ width: 1024, height: 1900 });
    for (const row of [0, 699, 700, 1399, 1400, 1899]) {
      expect(stitched.bitmap[row * 1024 * 4]).toBe(row % 251);
    }
    expect(calls[0]).toEqual([
      { x: 0, y: 0, width: 1024, height: 1900 },
      { stayHidden: true },
    ]);
    expect(calls.slice(1)).toHaveLength(3);
    expect(debug.viewport.y).toBe(123);
    expect(debug.sent.some((command) => command.method === 'Page.captureScreenshot')).toBe(false);
  });

  it('fullPage、真实 clip、透明背景与 locator/ref 截图继续走 Playwright CDP', async () => {
    const cases: Array<Record<string, unknown>> = [
      { fullPage: true },
      { captureBeyondViewport: true },
      { omitBackground: true },
      { clip: { x: 0, y: 0, width: 100, height: 100, scale: 1 } },
      { ref: '@e1', clip: { x: 0, y: 0, width: 1024, height: 720, scale: 1 } },
    ];

    for (const params of cases) {
      const transport = new ElectronCdpTransport();
      const { view, debug } = fakeView(`T-fallback-${Object.keys(params)[0]}`);
      let captureCount = 0;
      view.webContents.capturePage = async () => {
        captureCount += 1;
        return fakeNativeImage();
      };
      transport.addView(view);
      const sink = collect(transport);
      transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
      await flush();
      const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

      transport.send({
        id: 2,
        method: 'Page.captureScreenshot',
        params: { format: 'png', captureBeyondViewport: false, ...params },
        sessionId: session,
      });
      await flush();

      expect(sink.reply(2).result).toEqual({ ok: 'Page.captureScreenshot' });
      expect(captureCount).toBe(0);
      expect(debug.sent.some((command) => command.method === 'Page.captureScreenshot')).toBe(true);
    }
  });

  it('子 frame screenshot 继续路由到对应 CDP child session', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-child-screenshot');
    let captureCount = 0;
    view.webContents.capturePage = async () => {
      captureCount += 1;
      return fakeNativeImage();
    };
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'child-screenshot',
      targetInfo: { targetId: 'F-child-screenshot', type: 'iframe' },
    }, undefined);
    await flush();

    transport.send({
      id: 2,
      method: 'Page.captureScreenshot',
      params: { format: 'png', captureBeyondViewport: false },
      sessionId: 'child-screenshot',
    });
    await flush();

    expect(sink.reply(2).result).toEqual({ ok: 'Page.captureScreenshot' });
    expect(captureCount).toBe(0);
    expect(debug.sent).toContainEqual({
      method: 'Page.captureScreenshot',
      params: { format: 'png', captureBeyondViewport: false },
      sessionId: 'child-screenshot',
    });
  });

  it('合法 JPEG quality 使用 native encoder', async () => {
    const transport = new ElectronCdpTransport();
    const { view } = fakeView('T-jpeg-screenshot');
    let quality = -1;
    view.webContents.capturePage = async () => ({
      isEmpty: () => false,
      getSize: () => ({ width: 1024, height: 720 }),
      toPNG: () => fakePng(),
      toJPEG: (nextQuality: number) => {
        quality = nextQuality;
        return Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
      },
    });
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

    transport.send({
      id: 2,
      method: 'Page.captureScreenshot',
      params: {
        format: 'jpeg',
        quality: 65,
        clip: { x: 0, y: 0, width: 1024, height: 720, scale: 1 },
        captureBeyondViewport: false,
      },
      sessionId: session,
    });
    await flush();

    expect(sink.reply(2).result).toEqual({ data: '/9j/2Q==' });
    expect(quality).toBe(65);
  });

  it('native screenshot 在 capturePage 前后检查 frame/loader identity', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-changing-screenshot');
    view.webContents.capturePage = async () => {
      debug.loaderId = 'loader-after-capture';
      return fakeNativeImage();
    };
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

    transport.send({
      id: 2,
      method: 'Page.captureScreenshot',
      params: { format: 'png', captureBeyondViewport: false },
      sessionId: session,
    });
    await flush();

    expect(sink.reply(2).error.message).toMatch(/页面在截图期间已变化/);
  });

  it('拒绝空图与超过尺寸/像素上限的 native screenshot', async () => {
    const cases = [
      { id: 'T-empty-screenshot', image: fakeNativeImage(1024, 720, true), message: /截图为空/ },
      { id: 'T-oversized-screenshot', image: fakeNativeImage(10_000, 10_000), message: /尺寸无效或过大/ },
    ];

    for (const item of cases) {
      const transport = new ElectronCdpTransport();
      const { view } = fakeView(item.id);
      view.webContents.capturePage = async () => item.image;
      transport.addView(view);
      const sink = collect(transport);
      transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
      await flush();
      const session = sink.events('Target.attachedToTarget')[0].params.sessionId;

      transport.send({
        id: 2,
        method: 'Page.captureScreenshot',
        params: { format: 'png', captureBeyondViewport: false },
        sessionId: session,
      });
      await flush();

      expect(sink.reply(2).error.message).toMatch(item.message);
    }
  });

  it('close() 后不再向 Playwright 投递消息', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 1, method: 'Target.setAutoAttach', params: {} });
    await flush();

    let closedReason = '';
    transport.onclose = (r) => { closedReason = r ?? ''; };
    transport.close();
    const before = sink.messages.length;

    debug.emit('message', {}, 'Runtime.bindingCalled', {}, undefined);
    transport.send({ id: 9, method: 'Browser.getVersion' });
    await flush();

    expect(closedReason).toBeTruthy();
    expect(sink.messages).toHaveLength(before);
  });

  it('仅把公开 BrowserContext cookie Storage 命令路由到 owner 的活动页面', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-storage');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();

    const setParams = {
      cookies: [{ name: 'crew', value: 'exact', url: 'https://example.test/' }],
    };
    transport.send({ id: 1, method: 'Storage.setCookies', params: setParams });
    await flush();
    expect(sink.reply(1).error).toBeUndefined();
    expect(debug.sent).toContainEqual({
      method: 'Storage.setCookies',
      params: setParams,
      sessionId: undefined,
    });

    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    const browserSession = sink.reply(2).result.sessionId;
    transport.send({
      id: 3,
      method: 'Storage.getCookies',
      params: {},
      sessionId: browserSession,
    });
    await flush();
    expect(sink.reply(3).error).toBeUndefined();
    expect(debug.sent.some((command) => (
      command.method === 'Storage.getCookies'
      && command.sessionId === undefined
    ))).toBe(true);

    transport.send({ id: 4, method: 'Storage.clearCookies', params: {} });
    await flush();
    expect(sink.reply(4).error).toBeUndefined();
  });

  it('context.pages() 为空时仍由持久 Electron Session 完成 cookie API', async () => {
    const stored: any[] = [];
    let flushes = 0;
    const cookies = {
      get: async (): Promise<any[]> => stored.map((cookie) => ({ ...cookie })),
      set: async (details: Record<string, unknown>): Promise<void> => {
        const url = new URL(String(details.url));
        stored.push({
          name: details.name,
          value: details.value,
          domain: details.domain ?? url.hostname,
          path: details.path ?? '/',
          secure: details.secure === true,
          httpOnly: details.httpOnly === true,
          session: details.expirationDate === undefined,
          expirationDate: details.expirationDate,
          sameSite: details.sameSite ?? 'lax',
        });
      },
      remove: async (_url: string, name: string): Promise<void> => {
        const index = stored.findIndex((cookie) => cookie.name === name);
        if (index >= 0) stored.splice(index, 1);
      },
      flushStore: async (): Promise<void> => {
        flushes += 1;
      },
    };
    const transport = new ElectronCdpTransport();
    const { view } = fakeView('T-empty-context-storage');
    view.webContents.session = { cookies };
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();
    transport.removeView(view);
    await flush();

    transport.send({
      id: 1,
      method: 'Storage.setCookies',
      params: {
        cookies: [{
          name: 'after-close',
          value: 'still-live',
          url: 'https://example.test/path',
          domain: 'example.test',
          path: '/',
          secure: true,
          httpOnly: true,
          sameSite: 'Strict',
          expires: -1,
        }],
      },
    });
    await flush();
    expect(sink.reply(1).error).toBeUndefined();

    transport.send({ id: 2, method: 'Storage.getCookies', params: {} });
    await flush();
    expect(sink.reply(2).result.cookies).toEqual([
      expect.objectContaining({
        name: 'after-close',
        value: 'still-live',
        domain: 'example.test',
        sameSite: 'Strict',
      }),
    ]);

    transport.send({ id: 3, method: 'Storage.clearCookies', params: {} });
    await flush();
    expect(sink.reply(3).error).toBeUndefined();
    expect(stored).toEqual([]);
    expect(flushes).toBe(2);
  });

  it('Browser permissions 在 live owner 原样转发，并支持 browser session 路由', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-permissions-live');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();

    const grant = {
      permissions: ['geolocation', 'notifications'],
      origin: 'https://example.test',
    };
    transport.send({
      id: 1,
      method: 'Browser.grantPermissions',
      params: grant,
    });
    await flush();
    expect(sink.reply(1).error).toBeUndefined();
    expect(debug.sent).toContainEqual({
      method: 'Browser.grantPermissions',
      params: grant,
      sessionId: undefined,
    });

    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    transport.send({
      id: 3,
      method: 'Browser.resetPermissions',
      params: {},
      sessionId: sink.reply(2).result.sessionId,
    });
    await flush();
    expect(sink.reply(3).error).toBeUndefined();
    expect(debug.sent).toContainEqual({
      method: 'Browser.resetPermissions',
      params: {},
      sessionId: undefined,
    });
  });

  it('零页面 permissions journal 在下一 target 发布前按原顺序重放', async () => {
    const transport = new ElectronCdpTransport();
    const first = fakeView('T-permissions-first');
    transport.addView(first.view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();
    transport.removeView(first.view);

    transport.send({
      id: 1,
      method: 'Browser.grantPermissions',
      params: {
        permissions: ['clipboardReadWrite'],
        origin: 'https://one.example',
      },
    });
    transport.send({
      id: 2,
      method: 'Browser.resetPermissions',
      params: {},
    });
    transport.send({
      id: 3,
      method: 'Browser.grantPermissions',
      params: {
        permissions: ['geolocation'],
        origin: 'https://two.example',
      },
    });
    await flush();
    expect(sink.reply(1).error).toBeUndefined();
    expect(sink.reply(2).error).toBeUndefined();
    expect(sink.reply(3).error).toBeUndefined();

    const timeline: string[] = [];
    const next = fakeView('T-permissions-next');
    next.debug.onSend = (method) => {
      if (method.startsWith('Browser.')) timeline.push(method);
    };
    const originalOnMessage = transport.onmessage;
    transport.onmessage = (message: object) => {
      const event = message as { method?: string; params?: { targetInfo?: { targetId?: string } } };
      if (
        event.method === 'Target.attachedToTarget'
        && event.params?.targetInfo?.targetId === 'T-permissions-next'
      ) {
        timeline.push('Target.attachedToTarget');
      }
      originalOnMessage?.(message);
    };
    transport.addView(next.view);
    await flush();

    expect(timeline).toEqual([
      'Browser.grantPermissions',
      'Browser.resetPermissions',
      'Browser.grantPermissions',
      'Target.attachedToTarget',
    ]);
    expect(next.debug.sent.filter((command) => command.method.startsWith('Browser.')))
      .toEqual([
        {
          method: 'Browser.grantPermissions',
          params: {
            permissions: ['clipboardReadWrite'],
            origin: 'https://one.example',
          },
          sessionId: undefined,
        },
        {
          method: 'Browser.resetPermissions',
          params: {},
          sessionId: undefined,
        },
        {
          method: 'Browser.grantPermissions',
          params: {
            permissions: ['geolocation'],
            origin: 'https://two.example',
          },
          sessionId: undefined,
        },
      ]);
  });

  it('Page.printToPDF 完整映射 Electron options，并提供 owner-bound IO stream', async () => {
    const transport = new ElectronCdpTransport();
    const first = fakeView('T-pdf-first');
    const second = fakeView('T-pdf-second');
    const pdf = Buffer.from('%PDF-1.7\nCrew PDF payload\n', 'utf8');
    let observedOptions: Record<string, unknown> | null = null;
    first.view.webContents.printToPDF = async (
      options: Record<string, unknown>,
    ): Promise<Buffer> => {
      observedOptions = options;
      return pdf;
    };
    second.view.webContents.printToPDF = async (): Promise<Buffer> => pdf;
    transport.addView(first.view);
    transport.addView(second.view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();
    const firstSession = sink.events('Target.attachedToTarget').find(
      (event) => event.params.targetInfo.targetId === 'T-pdf-first',
    ).params.sessionId;
    const secondSession = sink.events('Target.attachedToTarget').find(
      (event) => event.params.targetInfo.targetId === 'T-pdf-second',
    ).params.sessionId;

    transport.send({
      id: 1,
      method: 'Page.printToPDF',
      sessionId: firstSession,
      params: {
        transferMode: 'ReturnAsStream',
        landscape: true,
        displayHeaderFooter: true,
        printBackground: true,
        scale: 0.75,
        paperWidth: 8.27,
        paperHeight: 11.69,
        marginTop: 0.25,
        marginBottom: 0.5,
        marginLeft: 0.75,
        marginRight: 1,
        pageRanges: '1-3, 5',
        headerTemplate: '<span class="title"></span>',
        footerTemplate: '<span class="pageNumber"></span>',
        preferCSSPageSize: true,
        generateTaggedPDF: true,
        generateDocumentOutline: true,
      },
    });
    await flush();
    expect(sink.reply(1).error).toBeUndefined();
    expect(observedOptions).toEqual({
      landscape: true,
      displayHeaderFooter: true,
      printBackground: true,
      preferCSSPageSize: true,
      generateTaggedPDF: true,
      generateDocumentOutline: true,
      scale: 0.75,
      pageSize: { width: 8.27, height: 11.69 },
      margins: {
        top: 0.25,
        bottom: 0.5,
        left: 0.75,
        right: 1,
      },
      pageRanges: '1-3, 5',
      headerTemplate: '<span class="title"></span>',
      footerTemplate: '<span class="pageNumber"></span>',
    });
    const handle = sink.reply(1).result.stream;
    expect(handle).toMatch(/^pw-pdf-stream-/);

    transport.send({
      id: 2,
      method: 'IO.read',
      sessionId: firstSession,
      params: { handle, size: 5 },
    });
    await flush();
    expect(Buffer.from(sink.reply(2).result.data, 'base64').toString()).toBe('%PDF-');
    expect(sink.reply(2).result).toMatchObject({
      base64Encoded: true,
      eof: false,
    });

    transport.send({
      id: 3,
      method: 'IO.read',
      sessionId: firstSession,
      params: { handle, offset: 5, size: pdf.length },
    });
    await flush();
    expect(Buffer.from(sink.reply(3).result.data, 'base64')).toEqual(pdf.subarray(5));
    expect(sink.reply(3).result.eof).toBe(true);

    transport.send({
      id: 4,
      method: 'IO.read',
      sessionId: secondSession,
      params: { handle },
    });
    await flush();
    expect(sink.reply(4).error.message).toMatch(/不属于当前 Page 会话/);

    transport.send({
      id: 5,
      method: 'IO.close',
      sessionId: firstSession,
      params: { handle },
    });
    await flush();
    expect(sink.reply(5).result).toEqual({});
    transport.send({
      id: 6,
      method: 'IO.read',
      sessionId: firstSession,
      params: { handle },
    });
    await flush();
    expect(sink.reply(6).error.message).toMatch(/Invalid stream handle/);

    transport.send({
      id: 7,
      method: 'Page.printToPDF',
      sessionId: firstSession,
      params: { transferMode: 'ReturnAsBase64' },
    });
    await flush();
    expect(Buffer.from(sink.reply(7).result.data, 'base64')).toEqual(pdf);
  });

  it('顶层 service_worker 只提升到 root，按 targetId 去重且命令回流物理 child', async () => {
    const transport = new ElectronCdpTransport();
    const first = fakeView('T-sw-page-one');
    const second = fakeView('T-sw-page-two');
    transport.addView(first.view);
    transport.addView(second.view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();

    const targetInfo = {
      targetId: 'SW-shared',
      type: 'service_worker',
      url: 'https://example.test/sw.js',
      browserContextId: 'default',
    };
    first.debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'physical-sw-one',
      targetInfo,
      waitingForDebugger: true,
    }, undefined);
    await flush();
    const rootAttaches = (): any[] => sink.messages.filter(
      (message) => (
        message.method === 'Target.attachedToTarget'
        && message.sessionId === undefined
        && message.params?.targetInfo?.targetId === 'SW-shared'
      ),
    );
    expect(rootAttaches()).toHaveLength(1);
    const firstPublicSession = rootAttaches()[0].params.sessionId;
    expect(firstPublicSession).toMatch(/^pw-sw-/);
    expect(sink.messages.some(
      (message) => (
        message.method === 'Target.attachedToTarget'
        && message.sessionId !== undefined
        && message.params?.targetInfo?.targetId === 'SW-shared'
      ),
    )).toBe(false);

    transport.send({
      id: 1,
      method: 'Runtime.evaluate',
      params: { expression: 'self.location.href' },
      sessionId: firstPublicSession,
    });
    await flush();
    expect(first.debug.sent).toContainEqual({
      method: 'Runtime.evaluate',
      params: { expression: 'self.location.href' },
      sessionId: 'physical-sw-one',
    });

    first.debug.emit('message', {}, 'Runtime.consoleAPICalled', {
      type: 'log',
    }, 'physical-sw-one');
    await flush();
    expect(sink.events('Runtime.consoleAPICalled')).toContainEqual(
      expect.objectContaining({ sessionId: firstPublicSession }),
    );

    second.debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'physical-sw-two',
      targetInfo,
      waitingForDebugger: true,
    }, undefined);
    await flush();
    expect(rootAttaches()).toHaveLength(1);
    expect(second.debug.sent).toContainEqual({
      method: 'Runtime.runIfWaitingForDebugger',
      params: undefined,
      sessionId: 'physical-sw-two',
    });
    const consoleCount = sink.events('Runtime.consoleAPICalled').length;
    second.debug.emit('message', {}, 'Runtime.consoleAPICalled', {
      type: 'log',
    }, 'physical-sw-two');
    await flush();
    expect(sink.events('Runtime.consoleAPICalled')).toHaveLength(consoleCount);

    // Non-primary detach is invisible to the one public Worker.
    second.debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'physical-sw-two',
      targetId: 'SW-shared',
    }, undefined);
    await flush();
    expect(sink.messages.filter(
      (message) => (
        message.method === 'Target.detachedFromTarget'
        && message.sessionId === undefined
        && message.params?.targetId === 'SW-shared'
      ),
    )).toHaveLength(0);

    // Re-introduce a duplicate, then remove the primary. Core observes an
    // ordered close/re-attach and the new synthetic session routes to tab two.
    second.debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'physical-sw-two-replacement',
      targetInfo,
      waitingForDebugger: true,
    }, undefined);
    await flush();
    second.debug.emit(
      'message',
      {},
      'Inspector.workerScriptLoaded',
      {},
      'physical-sw-two-replacement',
    );
    await flush();
    first.debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'physical-sw-one',
      targetId: 'SW-shared',
    }, undefined);
    await flush();
    expect(rootAttaches()).toHaveLength(2);
    const replacementSession = rootAttaches()[1].params.sessionId;
    expect(replacementSession).not.toBe(firstPublicSession);
    const rootDetaches = sink.messages.filter(
      (message) => (
        message.method === 'Target.detachedFromTarget'
        && message.sessionId === undefined
        && message.params?.targetId === 'SW-shared'
      ),
    );
    expect(rootDetaches.map((message) => message.params.sessionId))
      .toEqual([firstPublicSession]);
    const replayedLoaded = sink.messages.findIndex(
      (message) => (
        message.method === 'Inspector.workerScriptLoaded'
        && message.sessionId === replacementSession
      ),
    );
    const replacementAttach = sink.messages.indexOf(rootAttaches()[1]);
    expect(replayedLoaded).toBeGreaterThan(replacementAttach);

    transport.send({
      id: 2,
      method: 'Runtime.evaluate',
      params: { expression: '41 + 1' },
      sessionId: replacementSession,
    });
    await flush();
    expect(second.debug.sent).toContainEqual({
      method: 'Runtime.evaluate',
      params: { expression: '41 + 1' },
      sessionId: 'physical-sw-two-replacement',
    });

    second.debug.emit('message', {}, 'Target.detachedFromTarget', {
      sessionId: 'physical-sw-two-replacement',
      targetId: 'SW-shared',
    }, undefined);
    await flush();
    expect(sink.messages.filter(
      (message) => (
        message.method === 'Target.detachedFromTarget'
        && message.sessionId === undefined
        && message.params?.targetId === 'SW-shared'
      ),
    ).map((message) => message.params.sessionId)).toEqual([
      firstPublicSession,
      replacementSession,
    ]);
  });

  it('service_worker bootstrap 保留 core 并发，resume 不被挂起命令阻塞', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-sw-concurrent');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();
    debug.emit('message', {}, 'Target.attachedToTarget', {
      sessionId: 'physical-sw-concurrent',
      targetInfo: {
        targetId: 'SW-concurrent',
        type: 'service_worker',
        url: 'https://example.test/sw-concurrent.js',
        browserContextId: 'default',
      },
      waitingForDebugger: true,
    }, undefined);
    await flush();
    const publicSession = sink.messages.find(
      (message) => (
        message.method === 'Target.attachedToTarget'
        && message.sessionId === undefined
        && message.params?.targetInfo?.targetId === 'SW-concurrent'
      ),
    ).params.sessionId;

    let releaseEmulation!: () => void;
    debug.commandGates.set(
      'physical-sw-concurrent\u0000Emulation.setUserAgentOverride',
      new Promise<void>((resolve) => {
        releaseEmulation = resolve;
      }),
    );
    transport.send({
      id: 1,
      method: 'Emulation.setUserAgentOverride',
      params: { userAgent: 'Crew' },
      sessionId: publicSession,
    });
    await flush();
    transport.send({
      id: 2,
      method: 'Runtime.runIfWaitingForDebugger',
      params: {},
      sessionId: publicSession,
    });
    await flush();

    expect(sink.reply(1)).toBeUndefined();
    expect(sink.reply(2)?.error).toBeUndefined();
    expect(debug.sent).toContainEqual({
      method: 'Runtime.runIfWaitingForDebugger',
      params: {},
      sessionId: 'physical-sw-concurrent',
    });
    releaseEmulation();
    await flush();
    expect(sink.reply(1)?.error).toBeUndefined();
  });

  it('未知 root/browser 命令即使有 tab 也 fail closed，绝不借道任意页面', async () => {
    const transport = new ElectronCdpTransport();
    const { view, debug } = fakeView('T-1');
    transport.addView(view);
    const sink = collect(transport);
    transport.send({ id: 0, method: 'Target.setAutoAttach', params: {} });
    await flush();
    transport.send({ id: 1, method: 'Storage.clearDataForOrigin', params: {} });
    await flush();
    expect(sink.reply(1).error.message).toMatch(/不支持的 root CDP 命令/);
    expect(debug.sent.some((command) => command.method === 'Storage.clearDataForOrigin')).toBe(false);

    transport.send({ id: 2, method: 'Target.attachToBrowserTarget' });
    await flush();
    transport.send({
      id: 3,
      method: 'Browser.getHistograms',
      params: {},
      sessionId: sink.reply(2).result.sessionId,
    });
    await flush();
    expect(sink.reply(3).error.message).toMatch(/不支持的 browser session CDP 命令/);
    expect(debug.sent.some((command) => command.method === 'Browser.getHistograms')).toBe(false);
  });
});
