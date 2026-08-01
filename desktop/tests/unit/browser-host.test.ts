import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { access, mkdtemp, mkdir, realpath, rm, stat, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const electron = vi.hoisted(() => ({
  sessions: [] as any[],
  views: [] as any[],
  nextWebContentsId: 1,
  failDebuggerAttach: false,
  failSetProxy: false,
  axRole: 'button',
  axEditable: false,
  axValue: '',
  axDisabled: false,
  axReadonly: false,
  nodeName: 'BUTTON',
  nodeType: 'button',
  nodeAttributes: {} as Record<string, string>,
  includeForm: false,
  formAttributes: {} as Record<string, string>,
  oversizedDom: false,
  hitBackendNodeId: 7,
  hitNodeId: 70,
  hitNodes: {} as Record<number, { backendNodeId: number; parentId?: number }>,
  failInputType: '',
  screenshotVersion: 0,
  loaderId: 'loader-1',
  layoutPageX: 0,
  layoutPageY: 0,
  expectedHitLocation: null as { x: number; y: number } | null,
  focusedBackendNodeId: 0,
  focusRedirectBackendNodeId: 0,
  blockInsertText: false,
  navigateOnBlur: false,
  nodeAttributesAfterMouseMove: null as Record<string, string> | null,
  nodeAttributesAfterMousePressed: null as Record<string, string> | null,
  nodeAttributesAfterInsertText: null as Record<string, string> | null,
}));

vi.mock('electron', async () => {
  const { EventEmitter } = await import('node:events');

  class FakeDebugger extends EventEmitter {
    attached = false;
    commands: Array<{ method: string; params: unknown }> = [];

    attach(): void {
      if (electron.failDebuggerAttach) throw new Error('debugger crashed');
      this.attached = true;
    }

    detach(): void {
      this.attached = false;
      this.emit('detach', {}, 'target closed');
    }

    isAttached(): boolean {
      return this.attached;
    }

    async sendCommand(method: string, params?: unknown): Promise<any> {
      this.commands.push({ method, params });
      if (
        method === 'Input.dispatchMouseEvent'
        && (params as any)?.type === 'mouseMoved'
        && electron.nodeAttributesAfterMouseMove
      ) {
        electron.nodeAttributes = electron.nodeAttributesAfterMouseMove;
        electron.nodeAttributesAfterMouseMove = null;
      }
      if (
        method === 'Input.dispatchMouseEvent'
        && (params as any)?.type === 'mousePressed'
        && electron.nodeAttributesAfterMousePressed
      ) {
        electron.nodeAttributes = electron.nodeAttributesAfterMousePressed;
        electron.nodeAttributesAfterMousePressed = null;
      }
      if (method === 'DOM.focus') {
        electron.focusedBackendNodeId = electron.focusRedirectBackendNodeId
          || Number((params as any)?.backendNodeId);
      }
      if (method === 'Input.insertText' && !electron.blockInsertText) {
        electron.axValue = String((params as any)?.text ?? '');
        // 模拟页面在 input handler 里改写表单目的地（formaction 等）。
        if (electron.nodeAttributesAfterInsertText) {
          electron.nodeAttributes = electron.nodeAttributesAfterInsertText;
          electron.nodeAttributesAfterInsertText = null;
        }
      }
      if (
        method === 'Input.dispatchMouseEvent'
        && electron.failInputType
        && (params as any)?.type === electron.failInputType
      ) {
        electron.failInputType = '';
        throw new Error('input transport failed');
      }
      if (method === 'Accessibility.getFullAXTree') {
        const properties = [{ name: 'focusable', value: { value: true } }];
        if (electron.axEditable) properties.push({ name: 'editable', value: { value: 'plaintext' } });
        if (electron.axDisabled) properties.push({ name: 'disabled', value: { value: true } });
        if (electron.axReadonly) properties.push({ name: 'readonly', value: { value: true } });
        if (electron.focusedBackendNodeId === 7) properties.push({ name: 'focused', value: { value: true } });
        return {
          nodes: [
            {
              backendDOMNodeId: 7,
              role: { value: electron.axRole },
              name: { value: 'Submit' },
              ...(electron.axValue ? { value: { value: electron.axValue } } : {}),
              properties,
            },
          ],
        };
      }
      if (method === 'Accessibility.getPartialAXTree') {
        const properties = [{ name: 'focusable', value: { value: true } }];
        if (electron.axEditable) properties.push({ name: 'editable', value: { value: 'plaintext' } });
        if (electron.axDisabled) properties.push({ name: 'disabled', value: { value: true } });
        if (electron.axReadonly) properties.push({ name: 'readonly', value: { value: true } });
        if (electron.focusedBackendNodeId === 7) properties.push({ name: 'focused', value: { value: true } });
        return {
          nodes: [
            {
              backendDOMNodeId: 7,
              role: { value: electron.axRole },
              name: { value: 'Submit' },
              ...(electron.axValue ? { value: { value: electron.axValue } } : {}),
              properties,
            },
          ],
        };
      }
      if (method === 'DOM.getBoxModel') {
        return { model: { border: [10, 20, 110, 20, 110, 60, 10, 60] } };
      }
      if (method === 'DOM.getDocument') {
        if (electron.oversizedDom) {
          return {
            root: {
              backendNodeId: 1,
              nodeName: '#document',
              children: Array.from({ length: 50_001 }, (_, index) => ({
                backendNodeId: index + 10,
                nodeName: 'DIV',
                attributes: [],
              })),
            },
          };
        }
        const targetAttributes = [
          'aria-label', 'Submit',
          'type', electron.nodeType,
          ...Object.entries(electron.nodeAttributes).flatMap(([name, value]) => [name, value]),
        ];
        const target = {
          backendNodeId: 7,
          nodeName: electron.nodeName,
          attributes: targetAttributes,
        };
        const content = electron.includeForm
          ? [{
              backendNodeId: 6,
              nodeName: 'FORM',
              attributes: Object.entries(electron.formAttributes).flatMap(([name, value]) => [name, value]),
              children: [target],
            }]
          : [target];
        return {
          root: {
            backendNodeId: 1,
            nodeName: '#document',
            children: [{
              backendNodeId: 2,
              nodeName: 'HTML',
              attributes: [],
              children: [{ backendNodeId: 3, nodeName: 'BODY', attributes: [], children: content }],
            }],
          },
        };
      }
      if (method === 'DOM.getNodeForLocation') {
        if (
          electron.expectedHitLocation
          && (
            Number((params as any)?.x) !== electron.expectedHitLocation.x
            || Number((params as any)?.y) !== electron.expectedHitLocation.y
          )
        ) {
          throw new Error('No node found at given location');
        }
        return { backendNodeId: electron.hitBackendNodeId, nodeId: electron.hitNodeId };
      }
      if (method === 'DOM.pushNodesByBackendIdsToFrontend') {
        return { nodeIds: [electron.hitNodeId] };
      }
      if (method === 'DOM.resolveNode') return { object: { objectId: 'node-7' } };
      if (method === 'DOM.describeNode') {
        const nodeId = Number((params as any)?.nodeId);
        if (nodeId) {
          const hit = electron.hitNodes[nodeId] ?? {
            backendNodeId: electron.hitBackendNodeId,
            parentId: 0,
          };
          return { node: { ...hit, nodeName: 'DIV', attributes: [] } };
        }
        return {
          node: {
            backendNodeId: Number((params as any)?.backendNodeId) || 7,
            nodeName: electron.nodeName,
            attributes: [
              'aria-label', 'Submit',
              'type', electron.nodeType,
              ...Object.entries(electron.nodeAttributes).flatMap(([name, value]) => [name, value]),
            ],
          },
        };
      }
      if (method === 'Runtime.callFunctionOn') {
        const declaration = String((params as any)?.functionDeclaration ?? '');
        if (declaration.includes('owner.activeElement') && declaration.includes('prototype?.blur')) {
          const released = electron.focusedBackendNodeId === 7;
          if (released) electron.focusedBackendNodeId = 0;
          if (released && electron.navigateOnBlur) electron.loaderId = 'loader-after-blur';
          return { result: { value: released } };
        }
        if (declaration.includes('elementFromPoint')) return { result: { value: true } };
        return { result: { value: 'Submit' } };
      }
      if (method === 'Page.getFrameTree') {
        return { frameTree: { frame: { id: 'frame-1', loaderId: electron.loaderId } } };
      }
      if (method === 'Page.createIsolatedWorld') return { executionContextId: 12 };
      if (method === 'Page.getLayoutMetrics') {
        return {
          cssVisualViewport: {
            pageX: electron.layoutPageX,
            pageY: electron.layoutPageY,
            clientWidth: 1024,
            clientHeight: 720,
          },
          cssLayoutViewport: {
            pageX: electron.layoutPageX,
            pageY: electron.layoutPageY,
            clientWidth: 1024,
            clientHeight: 720,
          },
        };
      }
      if (method === 'Page.captureScreenshot') {
        const png = Buffer.alloc(25);
        Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png);
        png.writeUInt32BE(1024, 16);
        png.writeUInt32BE(720, 20);
        png[24] = electron.screenshotVersion;
        return { data: png.toString('base64') };
      }
      if (method === 'Runtime.evaluate') {
        return {
          result: {
            value: {
              token: 'a'.repeat(32),
              counter: 0,
              href: 'https://example.com/',
              timeOrigin: 1,
              scrollX: 0,
              scrollY: 0,
              width: 1024,
              height: 720,
              dpr: 1,
            },
          },
        };
      }
      return {};
    }
  }

  class FakeWebContents extends EventEmitter {
    id = electron.nextWebContentsId++;
    debugger = new FakeDebugger();
    url = 'about:blank';
    title = '';
    destroyed = false;
    focused = false;
    windowOpenHandler: ((details: any) => any) | null = null;
    canGoBackValue = false;
    canGoForwardValue = false;
    goBackCalled = false;
    goForwardCalled = false;
    // Electron 43 移除了 webContents.canGoBack/goBack 等方法，实现只能走 navigationHistory。
    navigationHistory = {
      canGoBack: () => this.canGoBackValue,
      canGoForward: () => this.canGoForwardValue,
      goBack: () => {
        this.goBackCalled = true;
      },
      goForward: () => {
        this.goForwardCalled = true;
      },
    };

    async loadURL(url: string): Promise<void> {
      this.url = url;
      this.title = 'Example';
      this.emit('did-navigate');
    }

    getURL(): string {
      return this.url;
    }

    getTitle(): string {
      return this.title;
    }

    isDestroyed(): boolean {
      return this.destroyed;
    }

    close(): void {
      this.destroyed = true;
      this.emit('destroyed');
    }

    focus(): void {
      this.focused = true;
    }

    reload(): void {}

    setWindowOpenHandler(handler: (details: any) => any): void {
      this.windowOpenHandler = handler;
    }

    async capturePage(): Promise<any> {
      return {
        getSize: () => ({ width: 1024, height: 720 }),
        toDataURL: () => 'data:image/png;base64,AA==',
        toPNG: () => Buffer.from('png'),
      };
    }
  }

  class FakeView {
    webContents = new FakeWebContents();
    options: any;
    bounds: any = null;
    visible = false;

    constructor(options: any) {
      this.options = options;
      electron.views.push(this);
    }

    setBounds(bounds: any): void {
      this.bounds = bounds;
    }

    setVisible(visible: boolean): void {
      this.visible = visible;
    }

    getBounds(): any {
      return this.bounds;
    }
  }

  class FakeSession extends EventEmitter {
    profilePath: string;
    permissionCheck: unknown = null;
    permissionRequest: unknown = null;
    proxy: unknown = null;
    proxyCalls: unknown[] = [];
    clearSteps: string[] = [];
    protocolHandlers = new Map<string, (request: Request) => Promise<Response> | Response>();
    protocol = {
      handle: (scheme: string, handler: (request: Request) => Promise<Response> | Response): void => {
        this.protocolHandlers.set(scheme, handler);
      },
      unhandle: (scheme: string): void => {
        this.protocolHandlers.delete(scheme);
      },
    };

    constructor(profilePath: string) {
      super();
      this.profilePath = profilePath;
    }

    setPermissionCheckHandler(handler: unknown): void {
      this.permissionCheck = handler;
    }

    setPermissionRequestHandler(handler: unknown): void {
      this.permissionRequest = handler;
    }

    async setProxy(proxy: unknown): Promise<void> {
      this.proxyCalls.push(proxy);
      if (electron.failSetProxy) throw new Error('setProxy failed');
      this.proxy = proxy;
    }

    async closeAllConnections(): Promise<void> { this.clearSteps.push('closeAllConnections'); }
    async clearData(): Promise<void> { this.clearSteps.push('clearData'); }
    async clearHostResolverCache(): Promise<void> { this.clearSteps.push('clearHostResolverCache'); }
    async clearStorageData(): Promise<void> {}
    async clearCache(): Promise<void> {}
    async clearAuthCache(): Promise<void> { this.clearSteps.push('clearAuthCache'); }
  }

  return {
    WebContentsView: FakeView,
    session: {
      fromPath(profilePath: string): FakeSession {
        const existing = electron.sessions.find((item) => item.profilePath === profilePath);
        if (existing) return existing;
        const created = new FakeSession(profilePath);
        electron.sessions.push(created);
        return created;
      },
    },
  };
});

