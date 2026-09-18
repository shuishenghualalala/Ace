import { createHash } from 'node:crypto';
import { EventEmitter } from 'node:events';
import { access, mkdtemp, mkdir, realpath, rm, stat, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const electron = vi.hoisted(() => ({
  sessions: [] as any[],
  views: [] as any[],
  popupProvisionalContents: [] as any[],
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
  // 追加几个与主目标同 role 同名、但 href 不同的元素（backendNodeId 20+i），
  // 用来验重名元素的指纹绑定。
  duplicateTargets: 0,
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
  debuggerCommandGates: new Map<string, Promise<void>>(),
  initialWebContentsURL: 'about:blank',
  loadURLGates: new Map<string, Promise<void>>(),
  frameExecuteHook: null as null | ((
    contents: any,
    expression: string,
  ) => void | Promise<void>),
}));

vi.mock('electron', async () => {
  const { EventEmitter } = await import('node:events');

  class FakeDebugger extends EventEmitter {
    attached = false;
    commands: Array<{ method: string; params: unknown; sessionId?: string }> = [];

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

    async sendCommand(method: string, params?: unknown, sessionId?: string): Promise<any> {
      this.commands.push({ method, params, ...(sessionId ? { sessionId } : {}) });
      const gate = electron.debuggerCommandGates.get(`${sessionId ?? ''}\u0000${method}`);
      if (gate) await gate;
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
            ...Array.from({ length: electron.duplicateTargets }, (_, index) => ({
              backendDOMNodeId: 20 + index,
              role: { value: electron.axRole },
              name: { value: 'Submit' },
              properties: [{ name: 'focusable', value: { value: true } }],
            })),
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
        // 重名兄弟节点：role/name 与主目标相同，只有 href 不同——正是列表页
        // 每行一个「详情」链接的形状。
        const duplicates = Array.from({ length: electron.duplicateTargets }, (_, index) => ({
          backendNodeId: 20 + index,
          nodeName: electron.nodeName,
          attributes: ['aria-label', 'Submit', 'href', `/row-${index + 1}`],
        }));
        const content = electron.includeForm
          ? [{
              backendNodeId: 6,
              nodeName: 'FORM',
              attributes: Object.entries(electron.formAttributes).flatMap(([name, value]) => [name, value]),
              children: [target, ...duplicates],
            }]
          : [target, ...duplicates];
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
        if ((params as any)?.objectId === 'active-element-1') {
          return {
            node: {
              backendNodeId: electron.focusedBackendNodeId,
              nodeName: electron.nodeName,
              attributes: [
                'aria-label', 'Submit',
                'type', electron.nodeType,
                ...Object.entries(electron.nodeAttributes).flatMap(([n, v]) => [n, v]),
              ],
            },
          };
        }
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
        if (String((params as any)?.expression || '') === 'location.href') {
          return { result: { value: 'https://example.com/' } };
        }
        // 记录自动化焦点出处：宿主问「现在谁有焦点」，拿到句柄后再 describeNode。
        if (String((params as any)?.expression || '') === 'document.activeElement') {
          return electron.focusedBackendNodeId
            ? { result: { objectId: 'active-element-1' } }
            : { result: {} };
        }
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
    url = electron.initialWebContentsURL;
    loadURLCalls: string[] = [];
    title = '';
    destroyed = false;
    focused = false;
    opener: any = null;
    windowOpenHandler: ((details: any) => any) | null = null;
    canGoBackValue = false;
    canGoForwardValue = false;
    goBackCalled = false;
    goForwardCalled = false;
    mainFrame: any;
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

    constructor() {
      super();
      const mainFrame: any = {
        detached: false,
        processId: 1,
        executeJavaScript: async (expression: string): Promise<unknown> => {
          await electron.frameExecuteHook?.(this, expression);
          return expression.includes('timeOrigin:performance.timeOrigin')
            ? {
                href: this.url || 'about:blank',
                timeOrigin: 1,
                scrollX: 0,
                scrollY: 0,
                width: 1024,
                height: 720,
                dpr: 1,
              }
            : undefined;
        },
      };
      mainFrame.framesInSubtree = [mainFrame];
      this.mainFrame = mainFrame;
    }

    async loadURL(url: string): Promise<void> {
      this.loadURLCalls.push(url);
      const gate = electron.loadURLGates.get(url);
      if (gate) await gate;
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

    stop(): void {}

    focus(): void {
      this.focused = true;
    }

    reload(): void {}

    setWindowOpenHandler(handler: (details: any) => any): void {
      this.windowOpenHandler = (details: any): any => {
        const response = handler(details);
        if (!response?.createWindow) return response;
        const createWindow = response.createWindow;
        return {
          ...response,
          createWindow: (provided: any = {}): any => {
            // Electron 43 supplies a provisional WebContents for ordinary
            // window.open, but not for a real middle-click background tab.
            const provisional = provided.webContents ?? (
              details.disposition === 'background-tab'
                ? undefined
                : new FakeWebContents()
            );
            if (provisional) {
              provisional.url = '';
              provisional.opener = this.mainFrame;
              electron.popupProvisionalContents.push(provisional);
            }
            const overridePreferences =
              response.overrideBrowserWindowOptions?.webPreferences ?? {};
            return createWindow({
              show: false,
              width: 800,
              height: 600,
              ...provided,
              ...(provisional ? { webContents: provisional } : {}),
              webPreferences: {
                ...overridePreferences,
                ...(provided.webPreferences ?? {}),
              },
            });
          },
        };
      };
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
    webContents: FakeWebContents;
    options: any;
    bounds: any = null;
    visible = false;

    constructor(options: any = {}) {
      this.options = options;
      this.webContents = options.webContents ?? new FakeWebContents();
      electron.views.push(this);
    }

    setBounds(bounds: any): void {
      this.bounds = bounds;
    }

    getBounds(): any {
      return this.bounds ?? { x: 0, y: 0, width: 1280, height: 800 };
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

  // 自动化宿主窗口（AutomationHost）用的替身。它永远不 show，只是给 WebContentsView
  // 一个合成上下文——没有它，隐藏的 view 既不出帧也没有视口，Playwright 点不动。
  class FakeBrowserWindow extends EventEmitter {
    destroyed = false;
    readonly children: unknown[] = [];
    readonly contentView = {
      addChildView: (view: unknown): void => {
        const index = this.children.indexOf(view);
        if (index >= 0) this.children.splice(index, 1);
        this.children.push(view);
      },
      removeChildView: (view: unknown): void => {
        const index = this.children.indexOf(view);
        if (index >= 0) this.children.splice(index, 1);
      },
    };

    isDestroyed(): boolean { return this.destroyed; }
    destroy(): void { this.destroyed = true; this.emit('closed'); }
    getContentBounds(): { width: number; height: number } { return { width: 1280, height: 800 }; }
  }

  return {
    WebContentsView: FakeView,
    BrowserWindow: FakeBrowserWindow,
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

/**
 * Playwright 引擎替身。
 *
 * 单测跑在 node 环境，起不了 Electron，也就没有真实的 Playwright 连接。观察面与
 * 动作面的**行为**由真实 Electron 上的契约测试负责（`scripts/pw-contract.ts`）；
 * 这里替身的职责只是让 BrowserHost 自己的逻辑——owner 队列、下载授权、面板挂载、
 * 会话隔离——仍然可测。
 */
const playwright = vi.hoisted(() => ({
  /** 快照文本。为 null 时按 electron.axRole 现算，测试可改写以驱动不同 ref 集合。 */
  snapshot: null as string | null,
  /** 每个 playwright ref 的指纹材料。改它即可模拟「元素被就地改写」。 */
  material: new Map<string, string>(),
  /** 记录动作调用，供断言。 */
  calls: [] as Array<{ method: string; ref: string; args: unknown[] }>,
  url: 'https://example.test/',
  title: 'Example',
  /** 下载授权按 URL 绑定；默认与 FakeDownloadItem.url 一致。 */
  downloadNavigation: 'https://example.com/file.txt',
  /** 每个 owner 一台引擎；测试用它直接审计 lease 隔离与 focus mode。 */
  engines: [] as any[],
  automationModes: [] as Array<{ engine: unknown; view: unknown; enabled: boolean }>,
  inputDispatches: [] as Array<{
    engine: unknown;
    view: unknown;
    method: string;
    preventedDuringLease: boolean;
  }>,
  automationModeFailure: null as null | { enabled: boolean; message: string },
  /** Deterministically hold snapshot work so queue draining remains testable. */
  snapshotGate: null as Promise<void> | null,
  snapshotGateEntries: 0,
  snapshotCalls: 0,
  /** Fail after the input lease was acquired, at the irreversible dispatch boundary. */
  inputFailure: null as null | { method: string; message: string },
  /** Strict selector counts used by Host-level input primitives. */
  selectorCounts: new Map<string, number>(),
  /** Optional page behavior fired at the fake native click boundary. */
  clickHook: null as null | ((
    selector: string,
    engine: any,
    view: any,
  ) => void | Promise<void>),
  /** Optional public Page.goto behavior, including attachment downloads. */
  gotoHook: null as null | ((
    url: string,
    page: any,
    view: any,
  ) => void | Promise<void>),
  /** Optional authoritative destinations for public history/reload methods. */
  navigationUrls: {
    back: undefined,
    forward: undefined,
    reload: undefined,
  } as Record<'back' | 'forward' | 'reload', string | null | undefined>,
  reset(): void {
    this.snapshot = null;
    this.material = new Map();
    this.calls = [];
    this.url = 'https://example.test/';
    this.title = 'Example';
    this.downloadNavigation = 'https://example.com/file.txt';
    this.engines = [];
    this.automationModes = [];
    this.inputDispatches = [];
    this.automationModeFailure = null;
    this.snapshotGate = null;
    this.snapshotGateEntries = 0;
    this.snapshotCalls = 0;
    this.inputFailure = null;
    this.selectorCounts = new Map();
    this.clickHook = null;
    this.gotoHook = null;
    this.navigationUrls = {
      back: undefined,
      forward: undefined,
      reload: undefined,
    };
  },
}));

vi.mock('../../src/main/browser/playwright-engine', () => {
  const record = (method: string, ref: string, ...args: unknown[]): void => {
    playwright.calls.push({ method, ref, args });
  };
  const screenshotImage = (options?: { type?: string }): Buffer => {
    if (options?.type === 'jpeg') {
      return Buffer.from([0xff, 0xd8, 0xff, 0xd9]);
    }
    const png = Buffer.alloc(25);
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]).copy(png);
    png.writeUInt32BE(1024, 16);
    png.writeUInt32BE(720, 20);
    return png;
  };
  const makeLocator = (
    selector: string,
    view: any,
    engine: any,
  ): Record<string, unknown> => {
    const ref = selector.startsWith('aria-ref=') ? selector.slice('aria-ref='.length) : selector;
    const keywordTarget = ref === 'e2' || selector.includes('Keyword');
    const logicalRef = keywordTarget ? 'e2' : 'e1';
    const editableRoot = logicalRef === 'e1' && electron.axEditable;
    const targetRole = logicalRef === 'e2' ? 'textbox' : electron.axRole;
    const targetName = logicalRef === 'e2' ? 'Keyword' : 'Submit';
    const targetTag = logicalRef === 'e2' || editableRoot ? 'input' : 'button';
    const targetType = logicalRef === 'e2' || editableRoot ? 'text' : 'submit';
    const targetAction = logicalRef === 'e1' && !editableRoot ? 'submit' : '';
    const targetActionKind = logicalRef === 'e1' && !editableRoot ? 'submit' : 'input';
    const locator: Record<string, unknown> = {
      // role 跟随 electron.axRole：焦点释放路径要把快照记下的 role/name 与 AX 树
      // 里的当前焦点元素对上，两份桩不一致就永远匹配不上。
      ariaSnapshot: async (): Promise<string> => {
        playwright.snapshotCalls += 1;
        if (playwright.snapshotGate) {
          playwright.snapshotGateEntries += 1;
          await playwright.snapshotGate;
        }
        if (selector === 'body') {
          return playwright.snapshot
            ?? `- ${electron.axRole} "Submit" [ref=e1]\n- textbox "Keyword" [ref=e2]`;
        }
        return `- ${targetRole} "${targetName}"`;
      },
      // captureSnapshot 用它算指纹；默认给每个 ref 一份稳定材料。
      evaluate: async (pageFunction: unknown): Promise<unknown> => {
        if (String(pageFunction).includes('DataTransfer')) {
          record('dropEmpty', ref);
          return undefined;
        }
        return {
          material: playwright.material.get(logicalRef) ?? `tag BUTTON\nref ${logicalRef}`,
          navigation: '',
          // 与 FakeDownloadItem.url 保持一致：下载授权按 URL 绑定，对不上就会被拒。
          downloadNavigation: playwright.downloadNavigation,
          action: targetAction,
          actionKind: targetActionKind,
          accessibleRole: targetRole,
          accessibleName: targetName,
          documentBaseURI: playwright.url,
          documentURL: playwright.url,
          tag: targetTag,
          inputType: targetType,
          contentEditable: false,
          fieldProbe: {
            type: targetType,
            autocomplete: '',
            name: '',
            id: '',
            placeholder: '',
            ariaLabel: '',
            labelText: '',
          },
          complete: true,
        };
      },
      boundingBox: async (): Promise<unknown> => ({ x: 10, y: 20, width: 100, height: 40 }),
      scrollIntoViewIfNeeded: async (): Promise<void> => {},
      waitFor: async (): Promise<void> => {},
      isEditable: async (): Promise<boolean> => true,
      count: async (): Promise<number> => playwright.selectorCounts.get(selector) ?? 1,
      normalize: async (): Promise<unknown> => locator,
      click: async (options?: { trial?: boolean }): Promise<void> => {
        if (options?.trial) return;
        await engine.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseDown', async () => {
          record('click', ref, options);
          await playwright.clickHook?.(selector, engine, view);
        });
      },
      hover: async (options?: { trial?: boolean }): Promise<void> => {
        if (options?.trial) return;
        await engine.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseMoved', () => {
          record('hover', ref);
        });
      },
      fill: async (value: string): Promise<void> => {
        await engine.dispatchInput(view, 'Input.insertText', 'keyDown', () => {
          record('fill', ref, value);
          // 真实 fill 会把焦点留在输入框上；宿主随后据此记录自动化焦点出处。
          electron.focusedBackendNodeId = electron.focusRedirectBackendNodeId || 7;
        });
      },
      pressSequentially: async (value: string): Promise<void> => {
        for (const character of value) {
          await engine.dispatchInput(view, 'Input.dispatchKeyEvent', 'keyDown', () => {
            record('pressSequentially', ref, character);
          });
        }
      },
      dragTo: async (target: unknown, options?: unknown): Promise<void> => {
        await engine.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseDown', () => {
          record('dragTo', ref, target, options);
        });
      },
      press: async (key: string): Promise<void> => {
        await engine.dispatchInput(view, 'Input.dispatchKeyEvent', 'keyDown', () => {
          record('press', ref, key);
        });
      },
      selectOption: async (values: string[]): Promise<string[]> => {
        await engine.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseDown', () => {
          record('selectOption', ref, values);
        });
        return values;
      },
      setChecked: async (checked: boolean, options?: { trial?: boolean }): Promise<void> => {
        if (options?.trial) return;
        await engine.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseDown', () => {
          record('setChecked', ref, checked);
        });
      },
      drop: async (
        payload: { files?: string | string[]; data?: Record<string, string> },
      ): Promise<void> => {
        await engine.dispatchInput(view, 'Input.dispatchDragEvent', 'drop', () => {
          record('drop', ref, payload);
        });
      },
      setInputFiles: async (files: string[]): Promise<void> => record('upload', ref, files),
      screenshot: async (options?: { type?: string }): Promise<Buffer> => {
        record('locatorScreenshot', ref, options);
        return screenshotImage(options);
      },
      textContent: async (): Promise<string> => `text:${ref}`,
      getAttribute: async (name: string): Promise<string> => `${name}:${ref}`,
    };
    return locator;
  };
  class FakeEngine {
    inputLeaseHook: null | ((context: Record<string, unknown>) => unknown) = null;
    childSessionLifecycleHook:
      | null
      | ((context: Record<string, unknown>) => unknown) = null;
    pageLifecycleHook:
      | null
      | {
          createPage: (context: Record<string, unknown>) => Promise<string>;
          closePage: (context: Record<string, unknown>) => Promise<void>;
        } = null;
    modalStateHook:
      | null
      | ((view: any, kind: 'fileChooser') => void) = null;
    pendingFileChooser: any = null;
    private readonly pendingFileChoosers = new Map<any, any>();
    private readonly pendingFileChooserCollisions = new Map<any, number>();
    private readonly pages = new Map<any, any>();
    private readonly dialogs = new Map<any, any[]>();

    constructor() {
      playwright.engines.push(this);
    }

    setInputCommandLeaseHook(hook: null | ((context: Record<string, unknown>) => unknown)): void {
      this.inputLeaseHook = hook;
    }

    setChildSessionLifecycleHook(
      hook: null | ((context: Record<string, unknown>) => unknown),
    ): void {
      this.childSessionLifecycleHook = hook;
    }

    setPageLifecycleHook(
      hook: FakeEngine['pageLifecycleHook'],
    ): void {
      this.pageLifecycleHook = hook;
    }

    withPageLifecycleSource<T>(
      _view: unknown,
      _deadlineAt: number,
      operation: () => Promise<T>,
    ): Promise<T> {
      return operation();
    }

    async waitForViewTarget(view: any): Promise<string> {
      return `fake-chromium-target-${view.webContents.id}`;
    }

    setModalStateHook(
      hook: null | ((view: any, kind: 'fileChooser') => void),
    ): void {
      this.modalStateHook = hook;
    }

    hasPendingFileChooser(view?: any): boolean {
      return this.pendingFileChoosers.has(view) || this.pendingFileChooser !== null;
    }

    pendingFileChooserCount(view?: any): number {
      if (this.pendingFileChoosers.has(view)) {
        return 1 + (this.pendingFileChooserCollisions.get(view) ?? 0);
      }
      return this.pendingFileChooser !== null ? 1 : 0;
    }

    takePendingFileChooser(view?: any): any {
      const chooser = this.pendingFileChoosers.get(view) ?? this.pendingFileChooser;
      this.pendingFileChoosers.delete(view);
      this.pendingFileChooserCollisions.delete(view);
      if (chooser === this.pendingFileChooser) this.pendingFileChooser = null;
      return chooser;
    }

    async setAutomationMode(view: unknown, enabled: boolean): Promise<void> {
      playwright.automationModes.push({ engine: this, view, enabled });
      const failure = playwright.automationModeFailure;
      if (failure?.enabled === enabled) {
        playwright.automationModeFailure = null;
        throw new Error(failure.message);
      }
    }

    async dispatchInput(
      view: any,
      method: string,
      inputType: string,
      operation: () => void | Promise<void>,
    ): Promise<void> {
      const release = await this.inputLeaseHook?.({
        view,
        method,
        sessionId: 'fake-tab-session',
        sessionKind: 'tab',
      });
      let preventedDuringLease = false;
      const event = {
        preventDefault(): void {
          preventedDuringLease = true;
        },
      };
      if (method === 'Input.dispatchKeyEvent' || method === 'Input.insertText') {
        view.webContents.emit('before-input-event', event, { type: inputType, key: 'Enter' });
      } else {
        view.webContents.emit('before-mouse-event', event, { type: inputType });
      }
      playwright.inputDispatches.push({
        engine: this,
        view,
        method,
        preventedDuringLease,
      });
      try {
        const failure = playwright.inputFailure;
        if (failure?.method === method) {
          playwright.inputFailure = null;
          throw new Error(failure.message);
        }
        await operation();
      } finally {
        if (typeof release === 'function') await release();
      }
    }

    registerTab(): void {}
    registerNativeDownload(): void {}
    unregisterTab(): void {}
    releaseToPanel(): void {}
    reclaimFromPanel(): void {}
    async context(): Promise<unknown> { return { pages: () => [...this.pages.values()] }; }
    emitFileChooser(view: any, chooser: any): void {
      const page = this.pages.get(view);
      if (!page) throw new Error('fake page is not connected');
      page.emit('filechooser', chooser);
      this.modalStateHook?.(view, 'fileChooser');
    }
    emitDialog(
      view: any,
      options: {
        type?: string;
        message?: string;
        defaultValue?: string;
        onClosed?: (accepted: boolean, promptText: string) => void;
      } = {},
    ): any {
      const page = this.pages.get(view);
      if (!page) throw new Error('fake page is not connected');
      let resolveClosed!: () => void;
      let settled = false;
      const closed = new Promise<void>((resolve) => {
        resolveClosed = resolve;
      });
      const close = (accepted: boolean, promptText = ''): void => {
        if (settled) throw new Error('fake dialog already handled');
        settled = true;
        view.webContents.debugger.emit('message', {}, 'Page.javascriptDialogClosed', {
          result: accepted,
          userInput: promptText,
        });
        options.onClosed?.(accepted, promptText);
        resolveClosed();
      };
      const dialog = {
        type: () => options.type ?? 'confirm',
        message: () => options.message ?? 'Continue?',
        defaultValue: () => options.defaultValue ?? '',
        accept: async (promptText?: string): Promise<void> => {
          close(true, promptText ?? '');
        },
        dismiss: async (): Promise<void> => {
          close(false, '');
        },
        closed,
      };
      const queue = this.dialogs.get(view) ?? [];
      queue.push(dialog);
      this.dialogs.set(view, queue);
      view.webContents.debugger.emit('message', {}, 'Page.javascriptDialogOpening', {
        type: options.type ?? 'confirm',
        message: options.message ?? 'Continue?',
        defaultPrompt: options.defaultValue ?? '',
      });
      page.emit('dialog', dialog);
      return dialog;
    }
    async handleDialog(
      view: any,
      options: {
        accept: boolean;
        promptText?: string;
      },
    ): Promise<Record<string, unknown>> {
      const queue = this.dialogs.get(view) ?? [];
      const dialog = queue.shift();
      if (!queue.length) this.dialogs.delete(view);
      if (!dialog) throw new Error('fake dialog missing');
      const observed = {
        type: dialog.type(),
        message: dialog.message(),
        defaultValue: dialog.defaultValue(),
      };
      if (!options.accept) await dialog.dismiss();
      else await dialog.accept(options.promptText);
      return observed;
    }
    async pageForView(view: any): Promise<unknown> {
      let page = this.pages.get(view);
      if (page) return page;
      const mainFrame = {
        url: (): string => view.webContents.getURL() || playwright.url,
        parentFrame: (): null => null,
        evaluate: async (): Promise<void> => undefined,
        waitForLoadState: async (
          state: string,
          options?: { timeout?: number },
        ): Promise<void> => {
          record('waitForLoadState', '', state, options);
        },
      };
      page = Object.assign(new EventEmitter(), {
        locator: (selector: string) => makeLocator(selector, view, this),
        screenshot: async (options?: { type?: string }): Promise<Buffer> => {
          record('pageScreenshot', '', options);
          return screenshotImage(options);
        },
        ariaSnapshot: async (): Promise<string> => {
          playwright.snapshotCalls += 1;
          if (playwright.snapshotGate) {
            playwright.snapshotGateEntries += 1;
            await playwright.snapshotGate;
          }
          return playwright.snapshot
            ?? `- ${electron.axRole} "Submit" [ref=e1]\n- textbox "Keyword" [ref=e2]`;
        },
        url: (): string => view.webContents.getURL() || playwright.url,
        title: async (): Promise<string> => playwright.title,
        frames: (): unknown[] => [mainFrame],
        mainFrame: (): unknown => mainFrame,
        context: (): unknown => ({
          newCDPSession: async (): Promise<unknown> => ({
            send: async (
              method: string,
              params: Record<string, unknown>,
            ): Promise<Record<string, never>> => {
              record('cdpSend', '', method, params);
              return {};
            },
            detach: async (): Promise<void> => {
              record('cdpDetach', '');
            },
          }),
        }),
        goto: async (
          url: string,
          options?: { waitUntil?: string; timeout?: number },
        ): Promise<Record<string, never>> => {
          record('goto', '', url, options);
          await playwright.gotoHook?.(url, page, view);
          await view.webContents.loadURL(url);
          return {};
        },
        waitForLoadState: async (
          state: string,
          options?: { timeout?: number },
        ): Promise<void> => {
          record('waitForLoadState', '', state, options);
        },
        mouse: {
          move: async (x: number, y: number): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseMoved', () => {
              record('mouseMove', '', x, y);
            });
          },
          down: async (options?: { button?: string }): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseDown', () => {
              record('mouseDown', '', options);
            });
          },
          up: async (options?: { button?: string }): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseUp', () => {
              record('mouseUp', '', options);
            });
          },
          wheel: async (deltaX: number, deltaY: number): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseWheel', () => {
              record('wheel', '', deltaX, deltaY);
            });
          },
          click: async (
            x: number,
            y: number,
            options?: { button?: string; clickCount?: number; delay?: number },
          ): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchMouseEvent', 'mouseDown', () => {
              record('mouseClick', '', x, y, options);
            });
          },
        },
        keyboard: {
          press: async (key: string): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchKeyEvent', 'keyDown', () => {
              record('press', '', key);
            });
          },
          down: async (key: string): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchKeyEvent', 'keyDown', () => {
              record('keyDown', '', key);
            });
          },
          up: async (key: string): Promise<void> => {
            await this.dispatchInput(view, 'Input.dispatchKeyEvent', 'keyUp', () => {
              record('keyUp', '', key);
            });
          },
        },
        goBack: async (): Promise<Record<string, never> | null> => {
          record('goBack', '');
          const destination = playwright.navigationUrls.back;
          if (destination === null) return null;
          if (typeof destination === 'string') {
            view.webContents.emit('did-start-navigation', {
              isMainFrame: true,
              isSameDocument: false,
              url: destination,
            });
            view.webContents.url = destination;
            view.webContents.emit('did-navigate');
          }
          return {};
        },
        goForward: async (): Promise<Record<string, never> | null> => {
          record('goForward', '');
          const destination = playwright.navigationUrls.forward;
          if (destination === null) return null;
          if (typeof destination === 'string') {
            view.webContents.emit('did-start-navigation', {
              isMainFrame: true,
              isSameDocument: false,
              url: destination,
            });
            view.webContents.url = destination;
            view.webContents.emit('did-navigate');
          }
          return {};
        },
        reload: async (): Promise<null> => {
          record('reload', '');
          const destination = playwright.navigationUrls.reload;
          if (typeof destination === 'string') {
            view.webContents.emit('did-start-navigation', {
              isMainFrame: true,
              isSameDocument: false,
              url: destination,
            });
            view.webContents.url = destination;
            view.webContents.emit('did-navigate');
          }
          return null;
        },
        setViewportSize: async (size: { width: number; height: number }): Promise<void> => {
          record('setViewportSize', '', size);
        },
        evaluate: async (_runner: unknown, expression?: string): Promise<unknown> => {
          if (typeof _runner === 'function' && expression === undefined) {
            record('evaluateViewport', '');
            const bounds = view.getBounds();
            return { width: bounds.width, height: bounds.height };
          }
          record('evaluate', '', expression);
          return {
            result: expression === '() => 42'
              ? 42
              : expression === '() => undefined'
                ? undefined
                : expression,
            isFunction: expression?.startsWith('(') === true
              || expression?.includes('=>') === true,
            isUndefined: expression === '() => undefined',
          };
        },
        waitForTimeout: async (milliseconds: number): Promise<void> => {
          record('waitForTimeout', '', milliseconds);
        },
        consoleMessages: async (options?: unknown): Promise<unknown[]> => {
          record('consoleMessages', '', options);
          return [];
        },
        pageErrors: async (options?: unknown): Promise<unknown[]> => {
          record('pageErrors', '', options);
          return [];
        },
        clearConsoleMessages: async (): Promise<void> => {
          record('clearConsoleMessages', '');
        },
        clearPageErrors: async (): Promise<void> => {
          record('clearPageErrors', '');
        },
        getByText: (text: string) => ({
          first: () => ({
            waitFor: async (options: unknown): Promise<void> => {
              record('waitForText', '', text, options);
            },
          }),
        }),
      });
      page.on('filechooser', (chooser: unknown) => {
        if (this.pendingFileChoosers.has(view)) {
          this.pendingFileChooserCollisions.set(
            view,
            (this.pendingFileChooserCollisions.get(view) ?? 0) + 1,
          );
        } else {
          this.pendingFileChoosers.set(view, chooser);
        }
      });
      this.pages.set(view, page);
      return page;
    }
    async dispose(): Promise<void> {
      this.inputLeaseHook = null;
      this.childSessionLifecycleHook = null;
      this.pageLifecycleHook = null;
      this.modalStateHook = null;
      this.pendingFileChooser = null;
      this.pendingFileChoosers.clear();
      this.pendingFileChooserCollisions.clear();
      this.pages.clear();
      this.dialogs.clear();
    }
  }
  return { PlaywrightEngine: FakeEngine };
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
let currentTargetId = '';

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
  const result: any = await host.handleRpc({
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
  currentTargetId = String(result.data.targetId);
  return result;
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
      target_id: currentTargetId,
    },
  });
}

async function approvedUpload(name: string, contents = 'approved upload'): Promise<string> {
  const target = path.join(path.dirname(PROFILE), 'approved-uploads', 'test-stage', name);
  await mkdir(path.dirname(target), { recursive: true });
  await writeFile(target, contents);
  return realpath(target);
}

async function approvedUploads(names: string[]): Promise<string[]> {
  return Promise.all(names.map((name) => approvedUpload(name)));
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
      reset: true,
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
      command: 'vision_screenshot',
      args: [output],
      proxy_url: PROXY_URL,
      target_id: currentTargetId,
    },
  });
  return String(result.data.host_epoch);
}

class FakeDownloadItem extends EventEmitter {
  cancelled = false;
  receivedBytes = 4;
  totalBytes = 4;
  savePath = '';
  savePathCalls: string[] = [];
  setSavePathError: Error | null = null;
  state: 'progressing' | 'completed' | 'cancelled' | 'interrupted' = 'progressing';
  url = 'https://example.com/file.txt';
  urlChain: string[] = [];
  filename = 'file.txt';

  cancel(): void {
    this.cancelled = true;
    this.state = 'cancelled';
    this.emit('done', {}, 'cancelled');
  }

  getState(): string { return this.state; }
  getTotalBytes(): number { return this.totalBytes; }
  getReceivedBytes(): number { return this.receivedBytes; }
  getFilename(): string { return this.filename; }
  getURL(): string { return this.url; }
  getURLChain(): string[] { return this.urlChain; }
  setSavePath(value: string): void {
    if (this.setSavePathError) throw this.setSavePathError;
    this.savePath = value;
    this.savePathCalls.push(value);
  }