import { BrowserHost } from '../../src/main/browser-host';

const OWNER_ID = 'owner:test';
const OWNER_DIGEST = createHash('sha256').update(OWNER_ID).digest('hex');
const RUNTIME_KEY = `crew_${OWNER_DIGEST.slice(0, 12)}`;
const ACCOUNT_DIR = `acct_${OWNER_DIGEST.slice(0, 16)}`;
const SESSION_ID = 'session-one';
const SESSION_HASH = createHash('sha256').update(SESSION_ID).digest('hex').slice(0, 32);
const TAB_LABEL = `s${SESSION_HASH}-1`;
const PROXY_URL = 'http://127.0.0.1:43123';
let tempRoot = '';
let PROFILE = '';

function fakeWindow(): any {
  return {
    destroyed: false,
    isDestroyed(): boolean {
      return this.destroyed;
    },
    getContentBounds: () => ({ x: 10, y: 20, width: 800, height: 600 }),
    webContents: { focus: vi.fn() },
    contentView: {
      addChildView: vi.fn(),
      removeChildView: vi.fn(),
    },
  };
}

async function createTab(host: BrowserHost): Promise<any> {
  return host.handleRpc({
    type: 'request',
    id: 'one',
    runtime_key: RUNTIME_KEY,
    method: 'execute',
    params: {
      profile_dir: PROFILE,
      command: 'tab',
      args: ['new', '--label', TAB_LABEL, 'https://example.com/'],
      proxy_url: PROXY_URL,
      mutating: true,
    },
  });
}

async function snapshot(host: BrowserHost): Promise<void> {
  await host.handleRpc({
    runtime_key: RUNTIME_KEY,
    method: 'execute',
    params: {
      profile_dir: PROFILE,
      command: 'snapshot',
      args: ['--compact'],
      proxy_url: PROXY_URL,
    },
  });
}

async function setMode(host: BrowserHost, targetId: string, mode: 'ai' | 'human' | 'paused'): Promise<void> {
  await host.handleRpc({
    runtime_key: RUNTIME_KEY,
    method: 'set_mode',
    params: { profile_dir: PROFILE, target_id: targetId, mode },
  });
}

async function pageGuard(host: BrowserHost, targetId: string): Promise<any> {
  const result = await host.handleRpc({
    runtime_key: RUNTIME_KEY,
    method: 'page_guard',
    params: {
      profile_dir: PROFILE,
      proxy_url: PROXY_URL,
      target_id: targetId,
      state_key: `__crew_guard_${'a'.repeat(32)}`,
      state_token: 'b'.repeat(32),
    },
  });
  return JSON.parse(String(result));
}

async function captureHostEpoch(host: BrowserHost): Promise<string> {
  const output = path.join(path.dirname(PROFILE), 'artifacts', `epoch-${Date.now()}.png`);
  await mkdir(path.dirname(output), { recursive: true });
  const result: any = await host.handleRpc({
    runtime_key: RUNTIME_KEY,
    method: 'execute',
    params: {
      profile_dir: PROFILE,
      command: 'screenshot',
      args: [output],
      proxy_url: PROXY_URL,
    },
  });
  return String(result.data.host_epoch);
}

class FakeDownloadItem extends EventEmitter {
  cancelled = false;
  receivedBytes = 4;
  totalBytes = 4;
  savePath = '';
  state: 'progressing' | 'completed' | 'cancelled' | 'interrupted' = 'progressing';
  url = 'https://example.com/file.txt';
  urlChain: string[] = [];

  cancel(): void {
    this.cancelled = true;
    this.state = 'cancelled';
    this.emit('done', {}, 'cancelled');
  }

  getState(): string { return this.state; }
  getTotalBytes(): number { return this.totalBytes; }
  getReceivedBytes(): number { return this.receivedBytes; }
  getFilename(): string { return path.basename(this.savePath) || 'file.txt'; }
  getURL(): string { return this.url; }
  getURLChain(): string[] { return this.urlChain; }
  setSavePath(value: string): void { this.savePath = value; }

  complete(): void {
    this.state = 'completed';
    this.emit('done', {}, 'completed');
  }
}

async function beginDownload(
  host: BrowserHost,
  targetId: string,
  overrides: Record<string, unknown> = {},
): Promise<{ promise: Promise<unknown>; target: string; quarantine: string }> {
  electron.axRole = 'link';
  electron.nodeName = 'A';
  electron.nodeType = '';
  electron.nodeAttributes = { href: '/file.txt', download: '' };
  await snapshot(host);
  const activeView = electron.views.at(-1);
  const browserRoot = path.dirname(PROFILE);
  const target = path.join(browserRoot, 'approved-downloads', 'grant-one', 'file.txt');
  const quarantine = path.join(browserRoot, 'download-quarantine');
  await mkdir(path.dirname(target), { recursive: true });
  const promise = host.handleRpc({
    runtime_key: RUNTIME_KEY,
    method: 'download',
    params: {
      profile_dir: PROFILE,
      target_id: targetId,
      ref: '@e1',
      target,
      download_dir: quarantine,
      proxy_url: PROXY_URL,
      max_bytes: 1024,
      timeout_ms: 10_000,
      ...overrides,
    },
  });
  await vi.waitFor(() => {
    expect(
      activeView.webContents.debugger.commands.some(
        (item: any) => item.method === 'Input.dispatchMouseEvent',
      ),
    ).toBe(true);
  });
  return { promise, target, quarantine };
}