  update(
    receivedBytes: number,
    state: 'progressing' | 'interrupted' = 'progressing',
  ): void {
    this.receivedBytes = receivedBytes;
    this.state = state;
    this.emit('updated', {}, state);
  }

  complete(): void {
    this.state = 'completed';
    this.emit('done', {}, 'completed');
  }

  fail(): void {
    this.state = 'interrupted';
    this.emit('done', {}, 'interrupted');
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
  const browserRoot = path.dirname(PROFILE);
  const target = typeof overrides.target === 'string'
    ? overrides.target
    : path.join(browserRoot, 'approved-downloads', 'grant-one', 'file.txt');
  const quarantine = path.join(browserRoot, 'download-quarantine');
  await mkdir(path.dirname(target), { recursive: true });
  const clicksBefore = playwright.calls.filter((item) => item.method === 'click').length;
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
  // 点击已改走 Playwright Locator，不再是 Input.dispatchMouseEvent。
  // 等的是**本次**点击：同一个用例里可能连开两次下载，用累计存在性判断会让第二次
  // 立刻命中第一次的记录，从而在授权武装之前就返回。
  await vi.waitFor(() => {
    const clicks = playwright.calls.filter((item) => item.method === 'click').length;
    expect(clicks).toBeGreaterThan(clicksBefore);
  });
  return { promise, target, quarantine };
}

describe('BrowserHost', () => {
  beforeEach(async () => {
    playwright.reset();
    electron.sessions.splice(0);
    electron.views.splice(0);
    electron.popupProvisionalContents.splice(0);
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
    electron.duplicateTargets = 0;
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
    electron.debuggerCommandGates = new Map();
    electron.initialWebContentsURL = 'about:blank';
    electron.loadURLGates = new Map();
    electron.frameExecuteHook = null;
    currentTargetId = '';
    tempRoot = await mkdtemp(path.join(os.tmpdir(), 'crew-browser-host-'));
    PROFILE = path.join(tempRoot, 'accounts', ACCOUNT_DIR, 'browser', 'profile');
    await mkdir(PROFILE, { recursive: true });
    PROFILE = await realpath(PROFILE);
  });

  afterEach(async () => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    await rm(tempRoot, { recursive: true, force: true });
  });

  it('uses a dedicated persistent Session, grants web capabilities, and keeps renderer isolation', async () => {
    const window = fakeWindow();
    const host = new BrowserHost(() => window);

    await createTab(host);

    expect(electron.sessions).toHaveLength(1);
    expect(electron.sessions[0].profilePath).toBe(PROFILE);
    expect(electron.sessions[0].permissionCheck).toBeTypeOf('function');
    expect(electron.sessions[0].proxy).toMatchObject({
      mode: 'fixed_servers',
      proxyRules: PROXY_URL,
    });
    expect(electron.sessions[0].permissionCheck(
      electron.views[0].webContents,
      'geolocation',
      'https://example.com',
    )).toBe(true);
    const permissionDecision = vi.fn();
    electron.sessions[0].permissionRequest(
      electron.views[0].webContents,
      'camera',
      permissionDecision,
      { requestingUrl: 'https://example.com' },
    );
    expect(permissionDecision).toHaveBeenCalledWith(true);
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
      for (const url of [
        'file:///private/tmp/local-workflow.html',
        'data:text/html,<title>inline</title>',
        'custom+workflow://tenant/action?ticket=signed-value',
      ]) {
        const navigation = { url, preventDefault: vi.fn() };
        electron.views[0].webContents.emit(eventName, navigation);
        expect(navigation.preventDefault).not.toHaveBeenCalled();
      }
    }
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: '' },
    })).rejects.toMatchObject({ code: 'proxy_required' });
    expect(electron.sessions[0].proxy).toMatchObject({
      mode: 'fixed_servers',
      proxyRules: PROXY_URL,
    });

    await host.dispose();
  });

  it('materializes an initial blank document before debugger setup and target navigation', async () => {
    electron.initialWebContentsURL = '';
    const host = new BrowserHost(() => fakeWindow());

    await createTab(host);

    const contents = electron.views[0].webContents;
    expect(contents.loadURLCalls).toEqual([
      'about:blank',
      'https://example.com/',
    ]);
    expect(contents.debugger.commands.map((entry: { method: string }) => entry.method))
      .toEqual(expect.arrayContaining([
        'Page.enable',
        'DOM.enable',
        'Accessibility.enable',
        'Network.enable',
        'Overlay.enable',
      ]));
    expect(contents.debugger.commands.map((entry: { method: string }) => entry.method))
      .not.toContain('Runtime.enable');
    await host.dispose();
  });

  it('find captures once, de-duplicates shared ancestor refs, and returns executable refs', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    playwright.snapshot = [
      '- document "Example" [ref=e1]:',
      '  - paragraph: before',
      '  - button "Keyword one" [ref=e2]',
      '  - paragraph: a',
      '  - paragraph: b',
      '  - paragraph: c',
      '  - paragraph: d',
      '  - paragraph: e',
      '  - paragraph: f',
      '  - paragraph: g',
      '  - paragraph: h',
      '  - button "Keyword two" [ref=e3]',
      '  - paragraph: after',
    ].join('\n');
    playwright.snapshotCalls = 0;

    const findResult: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'find',
        args: ['--text', 'keyword'],
        mutating: false,
      },
    });

    expect(playwright.snapshotCalls).toBe(1);
    expect(findResult.data.snapshot).toContain('Found 2 matches for "keyword":');
    expect(findResult.data.snapshot).toContain('\n\n----\n\n');
    expect(findResult.data.snapshot.match(/document "Example"/g)).toHaveLength(2);
    expect(findResult.data.snapshot.match(/\[ref=@e1\]/g)).toHaveLength(1);
    expect(findResult.data.snapshot).toContain('- document "Example" :');
    expect(findResult.data.snapshot.match(/\[ref=@e2\]/g)).toHaveLength(1);
    expect(findResult.data.snapshot.match(/\[ref=@e3\]/g)).toHaveLength(1);
    // Host retains the complete executable ref table internally even though
    // the wire response carries no per-ref security maps.
    expect(findResult.data.ref_keys).toBeUndefined();

    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'click',
        args: ['@e2'],
        mutating: true,
      },
    });
    expect(playwright.calls).toContainEqual(expect.objectContaining({
      method: 'click',
      ref: 'e2',
    }));

    const callsBeforeInvalid = playwright.snapshotCalls;
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'find',
        args: ['--regex', '['],
        mutating: false,
      },
    })).rejects.toMatchObject({ code: 'invalid_find_query' });
    expect(playwright.snapshotCalls).toBe(callsBeforeInvalid);

    const missing: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'find',
        args: ['--text', 'does-not-exist'],
        mutating: false,
      },
    });
    expect(playwright.snapshotCalls).toBe(callsBeforeInvalid + 1);
    expect(missing.data.snapshot).toBe('No matches found for "does-not-exist".');
    expect(missing.data.ref_keys).toBeUndefined();

    await host.dispose();
  });

  it('normalizes bare public hosts to HTTPS and localhost hosts to HTTP before page.goto', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    playwright.calls = [];

    const open = (value: string) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'open',
        args: [value],
      },
    });
    const publicResult: any = await open('docs.example.test/guide?q=1#start');
    const localResult: any = await open('localhost:4173/app');

    expect(playwright.calls.filter((call) => call.method === 'goto')).toEqual([
      expect.objectContaining({
        args: [
          'https://docs.example.test/guide?q=1#start',
          expect.objectContaining({ waitUntil: 'domcontentloaded' }),
        ],
      }),
      expect.objectContaining({
        args: [
          'http://localhost:4173/app',
          expect.objectContaining({ waitUntil: 'domcontentloaded' }),
        ],
      }),
    ]);
    expect(playwright.calls.filter((call) => call.method === 'waitForLoadState')).toEqual([
      expect.objectContaining({ args: ['load', expect.objectContaining({ timeout: 5_000 })] }),
      expect.objectContaining({ args: ['load', expect.objectContaining({ timeout: 5_000 })] }),
    ]);
    expect(publicResult.data.url).toBe('https://docs.example.test/guide?q=1#start');
    expect(localResult.data.url).toBe('http://localhost:4173/app');
    await host.dispose();
  });

  it('bounds initial-document creation and rolls back the half-created tab', async () => {
    vi.useFakeTimers();
    electron.initialWebContentsURL = '';
    electron.loadURLGates.set('about:blank', new Promise<void>(() => undefined));
    const host = new BrowserHost(() => fakeWindow());

    const rejected = createTab(host).catch((error: unknown) => error);
    for (let turn = 0; turn < 10 && electron.views.length === 0; turn += 1) {
      await Promise.resolve();
    }
    expect(electron.views).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(5_001);
    await expect(rejected).resolves.toMatchObject({
      code: 'command_timeout',
    });

    expect(electron.views[0].webContents.destroyed).toBe(true);
    const internals = host as unknown as {
      owners: Map<string, { tabs: Map<string, unknown> }>;
    };
    expect(internals.owners.get(RUNTIME_KEY)?.tabs.size).toBe(0);
    await host.dispose();
  });

  it('bounds CDP domain enablement and rolls back the half-created tab', async () => {
    vi.useFakeTimers();
    electron.debuggerCommandGates.set(
      '\u0000Page.enable',
      new Promise<void>(() => undefined),
    );
    const host = new BrowserHost(() => fakeWindow());

    const rejected = createTab(host).catch((error: unknown) => error);
    for (let turn = 0; turn < 10 && electron.views.length === 0; turn += 1) {
      await Promise.resolve();
    }
    expect(electron.views).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(5_001);
    await expect(rejected).resolves.toMatchObject({
      code: 'debugger_unavailable',
    });

    expect(electron.views[0].webContents.destroyed).toBe(true);
    const internals = host as unknown as {
      owners: Map<string, { tabs: Map<string, unknown> }>;
    };
    expect(internals.owners.get(RUNTIME_KEY)?.tabs.size).toBe(0);
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

  it('serves an HTML artifact without blocking its arbitrary navigation and popup behavior', async () => {
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
    expect(await response.text()).toContain('Crew preview');

    const artifactContents = electron.views[0].webContents;
    const sameDocument = { url: `${previewUrl}#section`, preventDefault: vi.fn() };
    artifactContents.emit('will-navigate', sameDocument);
    expect(sameDocument.preventDefault).not.toHaveBeenCalled();
    const external = { url: 'https://collector.example/leak', preventDefault: vi.fn() };
    artifactContents.emit('will-navigate', external);
    expect(external.preventDefault).not.toHaveBeenCalled();
    expect(artifactContents.windowOpenHandler({
      url: 'https://collector.example/popup',
    })).toMatchObject({ action: 'allow' });

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

  it('uses Electron popup disposition for active-tab semantics and restores the opener on close', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const opener = electron.views[0].webContents;

    const background = opener.windowOpenHandler({
      url: 'https://example.com/background-popup',
      disposition: 'background-tab',
    });
    background.createWindow({});
    let listed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(listed.data.tabs.map((tab: any) => [tab.tabId, tab.active])).toEqual([
      ['t1', true],
      ['t2', false],
    ]);

    const foreground = opener.windowOpenHandler({
      url: 'https://example.com/foreground-popup',
      disposition: 'foreground-tab',
    });
    foreground.createWindow({});
    listed = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(listed.data.tabs.map((tab: any) => [tab.tabId, tab.active])).toEqual([
      ['t1', false],
      ['t2', false],
      ['t3', true],
    ]);

    // Renderer-driven popup close must restore its exact opener, not an
    // arbitrary tab left in the owner's insertion order.
    electron.views[2].webContents.close();
    listed = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(listed.data.tabs.map((tab: any) => [tab.tabId, tab.active])).toEqual([
      ['t1', true],
      ['t2', false],
    ]);
    await host.dispose();
  });

  it('keeps a human middle/Ctrl popup in the background without moving the visible panel', async () => {
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
    window.contentView.addChildView.mockClear();
    window.contentView.removeChildView.mockClear();

    const background = electron.views[0].webContents.windowOpenHandler({
      url: 'https://example.com/background-popup',
      disposition: 'background-tab',
    });
    background.createWindow({});

    const listed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: ['list'], proxy_url: PROXY_URL },
    });
    expect(listed.data.tabs.map((tab: any) => [tab.tabId, tab.active])).toEqual([
      ['t1', true],
      ['t2', false],
    ]);
    expect(window.contentView.addChildView).not.toHaveBeenCalled();
    expect(window.contentView.removeChildView).not.toHaveBeenCalled();
    expect(electron.views[0]).toMatchObject({
      visible: true,
      bounds: { x: 25, y: 35, width: 400, height: 300 },
    });
    expect(electron.views[1].visible).toBe(false);
    expect(electron.views[1].webContents.focused).toBe(false);
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
    expect(electron.popupProvisionalContents).toHaveLength(1);
    expect(electron.views[1].webContents).toBe(electron.popupProvisionalContents[0]);
    expect(electron.views[1].options.webContents).toBe(electron.popupProvisionalContents[0]);

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

  it('does not impose a product tab limit on one Crew session', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const opener = electron.views[0].webContents;

    for (let index = 0; index < 64; index += 1) {
      const decision = opener.windowOpenHandler({ url: `https://example.com/popup-${index}` });
      expect(decision.action).toBe('allow');
      decision.createWindow({});
    }
    expect(electron.views).toHaveLength(65);
    const beyondLegacyLimits = opener.windowOpenHandler({
      url: 'custom+workflow://example/sixty-sixth-page',
    });
    expect(beyondLegacyLimits.action).toBe('allow');
    beyondLegacyLimits.createWindow({});
    expect(electron.views).toHaveLength(66);
    await host.dispose();
  });

  it('标签页有失控护栏：正常规模不受限，无限循环撞得到上限', async () => {
    // 与上一条不矛盾：那条钉住"没有产品级上限"（66 个要能开），这条钉住
    // "护栏确实存在"。每个标签页是一个真实 WebContentsView（独立渲染进程 +
    // 一份 CDP 会话），完全无界时一段失控的 window.open 循环会耗尽主进程内存，
    // 应用整个卡死——而应用崩掉同样伤成功率。
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const opener = electron.views[0].webContents;

    let allowed = 0;
    let rejected = 0;
    for (let index = 0; index < 600; index += 1) {
      const decision = opener.windowOpenHandler({ url: `https://example.com/p${index}` });
      if (decision.action !== 'allow') { rejected += 1; continue; }
      try {
        decision.createWindow({});
        allowed += 1;
      } catch {
        rejected += 1;
      }
    }
    // 远超任何真实工作流的规模才会被挡住
    expect(allowed).toBeGreaterThan(400);
    expect(rejected).toBeGreaterThan(0);

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

  it('leases only Playwright input dispatch, releases idempotently, and rejects human/cross-owner views', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const view = electron.views[0];
    const engine = playwright.engines[0];

    await snapshot(host);
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        command: 'click',
        args: ['@e1'],
        mutating: true,
      },
    });
    expect(playwright.inputDispatches.at(-1)).toMatchObject({
      engine,
      view,
      method: 'Input.dispatchMouseEvent',
      preventedDuringLease: false,
    });

    // The action's release must have run: native input immediately after dispatch is blocked again.
    const afterAction = { preventDefault: vi.fn() };
    view.webContents.emit('before-mouse-event', afterAction, { type: 'mouseDown' });
    expect(afterAction.preventDefault).toHaveBeenCalledOnce();

    // The engine hook itself is a capability lease. Releasing twice must not underflow the
    // automation depth and accidentally let later native input through.
    const release = engine.inputLeaseHook({ view });
    expect(release).toBeTypeOf('function');
    const whileLeased = { preventDefault: vi.fn() };
    view.webContents.emit('before-input-event', whileLeased, { type: 'keyDown', key: 'A' });
    expect(whileLeased.preventDefault).not.toHaveBeenCalled();
    release();
    release();
    const afterDoubleRelease = { preventDefault: vi.fn() };
    view.webContents.emit('before-input-event', afterDoubleRelease, { type: 'keyDown', key: 'A' });
    expect(afterDoubleRelease.preventDefault).toHaveBeenCalledOnce();

    await setMode(host, created.data.targetId, 'human');
    let humanError: unknown;
    try {
      engine.inputLeaseHook({ view });
    } catch (error) {
      humanError = error;
    }
    expect(humanError).toMatchObject({ code: 'control_mode_blocked' });

    // A hook is owner-scoped, not merely "any registered WebContentsView".
    const foreignRuntimeKey = 'crew_123456789abc';
    const foreignProfile = path.join(
      tempRoot,
      'accounts',
      'acct_123456789abcdef0',
      'browser',
      'profile',
    );
    await mkdir(foreignProfile, { recursive: true });
    const foreignSessionHash = createHash('sha256')
      .update('foreign-session')
      .digest('hex')
      .slice(0, 32);
    await host.handleRpc({
      runtime_key: foreignRuntimeKey,
      method: 'execute',
      params: {
        profile_dir: foreignProfile,
        command: 'tab',
        args: ['new', '--label', `s${foreignSessionHash}-1`, 'https://foreign.example/'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    let crossOwnerError: unknown;
    try {
      engine.inputLeaseHook({ view: electron.views[1] });
    } catch (error) {
      crossOwnerError = error;
    }
    expect(crossOwnerError).toMatchObject({ code: 'control_mode_blocked' });
    await host.dispose();
  });

  it('routes the Playwright action-parity command set to one exact target', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const targetId = created.data.targetId;
    await snapshot(host);
    const dropFiles = await approvedUploads(['--literal-path', 'two.txt']);

    const execute = (command: string, args: string[] = [], extra: Record<string, unknown> = {}) =>
      host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: {
          profile_dir: PROFILE,
          proxy_url: PROXY_URL,
          target_id: targetId,
          command,
          args,
          ...extra,
        },
      });

    // Backward-compatible single-tab callers may omit target_id. The host can
    // still resolve one exact tab without guessing.
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        command: 'click',
        args: ['@e1'],
      },
    })).resolves.toMatchObject({ success: true });

    await execute('click', [
      '@e1',
      '--button', 'right',
      '--click-count', '2',
      '--delay-ms', '40',
      '--position-x', '127.5',
      '--position-y', '42.25',
      '--modifier', 'Shift',
    ]);
    await execute('fill', ['@e2', 'ab', '--slowly', '--submit']);
    await execute('drag', ['@e1', '@e2']);
    await execute('press', ['Enter']);
    await execute('keydown', ['Shift']);
    await execute('keyup', ['Shift']);
    await execute('wait', ['--time-seconds', '0.01', '--text', 'Ready']);
    await execute('scroll', ['--delta-x', '120', '--delta-y', '-80']);
    await execute('mouse', ['move', '-12.5', '42.25']);
    await execute('mouse', ['down', 'right']);
    await execute('mouse', ['up', 'middle']);
    await execute('mouse', ['wheel', '0', '0']);
    await execute('mouse', ['click', '127.5', '-42.25', 'right', '4', '5001.5']);
    await execute('mouse', ['drag', '-1', '-2', '1000003', '4.25']);
    await execute('resize', ['963.5', '707.25']);
    await execute('drop', [
      '@e1',
      '--path', dropFiles[0],
      '--path', dropFiles[1],
      '--data', 'text/plain', '--literal-value',
      '--data', 'text/uri-list', 'https://example.test/',
    ]);
    await execute('drop', ['@e1', '--empty-data']);
    await execute('select', ['@e1', '']);
    const batch: any = await execute('fill_form', [], {
      fields: [
        { type: 'textbox', ref: '@e2', value: 'hello' },
        { type: 'checkbox', ref: '@e1', value: true },
      ],
    });
    const dependentBatch: any = await execute('fill_form', [], {
      fields: [
        { type: 'combobox', selector: '#country', value: 'cn', select_by: 'value' },
        { type: 'textbox', selector: '#dependent', value: 'now-visible' },
      ],
    });
    const emptyOptionBatch: any = await execute('fill_form', [], {
      fields: [
        { type: 'combobox', selector: '#empty-option', value: '', select_by: 'value' },
      ],
    });
    playwright.inputFailure = {
      method: 'Input.dispatchMouseEvent',
      message: 'renderer lost after first form field',
    };
    await expect(execute('fill_form', [], {
      fields: [
        { type: 'textbox', ref: '@e2', value: 'first-completes' },
        { type: 'checkbox', ref: '@e1', value: false },
      ],
    })).rejects.toMatchObject({
      code: 'input_uncertain',
      partial: true,
      completed_count: 1,
    });
    const completionCountBeforeEvaluate = playwright.calls.filter(
      (call) => call.method === 'waitForTimeout' && call.args[0] === 500,
    ).length;
    const evaluateResult: any = await execute('eval', ['() => 42']);
    expect(playwright.calls.filter(
      (call) => call.method === 'waitForTimeout' && call.args[0] === 500,
    )).toHaveLength(completionCountBeforeEvaluate + 1);
    const completionCountBeforeRunCode = playwright.calls.filter(
      (call) => call.method === 'waitForTimeout' && call.args[0] === 500,
    ).length;
    const runCodeResult: any = await execute('run_code_unsafe', [
      `async page => ({
        title: await page.title(),
        processType: typeof process,
        requireType: typeof require,
      })`,
    ]);
    expect(playwright.calls.filter(
      (call) => call.method === 'waitForTimeout' && call.args[0] === 500,
    )).toHaveLength(completionCountBeforeRunCode + 1);
    await execute('snapshot', ['--compact']);
    await expect(execute('run_code_unsafe', [
      'async () => { throw new Error("snippet exploded") }',
    ])).rejects.toThrow('snippet exploded');
    await expect(execute('click', ['@e1'])).rejects.toMatchObject({
      code: 'stale_ref',
    });
    await expect(execute('run_code_unsafe')).rejects.toMatchObject({
      code: 'invalid_input',
    });
    await expect(execute('get', ['title'])).resolves.toMatchObject({
      success: true,
      data: { title: playwright.title },
    });
    await execute('forward');
    await execute('reload');

    expect(playwright.calls.find(
      (call) => call.method === 'click' && (call.args[0] as any)?.button === 'right',
    )).toMatchObject({
      ref: 'e1',
      args: [expect.objectContaining({
        button: 'right',
        clickCount: 2,
        delay: 40,
        position: { x: 127.5, y: 42.25 },
        modifiers: ['Shift'],
      })],
    });
    expect(playwright.calls.filter((call) => call.method === 'pressSequentially')).toHaveLength(2);
    expect(playwright.calls).toEqual(expect.arrayContaining([
      expect.objectContaining({ method: 'dragTo', ref: 'e1' }),
      expect.objectContaining({ method: 'press', ref: '', args: ['Enter'] }),
      expect.objectContaining({ method: 'keyDown', args: ['Shift'] }),
      expect.objectContaining({ method: 'keyUp', args: ['Shift'] }),
      expect.objectContaining({ method: 'waitForTimeout', args: [10] }),
      expect.objectContaining({ method: 'waitForText', args: ['Ready', expect.anything()] }),
      expect.objectContaining({ method: 'wheel', args: [120, -80] }),
      expect.objectContaining({ method: 'mouseMove', args: [-12.5, 42.25] }),
      expect.objectContaining({ method: 'mouseDown', args: [{ button: 'right' }] }),
      expect.objectContaining({ method: 'mouseUp', args: [{ button: 'middle' }] }),
      expect.objectContaining({ method: 'wheel', args: [0, 0] }),
      expect.objectContaining({
        method: 'mouseClick',
        args: [
          127.5,
          -42.25,
          {
            button: 'right',
            clickCount: 4,
            delay: 5001.5,
          },
        ],
      }),
      expect.objectContaining({ method: 'setViewportSize', args: [{ width: 963.5, height: 707.25 }] }),
      expect.objectContaining({ method: 'evaluate', args: ['() => 42'] }),
      expect.objectContaining({ method: 'goForward' }),
      expect.objectContaining({ method: 'reload' }),
    ]));
    expect(playwright.calls.filter((call) => call.method === 'mouseMove')).toEqual([
      expect.objectContaining({ args: [-12.5, 42.25] }),
      expect.objectContaining({ args: [-1, -2] }),
      expect.objectContaining({ args: [1000003, 4.25] }),
    ]);
    expect(playwright.calls).toEqual(expect.arrayContaining([
      expect.objectContaining({
        method: 'drop',
        ref: 'e1',
        args: [{
          files: dropFiles,
          data: {
            'text/plain': '--literal-value',
            'text/uri-list': 'https://example.test/',
          },
        }],
      }),
      expect.objectContaining({
        method: 'dropEmpty',
        ref: 'e1',
        args: [],
      }),
    ]));
    expect(evaluateResult.data).toMatchObject({
      value: 42,
      is_function: true,
      is_undefined: false,
    });
    expect(runCodeResult.data).toEqual({
      has_result: true,
      result: JSON.stringify({
        title: playwright.title,
        processType: 'undefined',
        requireType: 'undefined',
      }),
    });
    expect(batch.data.completed_count).toBe(2);
    expect(dependentBatch.data.completed_count).toBe(2);
    expect(emptyOptionBatch.data.completed_count).toBe(1);
    expect(playwright.calls).toEqual(expect.arrayContaining([
      expect.objectContaining({ method: 'selectOption', ref: '#country' }),
      expect.objectContaining({ method: 'selectOption', ref: 'e1', args: [['']] }),
      expect.objectContaining({ method: 'selectOption', ref: '#empty-option', args: [{ value: '' }] }),
      expect.objectContaining({ method: 'fill', ref: '#dependent' }),
    ]));

    // Once the owner has multiple tabs, an omitted target is genuinely
    // ambiguous and must not be guessed.
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        command: 'tab',
        args: ['new', '--label', `s${SESSION_HASH}-2`, 'https://second.example/'],
      },
    });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        command: 'click',
        args: ['@e1'],
      },
    })).rejects.toMatchObject({ code: 'invalid_target' });
    await host.dispose();
  });

  it('rejects malformed official mouse/resize/drop wire commands before dispatch', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const execute = (command: string, args: string[]) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
      },
    });

    for (const [command, args] of [
      ['mouse', ['move', 'NaN', '1']],
      ['mouse', ['down', 'primary']],
      ['mouse', ['click', '1', '2', 'left', '0', '0']],
      ['mouse', ['drag', '1', '2', '3']],
      ['resize', ['800']],
      ['drop', ['@e1']],
      ['drop', ['@e1', '--path']],
      ['drop', ['@e1', '--data', '', 'value']],
      ['drop', ['@e1', '--data', 'text/plain', 'a', '--data', 'text/plain', 'b']],
      ['drop', ['@e1', '--empty-data', '--data', 'text/plain', 'value']],
      ['drop', ['@e1', '--unknown', 'value']],
    ] as const) {
      await expect(execute(command, [...args])).rejects.toMatchObject({
        code: 'invalid_input',
      });
    }

    expect(playwright.calls).not.toEqual(expect.arrayContaining([
      expect.objectContaining({ method: 'mouseMove' }),
      expect.objectContaining({ method: 'mouseDown' }),
      expect.objectContaining({ method: 'mouseClick' }),
      expect.objectContaining({ method: 'setViewportSize' }),
      expect.objectContaining({ method: 'drop' }),
    ]));
    await host.dispose();
  });

  it('synchronizes Playwright focus emulation with control mode and fails closed on switch failure', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const view = electron.views[0];
    const engine = playwright.engines[0];

    expect(playwright.automationModes).toContainEqual({ engine, view, enabled: true });
    await setMode(host, created.data.targetId, 'human');
    expect(playwright.automationModes.at(-1)).toEqual({ engine, view, enabled: false });
    await setMode(host, created.data.targetId, 'ai');
    expect(playwright.automationModes.at(-1)).toEqual({ engine, view, enabled: true });
    await setMode(host, created.data.targetId, 'human');

    playwright.automationModeFailure = { enabled: true, message: 'focus override rejected' };
    await expect(setMode(host, created.data.targetId, 'ai')).rejects.toMatchObject({
      code: 'focus_mode_failed',
    });
    expect(playwright.automationModes.at(-1)).toEqual({ engine, view, enabled: true });

    // Failed return-to-AI must leave the page in its previous human mode; automation input
    // remains unavailable even though Electron's focus override rejected the transition.
    const nativeInput = { preventDefault: vi.fn() };
    view.webContents.emit('before-input-event', nativeInput, { type: 'keyDown', key: 'A' });
    expect(nativeInput.preventDefault).not.toHaveBeenCalled();
    let leaseError: unknown;
    try {
      engine.inputLeaseHook({ view });
    } catch (error) {
      leaseError = error;
    }
    expect(leaseError).toMatchObject({ code: 'control_mode_blocked' });
    await host.dispose();
  });

  it('preserves uncertain/phase/partial when a leased Playwright dispatch fails', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    playwright.inputFailure = {
      method: 'Input.dispatchMouseEvent',
      message: 'transport closed after mouseDown',
    };

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        command: 'click',
        args: ['@e1'],
        mutating: true,
      },
    })).rejects.toMatchObject({
      code: 'input_uncertain',
      uncertain: true,
      phase: 'dispatching',
      partial: true,
    });
    expect(playwright.inputDispatches.at(-1)).toMatchObject({
      method: 'Input.dispatchMouseEvent',
      preventedDuringLease: false,
    });
    const afterFailure = { preventDefault: vi.fn() };
    electron.views[0].webContents.emit(
      'before-mouse-event',
      afterFailure,
      { type: 'mouseDown' },
    );
    expect(afterFailure.preventDefault).toHaveBeenCalledOnce();
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

  it('allows mouse wheel over a mounted AI page without treating it as takeover', async () => {
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

    electron.views[0].webContents.emit('before-mouse-event', blocked, { type: 'mouseWheel' });

    // 滚轮只供用户阅读页面：放行但不改变控制权。
    expect(blocked.preventDefault).not.toHaveBeenCalled();
    expect(requested).not.toHaveBeenCalled();
    await host.dispose();
  });

  it('reports main-frame load failures to the panel but ignores aborted navigations', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const failed = vi.fn();
    host.on('tab-load-failed', failed);
    const contents = electron.views[0].webContents;

    // ERR_ABORTED(-3) 是新导航打断旧导航的正常信号，不上报。
    contents.emit('did-fail-load', {}, -3, 'aborted', contents.url, true, 1, 1);
    expect(failed).not.toHaveBeenCalled();

    contents.emit('did-fail-load', {}, -105, 'ERR_NAME_NOT_RESOLVED', 'https://missing.example/', true, 1, 1);
    expect(failed).toHaveBeenCalledOnce();
    expect(failed).toHaveBeenCalledWith({
      runtimeKey: RUNTIME_KEY,
      label: TAB_LABEL,
      url: 'https://missing.example/',
      errorDescription: 'ERR_NAME_NOT_RESOLVED',
    });

    // 子框架失败不上报。
    contents.emit('did-fail-load', {}, -105, 'ERR_NAME_NOT_RESOLVED', 'https://missing.example/', false, 1, 1);
    expect(failed).toHaveBeenCalledOnce();
    await host.dispose();
  });

  it('keeps exact tab metadata while control mode still gates automation and capture', async () => {
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
    expect(listed.data.tabs[0]).toMatchObject({
      title: 'private title',
      url: 'https://example.com/private?token=secret',
      targetId: created.data.targetId,
    });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: { profile_dir: PROFILE, command: 'tab', args: [TAB_LABEL], proxy_url: PROXY_URL },
    })).resolves.toMatchObject({ success: true });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'console',
        args: ['--clear'],
        proxy_url: PROXY_URL,
      },
    })).resolves.toMatchObject({ success: true, data: { text: '' } });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'network',
        args: ['requests', '--clear'],
        proxy_url: PROXY_URL,
      },
    })).resolves.toMatchObject({ success: true, data: { text: '[]' } });
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

  it('retains exact unbounded debug metadata in AI mode and ignores new human-mode collection', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const debug = vi.fn();
    host.on('debug', debug);
    const contents = electron.views[0].webContents;

    contents.emit(
      'console-message',
      {},
      1,
      'password=hunter2 Bearer abcdefghijklmnop',
      12,
      'https://example.com/app.js?access_token=raw-token',
    );
    contents.debugger.emit('message', {}, 'Network.requestWillBeSent', {
      request: { method: 'POST', url: 'https://example.com/api?token=raw-token' },
    });
    expect(debug).toHaveBeenCalledTimes(2);
    expect(JSON.stringify(debug.mock.calls)).toContain('hunter2');
    expect(JSON.stringify(debug.mock.calls)).toContain('abcdefghijklmnop');
    expect(JSON.stringify(debug.mock.calls)).toContain('raw-token');
    expect(debug.mock.calls[0][0]).toMatchObject({
      type: 'debug',
      channel: 'console',
      runtimeKey: RUNTIME_KEY,
      targetId: created.data.targetId,
      record: {
        text: 'password=hunter2 Bearer abcdefghijklmnop',
        source: 'https://example.com/app.js?access_token=raw-token',
        level: 'info',
      },
    });
    expect(debug.mock.calls.every(([event]) => event.type === 'debug')).toBe(true);

    for (let index = 0; index < 300; index += 1) {
      contents.debugger.emit('message', {}, 'Network.requestWillBeSent', {
        request: {
          method: 'POST',
          url: `https://example.com/api/${index}?payload=${'x'.repeat(1_000)}`,
        },
      });
    }
    const networkBeforeTakeover: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'network',
        args: ['requests'],
        proxy_url: PROXY_URL,
      },
    });
    const retained = JSON.parse(networkBeforeTakeover.data.text);
    expect(retained).toHaveLength(301);
    expect(retained[0].url).toBe('https://example.com/api?token=raw-token');
    expect(retained.at(-1).url).toBe(
      `https://example.com/api/299?payload=${'x'.repeat(1_000)}`,
    );

    await setMode(host, created.data.targetId, 'human');
    contents.emit('console-message', {}, 1, 'password=human-secret', 0, '');
    contents.debugger.emit('message', {}, 'Network.responseReceived', {
      response: { status: 200, url: 'https://example.com/?token=human-secret' },
    });
    expect(debug).toHaveBeenCalledTimes(302);

    await setMode(host, created.data.targetId, 'ai');
    const networkAfterTakeover: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'network',
        args: ['requests'],
        proxy_url: PROXY_URL,
      },
    });
    expect(JSON.parse(networkAfterTakeover.data.text)).toEqual(retained);
    await host.dispose();
  });

  it('retains and emits exact JavaScript dialog metadata across human takeover', async () => {
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
    await expect(setMode(host, created.data.targetId, 'human'))
      .rejects.toMatchObject({ code: 'dialog_pending' });
    contents.debugger.emit('message', {}, 'Page.javascriptDialogClosed', {
      result: false,
      userInput: '',
    });
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
    expect(dialog).toHaveBeenCalledTimes(2);
    expect(dialog.mock.calls[1][0]).toMatchObject({
      dialog: {
        type: 'prompt',
        message: 'human-secret-message',
        defaultValue: 'human-secret-default',
      },
    });
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
      message: 'human-secret-message',
      defaultValue: 'human-secret-default',
    });
    expect(JSON.stringify(status)).toContain('human-secret-message');
    expect(JSON.stringify(status)).toContain('human-secret-default');
    await host.dispose();
  });

  it('surfaces a synchronous unknown dialog without holding the owner queue and joins the action', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const view = electron.views[0];
    const engine = playwright.engines[0];
    let opened: any;
    playwright.clickHook = async (_selector, currentEngine, currentView) => {
      opened = currentEngine.emitDialog(currentView, {
        type: 'confirm',
        message: 'Continue?',
      });
      await opened.closed;
    };
    const execute = (command: string, args: string[] = []) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    await expect(execute('click', ['@e1'])).rejects.toMatchObject({
      code: 'dialog_pending',
      phase: 'dispatching',
      uncertain: false,
    });
    await expect(execute('snapshot')).rejects.toMatchObject({
      code: 'dialog_pending',
    });
    await expect(execute('dialog', ['status'])).resolves.toMatchObject({
      data: {
        hasDialog: true,
        type: 'confirm',
        message: 'Continue?',
      },
    });
    await expect(execute('dialog', ['accept'])).resolves.toMatchObject({
      data: { hasDialog: false },
    });
    await expect(opened.closed).resolves.toBeUndefined();
    expect(playwright.calls.some((call) => call.method === 'click')).toBe(true);
    expect(engine).toBe(playwright.engines[0]);
    expect(view).toBe(electron.views[0]);
    await host.dispose();
  });

  it('keeps a chained dialog modal instead of deadlocking the owner queue', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    let second: any;
    playwright.clickHook = async (_selector, engine, view) => {
      const first = engine.emitDialog(view, {
        type: 'alert',
        message: 'one',
        onClosed: () => {
          second = engine.emitDialog(view, {
            type: 'confirm',
            message: 'two',
          });
        },
      });
      await first.closed;
      await second.closed;
    };
    const execute = (command: string, args: string[] = []) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    await expect(execute('click', ['@e1'])).rejects.toMatchObject({
      code: 'dialog_pending',
    });
    await expect(execute('dialog', ['accept'])).resolves.toMatchObject({
      data: {
        hasDialog: true,
        modalPending: true,
        type: 'confirm',
        message: 'two',
      },
    });
    await expect(execute('dialog', ['status'])).resolves.toMatchObject({
      data: { hasDialog: true, type: 'confirm', message: 'two' },
    });
    await expect(execute('dialog', ['dismiss'])).resolves.toMatchObject({
      data: { hasDialog: false },
    });
    await expect(execute('snapshot')).resolves.toMatchObject({ success: true });
    await host.dispose();
  });

  it('retains a late original-action rejection until the dialog command consumes it', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    playwright.clickHook = async (_selector, engine, view) => {
      const opened = engine.emitDialog(view, {
        type: 'confirm',
        message: 'Fail after close',
      });
      await opened.closed;
      throw new Error('renderer continuation failed');
    };
    const execute = (command: string, args: string[] = []) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    await expect(execute('click', ['@e1'])).rejects.toMatchObject({
      code: 'dialog_pending',
    });
    await expect(execute('dialog', ['dismiss'])).rejects.toMatchObject({
      code: 'modal_action_failed',
      uncertain: true,
      partial: true,
    });
    await expect(execute('dialog', ['status'])).resolves.toMatchObject({
      data: { hasDialog: false },
    });
    await host.dispose();
  });

  it('routes a popup dialog and file chooser through the shared session modal coordinator', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const opener = electron.views[0].webContents;
    const engine = playwright.engines[0];
    let popupDialog: any;
    playwright.clickHook = async () => {
      const popupRequest = opener.windowOpenHandler({
        url: 'about:blank',
        disposition: 'foreground-tab',
      });
      popupRequest.createWindow({});
      await new Promise<void>((resolve) => setImmediate(resolve));
      const popupView = electron.views[1];
      await engine.pageForView(popupView);
      popupDialog = engine.emitDialog(popupView, {
        type: 'prompt',
        message: 'popup prompt',
        defaultValue: 'default',
      });
      await popupDialog.closed;
    };
    const executeOnOpener = (command: string, args: string[] = []) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    await expect(executeOnOpener('click', ['@e1'])).rejects.toMatchObject({
      code: 'dialog_pending',
    });
    await expect(executeOnOpener('dialog', ['status'])).resolves.toMatchObject({
      data: { hasDialog: true, type: 'prompt', message: 'popup prompt' },
    });
    await expect(executeOnOpener('dialog', ['accept', 'typed'])).resolves.toMatchObject({
      data: { hasDialog: false },
    });
    await expect(popupDialog.closed).resolves.toBeUndefined();

    const popupView = electron.views[1];
    const setFiles = vi.fn(async () => undefined);
    engine.emitFileChooser(popupView, {
      isMultiple: () => false,
      setFiles,
    });
    const popupFile = await approvedUpload('popup.txt');
    await expect(executeOnOpener('file_upload', [popupFile])).resolves.toMatchObject({
      data: { canceled: false, uploaded: 1, multiple: false },
    });
    expect(setFiles).toHaveBeenCalledWith(
      [popupFile],
      { timeout: expect.any(Number) },
    );
    expect(setFiles.mock.calls[0][1]?.timeout).toBeGreaterThan(0);
    expect(setFiles.mock.calls[0][1]?.timeout).toBeLessThanOrEqual(15_000);
    await host.dispose();
  });

  it('never lets upload_with_trigger consume or mask a sibling popup chooser', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const opener = electron.views[0].webContents;
    const popupRequest = opener.windowOpenHandler({
      url: 'about:blank',
      disposition: 'foreground-tab',
    });
    popupRequest.createWindow({});
    await new Promise<void>((resolve) => setImmediate(resolve));

    const engine = playwright.engines[0];
    const popupView = electron.views[1];
    await engine.pageForView(popupView);
    const popupSetFiles = vi.fn(async (
      _files: string[],
      _options?: { timeout?: number },
    ) => undefined);
    engine.emitFileChooser(popupView, {
      isMultiple: () => false,
      setFiles: popupSetFiles,
    });
    const executeOnOpener = (
      command: string,
      args: string[] = [],
      extra: Record<string, unknown> = {},
    ) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
        ...extra,
      },
    });

    const openerFile = await approvedUpload('opener.txt');
    const popupFile = await approvedUpload('popup.txt');

    await expect(executeOnOpener('upload_with_trigger', [], {
      trigger_selector: '#opener-trigger',
      input_selector: '#opener-input',
      files: [openerFile],
    })).rejects.toMatchObject({
      code: 'file_chooser_pending',
    });
    expect(popupSetFiles).not.toHaveBeenCalled();

    await expect(executeOnOpener('file_upload', [popupFile]))
      .resolves.toMatchObject({
        data: { canceled: false, uploaded: 1, multiple: false },
      });
    expect(popupSetFiles).toHaveBeenCalledOnce();
    expect(popupSetFiles.mock.calls[0]?.[0]).toEqual([popupFile]);
    expect(popupSetFiles.mock.calls[0]?.[1]?.timeout).toBeGreaterThan(0);
    expect(popupSetFiles.mock.calls[0]?.[1]?.timeout).toBeLessThanOrEqual(15_000);
    await host.dispose();
  });

  it('surfaces an action-created file chooser, releases the queue, and joins after upload', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const setFiles = vi.fn(async (
      _files: string[],
      _options?: { timeout?: number },
    ) => undefined);
    playwright.clickHook = (_selector, engine, view) => {
      engine.emitFileChooser(view, {
        isMultiple: () => false,
        setFiles,
      });
    };
    const execute = (command: string, args: string[] = []) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    const uploadFile = await approvedUpload('from-action.txt');

    await expect(execute('click', ['@e1'])).rejects.toMatchObject({
      code: 'file_chooser_pending',
      phase: 'dispatching',
    });
    await expect(execute('snapshot')).rejects.toMatchObject({
      code: 'file_chooser_pending',
    });
    await expect(execute('file_upload', [uploadFile])).resolves.toMatchObject({
      data: { canceled: false, uploaded: 1, multiple: false },
    });
    expect(setFiles).toHaveBeenCalledOnce();
    expect(setFiles.mock.calls[0]?.[0]).toEqual([uploadFile]);
    expect(setFiles.mock.calls[0]?.[1]?.timeout).toBeGreaterThan(0);
    expect(setFiles.mock.calls[0]?.[1]?.timeout).toBeLessThanOrEqual(15_000);
    await expect(execute('snapshot')).resolves.toMatchObject({ success: true });
    await host.dispose();
  });

  it('releases page_guard when a dialog opens during its renderer read', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const engine = playwright.engines[0];
    const view = electron.views[0];
    await engine.pageForView(view);
    let fired = false;
    electron.frameExecuteHook = async (_contents, expression) => {
      if (fired || !expression.includes('timeOrigin:performance.timeOrigin')) return;
      fired = true;
      const opened = engine.emitDialog(view, {
        type: 'confirm',
        message: 'guard timer',
      });
      await opened.closed;
    };

    await expect(pageGuard(host, created.data.targetId)).rejects.toMatchObject({
      code: 'dialog_pending',
      phase: 'dispatching',
    });
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'dialog',
        args: ['accept'],
      },
    })).resolves.toMatchObject({ data: { hasDialog: false } });
    await expect(pageGuard(host, created.data.targetId)).resolves.toMatchObject({
      targetId: created.data.targetId,
      locationConsistent: true,
    });
    await host.dispose();
  });

  it('never resolves multiple pending file choosers by latest-wins guessing', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);
    const firstSetFiles = vi.fn(async () => undefined);
    const secondSetFiles = vi.fn(async () => undefined);
    playwright.clickHook = (_selector, engine, view) => {
      engine.emitFileChooser(view, {
        isMultiple: () => false,
        setFiles: firstSetFiles,
      });
      engine.emitFileChooser(view, {
        isMultiple: () => false,
        setFiles: secondSetFiles,
      });
    };
    const execute = (command: string, args: string[] = []) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    await expect(execute('click', ['@e1'])).rejects.toMatchObject({
      code: 'file_chooser_pending',
    });
    const ambiguousFile = await approvedUpload('ambiguous.txt');
    await expect(execute('file_upload', [ambiguousFile])).rejects.toMatchObject({
      code: 'file_chooser_race',
      uncertain: true,
      partial: true,
    });
    expect(firstSetFiles).not.toHaveBeenCalled();
    expect(secondSetFiles).not.toHaveBeenCalled();
    await expect(execute('snapshot')).resolves.toMatchObject({ success: true });
    await host.dispose();
  });

  it('passes an uncapped command_timeout_ms into the Playwright action context', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    await snapshot(host);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'click',
        args: ['@e1'],
        mutating: true,
        command_timeout_ms: 987_654,
      },
    })).resolves.toMatchObject({ success: true });

    const click = playwright.calls.find((call) => call.method === 'click');
    const forwardedTimeout = Number(click?.args[0]?.timeout);
    // BrowserHost forwards the remaining absolute command budget. A millisecond
    // may elapse between accepting the RPC and constructing the action context,
    // but the value must remain far above the historical timeout cap.
    expect(forwardedTimeout).toBeGreaterThan(987_500);
    expect(forwardedTimeout).toBeLessThanOrEqual(987_654);
    await host.dispose();
  });

  it('completes, cancels, and consumes one pending browser-native FileChooser', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const engine = playwright.engines.at(-1);
    const firstSetFiles = vi.fn(async (
      _files: string[],
      _options?: { timeout?: number },
    ) => undefined);
    engine.pendingFileChooser = {
      isMultiple: () => true,
      setFiles: firstSetFiles,
    };

    const execute = (command: string, args: string[]) => host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command,
        args,
        mutating: true,
      },
    });

    const [fileA, fileB, lateFile, compatibilityFile, expiredFile] = await approvedUploads([
      'a.txt',
      'b.txt',
      'late.txt',
      'compat.txt',
      'expired.txt',
    ]);

    await expect(execute('file_upload', [fileA, fileB]))
      .resolves.toMatchObject({
        success: true,
        data: { canceled: false, uploaded: 2, multiple: true },
      });
    expect(firstSetFiles).toHaveBeenCalledOnce();
    expect(firstSetFiles.mock.calls[0]?.[0]).toEqual([fileA, fileB]);
    expect(firstSetFiles.mock.calls[0]?.[1]?.timeout).toBeGreaterThan(0);
    expect(firstSetFiles.mock.calls[0]?.[1]?.timeout).toBeLessThanOrEqual(15_000);
    expect(engine.pendingFileChooser).toBeNull();

    const canceledSetFiles = vi.fn(async () => undefined);
    engine.pendingFileChooser = {
      isMultiple: () => false,
      setFiles: canceledSetFiles,
    };
    await expect(execute('file_upload', ['--cancel'])).resolves.toMatchObject({
      data: { canceled: true, uploaded: 0, multiple: false },
    });
    expect(canceledSetFiles).not.toHaveBeenCalled();

    await expect(execute('file_upload', [lateFile])).rejects.toMatchObject({
      code: 'no_file_chooser',
      uncertain: false,
    });

    const compatibilitySetFiles = vi.fn(async (
      _files: string[],
      _options?: { timeout?: number },
    ) => undefined);
    engine.pendingFileChooser = {
      isMultiple: () => false,
      setFiles: compatibilitySetFiles,
    };
    await expect(execute('upload', ['--chooser', compatibilityFile]))
      .resolves.toMatchObject({
        data: { canceled: false, uploaded: 1, multiple: false },
      });
    expect(compatibilitySetFiles).toHaveBeenCalledOnce();
    expect(compatibilitySetFiles.mock.calls[0]?.[0]).toEqual([compatibilityFile]);
    expect(compatibilitySetFiles.mock.calls[0]?.[1]?.timeout).toBeGreaterThan(0);
    expect(compatibilitySetFiles.mock.calls[0]?.[1]?.timeout).toBeLessThanOrEqual(15_000);

    const expiredSetFiles = vi.fn(async () => undefined);
    engine.pendingFileChooser = {
      isMultiple: () => false,
      setFiles: expiredSetFiles,
    };
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'file_upload',
        args: [expiredFile],
        command_timeout_ms: 15_000,
        command_deadline_ms: Date.now() - 1,
        mutating: true,
      },
    })).rejects.toMatchObject({
      code: 'command_timeout',
      uncertain: false,
    });
    expect(expiredSetFiles).not.toHaveBeenCalled();
    expect(engine.pendingFileChooser).not.toBeNull();
    await host.dispose();
  });

  it('atomically ignores a stale chooser and waits for this trigger delayed chooser', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const engine = playwright.engines.at(-1);
    const staleSetFiles = vi.fn(async () => undefined);
    const currentSetFiles = vi.fn(async () => undefined);
    engine.pendingFileChooser = {
      isMultiple: () => false,
      setFiles: staleSetFiles,
    };
    playwright.clickHook = (selector, currentEngine, view) => {
      if (selector !== '#delayed-upload-trigger') return;
      setTimeout(() => {
        currentEngine.emitFileChooser(view, {
          isMultiple: () => true,
          setFiles: currentSetFiles,
        });
      }, 750);
    };
    const currentFiles = await approvedUploads(['current-a.txt', 'current-b.txt']);

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '#delayed-upload-trigger',
        input_selector: '#exact-file-input',
        files: currentFiles,
        mutating: true,
      },
    });

    expect(result.data).toEqual({
      via: 'chooser',
      uploaded: 2,
      multiple: true,
    });
    expect(staleSetFiles).not.toHaveBeenCalled();
    expect(currentSetFiles).toHaveBeenCalledOnce();
    expect(currentSetFiles).toHaveBeenCalledWith(
      currentFiles,
      { timeout: expect.any(Number) },
    );
    expect(currentSetFiles.mock.calls[0][1]?.timeout).toBeGreaterThan(0);
    expect(currentSetFiles.mock.calls[0][1]?.timeout).toBeLessThan(15_000);
    expect(
      playwright.calls.some(
        (call) => call.method === 'upload' && call.ref === '#exact-file-input',
      ),
    ).toBe(false);
    expect(engine.pendingFileChooser).toBeNull();
    await host.dispose();
  });

  it('falls back to the exact input only for a provably undispatched trigger', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    playwright.selectorCounts.set('#missing-trigger', 0);
    const files = await approvedUploads(
      Array.from({ length: 256 }, (_, index) => `file-${index}.txt`),
    );

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '#missing-trigger',
        input_selector: '#exact-file-input',
        files,
        mutating: true,
      },
    });

    expect(result.data).toEqual({ via: 'input', uploaded: 256 });
    expect(playwright.calls.some((call) => call.method === 'click')).toBe(false);
    expect(
      playwright.calls.find(
        (call) => call.method === 'upload' && call.ref === '#exact-file-input',
      )?.args,
    ).toEqual([files]);

    const clicksBeforeClear = playwright.calls.filter(
      (call) => call.method === 'click',
    ).length;
    const clearResult: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '#must-not-click-for-clear',
        input_selector: '#exact-file-input',
        files: [],
        mutating: true,
      },
    });
    expect(clearResult.data).toEqual({ via: 'input', uploaded: 0 });
    expect(
      playwright.calls.filter((call) => call.method === 'click'),
    ).toHaveLength(clicksBeforeClear);
    expect(
      playwright.calls.filter(
        (call) => call.method === 'upload' && call.ref === '#exact-file-input',
      ).at(-1)?.args,
    ).toEqual([[]]);

    const unboundedFiles = await approvedUploads(
      Array.from({ length: 1_025 }, (_, index) => `unbounded-${index}.txt`),
    );
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '',
        input_selector: '#exact-file-input',
        files: unboundedFiles,
        mutating: true,
      },
    })).resolves.toMatchObject({
      success: true,
      data: { via: 'input', uploaded: 1_025 },
    });
    expect(
      playwright.calls.filter(
        (call) => call.method === 'upload' && call.ref === '#exact-file-input',
      ).at(-1)?.args,
    ).toEqual([unboundedFiles]);
    await host.dispose();
  });

  it('does not direct-upload after a trigger click becomes uncertain', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    playwright.inputFailure = {
      method: 'Input.dispatchMouseEvent',
      message: 'transport failed after click dispatch began',
    };
    const uploadFile = await approvedUpload('must-not-upload.txt');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '#uncertain-trigger',
        input_selector: '#exact-file-input',
        files: [uploadFile],
        mutating: true,
      },
    })).rejects.toMatchObject({
      code: 'input_uncertain',
      phase: 'dispatching',
      uncertain: true,
      partial: true,
    });
    expect(
      playwright.calls.some(
        (call) => call.method === 'upload' && call.ref === '#exact-file-input',
      ),
    ).toBe(false);
    expect(playwright.engines.at(-1).pendingFileChooser).toBeNull();
    await host.dispose();
  });

  it('rejects two chooser events from one trigger instead of choosing the wrong one', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const firstSetFiles = vi.fn(async () => undefined);
    const secondSetFiles = vi.fn(async () => undefined);
    playwright.clickHook = (_selector, engine, view) => {
      engine.emitFileChooser(view, {
        isMultiple: () => false,
        setFiles: firstSetFiles,
      });
      engine.emitFileChooser(view, {
        isMultiple: () => false,
        setFiles: secondSetFiles,
      });
    };
    const ambiguousFile = await approvedUpload('ambiguous.txt');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '#double-chooser-trigger',
        input_selector: '#exact-file-input',
        files: [ambiguousFile],
        mutating: true,
      },
    })).rejects.toMatchObject({
      code: 'file_chooser_race',
      uncertain: true,
      partial: true,
      completed_count: 1,
    });
    expect(firstSetFiles).not.toHaveBeenCalled();
    expect(secondSetFiles).not.toHaveBeenCalled();
    expect(playwright.engines.at(-1).pendingFileChooser).toBeNull();
    await host.dispose();
  });

  it('reports a confirmed trigger plus failed chooser upload as partial progress', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    playwright.clickHook = (_selector, engine, view) => {
      engine.emitFileChooser(view, {
        isMultiple: () => false,
        setFiles: async () => {
          throw new Error('ENOENT: selected file disappeared');
        },
      });
    };
    const uploadFile = await approvedUpload('disappeared.txt');

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        command: 'upload_with_trigger',
        args: [],
        trigger_selector: '#chooser-trigger',
        input_selector: '#exact-file-input',
        files: [uploadFile],
        mutating: true,
      },
    })).rejects.toMatchObject({
      code: 'invalid_upload',
      phase: 'partial',
      uncertain: false,
      partial: true,
      completed_count: 1,
    });
    expect(playwright.engines.at(-1).pendingFileChooser).toBeNull();
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

  it('Host-backed Playwright page lifecycle rolls back failed creation and prunes close topology', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const internals = host as unknown as {
      owners: Map<string, {
        tabs: Map<string, any>;
        engine: {
          pageLifecycleHook: {
            createPage(context: Record<string, unknown>): Promise<string>;
            closePage(context: Record<string, unknown>): Promise<void>;
          } | null;
        };
      }>;
      tabsByTarget: Map<string, unknown>;
      tabsByWebContentsId: Map<number, unknown>;
    };
    const owner = internals.owners.get(RUNTIME_KEY);
    const source = [...(owner?.tabs.values() ?? [])].find(
      (tab) => tab.targetId === created.data.targetId,
    );
    const hook = owner?.engine.pageLifecycleHook;
    if (!owner || !source || !hook) throw new Error('page lifecycle hook 未安装');

    const baselineViews = electron.views.length;
    electron.failDebuggerAttach = true;
    await expect(hook.createPage({
      sourceView: source.view,
      deadlineAt: Date.now() + 2_000,
      url: 'about:blank',
      browserContextId: '',
    })).rejects.toMatchObject({ code: 'debugger_unavailable' });
    expect(owner.tabs).toHaveLength(1);
    expect(internals.tabsByTarget).toHaveLength(1);
    expect(internals.tabsByWebContentsId).toHaveLength(1);
    expect(electron.views).toHaveLength(baselineViews + 1);
    expect(electron.views.at(-1).webContents.destroyed).toBe(true);

    electron.failDebuggerAttach = false;
    const chromiumTargetId = await hook.createPage({
      sourceView: source.view,
      deadlineAt: Date.now() + 2_000,
      url: 'about:blank',
      browserContextId: '',
    });
    const other = [...owner.tabs.values()].find((tab) => tab !== source);
    expect(chromiumTargetId).toBe(`fake-chromium-target-${other.webContentsId}`);
    expect(owner.tabs).toHaveLength(2);
    await hook.closePage({
      sourceView: source.view,
      deadlineAt: Date.now() + 2_000,
      targetId: chromiumTargetId,
      view: other.view,
    });
    expect(owner.tabs).toHaveLength(1);
    expect(internals.tabsByTarget).toHaveLength(1);
    expect(internals.tabsByWebContentsId).toHaveLength(1);
    expect(other.view.webContents.destroyed).toBe(true);
    await host.dispose();
  });

  it('dispatches a visual coordinate atomically without requiring DOM/AX semantics', async () => {
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
    expect(result).toEqual({ clicked: true, x: 60, y: 40 });
    const input = electron.views[0].webContents.debugger.commands
      .filter((item: any) => item.method === 'Input.dispatchMouseEvent')
      .map((item: any) => item.params.type);
    expect(input).toEqual(['mouseMoved', 'mousePressed', 'mouseReleased']);
    expect(
      electron.views[0].webContents.debugger.commands.filter(
        (item: any) => item.method === 'DOM.getNodeForLocation',
      ),
    ).toHaveLength(0);
    await host.dispose();
  });

  it('keeps visual input in viewport coordinates on a scrolled document', async () => {
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
      .toEqual([]);
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

  it('rejects a changed document but tolerates live pixel churn', async () => {
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
    })).resolves.toMatchObject({ clicked: true, x: 60, y: 40 });
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Input.dispatchMouseEvent',
    )).toBe(true);
    await host.dispose();
  });

  it('supports semantic-free coordinate targets and reports post-press failures as uncertain', async () => {
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
    })).resolves.toMatchObject({ clicked: true });

    electron.nodeName = 'BUTTON';
    electron.views[0].webContents.debugger.commands = [];
    const failingEpoch = await captureHostEpoch(host);
    electron.failInputType = 'mouseReleased';
    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'coordinate_click',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        target_id: created.data.targetId,
        expected_epoch: failingEpoch,
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

  it('rejects an expired coordinate click before dispatching any browser input', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const expectedEpoch = await captureHostEpoch(host);
    const contents = electron.views[0].webContents;
    const before = contents.debugger.commands.filter(
      (item: any) => item.method === 'Input.dispatchMouseEvent',
    ).length;

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
        command_timeout_ms: 15_000,
        command_deadline_ms: Date.now() - 1,
      },
    })).rejects.toMatchObject({
      code: 'command_timeout',
      uncertain: false,
    });
    expect(contents.debugger.commands.filter(
      (item: any) => item.method === 'Input.dispatchMouseEvent',
    )).toHaveLength(before);
    await host.dispose();
  });

  it('keeps default snapshot and page_guard free of security metadata', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const captured: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        proxy_url: PROXY_URL,
        command: 'snapshot',
        args: ['--compact'],
      },
    });
    const data = captured.data;
    expect(data.ref_keys).toBeUndefined();
    expect(data.ref_roles).toBeUndefined();
    expect(data.ref_names).toBeUndefined();
    expect(data.ref_action_kinds).toBeUndefined();
    expect(data.ref_content_editable).toBeUndefined();
    expect(data.ref_actions).toBeUndefined();
    expect(data.element_security).toBeUndefined();
    expect(data.element_navigation).toBeUndefined();
    expect(data.security_digest).toBeUndefined();

    const guarded = await pageGuard(host, created.data.targetId);
    expect(guarded.elementSecurity).toBeUndefined();
    expect(guarded.elementNavigation).toBeUndefined();
    expect(guarded.securityDigest).toBeUndefined();
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

  it('publicUrl 对任意 scheme、长查询串和 hash 元数据做精确透传', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const urls = [
      `https://oa.example/app?metadata=${'m'.repeat(5_000)}#/ticket/detail?id=GD-1&token=abc123`,
      'file:///private/tmp/workflow.html?ticket=signed-value#step=4',
      'custom+workflow://tenant/action?payload=%7B%22exact%22%3Atrue%7D#callback',
    ];
    for (const expected of urls) {
      await host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: {
          profile_dir: PROFILE,
          proxy_url: PROXY_URL,
          command: 'open',
          args: [expected],
        },
      });
      const tabs: any = await host.handleRpc({
        runtime_key: RUNTIME_KEY,
        method: 'execute',
        params: { profile_dir: PROFILE, proxy_url: PROXY_URL, command: 'tab', args: ['list'] },
      });
      expect(tabs.data.tabs[0].url).toBe(expected);
    }
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
        command: 'vision_screenshot',
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

  it('exports page and strict-ref screenshots through public Playwright options', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    await snapshot(host);
    const outputDir = path.join(path.dirname(PROFILE), 'artifacts');
    await mkdir(outputDir, { recursive: true });
    const jpeg = path.join(outputDir, 'element.jpeg');
    const elementResult: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: [
          '--ref', '@e1',
          '--type', 'jpeg',
          '--scale', 'device',
          jpeg,
        ],
        proxy_url: PROXY_URL,
      },
    });
    expect(elementResult.data).toMatchObject({
      path: jpeg,
      type: 'jpeg',
      bytes: 4,
    });
    const locatorScreenshot = playwright.calls.find(
      (call) => call.method === 'locatorScreenshot' && call.ref === 'e1',
    );
    expect(locatorScreenshot?.args[0]).toMatchObject({
      type: 'jpeg',
      scale: 'device',
      quality: 90,
    });
    expect(Number((locatorScreenshot?.args[0] as { timeout?: number })?.timeout))
      .toBeGreaterThanOrEqual(14_900);
    expect(Number((locatorScreenshot?.args[0] as { timeout?: number })?.timeout))
      .toBeLessThanOrEqual(15_000);

    const png = path.join(outputDir, 'full.png');
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--full-page', '--type', 'png', '--scale', 'css', png],
        proxy_url: PROXY_URL,
      },
    });
    expect(playwright.calls).toContainEqual({
      method: 'pageScreenshot',
      ref: '',
      args: [{
        type: 'png',
        scale: 'css',
        timeout: 15_000,
        fullPage: true,
      }],
    });
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Page.captureScreenshot',
    )).toBe(false);

    await expect(host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'screenshot',
        args: ['--ref', '@e1', '--full-page', png],
        proxy_url: PROXY_URL,
      },
    })).rejects.toMatchObject({ code: 'invalid_input' });
    await host.dispose();
  });

  it('serializes evaluate results and closes the active tab when tab id is omitted', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const first: any = await createTab(host);
    const evaluated: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'eval',
        args: ['() => 42'],
        proxy_url: PROXY_URL,
        target_id: first.data.targetId,
      },
    });
    expect(evaluated.data).toMatchObject({
      value: 42,
      is_function: true,
      is_undefined: false,
      serialized: '42',
    });
    const undefinedResult: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'eval',
        args: ['() => undefined'],
        proxy_url: PROXY_URL,
        target_id: first.data.targetId,
      },
    });
    expect(undefinedResult.data.serialized).toBe('undefined');

    const secondLabel = `s${SESSION_HASH}-2`;
    const second: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'tab',
        args: ['new', '--label', secondLabel, 'https://example.com/second'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'tab',
        args: ['close'],
        proxy_url: PROXY_URL,
        mutating: true,
      },
    });
    const listed: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        command: 'tab',
        args: ['list'],
        proxy_url: PROXY_URL,
      },
    });
    expect(listed.data.tabs).toHaveLength(1);
    expect(listed.data.tabs[0]).toMatchObject({
      targetId: first.data.targetId,
      active: true,
    });
    expect(listed.data.tabs.some(
      (tab: any) => tab.targetId === second.data.targetId,
    )).toBe(false);
    await host.dispose();
  });

  it('settles a user export without adding post-fill focus provenance work', async () => {
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

    expect(result.data).toMatchObject({ settled: true, focus_released: false });
    expect(electron.focusedBackendNodeId).toBe(7);
    const commands = electron.views[0].webContents.debugger.commands;
    const blurIndex = commands.findIndex(
      (item: any) => item.method === 'Runtime.callFunctionOn'
        && String(item.params?.functionDeclaration).includes('prototype?.blur'),
    );
    const captureIndex = playwright.calls.findIndex(
      (item) => item.method === 'pageScreenshot',
    );
    expect(blurIndex).toBe(-1);
    expect(captureIndex).toBeGreaterThanOrEqual(0);
    expect(commands).toContainEqual({ method: 'Overlay.hideHighlight', params: undefined });
    expect(commands.some((item: any) => item.method === 'Page.captureScreenshot')).toBe(false);
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

  it('does not synthesize focusout navigation while settling a screenshot', async () => {
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
    expect(playwright.calls.some((item) => item.method === 'pageScreenshot')).toBe(true);
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Page.captureScreenshot',
    )).toBe(false);
    await host.dispose();
  });

  it('does not carry post-fill focus provenance across same-origin form navigation', async () => {
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

    expect(result.data).toMatchObject({ settled: true, focus_released: false });
    expect(electron.focusedBackendNodeId).toBe(7);
    expect(electron.views[0].webContents.debugger.commands.some(
      (item: any) => item.method === 'Accessibility.getFullAXTree',
    )).toBe(false);
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

  it('saves an explicit download to the exact requested absolute target', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const { promise, target } = await beginDownload(host, created.data.targetId);
    const item = new FakeDownloadItem();
    const event = { preventDefault: vi.fn() };
    electron.sessions[0].emit('will-download', event, item, electron.views[0].webContents);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(item.savePath).toBe(target);
    expect(item.savePathCalls).toEqual([target]);
    await writeFile(target, 'data');
    item.complete();

    await expect(promise).resolves.toMatchObject({ path: target, bytes: 4 });
    await host.dispose();
  });

  it('binds downloads to the exact click event rather than a guessed HTTP URL', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const { promise, target } = await beginDownload(host, created.data.targetId);

    const generated = new FakeDownloadItem();
    generated.url = 'blob:https://example.com/generated-report';
    generated.urlChain = ['https://example.com/report/export'];
    const generatedEvent = { preventDefault: vi.fn() };
    electron.sessions[0].emit(
      'will-download',
      generatedEvent,
      generated,
      electron.views[0].webContents,
    );
    expect(generatedEvent.preventDefault).not.toHaveBeenCalled();
    expect(generated.savePath).toBe(target);
    await writeFile(target, 'data');
    generated.complete();
    await expect(promise).resolves.toMatchObject({ path: target, bytes: 4 });
    await host.dispose();
  });

  it('settles an explicit grant when setSavePath throws and permits the next download', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const first = await beginDownload(host, created.data.targetId);
    const broken = new FakeDownloadItem();
    broken.setSavePathError = new Error('disk unavailable');
    electron.sessions[0].emit(
      'will-download',
      { preventDefault: vi.fn() },
      broken,
      electron.views[0].webContents,
    );
    await expect(first.promise).rejects.toMatchObject({
      code: 'download_save_path_failed',
    });
    expect(broken.cancelled).toBe(true);
    expect(broken.savePathCalls).toEqual([]);

    const second = await beginDownload(host, created.data.targetId);
    const healthy = new FakeDownloadItem();
    electron.sessions[0].emit(
      'will-download',
      { preventDefault: vi.fn() },
      healthy,
      electron.views[0].webContents,
    );
    await writeFile(second.target, 'healthy');
    healthy.complete();
    await expect(second.promise).resolves.toMatchObject({ path: second.target });
    expect(healthy.savePathCalls).toEqual([second.target]);
    await host.dispose();
  });

  it('allows an explicit absolute download target outside account roots and ignores size/quarantine policy', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const arbitraryTarget = path.join(tempRoot, 'user-selected', 'exports', 'large.bin');
    const { promise } = await beginDownload(host, created.data.targetId, {
      target: arbitraryTarget,
      download_dir: path.join(tempRoot, 'unrelated-legacy-quarantine'),
      max_bytes: 1,
    });
    const canonicalTarget = path.join(
      await realpath(path.dirname(arbitraryTarget)),
      path.basename(arbitraryTarget),
    );
    const item = new FakeDownloadItem();
    item.receivedBytes = 10_000_000;
    item.totalBytes = 10_000_000;
    const event = { preventDefault: vi.fn() };
    electron.sessions[0].emit('will-download', event, item, electron.views[0].webContents);
    expect(event.preventDefault).not.toHaveBeenCalled();
    expect(item.savePath).toBe(canonicalTarget);
    await writeFile(arbitraryTarget, 'complete');
    item.complete();
    await expect(promise).resolves.toMatchObject({
      path: canonicalTarget,
      bytes: 10_000_000,
    });
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

  it('auto-saves every public Page click download once with collision-free names and terminal events', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const downloadDir = path.join(tempRoot, 'task-downloads', 'browser');
    await mkdir(downloadDir, { recursive: true });
    const canonicalDownloadDir = await realpath(downloadDir);
    await writeFile(path.join(downloadDir, 'report.csv'), 'existing');
    await snapshot(host);
    const items = [new FakeDownloadItem(), new FakeDownloadItem()];
    for (const item of items) {
      item.filename = 'report.csv';
      item.url = 'https://example.com/report.csv';
    }
    const events: any[] = [];
    host.on('download', (event) => events.push({ ...event }));
    playwright.clickHook = async (_selector, engine, view) => {
      const page = await engine.pageForView(view) as EventEmitter;
      for (const [index, item] of items.entries()) {
        page.emit('download', {});
        electron.sessions[0].emit(
          'will-download',
          { preventDefault: vi.fn() },
          item,
          view.webContents,
        );
        await writeFile(item.savePath, `download-${index}`);
        item.complete();
      }
    };

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        download_dir: downloadDir,
        mutating: true,
      },
    });

    expect(result.data.downloads).toHaveLength(2);
    expect(result.data.downloads).toEqual([
      expect.objectContaining({
        name: 'report (1).csv',
        suggestedFilename: 'report.csv',
        state: 'completed',
        receivedBytes: 4,
      }),
      expect.objectContaining({
        name: 'report (2).csv',
        suggestedFilename: 'report.csv',
        state: 'completed',
        receivedBytes: 4,
      }),
    ]);
    expect(new Set(result.data.downloads.map((item: any) => item.downloadId)).size).toBe(2);
    expect(items.map((item) => item.savePathCalls)).toEqual([
      [path.join(canonicalDownloadDir, 'report (1).csv')],
      [path.join(canonicalDownloadDir, 'report (2).csv')],
    ]);
    expect(events.filter((event) => event.state === 'progressing')).toHaveLength(2);
    expect(events.filter((event) => event.state === 'completed')).toHaveLength(2);
    await host.dispose();
  });

  it('treats attachment goto as a successful auto-download and returns its native item', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const downloadDir = path.join(tempRoot, 'goto-downloads', 'browser');
    await mkdir(downloadDir, { recursive: true });
    const canonicalDownloadDir = await realpath(downloadDir);
    const item = new FakeDownloadItem();
    item.filename = 'attachment.bin';
    item.url = 'https://example.com/attachment';
    playwright.gotoHook = async (_url, page, view) => {
      page.emit('download', {});
      electron.sessions[0].emit(
        'will-download',
        { preventDefault: vi.fn() },
        item,
        view.webContents,
      );
      await writeFile(item.savePath, 'attachment');
      item.complete();
      throw new Error('page.goto: Download is starting');
    };

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        command: 'open',
        args: ['https://example.com/attachment'],
        proxy_url: PROXY_URL,
        download_dir: downloadDir,
        mutating: true,
      },
    });

    expect(result.data).toMatchObject({
      download_started: true,
      downloads: [
        expect.objectContaining({
          name: 'attachment.bin',
          state: 'completed',
          path: path.join(canonicalDownloadDir, 'attachment.bin'),
        }),
      ],
    });
    expect(item.savePathCalls).toEqual([path.join(canonicalDownloadDir, 'attachment.bin')]);
    await host.dispose();
  });

  it('auto-saves a download triggered inside public Page run_code', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const downloadDir = path.join(tempRoot, 'run-code-downloads', 'browser');
    await mkdir(downloadDir, { recursive: true });
    const canonicalDownloadDir = await realpath(downloadDir);
    const item = new FakeDownloadItem();
    item.filename = 'generated.json';
    playwright.clickHook = async (_selector, engine, view) => {
      const page = await engine.pageForView(view) as EventEmitter;
      page.emit('download', {});
      electron.sessions[0].emit(
        'will-download',
        { preventDefault: vi.fn() },
        item,
        view.webContents,
      );
      await writeFile(item.savePath, '{}');
      item.complete();
    };

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        command: 'run_code_unsafe',
        args: ['async page => { await page.locator("#export").click(); return "done"; }'],
        proxy_url: PROXY_URL,
        download_dir: downloadDir,
        mutating: true,
      },
    });

    expect(result.data).toMatchObject({
      downloads: [
        expect.objectContaining({
          name: 'generated.json',
          state: 'completed',
          path: path.join(canonicalDownloadDir, 'generated.json'),
        }),
      ],
    });
    expect(item.savePathCalls).toEqual([path.join(canonicalDownloadDir, 'generated.json')]);
    await host.dispose();
  });

  it('publishes deduplicated native byte progress and one failed terminal state', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const downloadDir = path.join(tempRoot, 'failed-downloads', 'browser');
    await mkdir(downloadDir, { recursive: true });
    const item = new FakeDownloadItem();
    item.filename = 'partial.zip';
    item.totalBytes = 100;
    const events: any[] = [];
    host.on('download', (event) => events.push({ ...event }));
    await snapshot(host);
    playwright.clickHook = async (_selector, engine, view) => {
      const page = await engine.pageForView(view) as EventEmitter;
      page.emit('download', {});
      electron.sessions[0].emit(
        'will-download',
        { preventDefault: vi.fn() },
        item,
        view.webContents,
      );
      item.update(10);
      item.update(10);
      item.update(55);
      item.fail();
    };

    const result: any = await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        command: 'click',
        args: ['@e1'],
        proxy_url: PROXY_URL,
        download_dir: downloadDir,
        mutating: true,
      },
    });

    expect(events.map((event) => [
      event.state,
      event.receivedBytes,
      event.completedAt > 0,
    ])).toEqual([
      ['progressing', 0, false],
      ['progressing', 10, false],
      ['progressing', 55, false],
      ['interrupted', 55, true],
    ]);
    expect(result.data.downloads).toEqual([
      expect.objectContaining({
        name: 'partial.zip',
        state: 'interrupted',
        receivedBytes: 55,
        totalBytes: 100,
        error: '浏览器下载状态：interrupted',
      }),
    ]);
    await host.dispose();
  });

  it('allows nested capture scopes without an owner-global download_busy failure', async () => {
    const host = new BrowserHost(() => fakeWindow());
    const created: any = await createTab(host);
    const downloadDir = path.join(tempRoot, 'nested-downloads', 'browser');
    await mkdir(downloadDir, { recursive: true });
    await host.handleRpc({
      runtime_key: RUNTIME_KEY,
      method: 'execute',
      params: {
        profile_dir: PROFILE,
        target_id: created.data.targetId,
        command: 'get',
        args: ['title'],
        proxy_url: PROXY_URL,
        download_dir: downloadDir,
      },
    });
    const owner = [...(host as any).owners.values()][0];
    const tab = [...owner.tabs.values()][0];

    await expect((host as any).withGenericDownloadCapture(
      owner,
      tab,
      1_000,
      () => (host as any).withGenericDownloadCapture(
        owner,
        tab,
        1_000,
        async () => ({ nested: true }),
      ),
    )).resolves.toEqual({ nested: true });
    expect(owner.genericDownloadCaptures).toEqual([]);
    await host.dispose();
  });

  it('allows ordinary and concurrent page downloads without an explicit one-shot grant', async () => {
    const host = new BrowserHost(() => fakeWindow());
    await createTab(host);
    const downloads = Array.from({ length: 4 }, () => ({
      item: { cancel: vi.fn() },
      event: { preventDefault: vi.fn() },
    }));
    for (const { event, item } of downloads) {
      electron.sessions[0].emit(
        'will-download',
        event,
        item,
        electron.views[0].webContents,
      );
    }
    for (const { event, item } of downloads) {
      expect(event.preventDefault).not.toHaveBeenCalled();
      expect(item.cancel).not.toHaveBeenCalled();
    }
    await host.dispose();
  });
});