describe('BrowserHost', () => {
  beforeEach(async () => {
    electron.sessions.splice(0);
    electron.views.splice(0);
    electron.nextWebContentsId = 1;
    electron.failDebuggerAttach = false;
    electron.failSetProxy = false;
    electron.axRole = 'button';
    electron.axEditable = false;
    electron.axValue = '';
    electron.axDisabled = false;
    electron.axReadonly = false;
    electron.nodeName = 'BUTTON';
    electron.nodeType = 'button';
    electron.nodeAttributes = {};
    electron.includeForm = false;
    electron.formAttributes = {};
    electron.oversizedDom = false;
    electron.hitBackendNodeId = 7;
    electron.hitNodeId = 70;
    electron.hitNodes = {};
    electron.failInputType = '';
    electron.screenshotVersion = 0;
    electron.loaderId = 'loader-1';
    electron.layoutPageX = 0;
    electron.layoutPageY = 0;
    electron.expectedHitLocation = null;
    electron.focusedBackendNodeId = 0;
    electron.focusRedirectBackendNodeId = 0;
    electron.blockInsertText = false;
    electron.navigateOnBlur = false;
    electron.nodeAttributesAfterMouseMove = null;
    electron.nodeAttributesAfterMousePressed = null;
    tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-browser-host-'));
    PROFILE = path.join(tempRoot, 'accounts', ACCOUNT_DIR, 'browser', 'profile');
    await mkdir(PROFILE, { recursive: true });
    PROFILE = await realpath(PROFILE);
  });

  afterEach(async () => {
    vi.useRealTimers();
    await rm(tempRoot, { recursive: true, force: true });
  });

  it('uses a dedicated persistent Session and hardened remote WebContentsView', async () => {
    const window = fakeWindow();
    const host = new BrowserHost(() => window);

    await createTab(host);

    expect(electron.sessions).toHaveLength(1);
    expect(electron.sessions[0].profilePath).toBe(PROFILE);
    expect(electron.sessions[0].permissionCheck).toBeTypeOf('function');
    expect(electron.sessions[0].proxy).toMatchObject({
      mode: 'fixed_servers',
      proxyBypassRules: '<-loopback>',
    });
    expect(electron.views[0].options.webPreferences).toMatchObject({
      session: electron.sessions[0],
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      webSecurity: true,
      webviewTag: false,
      devTools: false,
    });
    for (const eventName of ['will-navigate', 'will-redirect', 'will-frame-navigate']) {
      const blocked = { url: 'file:///etc/passwd', preventDefault: vi.fn() };
      electron.views[0].webContents.emit(eventName, blocked);
      expect(blocked.preventDefault).toHaveBeenCalledOnce();
      const allowed = { url: 'https://example.com/next', preventDefault: vi.fn() };
      electron.views[0].webContents.emit(eventName, allowed);
      expect(allowed.preventDefault).not.toHaveBeenCalled();
    }
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: '' },
    })).rejects.toMatchObject({ code: 'proxy_required' });
    expect(electron.sessions[0].proxy).not.toMatchObject({ mode: 'direct' });

    await host.dispose();
  });

  it('reports real navigation-history capability for browser chrome', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await setMode(host, created.data.targetId, 'human');
    const contents = electron.views[0].webContents;
    contents.canGoBackValue = true;
    contents.canGoForwardValue = false;

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'get',
        args: ['history'],
        proxy_url: PROXY_URL,
      },
    });

    expect(result.data).toEqual({ can_go_back: true, can_go_forward: false });
    expect(host.getPanelNavigation({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
    })).toMatchObject({
      url: 'https://example.com/',
      title: 'Example',
      can_go_back: true,
      can_go_forward: false,
    });
    await host.dispose();
  });

  it('serves a user-approved HTML artifact through an isolated private capability URL', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await setMode(host, created.data.targetId, 'human');

    const workspace = path.join(tempRoot, 'workspace');
    const artifact = path.join(workspace, 'index.html');
    await mkdir(workspace, { recursive: true });
    await writeFile(artifact, '<!doctype html><title>Crew preview</title>', 'utf8');

    const preview: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'preview',
        args: [artifact, workspace],
        proxy_url: PROXY_URL,
      },
    });
    const previewUrl = String(preview.data.url);
    expect(previewUrl).toMatch(/^crew-artifact:\/\/[a-f0-9]{32}\/index\.html$/);

    const handler = electron.sessions[0].protocolHandlers.get('crew-artifact');
    expect(handler).toBeTypeOf('function');
    const response = await handler(new Request(previewUrl));
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    const csp = response.headers.get('content-security-policy') ?? '';
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("connect-src 'none'");
    expect(csp).toContain("form-action 'none'");
    expect(csp).toContain('sandbox allow-scripts');
    expect(csp).not.toContain('http:');
    expect(csp).not.toContain('https:');
    expect(response.headers.get('referrer-policy')).toBe('no-referrer');
    expect(response.headers.get('x-dns-prefetch-control')).toBe('off');
    expect(await response.text()).toContain('Crew preview');

    const artifactContents = electron.views[0].webContents;
    const sameDocument = { url: `${previewUrl}#section`, preventDefault: vi.fn() };
    artifactContents.emit('will-navigate', sameDocument);
    expect(sameDocument.preventDefault).not.toHaveBeenCalled();
    const external = { url: 'https://collector.example/leak', preventDefault: vi.fn() };
    artifactContents.emit('will-navigate', external);
    expect(external.preventDefault).toHaveBeenCalledOnce();
    expect(artifactContents.windowOpenHandler({
      url: 'https://collector.example/popup',
    })).toEqual({ action: 'deny' });

    const sibling = await handler(new Request(previewUrl.replace('/index.html', '/secret.txt')));
    expect(sibling.status).toBe(404);
    const query = await handler(new Request(`${previewUrl}?file=../secret.txt`));
    expect(query.status).toBe(404);
    const post = await handler(new Request(previewUrl, { method: 'POST' }));
    expect(post.status).toBe(405);

    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'close_target',
      params: { profile_dir: PROFILE, target_id: created.data.targetId },
    });
    expect((await handler(new Request(previewUrl))).status).toBe(404);
    await host.dispose();
  });

  it('rejects an HTML artifact whose canonical path escapes the workspace', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await setMode(host, created.data.targetId, 'human');

    const workspace = path.join(tempRoot, 'workspace');
    const outside = path.join(tempRoot, 'outside.html');
    const linked = path.join(workspace, 'index.html');
    await mkdir(workspace, { recursive: true });
    await writeFile(outside, '<!doctype html><title>Outside</title>', 'utf8');
    await symlink(outside, linked, process.platform === 'win32' ? 'file' : undefined);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'preview',
        args: [linked, workspace],
        proxy_url: PROXY_URL,
      },
    })).rejects.toMatchObject({ code: 'artifact_outside_workspace' });
    expect(electron.sessions[0].protocolHandlers.has('crew-artifact')).toBe(false);
    await host.dispose();
  });

  it('retries a proxy transition when Electron rejected the previous attempt', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const changedProxy = 'http://127.0.0.1:43124';

    electron.failSetProxy = true;
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: changedProxy },
    })).rejects.toMatchObject({ code: 'proxy_unavailable' });
    expect(electron.sessions[0].proxy).toMatchObject({ proxyRules: expect.stringContaining('43123') });

    electron.failSetProxy = false;
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: changedProxy },
    })).resolves.toMatchObject({ success: true });
    expect(electron.sessions[0].proxyCalls).toHaveLength(3);
    expect(electron.sessions[0].proxy).toMatchObject({ proxyRules: expect.stringContaining('43124') });
    await host.dispose();
  });

  it('binds panel access to runtime key and the raw Crew session id', async () => {
    const window = fakeWindow();
    const host = new BrowserHost(() => window);
    await createTab(host);

    expect(() =>
      host.setPanel({
        runtimeKey: RUNTIME_KEY,
        sessionId: 'another-session',
        tabLabel: TAB_LABEL,
        mode: 'ai',
        bounds: { x: 100, y: 100, width: 900, height: 900 },
        visible: true,
      }),
    ).toThrow(/不属于当前 Crew 会话/);

    host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'ai',
      bounds: { x: 100, y: 100, width: 900, height: 900 },
      visible: true,
    });

    expect(window.contentView.addChildView).toHaveBeenCalledWith(electron.views[0]);
    expect(electron.views[0].bounds).toEqual({ x: 100, y: 100, width: 700, height: 500 });
    expect(electron.views[0].visible).toBe(true);
    await host.dispose();
  });

  it('mounts each session panel independently while several sessions run browser tasks', async () => {
    const window = fakeWindow();
    const host = new BrowserHost(() => window);
    await createTab(host);

    // A second session starts its own browser task. Creating its tab makes that
    // tab the owner-level activeTabId, so session one is no longer "current".
    const sessionTwo = 'session-two';
    const hashTwo = createHash('sha256').update(sessionTwo).digest('hex').slice(0, 32);
    const labelTwo = `s${hashTwo}-1`;
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'tab',
        args: ['new', '--label', labelTwo, 'https://example.com/two'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });

    // Switching the UI back to session one must still mount session one's own tab,
    // even though session two's tab is the owner's current activeTabId. Coupling
    // the panel to activeTabId used to reject this with inactive_panel_tab.
    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'ai',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    })).not.toThrow();
    expect(electron.views[0].visible).toBe(true);

    // ...and switching forward to session two mounts its tab as well.
    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: sessionTwo,
      tabLabel: labelTwo,
      mode: 'ai',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    })).not.toThrow();
    expect(electron.views[1].visible).toBe(true);

    // Session isolation is unchanged: one session may never mount another's tab.
    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: sessionTwo,
      tabLabel: TAB_LABEL,
      mode: 'ai',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    })).toThrow(/不属于当前 Crew 会话/);
    await host.dispose();
  });

  it('does not let an AI-mode page popup hijack the host-selected active tab', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const decision = electron.views[0].webContents.windowOpenHandler({
      url: 'https://example.com/active-popup',
    });
    decision.createWindow({});

    // Security: a page-spawned popup must NOT steal the active tab in AI mode
    // (that would let untrusted page content redirect which tab the AI's
    // activeTab-scoped commands observe — a prompt-injection surface). The
    // original session tab therefore stays the host-selected active tab and can
    // still be laid out by the renderer.
    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'ai',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    })).not.toThrow();
    await host.dispose();
  });

  it('keeps tN compatibility and lets an opener-bound popup use its native tab id', async () => {
    const window = fakeWindow();
    const host = new BrowserHost(() => window);
    const created: any = await createTab(host);
    await setMode(host, created.data.targetId, 'human');
    host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'human',
      bounds: { x: 25, y: 35, width: 400, height: 300 },
      visible: true,
    });
    const opener = electron.views[0].webContents;
    const popupDecision = opener.windowOpenHandler({ url: 'https://login.example.com/' });
    expect(popupDecision.action).toBe('allow');
    popupDecision.createWindow({});

    const listed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(listed.data.tabs.map((tab: any) => tab.tabId)).toEqual(['t1', 't2']);
    expect(listed.data.tabs[1]).toMatchObject({
      label: '',
      active: true,
      openerTargetId: listed.data.tabs[0].targetId,
    });
    expect(window.contentView.addChildView).toHaveBeenCalledWith(electron.views[1]);
    expect(window.contentView.removeChildView).toHaveBeenCalledWith(electron.views[0]);
    expect(electron.views[0].visible).toBe(false);
    expect(electron.views[1]).toMatchObject({
      visible: true,
      bounds: { x: 25, y: 35, width: 400, height: 300 },
    });
    expect(electron.views[1].webContents.focused).toBe(true);
    const popupInput = { preventDefault: vi.fn() };
    electron.views[1].webContents.emit('before-input-event', popupInput, {});
    electron.views[1].webContents.emit('before-mouse-event', popupInput, {});
    expect(popupInput.preventDefault).not.toHaveBeenCalled();

    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'human',
      bounds: { x: 0, y: 0, width: 200, height: 200 },
      visible: true,
    })).not.toThrow();
    expect(electron.views[1].visible).toBe(true);
    expect(electron.views[1].bounds).toEqual({ x: 0, y: 0, width: 200, height: 200 });

    const nestedDecision = electron.views[1].webContents.windowOpenHandler({
      url: 'https://login.example.com/nested',
    });
    nestedDecision.createWindow({});
    expect(electron.views[1].visible).toBe(false);
    expect(electron.views[2].visible).toBe(true);
    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'human',
      bounds: { x: 5, y: 6, width: 210, height: 220 },
      visible: true,
    })).not.toThrow();
    expect(electron.views[2].bounds).toEqual({ x: 5, y: 6, width: 210, height: 220 });
    electron.views[2].webContents.close();
    expect(electron.views[2].visible).toBe(false);
    expect(electron.views[1].visible).toBe(true);
    expect(electron.views[1].bounds).toEqual({ x: 5, y: 6, width: 210, height: 220 });

    electron.views[1].webContents.close();
    expect(electron.views[1].visible).toBe(false);
    expect(electron.views[0].visible).toBe(true);
    expect(electron.views[0].bounds).toEqual({ x: 5, y: 6, width: 210, height: 220 });
    const afterClose: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(afterClose.data.tabs).toHaveLength(1);
    expect(afterClose.data.tabs[0]).toMatchObject({ tabId: 't1', active: true });

    const crashDecision = opener.windowOpenHandler({ url: 'https://login.example.com/again' });
    crashDecision.createWindow({});
    electron.views[3].webContents.emit('render-process-gone', {}, { reason: 'crashed' });
    expect(electron.views[3].visible).toBe(false);
    expect(electron.views[0].visible).toBe(true);
    await host.dispose();
  });

  it('denies popup creation after eight tabs in the same Crew session', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const opener = electron.views[0].webContents;

    for (let index = 0; index < 7; index += 1) {
      const decision = opener.windowOpenHandler({ url: `https://example.com/popup-${index}` });
      expect(decision.action).toBe('allow');
      decision.createWindow({});
    }
    expect(electron.views).toHaveLength(8);
    expect(opener.windowOpenHandler({ url: 'https://example.com/overflow' })).toEqual({ action: 'deny' });
    expect(electron.views).toHaveLength(8);
    await host.dispose();
  });

  it('applies control mode to every tab and popup in the Crew session', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const popupDecision = electron.views[0].webContents.windowOpenHandler({
      url: 'https://example.com/popup',
    });
    popupDecision.createWindow({});

    await setMode(host, created.data.targetId, 'human');
    for (const view of electron.views) {
      const event = { preventDefault: vi.fn() };
      view.webContents.emit('before-input-event', event, {});
      expect(event.preventDefault).not.toHaveBeenCalled();
    }

    await setMode(host, created.data.targetId, 'ai');
    for (const view of electron.views) {
      const event = { preventDefault: vi.fn() };
      view.webContents.emit('before-input-event', event, {});
      expect(event.preventDefault).toHaveBeenCalledOnce();
    }
    await host.dispose();
  });

  it('canonicalizes Profile paths and rejects paths outside the expected account directory', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const outside = path.join(tempRoot, 'outside-owner');
    await mkdir(path.join(outside, 'browser', 'profile'), { recursive: true });
    const alias = path.join(tempRoot, 'accounts', ACCOUNT_DIR);
    await rm(alias, { recursive: true, force: true });
    await symlink(outside, alias, process.platform === 'win32' ? 'junction' : 'dir');
    PROFILE = path.join(alias, 'browser', 'profile');

    await expect(createTab(host)).rejects.toMatchObject({ code: 'invalid_profile' });
    expect(electron.sessions).toHaveLength(0);
    await host.dispose();
  });

  it('rejects a Profile whose account hash does not match the runtime key', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const otherDigest = createHash('sha256').update('other:owner').digest('hex');
    PROFILE = path.join(
      tempRoot,
      'accounts',
      `acct_${otherDigest.slice(0, 16)}`,
      'browser',
      'profile',
    );
    await mkdir(PROFILE, { recursive: true });

    await expect(createTab(host)).rejects.toMatchObject({ code: 'profile_owner_mismatch' });
    expect(electron.sessions).toHaveLength(0);
    await host.dispose();
  });

  it('blocks direct input in AI mode and allows native input only during takeover', async () => {
    const window = fakeWindow();
    const host = new BrowserHost(() => window);
    const created: any = await createTab(host);
    const contents = electron.views[0].webContents;

    const blocked = { preventDefault: vi.fn() };
    contents.emit('before-input-event', blocked, {});
    contents.emit('before-mouse-event', blocked, {});
    expect(blocked.preventDefault).toHaveBeenCalledTimes(2);

    expect(() => host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'human',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    })).toThrow(/控制模式/);
    const stillBlocked = { preventDefault: vi.fn() };
    contents.emit('before-input-event', stillBlocked, {});
    expect(stillBlocked.preventDefault).toHaveBeenCalledOnce();

    await setMode(host, created.data.targetId, 'human');
    host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'human',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    });
    const allowed = { preventDefault: vi.fn() };
    contents.emit('before-input-event', allowed, {});
    contents.emit('before-mouse-event', allowed, {});
    expect(allowed.preventDefault).not.toHaveBeenCalled();
    expect(contents.focused).toBe(true);
    await host.dispose();
  });

  it('requests trusted takeover on the first real input over a mounted AI page', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    host.setPanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
      mode: 'ai',
      bounds: { x: 0, y: 0, width: 400, height: 300 },
      visible: true,
    });
    const requested = vi.fn();
    host.on('user-interaction-requested', requested);
    const blocked = { preventDefault: vi.fn() };

    electron.views[0].webContents.emit('before-mouse-event', blocked, { type: 'mouseDown' });
    electron.views[0].webContents.emit('before-mouse-event', blocked, { type: 'mouseUp' });

    expect(blocked.preventDefault).toHaveBeenCalledTimes(2);
    expect(requested).toHaveBeenCalledOnce();
    expect(requested).toHaveBeenCalledWith({
      runtimeKey: RUNTIME_KEY,
      label: TAB_LABEL,
      source: 'pointer',
    });
    await host.dispose();
  });

  it('fails closed against automation and page capture outside AI mode', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const contents = electron.views[0].webContents;
    contents.title = 'private title';
    contents.url = 'https://example.com/private?token=secret';
    await setMode(host, created.data.targetId, 'human');

    const listed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(listed.data.tabs[0]).toMatchObject({ title: '', url: '', targetId: created.data.targetId });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: [TAB_LABEL], proxy_url: PROXY_URL },
    })).resolves.toMatchObject({ success: true });
    for (const [command, args] of [
      ['console', ['--clear']],
      ['network', ['requests', '--clear']],
    ] as const) {
      await expect(host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: { profile_dir: PROFILE, command, args, proxy_url: PROXY_URL },
      })).resolves.toMatchObject({ success: true, data: { text: '[]' } });
    }
    for (const [command, args] of [
      ['snapshot', ['--compact']],
      ['click', ['@e1']],
      ['screenshot', [path.join(path.dirname(PROFILE), 'blocked.png')]],
    ] as const) {
      await expect(host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: { profile_dir: PROFILE, command, args, proxy_url: PROXY_URL, mutating: true },
      })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    }
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'tab',
        args: ['new', '--label', `s${SESSION_HASH}-2`, 'https://example.com/'],
        proxy_url: PROXY_URL,
      },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['close', TAB_LABEL], proxy_url: PROXY_URL },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'page_guard',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        state_key: `__crew_guard_${'a'.repeat(32)}`,
        state_token: 'b'.repeat(32),
      },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'page_images',
      params: { profile_dir: PROFILE, proxy_url: PROXY_URL, target_id: created.data.targetId },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'download',
      params: { profile_dir: PROFILE, proxy_url: PROXY_URL, target_id: created.data.targetId },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    await expect(host.capturePanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
    })).rejects.toMatchObject({ code: 'capture_blocked' });

    await setMode(host, created.data.targetId, 'paused');
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: [], proxy_url: PROXY_URL },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });
    await expect(host.capturePanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
    })).rejects.toMatchObject({ code: 'capture_blocked' });

    await setMode(host, created.data.targetId, 'ai');
    await expect(host.capturePanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
    })).resolves.toMatchObject({ width: 1024, height: 720 });
    contents.emit('render-process-gone', {}, { reason: 'crashed' });
    await expect(host.capturePanel({
      runtimeKey: RUNTIME_KEY,
      sessionId: SESSION_ID,
      tabLabel: TAB_LABEL,
    })).rejects.toMatchObject({ code: 'tab_stopped' });
    await host.dispose();
  });

  it('emits bounded redacted debug records in AI mode and drops them in human mode', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const debug = vi.fn();
    host.on('debug', debug);
    const contents = electron.views[0].webContents;

    contents.emit('console-message', {
      level: 'info',
      message: 'password=hunter2 Bearer abcdefghijklmnop',
      sourceId: 'https://example.com/app.js?access_token=raw-token',
      lineNumber: 12,
    });
    contents.debugger.emit('message', {}, 'Network.requestWillBeSent', {
      request: { method: 'POST', url: 'https://example.com/api?token=raw-token' },
    });
    expect(debug).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(debug.mock.calls)).not.toContain('hunter2');
    expect(JSON.stringify(debug.mock.calls)).not.toContain('abcdefghijklmnop');
    expect(JSON.stringify(debug.mock.calls)).not.toContain('raw-token');
    expect(debug.mock.calls[0][0]).toMatchObject({
      type: 'debug',
      channel: 'console',
      runtimeKey: RUNTIME_KEY,
      targetId: created.data.targetId,
      record: { text: expect.any(String) },
    });
    expect(debug.mock.calls.every(([event]) => event.type === 'debug')).toBe(true);

    await setMode(host, created.data.targetId, 'human');
    contents.emit('console-message', {
      level: 'info', message: 'password=human-secret', sourceId: '', lineNumber: 0,
    });
    contents.debugger.emit('message', {}, 'Network.responseReceived', {
      response: { status: 200, url: 'https://example.com/?token=human-secret' },
    });
    expect(debug).toHaveBeenCalledTimes(2);

    const consoleResult: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'console', args: ['--clear'], proxy_url: PROXY_URL },
    });
    const networkResult: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'network',
        args: ['requests', '--clear'],
        proxy_url: PROXY_URL,
      },
    });
    expect(consoleResult.data.text).toBe('[]');
    expect(networkResult.data.text).toBe('[]');
    await host.dispose();
  });

  it('does not retain or emit JavaScript dialog text during human takeover', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const dialog = vi.fn();
    host.on('dialog', dialog);
    const contents = electron.views[0].webContents;

    contents.debugger.emit('message', {}, 'Page.javascriptDialogOpening', {
      type: 'prompt',
      message: 'AI-visible message',
      defaultPrompt: 'AI-visible default',
    });
    expect(dialog).toHaveBeenCalledOnce();
    await setMode(host, created.data.targetId, 'human');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'dialog',
        args: ['status'],
        proxy_url: PROXY_URL,
      },
    })).rejects.toMatchObject({ code: 'control_mode_blocked' });

    contents.debugger.emit('message', {}, 'Page.javascriptDialogOpening', {
      type: 'prompt',
      message: 'human-secret-message',
      defaultPrompt: 'human-secret-default',
    });
    expect(dialog).toHaveBeenCalledOnce();
    await setMode(host, created.data.targetId, 'ai');
    const status: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'dialog',
        args: ['status'],
        proxy_url: PROXY_URL,
      },
    });
    expect(status.data).toEqual({
      hasDialog: true,
      type: 'prompt',
      message: '',
      defaultValue: '',
    });
    expect(JSON.stringify(status)).not.toContain('human-secret');
    await host.dispose();
  });

  it('reports renderer and debugger failures as tab-scoped, not account-stopped', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.views[0].webContents.emit('render-process-gone', {}, { reason: 'crashed' });

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'snapshot',
        args: ['--compact'],
        proxy_url: PROXY_URL,
      },
    })).rejects.toMatchObject({ code: 'tab_stopped', browser_stopped: false });
    await host.dispose();

    electron.failDebuggerAttach = true;
    const second = new BrowserHost(() => fakeWindow());
    await expect(createTab(second)).rejects.toMatchObject({
      code: 'debugger_unavailable',
      browser_stopped: false,
    });
    await second.dispose();
  });

  it('returns AX refs and translates ref clicks into fixed CDP input commands', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);

    const snapshot: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'snapshot',
        args: ['--compact'],
        proxy_url: PROXY_URL,
      },
    });
    expect(snapshot.data.snapshot).toContain('- button "Submit" [ref=@e1]');
    expect(
      electron.views[0].webContents.debugger.commands.find(
        (item: any) => item.method === 'Accessibility.getFullAXTree',
      )?.params,
    ).toEqual({ depth: 32 });

    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    const commands = electron.views[0].webContents.debugger.commands;
    expect(commands.filter((item: any) => item.method === 'Input.dispatchMouseEvent')).toHaveLength(3);
    expect(commands.some((item: any) => item.method === 'Runtime.evaluate')).toBe(false);
    expect(commands.some(
      (item: any) => item.method === 'Runtime.callFunctionOn'
        && String(item.params?.functionDeclaration ?? '').includes('elementFromPoint'),
    )).toBe(false);
    await host.dispose();
  });

  it('fails a click when the CDP hit node is not the ref or one of its descendants', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    electron.hitBackendNodeId = 99;
    electron.hitNodeId = 99;
    electron.hitNodes = { 99: { backendNodeId: 99, parentId: 0 } };

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'hit_test_failed' });
    const commands = electron.views[0].webContents.debugger.commands;
    expect(commands.some(
      (item: any) => item.method === 'Runtime.callFunctionOn'
        && /elementFromPoint|contains/.test(String(item.params?.functionDeclaration ?? '')),
    )).toBe(false);
    expect(commands.some((item: any) => item.method === 'Input.dispatchMouseEvent')).toBe(false);
    await host.dispose();
  });

  it('accepts a CDP hit node only through a bounded native parent chain', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    electron.hitBackendNodeId = 99;
    electron.hitNodeId = 90;
    electron.hitNodes = {
      90: { backendNodeId: 99, parentId: 80 },
      80: { backendNodeId: 7, parentId: 0 },
    };

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });
    expect(
      electron.views[0].webContents.debugger.commands.filter(
        (item: any) => item.method === 'DOM.describeNode' && item.params?.nodeId,
      ),
    ).toHaveLength(4);
    await host.dispose();
  });

  it('converts a scrolled viewport point to document coordinates only for hit-testing', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    // The target box center is viewport point (60, 40). A narrow panel has
    // horizontally scrolled the fixed-width page by 761 CSS pixels.
    electron.layoutPageX = 761;
    electron.expectedHitLocation = { x: 821, y: 40 };

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });

    const commands = electron.views[0].webContents.debugger.commands;
    const probes = commands.filter((item: any) => item.method === 'DOM.getNodeForLocation');
    expect(probes).toHaveLength(2);
    expect(probes.every((item: any) => item.params.x === 821 && item.params.y === 40)).toBe(true);
    const input = commands
      .filter((item: any) => item.method === 'Input.dispatchMouseEvent')
      .map((item: any) => item.params);
    expect(input).toEqual([
      { type: 'mouseMoved', x: 60, y: 40, button: 'none' },
      { type: 'mousePressed', x: 60, y: 40, button: 'left', buttons: 1, clickCount: 1 },
      { type: 'mouseReleased', x: 60, y: 40, button: 'left', buttons: 0, clickCount: 1 },
    ]);
    await host.dispose();
  });

  it('recomputes the snapshot security fingerprint after hover and before mouse press', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.nodeType = 'submit';
    electron.nodeAttributes = { formaction: '/submit-one' };
    await snapshot(host);
    electron.nodeAttributesAfterMouseMove = { formaction: '/submit-two' };

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'stale_ref_security', uncertain: false });
    const mouseEvents = electron.views[0].webContents.debugger.commands
      .filter((item: any) => item.method === 'Input.dispatchMouseEvent')
      .map((item: any) => item.params.type);
    expect(mouseEvents).toEqual(['mouseMoved']);
    await host.dispose();
  });

  it('releases away from a target whose security fingerprint changes on mousedown', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.nodeType = 'submit';
    electron.nodeAttributes = { formaction: '/submit-one' };
    await snapshot(host);
    electron.nodeAttributesAfterMousePressed = { formaction: '/submit-two' };

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'stale_ref_security', uncertain: true });
    const events = electron.views[0].webContents.debugger.commands
      .filter((item: any) => item.method === 'Input.dispatchMouseEvent')
      .map((item: any) => item.params);
    expect(events.map((item: any) => item.type)).toEqual([
      'mouseMoved',
      'mousePressed',
      'mouseMoved',
      'mouseReleased',
    ]);
    expect(events.at(-1)).toMatchObject({ x: 1056, y: 752, type: 'mouseReleased' });
    await host.dispose();
  });

  it('performs coordinate hit-test and click atomically in one Host RPC', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const expectedEpoch = await captureHostEpoch(host);
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: expectedEpoch,
        x: 60,
        y: 40,
      },
    });
    expect(result).toMatchObject({
      clicked: true,
      target: { tag: 'BUTTON', role: 'button', name: 'Submit' },
    });
    const input = electron.views[0].webContents.debugger.commands
      .filter((item: any) => item.method === 'Input.dispatchMouseEvent')
      .map((item: any) => item.params.type);
    expect(input).toEqual(['mouseMoved', 'mousePressed', 'mouseReleased']);
    expect(
      electron.views[0].webContents.debugger.commands.filter(
        (item: any) => item.method === 'DOM.getNodeForLocation',
      ),
    ).toHaveLength(2);
    await host.dispose();
  });

  it('uses document coordinates for a scrolled visual hit-test and viewport coordinates for input', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    electron.layoutPageX = 761;
    electron.layoutPageY = 25;
    electron.expectedHitLocation = { x: 821, y: 65 };
    const expectedEpoch = await captureHostEpoch(host);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: expectedEpoch,
        x: 60,
        y: 40,
      },
    })).resolves.toMatchObject({ clicked: true });

    const commands = electron.views[0].webContents.debugger.commands;
    expect(commands.filter((item: any) => item.method === 'DOM.getNodeForLocation'))
      .toEqual([
        expect.objectContaining({ params: expect.objectContaining({ x: 821, y: 65 }) }),
        expect.objectContaining({ params: expect.objectContaining({ x: 821, y: 65 }) }),
      ]);
    expect(commands
      .filter((item: any) => item.method === 'Input.dispatchMouseEvent')
      .map((item: any) => ({ type: item.params.type, x: item.params.x, y: item.params.y })))
      .toEqual([
        { type: 'mouseMoved', x: 60, y: 40 },
        { type: 'mousePressed', x: 60, y: 40 },
        { type: 'mouseReleased', x: 60, y: 40 },
      ]);
    await host.dispose();
  });

  it('requires a fresh visual Host epoch and consumes it after one coordinate click', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        x: 60,
        y: 40,
      },
    })).rejects.toMatchObject({ code: 'invalid_visual_epoch' });

    const expectedEpoch = await captureHostEpoch(host);
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: expectedEpoch,
        x: 60,
        y: 40,
      },
    })).resolves.toMatchObject({ clicked: true });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: expectedEpoch,
        x: 60,
        y: 40,
      },
    })).rejects.toMatchObject({ code: 'invalid_visual_epoch' });
    await host.dispose();
  });

  it('rejects a coordinate epoch when either the page marker or captured pixels changed', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const pageEpoch = await captureHostEpoch(host);
    electron.loaderId = 'loader-2';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: pageEpoch,
        x: 60,
        y: 40,
      },
    })).rejects.toMatchObject({ code: 'invalid_visual_epoch' });

    electron.loaderId = 'loader-1';
    const pixelEpoch = await captureHostEpoch(host);
    electron.screenshotVersion = 1;
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: pixelEpoch,
        x: 60,
        y: 40,
      },
    })).rejects.toMatchObject({ code: 'invalid_visual_epoch' });
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Input.dispatchMouseEvent',
    )).toBe(false);
    await host.dispose();
  });

  it('rejects unsafe coordinate targets and reports post-press failures as uncertain', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const expectedEpoch = await captureHostEpoch(host);
    electron.nodeName = 'BODY';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: expectedEpoch,
        x: 60,
        y: 40,
      },
    })).rejects.toMatchObject({ code: 'not_interactable', uncertain: false });

    electron.nodeName = 'BUTTON';
    electron.failInputType = 'mouseReleased';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: expectedEpoch,
        x: 60,
        y: 40,
      },
    })).rejects.toMatchObject({ code: 'input_failed', uncertain: true });
    const releases = electron.views[0].webContents.debugger.commands.filter(
      (item: any) => item.method === 'Input.dispatchMouseEvent'
        && item.params?.type === 'mouseReleased',
    );
    expect(releases).toHaveLength(2);
    await host.dispose();
  });

  it('indexes DOM security once per observation and invalidates only security attributes', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    electron.nodeAttributes = { class: 'before', formaction: '/submit-one' };
    electron.nodeType = 'submit';
    electron.includeForm = true;
    electron.formAttributes = { action: '/form-one', method: 'post' };
    await snapshot(host);
    const first = await pageGuard(host, created.data.targetId);

    electron.nodeAttributes = { class: 'after', formaction: '/submit-one' };
    const unrelated = await pageGuard(host, created.data.targetId);
    expect(unrelated.elementSecurity).toEqual(first.elementSecurity);

    electron.nodeAttributes = { class: 'after', formaction: '/submit-two' };
    const changed = await pageGuard(host, created.data.targetId);
    expect(changed.elementSecurity).not.toEqual(first.elementSecurity);
    const commands = electron.views[0].webContents.debugger.commands;
    expect(commands.filter((item: any) => item.method === 'DOM.getDocument')).toHaveLength(4);
    expect(commands.some(
      (item: any) => item.method === 'DOM.describeNode' && item.params?.backendNodeId === 7,
    )).toBe(false);
    await host.dispose();
  });

  it('exposes a lightweight main-page transition epoch without weakening the capture guard', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const contents = electron.views[0].webContents;
    const initial = await pageGuard(host, created.data.targetId);
    const documentReads = contents.debugger.commands.filter(
      (item: any) => item.method === 'DOM.getDocument',
    ).length;

    const lightweight = JSON.parse(String(await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'page_guard',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        state_key: `__crew_guard_${'c'.repeat(32)}`,
        state_token: 'd'.repeat(32),
        include_security: false,
      },
    })));
    expect(lightweight).toMatchObject({
      navigationPending: false,
      locationConsistent: true,
    });
    expect(lightweight.navigationEpoch).toBeGreaterThanOrEqual(initial.navigationEpoch);
    expect(lightweight.securityDigest).toBeUndefined();
    expect(contents.debugger.commands.filter(
      (item: any) => item.method === 'DOM.getDocument',
    )).toHaveLength(documentReads);

    contents.emit('did-start-navigation', {
      isMainFrame: false,
      isSameDocument: true,
    });
    const subframeStart = await pageGuard(host, created.data.targetId);
    expect(subframeStart.navigationEpoch).toBe(lightweight.navigationEpoch);
    expect(subframeStart.navigationPending).toBe(false);

    // Legacy Electron shape: (event, url, isInPlace, isMainFrame, ...).
    contents.emit('did-start-navigation', {}, contents.url, true, true, 1, 1);
    const pending = await pageGuard(host, created.data.targetId);
    expect(pending.navigationPending).toBe(true);
    expect(pending.navigationEpoch).toBeGreaterThan(lightweight.navigationEpoch);

    // A structured subframe in-page event must not clear main-frame pending.
    contents.emit('did-navigate-in-page', { isMainFrame: false });
    const stillPending = await pageGuard(host, created.data.targetId);
    expect(stillPending.navigationPending).toBe(true);
    expect(stillPending.navigationEpoch).toBe(pending.navigationEpoch);

    // A decorative title change must NOT bump the epoch. Otherwise a page with a
    // churning title — countdowns, "(3) Inbox", a hostile
    // setInterval(()=>document.title=...) — resets the snapshot stability gate
    // forever and becomes permanently un-observable. The title is still tracked
    // for display (titleDigest updates); only navigation bumps the epoch.
    contents.title = '云南旅游视频_百度搜索';
    contents.emit('page-title-updated');
    const titledButNotNavigated = await pageGuard(host, created.data.targetId);
    expect(titledButNotNavigated.navigationEpoch).toBe(stillPending.navigationEpoch);
    expect(titledButNotNavigated.titleDigest).toBe(
      createHash('sha256').update('云南旅游视频_百度搜索', 'utf8').digest('hex'),
    );

    // Same-document navigation (Electron 43 structured shape: main-frame
    // identity carried by details) is what legitimately bumps the epoch.
    contents.emit('did-navigate-in-page', { isMainFrame: true });
    const settled = await pageGuard(host, created.data.targetId);
    expect(settled).toMatchObject({
      navigationPending: false,
      locationConsistent: true,
      titleDigest: createHash('sha256')
        .update('云南旅游视频_百度搜索', 'utf8')
        .digest('hex'),
    });
    expect(settled.navigationEpoch).toBeGreaterThan(titledButNotNavigated.navigationEpoch);

    contents.emit('did-start-navigation', {
      isMainFrame: true,
      isSameDocument: true,
      url: contents.url,
    });
    contents.emit('did-fail-load', {}, -3, 'aborted', contents.url, true, 1, 1);
    const failedButUnsettled = await pageGuard(host, created.data.targetId);
    expect(failedButUnsettled.navigationPending).toBe(true);
    expect(failedButUnsettled.navigationEpoch).toBeGreaterThan(settled.navigationEpoch);
    contents.emit('did-stop-loading');
    const stopped = await pageGuard(host, created.data.targetId);
    expect(stopped.navigationPending).toBe(false);
    expect(stopped.navigationEpoch).toBeGreaterThan(failedButUnsettled.navigationEpoch);
    await host.dispose();
  });

  it('marks submit controls without treating form actions as direct navigation', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    electron.nodeType = 'submit';
    electron.nodeAttributes = { formaction: '/dangerous-submit' };
    electron.includeForm = true;
    electron.formAttributes = { action: '/form-submit', method: 'post' };
    const observed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: ['--compact'], proxy_url: PROXY_URL },
    });
    expect(observed.data.snapshot).toContain('[action=submit]');
    const marker = await pageGuard(host, created.data.targetId);
    expect(marker.elementNavigation['button\0submit']).toBeUndefined();

    electron.axRole = 'link';
    electron.nodeName = 'A';
    electron.nodeType = '';
    electron.nodeAttributes = { href: '/ordinary-link' };
    electron.includeForm = false;
    await snapshot(host);
    const linkMarker = await pageGuard(host, created.data.targetId);
    expect(linkMarker.elementNavigation['link\0submit']).toBe('https://example.com/ordinary-link');
    await host.dispose();
  });

  it('fails closed when the flattened DOM security index exceeds its bound', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.oversizedDom = true;
    await expect(snapshot(host)).rejects.toMatchObject({ code: 'dom_tree_too_large' });
    await host.dispose();
  });

  it('captures a hidden model viewport through CDP as a bounded private PNG', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const output = path.join(path.dirname(PROFILE), 'artifacts', 'hidden.png');
    await mkdir(path.dirname(output), { recursive: true });
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: [output],
        proxy_url: PROXY_URL,
      },
    });
    expect(result.data).toMatchObject({ path: output, width: 1024, height: 720 });
    expect(result.data.host_epoch).toMatch(/^[a-f0-9]{32}$/);
    expect(electron.views[0].visible).toBe(false);
    expect(electron.views[0].webContents.debugger.commands).toContainEqual({
      method: 'Page.captureScreenshot',
      params: { format: 'png', fromSurface: true, captureBeyondViewport: false },
    });
    expect((await stat(output)).mode & 0o777).toBe(0o600);
    await host.dispose();
  });

  it('settles a user export by releasing only focus left by Crew fill automation', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeName = 'INPUT';
    electron.nodeType = 'text';
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', '云南旅游视频'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    // Mirrors BrowserManager's post-fill observation. It consumes the private
    // value verifier but must retain provenance for a later settled export.
    await snapshot(host);
    expect(electron.focusedBackendNodeId).toBe(7);

    const output = path.join(path.dirname(PROFILE), 'artifacts', 'settled.png');
    await mkdir(path.dirname(output), { recursive: true });
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--settled', output],
        proxy_url: PROXY_URL,
      },
    });

    expect(result.data).toMatchObject({ settled: true, focus_released: true });
    expect(electron.focusedBackendNodeId).toBe(0);
    const commands = electron.views[0].webContents.debugger.commands;
    const blurIndex = commands.findIndex(
      (item: any) => item.method === 'Runtime.callFunctionOn'
        && String(item.params?.functionDeclaration).includes('prototype?.blur'),
    );
    const captureIndex = commands.findIndex((item: any) => item.method === 'Page.captureScreenshot');
    expect(blurIndex).toBeGreaterThanOrEqual(0);
    expect(captureIndex).toBeGreaterThan(blurIndex);
    expect(commands).toContainEqual({ method: 'Overlay.hideHighlight', params: undefined });
    await host.dispose();
  });

  it('preserves current interaction UI unless settled export is explicitly requested', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeName = 'INPUT';
    electron.nodeType = 'text';
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'query'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    const output = path.join(path.dirname(PROFILE), 'artifacts', 'current-state.png');
    await mkdir(path.dirname(output), { recursive: true });
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: [output],
        proxy_url: PROXY_URL,
      },
    });

    expect(result.data).toMatchObject({ settled: false, focus_released: false });
    expect(electron.focusedBackendNodeId).toBe(7);
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Runtime.callFunctionOn'
        && String(item.params?.functionDeclaration).includes('prototype?.blur'),
    )).toBe(false);
    await host.dispose();
  });

  it('fails closed instead of capturing a page navigated by a focusout handler', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeName = 'INPUT';
    electron.nodeType = 'text';
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'query'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    electron.navigateOnBlur = true;
    const output = path.join(path.dirname(PROFILE), 'artifacts', 'changed-on-blur.png');
    await mkdir(path.dirname(output), { recursive: true });

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--settled', output],
        proxy_url: PROXY_URL,
      },
    })).rejects.toMatchObject({ code: 'page_changed', uncertain: false });
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Page.captureScreenshot',
    )).toBe(false);
    await host.dispose();
  });

  it('carries a matching short-lived Crew searchbox proof across same-origin form navigation', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeName = 'INPUT';
    electron.nodeType = 'text';
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', '云南旅游视频'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    // Search result pages commonly recreate/autofocus the query box after a
    // form navigation. Keep only the prior fill provenance, not the old node.
    electron.views[0].webContents.emit('did-start-navigation', {
      isMainFrame: true,
      isSameDocument: false,
      url: 'https://example.com/search?q=travel',
    });
    electron.loaderId = 'loader-2';

    const output = path.join(path.dirname(PROFILE), 'artifacts', 'navigated-search.png');
    await mkdir(path.dirname(output), { recursive: true });
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--settled', output],
        proxy_url: PROXY_URL,
      },
    });

    expect(result.data).toMatchObject({ settled: true, focus_released: true });
    expect(electron.focusedBackendNodeId).toBe(0);
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Accessibility.getFullAXTree',
    )).toBe(true);
    await host.dispose();
  });

  it('does not blur cross-document autofocus when origin or DOM proof differs', async () => {
    const run = async (navigationUrl: string, changedAttributes: Record<string, string>) => {
      const host = new BrowserHost(() => fakeWindow());
      await createTab(host);
      electron.axRole = 'textbox';
      electron.axEditable = true;
      electron.nodeName = 'INPUT';
      electron.nodeType = 'text';
      await snapshot(host);
      await host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: {
          profile_dir: PROFILE,
          command: 'fill',
          args: ['@e1', 'query'],
          proxy_url: PROXY_URL,
          mutating: true,
        },
      });
      electron.views.at(-1).webContents.emit('did-start-navigation', {
        isMainFrame: true,
        isSameDocument: false,
        url: navigationUrl,
      });
      electron.loaderId = `loader-${electron.views.length + 1}`;
      electron.nodeAttributes = changedAttributes;
      const output = path.join(
        path.dirname(PROFILE),
        'artifacts',
        `unmatched-focus-${electron.views.length}.png`,
      );
      await mkdir(path.dirname(output), { recursive: true });
      const result: any = await host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: {
          profile_dir: PROFILE,
          command: 'screenshot',
          args: ['--settled', output],
          proxy_url: PROXY_URL,
        },
      });
      expect(result.data.focus_released).toBe(false);
      expect(electron.focusedBackendNodeId).toBe(7);
      await host.dispose();
    };

    await run('https://other.example/search', {});
    electron.loaderId = 'loader-1';
    electron.nodeAttributes = {};
    electron.focusedBackendNodeId = 0;
    await run('https://example.com/search', { placeholder: 'different field' });
  });

  it('consumes expired cross-document Crew focus proof without blurring', async () => {
    const now = vi.spyOn(Date, 'now').mockReturnValue(1_000);
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeName = 'INPUT';
    electron.nodeType = 'text';
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'query'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    electron.views[0].webContents.emit('did-start-navigation', {
      isMainFrame: true,
      isSameDocument: false,
      url: 'https://example.com/search',
    });
    electron.loaderId = 'loader-2';
    now.mockReturnValue(7_000);
    const output = path.join(path.dirname(PROFILE), 'artifacts', 'expired-focus.png');
    await mkdir(path.dirname(output), { recursive: true });
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--settled', output],
        proxy_url: PROXY_URL,
      },
    });

    expect(result.data.focus_released).toBe(false);
    expect(electron.focusedBackendNodeId).toBe(7);
    now.mockRestore();
    await host.dispose();
  });

  it('does not blur an editable element whose focus was not created by Crew automation', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeName = 'INPUT';
    electron.nodeType = 'text';
    electron.focusedBackendNodeId = 7;
    const output = path.join(path.dirname(PROFILE), 'artifacts', 'site-focus.png');
    await mkdir(path.dirname(output), { recursive: true });
    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--settled', output],
        proxy_url: PROXY_URL,
      },
    });

    expect(result.data).toMatchObject({ settled: true, focus_released: false });
    expect(electron.focusedBackendNodeId).toBe(7);
    await host.dispose();
  });

  it('fills only a currently editable AX control and uses the platform select-all modifier', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'blocked'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'not_editable' });

    electron.axRole = 'textbox';
    electron.axEditable = true;
    await snapshot(host);
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'allowed'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });
    const selectAll = electron.views[0].webContents.debugger.commands.find(
      (item: any) => item.method === 'Input.dispatchKeyEvent'
        && item.params?.type === 'rawKeyDown'
        && item.params?.code === 'KeyA',
    );
    expect(selectAll?.params).toMatchObject({ modifiers: process.platform === 'darwin' ? 4 : 2 });
    expect(electron.views[0].webContents.debugger.commands).toContainEqual({
      method: 'Input.insertText',
      params: { text: 'allowed' },
    });
    await host.dispose();
  });

  it('echoes only the field just filled by automation and only once', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.axValue = 'private prefill';
    const before: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: ['--compact'], proxy_url: PROXY_URL },
    });
    expect(before.data.snapshot).not.toContain('private prefill');

    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'automation query'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    const verified: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: ['--compact'], proxy_url: PROXY_URL },
    });
    expect(verified.data.snapshot).toContain('value="automation query"');

    const later: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: ['--compact'], proxy_url: PROXY_URL },
    });
    expect(later.data.snapshot).not.toContain('automation query');
    await host.dispose();
  });

  it('does not expose a prefilled value when insertText was rejected', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.axValue = 'private prefill';
    await snapshot(host);
    electron.blockInsertText = true;

    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'expected query'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    const observed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: ['--compact'], proxy_url: PROXY_URL },
    });
    expect(observed.data.snapshot).not.toContain('private prefill');
    expect(observed.data.snapshot).not.toContain('value=');
    await host.dispose();
  });

  it('clears pending filled-value verification across control mode transitions', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'automation query'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'set_mode',
      params: { profile_dir: PROFILE, target_id: created.data.targetId, mode: 'human' },
    });
    electron.axValue = 'human private value';
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'set_mode',
      params: { profile_dir: PROFILE, target_id: created.data.targetId, mode: 'ai' },
    });
    const observed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'snapshot', args: ['--compact'], proxy_url: PROXY_URL },
    });
    expect(observed.data.snapshot).not.toContain('human private value');
    await host.dispose();
  });

  it('rejects fill when a focus handler redirects away from the exact ref', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    await snapshot(host);
    electron.focusRedirectBackendNodeId = 8;

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'secret'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'target_focus_lost', uncertain: false });
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Input.insertText',
    )).toBe(false);
    await host.dispose();
  });

  it('binds Enter to an exact snapshot ref and rejects unknown focus submission', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'press',
        args: ['Enter'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'invalid_input' });

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'press',
        args: ['Enter', '@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });
    expect(electron.views[0].webContents.debugger.commands).toContainEqual({
      method: 'DOM.focus',
      params: { backendNodeId: 7 },
    });
    expect(electron.views[0].webContents.debugger.commands).toContainEqual({
      method: 'Input.dispatchKeyEvent',
      params: {
        type: 'rawKeyDown',
        key: 'Enter',
        code: 'Enter',
        windowsVirtualKeyCode: 13,
      },
    });
    await host.dispose();
  });

  it('type submit inserts the text and presses Enter atomically in one RPC', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    await snapshot(host);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', '世界杯赛况', '--submit'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });

    const commands = electron.views[0].webContents.debugger.commands;
    const insertIdx = commands.findIndex(
      (item: any) => item.method === 'Input.insertText' && item.params?.text === '世界杯赛况',
    );
    // Enter must be dispatched AFTER the text, within this single RPC — no model
    // round trip (and no page change) can slip between the fill and the submit.
    const enterIdx = commands.findIndex(
      (item: any) => item.method === 'Input.dispatchKeyEvent'
        && item.params?.type === 'rawKeyDown'
        && item.params?.code === 'Enter',
    );
    expect(insertIdx).toBeGreaterThanOrEqual(0);
    expect(enterIdx).toBeGreaterThan(insertIdx);
    // 回车前必须重新聚焦目标：insertText 的 input handler 可能把焦点迁走，
    // 否则回车会提交到未知元素。用 lastIndexOf——fillRef 自己先 focus 过一次。
    const lastFocusIdx = commands
      .map((item: any) => item.method)
      .lastIndexOf('DOM.focus');
    expect(lastFocusIdx).toBeGreaterThan(insertIdx);
    expect(enterIdx).toBeGreaterThan(lastFocusIdx);
    await host.dispose();
  });

  it('recovers from a transient focus bounce instead of failing the fill', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    await snapshot(host);

    // 百度这类页面：搜索框一聚焦就弹联想层，焦点被弹走一下随即回来。一次性断言会
    // 直接判死（用户 trace 里的「页面把焦点移离了 snapshot 目标，输入已拒绝」）；
    // 有界重试应当自愈。这里让第一次 DOM.focus 被重定向，之后恢复正常。
    electron.focusRedirectBackendNodeId = 8;
    setTimeout(() => { electron.focusRedirectBackendNodeId = 0; }, 30);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', '云南旅游视频'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });

    // 自愈不等于放松：文本只能写入一次，重试绝不能造成重复输入。
    const inserts = electron.views[0].webContents.debugger.commands.filter(
      (item: any) => item.method === 'Input.insertText',
    );
    expect(inserts).toHaveLength(1);
    electron.focusRedirectBackendNodeId = 0;
    await host.dispose();
  });

  it('type submit refuses to press Enter when the form destination changed', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    electron.nodeAttributes = { formaction: '/search' };
    await snapshot(host);

    // 页面在 insertText 同步触发的 input handler 里把提交目的地改掉——用户批准的是
    // 「在搜索框填词并回车」，此时回车会提交到攻击者选定的地址。必须拒绝。
    electron.nodeAttributesAfterInsertText = { formaction: 'https://attacker.example/exfil' };
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', '世界杯赛况', '--submit'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'stale_ref_security' });

    // 关键：文本已写入，但回车绝不能发出去。
    const keys = electron.views[0].webContents.debugger.commands.filter(
      (item: any) => item.method === 'Input.dispatchKeyEvent' && item.params?.code === 'Enter',
    );
    expect(keys).toHaveLength(0);
    electron.nodeAttributes = {};
    await host.dispose();
  });

  it('plain fill without --submit never presses Enter', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.axRole = 'textbox';
    electron.axEditable = true;
    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'fill',
        args: ['@e1', 'just text'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    const enter = electron.views[0].webContents.debugger.commands.find(
      (item: any) => item.method === 'Input.dispatchKeyEvent' && item.params?.code === 'Enter',
    );
    expect(enter).toBeUndefined();
    await host.dispose();
  });

  it('keeps exact refs across same-document URL changes but rejects a new loader', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);

    // Search/autocomplete pages commonly update query/history while a user is
    // answering an approval prompt. The document and exact target are intact.
    electron.views[0].webContents.url = 'https://example.com/?query=updated';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'press',
        args: ['Enter', '@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });

    // A cross-document navigation has a new loader and remains fail-closed.
    electron.loaderId = 'loader-2';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'press',
        args: ['Enter', '@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'stale_ref_security', uncertain: false });
    await host.dispose();
  });

  it('rejects ref-bound Enter when focus is redirected before dispatch', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    electron.focusRedirectBackendNodeId = 8;

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'press',
        args: ['Enter', '@e1'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'target_focus_lost', uncertain: false });
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Input.dispatchKeyEvent',
    )).toBe(false);
    await host.dispose();
  });

  it('uploads only when the current DOM node is an input[type=file]', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    const uploadPath = path.join(tempRoot, 'upload.txt');
    await writeFile(uploadPath, 'upload');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'upload',
        args: ['@e1', uploadPath],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'invalid_upload_target' });

    electron.nodeName = 'INPUT';
    electron.nodeType = 'file';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'upload',
        args: ['@e1', uploadPath],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).rejects.toMatchObject({ code: 'stale_ref_security' });
    await snapshot(host);
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'upload',
        args: ['@e1', uploadPath],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    })).resolves.toMatchObject({ success: true });
    expect(electron.views[0].webContents.debugger.commands).toContainEqual({
      method: 'DOM.setFileInputFiles',
      params: { backendNodeId: 7, files: [uploadPath] },
    });
    await host.dispose();
  });

  it('accepts approved-downloads targets while keeping quarantine as a separate fixed root', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const { promise, target } = await beginDownload(host, created.data.targetId);
    const item = new FakeDownloadItem();
    const event = { preventDefault: vi.fn() };
    electron.sessions[0].emit('will-download', event, item, electron.views[0].webContents);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(item.savePath).toBe(target);
    await writeFile(target, 'data');
    item.complete();

    await expect(promise).resolves.toMatchObject({ path: target, bytes: 4 });
    await host.dispose();
  });

  it('does not let an unrelated same-tab download steal the active one-shot grant', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const { promise, target } = await beginDownload(host, created.data.targetId);
    let settled = false;
    void promise.then(
      () => { settled = true; },
      () => { settled = true; },
    );

    const unrelated = new FakeDownloadItem();
    unrelated.url = 'https://example.com/scheduled-unrelated.txt';
    const unrelatedEvent = { preventDefault: vi.fn() };
    electron.sessions[0].emit(
      'will-download',
      unrelatedEvent,
      unrelated,
      electron.views[0].webContents,
    );
    await Promise.resolve();
    expect(unrelatedEvent.preventDefault).toHaveBeenCalledOnce();
    expect(unrelated.cancelled).toBe(true);
    expect(settled).toBe(false);

    const expected = new FakeDownloadItem();
    const expectedEvent = { preventDefault: vi.fn() };
    electron.sessions[0].emit(
      'will-download',
      expectedEvent,
      expected,
      electron.views[0].webContents,
    );
    expect(expectedEvent.preventDefault).not.toHaveBeenCalled();
    expect(expected.savePath).toBe(target);
    await writeFile(target, 'data');
    expected.complete();
    await expect(promise).resolves.toMatchObject({ path: target, bytes: 4 });
    await host.dispose();
  });

  it('rejects the legacy quarantine-as-approved download boundary', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const browserRoot = path.dirname(PROFILE);
    const quarantine = path.join(browserRoot, 'download-quarantine');
    const unsafeTarget = path.join(quarantine, 'file.txt');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'download',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        ref: '@e1',
        target: unsafeTarget,
        download_dir: quarantine,
        proxy_url: PROXY_URL,
        max_bytes: 1024,
        timeout_ms: 10_000,
      },
    })).rejects.toMatchObject({ code: 'invalid_download_path' });
    await host.dispose();
  });

  it('lets deny_downloads preempt a pending download and cancels/deletes its partial file', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const { promise, target } = await beginDownload(host, created.data.targetId);
    const rejected = expect(promise).rejects.toMatchObject({ code: 'download_denied' });
    const item = new FakeDownloadItem();
    electron.sessions[0].emit(
      'will-download',
      { preventDefault: vi.fn() },
      item,
      electron.views[0].webContents,
    );
    await writeFile(target, 'partial');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'deny_downloads',
      params: { profile_dir: PROFILE },
    })).resolves.toEqual({ denied: true });
    await rejected;
    expect(item.cancelled).toBe(true);
    await expect(access(target)).rejects.toMatchObject({ code: 'ENOENT' });
    await host.dispose();
  });

  it('lets close_owner and clear_owner_data preempt the ordinary owner queue', async () => {
    const closingHost = new BrowserHost(() => fakeWindow());
    const first: any = await createTab(closingHost);
    const closingDownload = await beginDownload(closingHost, first.data.targetId);
    const closingRejected = expect(closingDownload.promise).rejects.toMatchObject({ code: 'owner_closed' });
    const item = new FakeDownloadItem();
    electron.sessions[0].emit(
      'will-download',
      { preventDefault: vi.fn() },
      item,
      electron.views[0].webContents,
    );
    await writeFile(closingDownload.target, 'partial');
    await expect(closingHost.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'close_owner',
      params: { profile_dir: PROFILE },
    })).resolves.toEqual({ closed: true });
    await closingRejected;
    expect(item.cancelled).toBe(true);
    await expect(access(closingDownload.target)).rejects.toMatchObject({ code: 'ENOENT' });
    await closingHost.dispose();

    electron.sessions.splice(0);
    electron.views.splice(0);
    const clearingHost = new BrowserHost(() => fakeWindow());
    const second: any = await createTab(clearingHost);
    const clearingDownload = await beginDownload(clearingHost, second.data.targetId);
    const clearingRejected = expect(clearingDownload.promise).rejects.toMatchObject({
      code: 'download_cancelled',
    });
    await expect(clearingHost.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'clear_owner_data',
      params: { profile_dir: PROFILE },
    })).resolves.toEqual({ cleared: true });
    await clearingRejected;
    expect(electron.views[0].webContents.destroyed).toBe(true);
    await clearingHost.dispose();
  });

  it('rewires a reused Electron Session without leaking a stale download listener', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const reusedSession = electron.sessions[0];
    expect(reusedSession.listenerCount('will-download')).toBe(1);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'close_owner',
      params: { profile_dir: PROFILE },
    });
    expect(reusedSession.listenerCount('will-download')).toBe(0);

    const reopened: any = await createTab(host);
    expect(electron.sessions[0]).toBe(reusedSession);
    expect(reusedSession.listenerCount('will-download')).toBe(1);
    const { promise, target } = await beginDownload(host, reopened.data.targetId);
    const item = new FakeDownloadItem();
    const event = { preventDefault: vi.fn() };
    reusedSession.emit('will-download', event, item, electron.views.at(-1).webContents);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(item.cancelled).toBe(false);
    await writeFile(target, 'data');
    item.complete();
    await expect(promise).resolves.toMatchObject({ path: target });
    await host.dispose();
    expect(reusedSession.listenerCount('will-download')).toBe(0);
  });

  it('clears shared Session data in a deterministic sequence before closing connections', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    electron.sessions[0].clearSteps = [];
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'clear_owner_data',
      params: { profile_dir: PROFILE },
    })).resolves.toEqual({ cleared: true });
    expect(electron.sessions[0].clearSteps).toEqual([
      'clearData',
      'clearAuthCache',
      'clearHostResolverCache',
      'closeAllConnections',
    ]);
    await host.dispose();
  });

  it('expires a download grant from timeout_ms before the bridge deadline', async () => {
    vi.useFakeTimers();
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    electron.axRole = 'link';
    electron.nodeName = 'A';
    electron.nodeType = '';
    electron.nodeAttributes = { href: '/file.txt', download: '' };
    await snapshot(host);
    const browserRoot = path.dirname(PROFILE);
    const target = path.join(browserRoot, 'approved-downloads', 'grant-timeout', 'file.txt');
    await mkdir(path.dirname(target), { recursive: true });
    const promise = host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'download',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        ref: '@e1',
        target,
        download_dir: path.join(browserRoot, 'download-quarantine'),
        proxy_url: PROXY_URL,
        max_bytes: 1024,
        timeout_ms: 1_000,
      },
    });
    const rejected = expect(promise).rejects.toMatchObject({ code: 'download_timeout' });
    await vi.advanceTimersByTimeAsync(600);
    await rejected;
    await host.dispose();
  });

  it('denies every download that lacks a one-shot bound grant', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const item = {
      cancel: vi.fn(),
    };
    const event = { preventDefault: vi.fn() };

    electron.sessions[0].emit('will-download', event, item, electron.views[0].webContents);

    expect(event.preventDefault).toHaveBeenCalledOnce();
    expect(item.cancel).toHaveBeenCalledOnce();
    await host.dispose();
  });
});
