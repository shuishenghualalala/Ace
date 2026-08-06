/**
 * Electron ↔ Playwright 的 CDP 传输层。
 *
 * `webContents.debugger` 是页面级会话，而 Playwright 连接的是浏览器级端点。本文件
 * 因此本地维护一个最小 target 模型，而不是把根命令随意转发给某个页面。
 *
 * ## 支持边界
 *
 * 这个 transport 只接管**已经由 Crew 创建的 WebContentsView**：
 *
 * - 支持既有页面、Host-backed 页面创建/关闭、OOPIF/worker 子会话和
 *   `newCDPSession(page)`；
 * - 不支持 Playwright 创建/关闭 BrowserContext；
 * - 不允许未知 root/browser 命令借道任意 tab。新增能力必须先在下面的显式
 *   allowlist 中审计，否则 fail closed。
 *
 * 每个 owner 一个 transport 实例，且只登记该 owner 的 view。不要通过新增
 * browser-scoped 转发命令破坏这条隔离边界。
 */

import { AsyncLocalStorage } from 'node:async_hooks';
import { copyFile, link, mkdir } from 'node:fs/promises';
import path from 'node:path';

import type {
  Cookie,
  DownloadItem,
  PrintToPDFOptions,
  Session,
  WebContentsView,
} from 'electron';

/** Playwright 的 `ConnectOverCDPTransport` 结构。 */
export interface CdpTransport {
  send(message: object): void;
  close(): void;
  onmessage?: (message: object) => void;
  onclose?: (reason?: string) => void;
}

interface CdpMessage {
  id?: number;
  sessionId?: string;
  method?: string;
  params?: Record<string, unknown>;
}

type ViewAttachStatus = 'pending' | 'attaching' | 'attached' | 'failed' | 'removed';

interface ViewWaiter {
  resolve(targetId: string): void;
  reject(error: Error): void;
  timer: ReturnType<typeof setTimeout> | null;
}

interface ViewState {
  view: WebContentsView;
  /** Explicit Electron opener topology for a renderer-created popup. */
  openerView: WebContentsView | null;
  /**
   * PlaywrightEngine installs modal listeners before releasing buffered page
   * events. Bare Transport clients have no such handshake and receive events
   * immediately, matching a normal CDP websocket transport.
   */
  deferPageEventsUntilReady: boolean;
  status: ViewAttachStatus;
  generation: number;
  error: Error | null;
  tab: TabSession | null;
  attachTask: Promise<void> | null;
  waiters: Set<ViewWaiter>;
}

interface TabSession {
  view: WebContentsView;
  state: ViewState;
  /** 我们分配的页面会话 id。 */
  sessionId: string;
  /** `Target.getTargetInfo` 的结果，Playwright 用它认页面。 */
  targetInfo: Record<string, unknown> | null;
  /** Chromium 分配的子会话（OOPIF / worker），含转发给 Playwright 前的生命周期屏障。 */
  childSessions: Map<string, ChildSessionState>;
  /** transport 自己是否执行了 debugger.attach。 */
  attachedByTransport: boolean;
  /** debugger 监听器，释放时必须全部摘掉。 */
  messageListener: ((...args: unknown[]) => void) | null;
  detachListener: ((...args: unknown[]) => void) | null;
  /**
   * Modal events may arrive after debugger attach but before playwright-core
   * has constructed the Page and installed its listeners. They must be held
   * behind an explicit Engine handshake or core silently drops the unknown
   * session event forever.
   */
  pageEventsReady: boolean;
  bufferedPageEvents: Array<{
    method: string;
    params: Record<string, unknown> | undefined;
    sourceSessionId?: string;
  }>;
  /**
   * An unpublished dialog is closed natively before playwright-core ever sees
   * its opening event. Drop the matching closed event as well, otherwise core
   * observes an impossible half-pair during Page construction.
   */
  suppressedDialogClosed: number;
}

interface NativeDownloadBinding {
  tab: TabSession;
  item: DownloadItem;
  url: string;
  suggestedFilename: string;
}

interface PublicDownloadBinding {
  tab: TabSession;
  url: string;
  suggestedFilename: string;
  item: DownloadItem | null;
  cancelRequested: boolean;
  waiters: Set<(item: DownloadItem | null) => void>;
}

interface AliasSession {
  tab: TabSession;
  /**
   * Empty for a second logical session attached to the top-level page. When
   * present, the alias is attached to an existing flattened OOPIF/worker
   * session so BrowserContext.newCDPSession(frame) keeps official Playwright
   * semantics.
   */
  childSessionId?: string;
  /**
   * Session ids are opaque and Chromium may eventually reuse one after a
   * detach. Keep object identity as well so an old alias can never start
   * routing to a later child that happens to receive the same id.
   */
  child?: ChildSessionState;
  targetId: string;
}

export type InputSessionKind = 'tab' | 'alias' | 'child';

export interface UnpublishedDialogResult {
  type: string;
  message: string;
  defaultValue: string;
  matched: boolean;
}

interface ChildSessionState {
  targetInfo: Record<string, unknown>;
  /**
   * A top-level service-worker child is physically attached below an Electron
   * page debugger, but playwright-core only recognizes service workers on the
   * browser/root Target graph. This points at its promoted root binding.
   */
  serviceWorkerPhysical: ServiceWorkerPhysical | null;
  /** Inspector.workerScriptLoaded may arrive while this is a deduped standby. */
  serviceWorkerScriptLoaded: boolean;
  /**
   * false as soon as Chromium reports detach.  The attach/detach notifications
   * may still be draining to Playwright, but no new command may route here.
   */
  live: boolean;
  /** Preserves attach → child events → detach ordering while an async hook runs. */
  delivery: Promise<void>;
  /**
   * Electron needs the initial FrameSession commands to complete in order.
   * Once Runtime.runIfWaitingForDebugger succeeds, ordinary CDP traffic must
   * become concurrent again so Playwright can cancel a hung evaluation.
   */
  initializing: boolean;
  initializationTail: Promise<void>;
}

interface ServiceWorkerPhysical {
  tab: TabSession;
  childSessionId: string;
  child: ChildSessionState;
  attachParams: Record<string, unknown>;
}

interface PromotedServiceWorker {
  targetId: string;
  /** Transport-owned root session id exposed to playwright-core. */
  sessionId: string;
  targetInfo: Record<string, unknown>;
  physicals: Set<ServiceWorkerPhysical>;
  primary: ServiceWorkerPhysical;
}

interface PendingPermissionCommand {
  method: 'Browser.grantPermissions' | 'Browser.resetPermissions';
  params: Record<string, unknown> | undefined;
}

interface PdfStreamBinding {
  tab: TabSession;
  /** Logical Playwright tab/alias session that created the stream. */
  ownerSessionId: string;
  bytes: Buffer;
  offset: number;
}

export interface ChildSessionLifecycleContext {
  view: WebContentsView;
  phase: 'attached' | 'detached';
  /** Chromium's real flattened child-session id. */
  sessionId: string;
  /** Empty for a direct child of the page session; otherwise the parent child session. */
  parentSessionId: string;
  targetInfo: Readonly<Record<string, unknown>>;
  /**
   * Aborted when the lifecycle barrier reaches its deadline. Hooks must stop
   * accepting recorder events and arrange cleanup for any CDP command that
   * settles late.
   */
  signal: AbortSignal;
}

/**
 * Runs at the CDP session boundary before Playwright can initialize/resume a
 * newly attached child target, and before it observes a detach.
 *
 * The hook is intentionally per-view: callers must preserve owner topology and
 * must not use a browser-global child-session registry.
 */
export type ChildSessionLifecycleHook = (
  context: ChildSessionLifecycleContext,
) => void | Promise<void>;

export interface InputCommandLeaseContext {
  view: WebContentsView;
  method: string;
  /** Playwright 发来的 sessionId。 */
  sessionId: string;
  /** 命令经过主页面、别名还是 Chromium 子会话。 */
  sessionKind: InputSessionKind;
}

/**
 * 只包住真正的 `Input.*` CDP 发送，不包 Locator 的等待/actionability 阶段。
 *
 * BrowserHost 接入时应在 acquire 中增加 automationDepth，并在返回的 release 中减少。
 * acquire 失败会阻止输入发送；release 总在 sendCommand 的 finally 中执行。
 */
export type InputCommandLeaseHook = (
  context: InputCommandLeaseContext,
) => void | (() => void | Promise<void>) | Promise<void | (() => void | Promise<void>)>;

export interface CreatePageLifecycleContext {
  /** The exact Page/View whose public API initiated this root command, when known. */
  sourceView: WebContentsView | null;
  /** Absolute Host command deadline; zero means the caller did not install one. */
  deadlineAt: number;
  url: string;
  /** Empty for Chromium's default persistent context. */
  browserContextId: string;
}

export interface ClosePageLifecycleContext {
  /** The exact Page/View whose public API initiated this root command, when known. */
  sourceView: WebContentsView | null;
  /** Absolute Host command deadline; zero means the caller did not install one. */
  deadlineAt: number;
  /** Chromium's immutable target identity, never Crew's process-local target id. */
  targetId: string;
  /** The already-resolved top-level view for targetId. */
  view: WebContentsView;
}

/**
 * Host-owned implementation of the two public Page lifecycle operations that
 * Chromium normally performs behind its browser websocket.
 *
 * The transport owns protocol ordering and identity validation; BrowserHost
 * owns creation/destruction of the real Electron WebContentsView and rollback.
 */
export interface PageLifecycleHook {
  createPage(context: CreatePageLifecycleContext): Promise<string>;
  closePage(context: ClosePageLifecycleContext): Promise<void>;
}

interface PageLifecycleSource {
  view: WebContentsView;
  deadlineAt: number;
  /**
   * Async resources created by user code can outlive the originating RPC.
   * Keep attribution for those callbacks, but never reuse the expired command
   * deadline after the scoped operation has returned.
   */
  active: boolean;
}

/** 合成的浏览器目标会话 id。`newCDPSession` 的第一步会拿到它。 */
const BROWSER_SESSION_ID = 'pw-browser';
const DEFAULT_ATTACH_TIMEOUT_MS = 10_000;
const POPUP_ADOPTION_TIMEOUT_MS = 5_000;
const PAGE_READY_BUFFERED_EVENTS = new Set([
  'Page.javascriptDialogOpening',
  'Page.javascriptDialogClosed',
  'Page.frameRequestedNavigation',
  'Page.fileChooserOpened',
]);

/** 本地实现、绝不转发的 root 命令。 */
export const ELECTRON_CDP_ROOT_COMMANDS = Object.freeze([
  'Browser.cancelDownload',
  'Browser.getVersion',
  'Browser.grantPermissions',
  'Browser.resetPermissions',
  'Browser.setDownloadBehavior',
  'Storage.clearCookies',
  'Storage.getCookies',
  'Storage.setCookies',
  'Target.closeTarget',
  'Target.createTarget',
  'Target.getTargets',
  'Target.getTargetInfo',
  'Target.setAutoAttach',
  'Target.setDiscoverTargets',
] as const);

/** 合成 browser session 上允许的全部命令。 */
export const ELECTRON_CDP_BROWSER_SESSION_COMMANDS = Object.freeze([
  'Browser.grantPermissions',
  'Browser.resetPermissions',
  'Storage.clearCookies',
  'Storage.getCookies',
  'Storage.setCookies',
  'Target.attachToBrowserTarget',
  'Target.attachToTarget',
  'Target.detachFromTarget',
  'Target.getTargets',
  'Target.getTargetInfo',
] as const);

export const ELECTRON_CDP_CAPABILITIES = Object.freeze({
  existingPages: true,
  oopifAndWorkerSessions: true,
  pageCdpSessions: true,
  independentAliasEventDomains: false,
  createPage: true,
  closePage: true,
  createBrowserContext: false,
  persistentContextCookies: true,
  persistentContextPermissions: true,
  pagePdf: true,
  serviceWorkers: true,
  browserScopedForwarding: false,
});

const PDF_STREAM_PREFIX = 'pw-pdf-stream-';

function asError(error: unknown, prefix = ''): Error {
  const detail = error instanceof Error ? error.message : String(error);
  return new Error(prefix ? `${prefix}: ${detail}` : detail);
}

function fallbackPlatformToken(): string {
  if (process.platform === 'win32') return 'Windows NT 10.0; Win64; x64';
  if (process.platform === 'darwin') return 'Macintosh; Intel Mac OS X 10_15_7';
  if (process.arch === 'arm64') return 'X11; Linux aarch64';
  return 'X11; Linux x86_64';
}

export class ElectronCdpTransport implements CdpTransport {
  private readonly tabs = new Map<string, TabSession>();
  /** 别名会话 → 它指向的页面或 OOPIF/worker 物理会话。 */
  private readonly aliases = new Map<string, AliasSession>();
  /** 每个 view 独立的 attach 状态；单个坏 tab 不得拖垮其他 tab。 */
  private readonly views = new Map<WebContentsView, ViewState>();
  /** Electron Page.downloadWillBegin guid → physical page debugger. */
  private readonly downloadTabs = new Map<string, TabSession>();
  /** Public Playwright guid → the authoritative Electron DownloadItem. */
  private readonly publicDownloads = new Map<string, PublicDownloadBinding>();
  /** One browser/root service-worker target per Chromium targetId. */
  private readonly serviceWorkers = new Map<string, PromotedServiceWorker>();
  /** Synthetic root session id → promoted service worker. */
  private readonly serviceWorkerSessions = new Map<string, PromotedServiceWorker>();
  /** Electron-backed Page.printToPDF streams, scoped to their exact logical session. */
  private readonly pdfStreams = new Map<string, PdfStreamBinding>();
  /** Native events may precede Page.downloadWillBegin; retain them until paired. */
  private readonly pendingNativeDownloads: NativeDownloadBinding[] = [];
  private readonly nativeDownloadDone = new WeakMap<DownloadItem, Promise<string>>();
  private artifactsDirectory = '';
  private autoAttach = false;
  private sequence = 0;
  private closed = false;
  /** Owner-persistent Electron Session retained even while context.pages() is empty. */
  private electronSession: Session | null = null;
  /**
   * Browser.grantPermissions/resetPermissions are browser-scoped in Chromium,
   * but Electron exposes CDP only through a live page debugger. Commands made
   * while pages() is empty are replayed before the next target is published.
   */
  private readonly pendingPermissionCommands: PendingPermissionCommand[] = [];
  private permissionReplayTail: Promise<void> = Promise.resolve();
  private inputCommandLeaseHook: InputCommandLeaseHook | null = null;
  private childSessionLifecycleHook: ChildSessionLifecycleHook | null = null;
  private pageLifecycleHook: PageLifecycleHook | null = null;
  /**
   * A root Target.createTarget request has no protocol field pointing back to
   * the Page that called context().newPage(). AsyncLocalStorage preserves that
   * invocation identity without wrapping or replacing Playwright's public Page.
   */
  private readonly pageLifecycleSource = new AsyncLocalStorage<PageLifecycleSource>();
  private readonly childSessionHookTimeoutMs: number;
  /**
   * Mouse/key commands share one physical input lane per view. This is
   * intentionally narrower than a general response-serialization lane:
   * Playwright must be able to send cancellation/cleanup commands while a
   * Runtime.callFunctionOn promise is still pending.
   */
  private readonly inputCommandTails = new WeakMap<WebContentsView, Promise<void>>();
  /**
   * Human-owned tabs keep native JavaScript dialogs outside playwright-core.
   * Otherwise a user can close the Chromium modal directly while core retains
   * an unhandled Dialog forever, poisoning every later evaluation.
   */
  private readonly forwardDialogs = new WeakMap<WebContentsView, boolean>();

  onmessage?: (message: object) => void;
  onclose?: (reason?: string) => void;

  constructor(options: {
    inputCommandLeaseHook?: InputCommandLeaseHook;
    childSessionLifecycleHook?: ChildSessionLifecycleHook;
    pageLifecycleHook?: PageLifecycleHook;
    /** Optional test/diagnostic deadline. Production waits for exact lifecycle ordering. */
    childSessionHookTimeoutMs?: number;
  } = {}) {
    this.inputCommandLeaseHook = options.inputCommandLeaseHook ?? null;
    this.childSessionLifecycleHook = options.childSessionLifecycleHook ?? null;
    this.pageLifecycleHook = options.pageLifecycleHook ?? null;
    this.childSessionHookTimeoutMs = Math.max(0, options.childSessionHookTimeoutMs ?? 0);
  }

  /** BrowserHost 可在引擎创建后接入精确的 native-input lease。 */
  setInputCommandLeaseHook(hook: InputCommandLeaseHook | null): void {
    this.inputCommandLeaseHook = hook;
  }

  /**
   * Install a barrier for OOPIF/worker session lifecycle.
   *
   * The target event remains behind this barrier so the recorder and
   * playwright-core observe one exact child-session order. Production does not
   * silently skip a slow OOPIF installation; tests may inject an explicit
   * diagnostic deadline through the constructor.
   */
  setChildSessionLifecycleHook(hook: ChildSessionLifecycleHook | null): void {
    this.childSessionLifecycleHook = hook;
  }

  setPageLifecycleHook(hook: PageLifecycleHook | null): void {
    this.pageLifecycleHook = hook;
  }

  /**
   * Attribute root lifecycle commands emitted by a public Playwright call to
   * their real Crew view. This is invocation metadata only: the Page object is
   * still the unmodified public playwright-core Page.
   */
  runWithPageLifecycleSource<T>(
    view: WebContentsView,
    deadlineAt: number,
    operation: () => Promise<T>,
  ): Promise<T> {
    const source: PageLifecycleSource = { view, deadlineAt, active: true };
    return this.pageLifecycleSource.run(source, async () => {
      try {
        return await operation();
      } finally {
        source.active = false;
      }
    });
  }

  setDialogForwarding(view: WebContentsView, enabled: boolean): void {
    this.forwardDialogs.set(view, enabled);
  }

  setArtifactsDirectory(directory: string): void {
    this.artifactsDirectory = directory;
  }

  /**
   * Pair Electron's authoritative native item with the public Playwright
   * Download guid emitted separately by the page debugger.
   */
  registerNativeDownload(view: WebContentsView, item: DownloadItem): void {
    const tab = this.views.get(view)?.tab;
    if (!tab || !this.isTabCurrent(tab)) return;
    const binding: NativeDownloadBinding = {
      tab,
      item,
      url: this.nativeDownloadText(item, 'url'),
      suggestedFilename: this.nativeDownloadText(item, 'filename'),
    };
    const done = new Promise<string>((resolve) => {
      item.once('done', (_event, state) => resolve(state));
    });
    this.nativeDownloadDone.set(item, done);
    const route = this.bestPublicDownloadBinding(binding);
    if (route) {
      this.bindNativeDownload(route, item);
    } else {
      this.pendingNativeDownloads.push(binding);
    }
    void done.then(() => {
      const index = this.pendingNativeDownloads.indexOf(binding);
      if (index >= 0) this.pendingNativeDownloads.splice(index, 1);
    });
  }

  /**
   * 登记一个 view。`Target.setAutoAttach` 前只记账；握手后登记则独立异步 attach。
   * late tab attach 失败只会让该 view 的 waiter 失败，不会关闭 transport。
   */
  addView(
    view: WebContentsView,
    options: {
      opener?: WebContentsView | null | undefined;
      deferPageEventsUntilReady?: boolean;
    } = {},
  ): void {
    if (this.closed) return;
    const electronSession = view.webContents.session;
    if (this.electronSession && this.electronSession !== electronSession) {
      throw new Error('同一 Playwright transport 不能混用不同 Electron Session');
    }
    this.electronSession = electronSession;
    const existing = this.views.get(view);
    if (existing && existing.status !== 'removed') {
      if (!existing.openerView && options.opener) existing.openerView = options.opener;
      return;
    }

    const state: ViewState = {
      view,
      openerView: options.opener ?? null,
      deferPageEventsUntilReady: options.deferPageEventsUntilReady === true,
      status: 'pending',
      generation: 0,
      error: null,
      tab: null,
      attachTask: null,
      waiters: new Set(),
    };
    this.views.set(view, state);
    if (this.autoAttach) void this.startAttach(state).catch(() => undefined);
  }

  /**
   * 等待指定 view 真正完成 debugger attach、targetInfo 获取和事件发布。
   *
   * 这是 Engine 唯一应使用的 ready API；同步 `targetIdForView()` 只用于诊断。
   */
  async waitForViewTarget(
    view: WebContentsView,
    timeoutMs = DEFAULT_ATTACH_TIMEOUT_MS,
  ): Promise<string> {
    if (this.closed) throw new Error('CDP transport 已关闭');
    const state = this.views.get(view);
    if (!state || state.status === 'removed') throw new Error('该标签页未登记或已移除');
    const readyTarget = this.targetIdFromState(state);
    if (readyTarget) return readyTarget;
    if (view.webContents.isDestroyed()) throw new Error('标签页已销毁');

    // 失败状态允许按需重试；这对 late tab 的瞬时 debugger 竞争很重要。
    if (state.status === 'failed') {
      state.status = 'pending';
      state.error = null;
    }

    const promise = new Promise<string>((resolve, reject) => {
      const waiter: ViewWaiter = {
        resolve,
        reject,
        timer: null,
      };
      if (timeoutMs > 0) {
        waiter.timer = setTimeout(() => {
          state.waiters.delete(waiter);
          reject(new Error(`等待 Playwright 收编标签页超时（${timeoutMs}ms）`));
        }, timeoutMs);
      }
      state.waiters.add(waiter);
    });

    if (this.autoAttach) void this.startAttach(state).catch(() => undefined);
    return await promise;
  }

  /** 同步诊断接口；未 ready 时明确返回空串。 */
  targetIdForView(view: WebContentsView): string {
    const state = this.views.get(view);
    return state ? this.targetIdFromState(state) : '';
  }

  /**
   * Called only after PlaywrightEngine has a real Page, installed dialog /
   * filechooser listeners and bound Page ↔ Electron view identity.
   */
  markPageEventsReady(view: WebContentsView): void {
    const state = this.views.get(view);
    const tab = state?.tab;
    if (!tab || tab.pageEventsReady) return;
    tab.pageEventsReady = true;
    const buffered = tab.bufferedPageEvents.splice(0);
    for (const event of buffered) {
      if (!this.isTabCurrent(tab)) break;
      this.emitDebuggerEvent(
        tab,
        event.method,
        event.params,
        event.sourceSessionId,
      );
    }
  }

  /**
   * Close a modal that blocked Playwright from constructing its Page.
   *
   * This is the one safe raw-CDP exception: while pageEventsReady=false the
   * opening event is still private to this transport, so playwright-core has
   * no DialogManager state to clean up. We remove the opening and suppress the
   * matching close as one transaction. Once events are published, callers
   * must use Playwright's public Dialog API instead.
   */
  async handleUnpublishedDialog(
    view: WebContentsView,
    options: {
      accept: boolean;
      expectedType?: string;
      promptText?: string;
    },
  ): Promise<UnpublishedDialogResult | null> {
    const state = this.views.get(view);
    const tab = state?.tab;
    if (
      !tab
      || !this.isTabCurrent(tab)
      || tab.pageEventsReady
      || view.webContents.isDestroyed()
    ) {
      return null;
    }
    const openingIndex = tab.bufferedPageEvents.findIndex(
      (event) => event.method === 'Page.javascriptDialogOpening',
    );
    if (openingIndex < 0) return null;
    const [opening] = tab.bufferedPageEvents.splice(openingIndex, 1);
    const params = opening?.params ?? {};
    const type = typeof params.type === 'string' ? params.type : '';
    const message = typeof params.message === 'string' ? params.message : '';
    const defaultValue = typeof params.defaultPrompt === 'string'
      ? params.defaultPrompt
      : '';
    const matched = !options.expectedType || type === options.expectedType;

    // beforeunload navigation bookkeeping is paired with the Dialog object in
    // playwright-core. Since core will never see this early Dialog, it must not
    // receive the buffered precursor either.
    if (type === 'beforeunload') {
      const navigationIndex = tab.bufferedPageEvents.findIndex(
        (event) => event.method === 'Page.frameRequestedNavigation',
      );
      if (navigationIndex >= 0) tab.bufferedPageEvents.splice(navigationIndex, 1);
    }

    const suppressionBefore = tab.suppressedDialogClosed;
    tab.suppressedDialogClosed += 1;
    try {
      await view.webContents.debugger.sendCommand('Page.handleJavaScriptDialog', {
        accept: matched && options.accept,
        ...(matched && options.accept && options.promptText !== undefined
          ? { promptText: options.promptText }
          : {}),
      });
    } catch (error) {
      // If no closed event consumed this token, restore the opening so a later
      // Page can still receive a coherent event instead of silently losing it.
      if (tab.suppressedDialogClosed > suppressionBefore) {
        tab.suppressedDialogClosed -= 1;
        tab.bufferedPageEvents.splice(openingIndex, 0, opening);
      }
      throw error;
    }
    return { type, message, defaultValue, matched };
  }

  /** 移除 view；会取消 pending/in-flight attach 并拒绝所有 waiter。 */
  removeView(view: WebContentsView): void {
    const state = this.views.get(view);
    if (!state) return;
    this.views.delete(view);
    state.generation += 1;
    state.status = 'removed';
    state.error = new Error('标签页已移除');
    this.rejectWaiters(state, state.error);

    const tab = state.tab;
    state.tab = null;
    if (tab) {
      const published = this.tabs.get(tab.sessionId) === tab;
      this.releaseTab(tab, { emitTargetDetach: published, detachDebugger: tab.attachedByTransport });
    }
  }

  send(message: object): void {
    void this.handle(message as CdpMessage);
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    const error = new Error('CDP transport 已关闭');
    for (const state of this.views.values()) {
      state.generation += 1;
      state.status = 'removed';
      state.error = error;
      this.rejectWaiters(state, error);
      if (state.tab) {
        this.releaseTab(state.tab, {
          emitTargetDetach: false,
          detachDebugger: state.tab.attachedByTransport,
        });
        state.tab = null;
      }
    }
    this.views.clear();
    this.tabs.clear();
    this.aliases.clear();
    this.serviceWorkers.clear();
    this.serviceWorkerSessions.clear();
    this.pdfStreams.clear();
    this.onclose?.('transport closed');
  }

  // ── 协议分发 ─────────────────────────────────────────────────────────

  private emit(message: object): void {
    if (this.closed) return;
    this.onmessage?.(message);
  }

  private async handle(message: CdpMessage): Promise<void> {
    const { id, sessionId, method, params } = message;
    try {
      const result = await this.dispatch(method ?? '', params, sessionId);
      this.emit({ id, sessionId, result: result ?? {} });
    } catch (error) {
      this.emit({
        id,
        sessionId,
        error: { code: -32000, message: error instanceof Error ? error.message : String(error) },
      });
    }
  }

  private async dispatch(
    method: string,
    params: Record<string, unknown> | undefined,
    sessionId: string | undefined,
  ): Promise<Record<string, unknown> | undefined> {
    if (sessionId === BROWSER_SESSION_ID || (!sessionId && method === 'Target.attachToBrowserTarget')) {
      return await this.dispatchBrowserSession(method, params);
    }
    if (!sessionId) return await this.dispatchRoot(method, params);
    return await this.dispatchTab(method, params, sessionId);
  }

  private async dispatchBrowserSession(
    method: string,
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown> | undefined> {
    switch (method) {
      case 'Target.attachToBrowserTarget':
        return { sessionId: BROWSER_SESSION_ID };
      case 'Target.attachToTarget': {
        const targetId = typeof params?.targetId === 'string' ? params.targetId : '';
        if (params?.flatten === false) {
          throw new Error('Electron transport 只支持 flatten=true 的 Target.attachToTarget');
        }
        const target = this.targetRoute(targetId);
        if (!target) throw new Error(`未知 target: ${targetId}`);
        const alias = `pw-raw-${++this.sequence}`;
        this.aliases.set(alias, { ...target, targetId });
        return { sessionId: alias };
      }
      case 'Target.detachFromTarget': {
        const alias = typeof params?.sessionId === 'string' ? params.sessionId : '';
        if (!alias || !this.aliases.delete(alias)) {
          throw new Error(`未知或已关闭的别名 sessionId: ${alias || '<empty>'}`);
        }
        return {};
      }
      case 'Target.getTargetInfo':
        return { targetInfo: this.targetInfoFor(params) };
      case 'Target.getTargets':
        return { targetInfos: this.targetInfosForDiscovery() };
      case 'Browser.grantPermissions':
      case 'Browser.resetPermissions':
        return await this.dispatchOwnerPermissionCommand(method, params);
      case 'Storage.getCookies':
      case 'Storage.setCookies':
      case 'Storage.clearCookies':
        return await this.dispatchOwnerStorageCommand(method, params);
      default:
        throw this.unsupportedBrowserCommand(method, 'browser session');
    }
  }

  private async dispatchRoot(
    method: string,
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown> | undefined> {
    switch (method) {
      case 'Browser.getVersion': {
        const userAgent = this.browserUserAgent();
        const chromeVersion =
          process.versions.chrome ?? /(?:Chrome|Chromium)\/([\d.]+)/.exec(userAgent)?.[1] ?? '0.0.0.0';
        return {
          protocolVersion: '1.3',
          product: `Chrome/${chromeVersion}`,
          revision: '',
          userAgent,
          jsVersion: process.versions.v8,
        };
      }
      case 'Target.setAutoAttach': {
        if (params?.autoAttach === false) {
          this.autoAttach = false;
          return {};
        }
        this.autoAttach = true;
        const pending = [...this.views.values()].filter(
          (state) => state.status === 'pending' || state.status === 'failed',
        );
        // 单个 tab 的 debugger 失败不能阻止其他 pending tab 被 Playwright 发现。
        await Promise.allSettled(pending.map((state) => this.startAttach(state)));
        return {};
      }
      case 'Target.createTarget': {
        const hook = this.pageLifecycleHook;
        if (!hook) {
          throw new Error('BrowserHost 未安装 Playwright 页面创建生命周期');
        }
        const source = this.pageLifecycleSource.getStore();
        const url = typeof params?.url === 'string' && params.url
          ? params.url
          : 'about:blank';
        const browserContextId = typeof params?.browserContextId === 'string'
          ? params.browserContextId
          : '';
        const targetId = await hook.createPage({
          sourceView: source?.view ?? null,
          deadlineAt: source?.active ? source.deadlineAt : 0,
          url,
          browserContextId,
        });
        if (!targetId) throw new Error('BrowserHost 创建页面后未返回 targetId');
        const route = this.targetRoute(targetId);
        if (!route || route.childSessionId) {
          throw new Error(`BrowserHost 返回了未登记的页面 targetId: ${targetId}`);
        }
        return { targetId };
      }
      case 'Target.closeTarget': {
        const hook = this.pageLifecycleHook;
        if (!hook) {
          throw new Error('BrowserHost 未安装 Playwright 页面关闭生命周期');
        }
        const targetId = typeof params?.targetId === 'string' ? params.targetId : '';
        if (!targetId) throw new Error('Target.closeTarget 缺少 targetId');
        const route = this.targetRoute(targetId);
        if (!route || route.childSessionId) {
          throw new Error(`未知或非页面 target: ${targetId}`);
        }
        const source = this.pageLifecycleSource.getStore();
        await hook.closePage({
          sourceView: source?.view ?? null,
          deadlineAt: source?.active ? source.deadlineAt : 0,
          targetId,
          view: route.tab.view,
        });
        if (this.targetRoute(targetId)) {
          throw new Error(`BrowserHost 关闭页面后 target 仍处于登记状态: ${targetId}`);
        }
        return { success: true };
      }
      case 'Target.getTargetInfo':
        return { targetInfo: this.targetInfoFor(params) };
      case 'Target.getTargets':
        return { targetInfos: this.targetInfosForDiscovery() };
      case 'Target.setDiscoverTargets':
        return {};
      case 'Browser.setDownloadBehavior':
        // noDefaults 下通常不会调用；Crew 的 Session will-download 是权威下载策略。
        return {};
      case 'Browser.cancelDownload': {
        const guid = typeof params?.guid === 'string' ? params.guid : '';
        if (!guid) throw new Error('Browser.cancelDownload 缺少 guid');
        const route = this.publicDownloads.get(guid);
        // Chromium deliberately treats unknown/already-finished guids as a
        // successful no-op. Preserve that public Playwright contract.
        if (!route || !this.isTabCurrent(route.tab)) return {};
        route.cancelRequested = true;
        if (route.item) this.cancelNativeDownload(route.item);
        return {};
      }
      case 'Browser.grantPermissions':
      case 'Browser.resetPermissions':
        return await this.dispatchOwnerPermissionCommand(method, params);
      case 'Storage.getCookies':
      case 'Storage.setCookies':
      case 'Storage.clearCookies':
        return await this.dispatchOwnerStorageCommand(method, params);
      default:
        throw this.unsupportedBrowserCommand(method, 'root');
    }
  }

  private async dispatchTab(
    method: string,
    params: Record<string, unknown> | undefined,
    sessionId: string,
  ): Promise<Record<string, unknown> | undefined> {
    const route = this.route(sessionId);
    if (!route) throw new Error(`未知 sessionId: ${sessionId}（${method}）`);
    const { tab, childSessionId, sessionKind } = route;
    if (tab.view.webContents.isDestroyed()) throw new Error('标签页已销毁');

    if (!method.startsWith('Input.')) {
      if (method === 'Page.printToPDF') {
        if (childSessionId) {
          throw new Error('Page.printToPDF 只支持顶层 Page 会话');
        }
        return await this.dispatchPrintToPDF(tab, sessionId, params);
      }
      if (method === 'IO.read' || method === 'IO.close') {
        const handle = typeof params?.handle === 'string' ? params.handle : '';
        if (handle.startsWith(PDF_STREAM_PREFIX)) {
          return this.dispatchPdfStreamCommand(
            method,
            handle,
            tab,
            sessionId,
            params,
          );
        }
      }
      const child = childSessionId
        ? tab.childSessions.get(childSessionId)
        : undefined;
      if (
        childSessionId
        && child?.initializing
        // CRServiceWorker deliberately sends network/user-agent setup,
        // Runtime.enable and Runtime.runIfWaitingForDebugger concurrently.
        // Serializing those commands can deadlock the pre-resume worker:
        // an earlier command waits for execution while the resume is queued
        // behind it. The Electron serialization workaround is OOPIF-only.
        && child.targetInfo.type !== 'service_worker'
      ) {
        return await this.dispatchInitializingChildCommand(
          tab,
          child,
          childSessionId,
          method,
          params,
        );
      }
      const result = (await tab.view.webContents.debugger.sendCommand(
        method,
        params,
        childSessionId,
      )) as Record<string, unknown>;
      if (
        child
        && method === 'Runtime.runIfWaitingForDebugger'
        && tab.childSessions.get(childSessionId ?? '') === child
      ) {
        child.initializing = false;
      }
      return result;
    }

    // Serialize the physical input lane across tab/alias/child logical sessions
    // that all target this same WebContentsView.
    const previous = this.inputCommandTails.get(tab.view) ?? Promise.resolve();
    let unlock!: () => void;
    const gate = new Promise<void>((resolve) => {
      unlock = resolve;
    });
    const tail = previous.catch(() => undefined).then(() => gate);
    this.inputCommandTails.set(tab.view, tail);
    await previous.catch(() => undefined);

    let release: void | (() => void | Promise<void>) = undefined;
    try {
      if (this.closed || tab.view.webContents.isDestroyed()) {
        throw new Error('标签页已销毁或 CDP transport 已关闭');
      }
      // `route()` above ran before this command entered the per-view Input
      // lane. An alias may have been closed or an OOPIF may have detached while
      // an earlier mouse/key command was in flight. Re-resolve at the physical
      // dispatch boundary so a queued command can never escape through a stale
      // Electron child session.
      const currentRoute = this.route(sessionId);
      if (
        !currentRoute
        || currentRoute.tab !== tab
        || currentRoute.childSessionId !== childSessionId
        || currentRoute.sessionKind !== sessionKind
      ) {
        throw new Error(`sessionId 已在等待输入调度时失效: ${sessionId}（${method}）`);
      }
      if (this.inputCommandLeaseHook) {
        release = await this.inputCommandLeaseHook({
          view: tab.view,
          method,
          sessionId,
          sessionKind,
        });
      }
      try {
        return (await tab.view.webContents.debugger.sendCommand(
          method,
          params,
          childSessionId,
        )) as Record<string, unknown>;
      } finally {
        await release?.();
      }
    } finally {
      unlock();
      if (this.inputCommandTails.get(tab.view) === tail) {
        this.inputCommandTails.delete(tab.view);
      }
    }
  }

  /**
   * Electron 43 can permanently pause a live OOPIF when Playwright's
   * FrameSession initialization commands overlap as sendCommand promises.
   * Serialize only that short bootstrap sequence. A general per-view response
   * lane is incorrect: a hung actionability evaluation must not block the
   * timeout's cancellation/focus/cleanup commands.
   */
  private async dispatchInitializingChildCommand(
    tab: TabSession,
    child: ChildSessionState,
    childSessionId: string,
    method: string,
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown>> {
    const command = child.initializationTail
      .catch(() => undefined)
      .then(async () => {
        if (
          !this.isTabCurrent(tab)
          || !child.live
          || tab.childSessions.get(childSessionId) !== child
          || tab.view.webContents.isDestroyed()
        ) {
          throw new Error(
            `child session 已在初始化队列中失效: ${childSessionId}（${method}）`,
          );
        }
        const result = (await tab.view.webContents.debugger.sendCommand(
          method,
          params,
          childSessionId,
        )) as Record<string, unknown>;
        if (method === 'Runtime.runIfWaitingForDebugger') {
          child.initializing = false;
        }
        return result;
      });
    child.initializationTail = command.then(
      () => undefined,
      () => undefined,
    );
    return await command;
  }

  private route(sessionId: string): {
    tab: TabSession;
    childSessionId?: string;
    sessionKind: InputSessionKind;
  } | null {
    const direct = this.tabs.get(sessionId);
    if (direct) return { tab: direct, sessionKind: 'tab' };
    const promotedWorker = this.serviceWorkerSessions.get(sessionId);
    if (promotedWorker) {
      const { primary } = promotedWorker;
      if (
        primary.child.live
        && this.isTabCurrent(primary.tab)
        && primary.tab.childSessions.get(primary.childSessionId) === primary.child
      ) {
        return {
          tab: primary.tab,
          childSessionId: primary.childSessionId,
          sessionKind: 'child',
        };
      }
      return null;
    }
    const alias = this.aliases.get(sessionId);
    if (alias) {
      if (!alias.childSessionId) return { tab: alias.tab, sessionKind: 'alias' };
      const child = alias.tab.childSessions.get(alias.childSessionId);
      if (child && child === alias.child && child.live) {
        return {
          tab: alias.tab,
          childSessionId: alias.childSessionId,
          sessionKind: 'alias',
        };
      }
      return null;
    }
    for (const tab of this.tabs.values()) {
      const child = tab.childSessions.get(sessionId);
      if (child?.live) {
        return { tab, childSessionId: sessionId, sessionKind: 'child' };
      }
    }
    return null;
  }

  private firstTab(): TabSession | undefined {
    return this.tabs.values().next().value;
  }

  /**
   * Chromium permissions belong to the one persistent owner context. Electron
   * has no browser websocket, so use any live owner debugger. If pages() is
   * empty, retain the exact ordered command journal and acknowledge it; the
   * next view replays that journal before its Target.attachedToTarget event.
   */
  private async dispatchOwnerPermissionCommand(
    method: PendingPermissionCommand['method'],
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown>> {
    const tab = [...this.tabs.values()].find(
      (candidate) => (
        this.isTabCurrent(candidate)
        && !candidate.view.webContents.isDestroyed()
      ),
    );
    if (tab) {
      return (await tab.view.webContents.debugger.sendCommand(
        method,
        params,
      )) as Record<string, unknown>;
    }
    this.pendingPermissionCommands.push({
      method,
      params: params
        ? {
            ...params,
            ...(Array.isArray(params.permissions)
              ? { permissions: [...params.permissions] }
              : {}),
          }
        : undefined,
    });
    return {};
  }

  /**
   * Multiple late views may attach concurrently. Only one may consume the
   * zero-page permission journal, and publication waits for that replay.
   */
  private async replayPendingPermissionCommands(tab: TabSession): Promise<void> {
    const replay = this.permissionReplayTail
      .catch(() => undefined)
      .then(async () => {
        while (this.pendingPermissionCommands.length > 0) {
          const command = this.pendingPermissionCommands[0];
          if (!command) break;
          await tab.view.webContents.debugger.sendCommand(
            command.method,
            command.params,
          );
          if (this.pendingPermissionCommands[0] === command) {
            this.pendingPermissionCommands.shift();
          }
        }
      });
    this.permissionReplayTail = replay.then(
      () => undefined,
      () => undefined,
    );
    await replay;
  }

  private async dispatchPrintToPDF(
    tab: TabSession,
    ownerSessionId: string,
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown>> {
    const options = this.toElectronPrintToPDFOptions(params);
    const transferMode = this.pdfString(params, 'transferMode');
    if (
      transferMode !== undefined
      && transferMode !== 'ReturnAsBase64'
      && transferMode !== 'ReturnAsStream'
    ) {
      throw new Error(`Page.printToPDF.transferMode 无效: ${transferMode}`);
    }
    const printToPDF = tab.view.webContents.printToPDF;
    if (typeof printToPDF !== 'function') {
      throw new Error('当前 Electron WebContents 不支持 printToPDF');
    }
    const result = await printToPDF.call(tab.view.webContents, options);
    const bytes = Buffer.from(result);
    if (transferMode !== 'ReturnAsStream') {
      return { data: bytes.toString('base64') };
    }
    const handle = `${PDF_STREAM_PREFIX}${++this.sequence}`;
    this.pdfStreams.set(handle, {
      tab,
      ownerSessionId,
      bytes,
      offset: 0,
    });
    return { data: '', stream: handle };
  }

  private dispatchPdfStreamCommand(
    method: 'IO.read' | 'IO.close',
    handle: string,
    tab: TabSession,
    ownerSessionId: string,
    params: Record<string, unknown> | undefined,
  ): Record<string, unknown> {
    const stream = this.pdfStreams.get(handle);
    if (!stream) throw new Error(`Invalid stream handle: ${handle}`);
    if (stream.tab !== tab || stream.ownerSessionId !== ownerSessionId) {
      throw new Error(`PDF stream 不属于当前 Page 会话: ${handle}`);
    }
    if (method === 'IO.close') {
      this.pdfStreams.delete(handle);
      return {};
    }

    const rawOffset = params?.offset;
    if (
      rawOffset !== undefined
      && (
        typeof rawOffset !== 'number'
        || !Number.isSafeInteger(rawOffset)
        || rawOffset < 0
      )
    ) {
      throw new Error('IO.read.offset 必须是非负安全整数');
    }
    const rawSize = params?.size;
    if (
      rawSize !== undefined
      && (
        typeof rawSize !== 'number'
        || !Number.isSafeInteger(rawSize)
        || rawSize < 0
      )
    ) {
      throw new Error('IO.read.size 必须是非负安全整数');
    }
    const start = rawOffset ?? stream.offset;
    const available = Math.max(0, stream.bytes.length - start);
    const size = rawSize ?? Math.min(64 * 1024, available);
    const end = Math.min(stream.bytes.length, start + size);
    const chunk = start >= stream.bytes.length
      ? Buffer.alloc(0)
      : stream.bytes.subarray(start, end);
    stream.offset = end;
    return {
      base64Encoded: true,
      data: chunk.toString('base64'),
      eof: end >= stream.bytes.length,
    };
  }

  private toElectronPrintToPDFOptions(
    params: Record<string, unknown> | undefined,
  ): PrintToPDFOptions {
    const options: PrintToPDFOptions = {};
    const booleanMappings = [
      ['landscape', 'landscape'],
      ['displayHeaderFooter', 'displayHeaderFooter'],
      ['printBackground', 'printBackground'],
      ['preferCSSPageSize', 'preferCSSPageSize'],
      ['generateTaggedPDF', 'generateTaggedPDF'],
      ['generateDocumentOutline', 'generateDocumentOutline'],
    ] as const;
    for (const [protocolName, electronName] of booleanMappings) {
      const value = this.pdfBoolean(params, protocolName);
      if (value !== undefined) options[electronName] = value;
    }

    const scale = this.pdfNumber(params, 'scale', { positive: true });
    if (scale !== undefined) options.scale = scale;
    const paperWidth = this.pdfNumber(params, 'paperWidth', { positive: true });
    const paperHeight = this.pdfNumber(params, 'paperHeight', { positive: true });
    if (paperWidth !== undefined || paperHeight !== undefined) {
      options.pageSize = {
        width: paperWidth ?? 8.5,
        height: paperHeight ?? 11,
      };
    }

    const marginTop = this.pdfNumber(params, 'marginTop', { nonNegative: true });
    const marginBottom = this.pdfNumber(params, 'marginBottom', { nonNegative: true });
    const marginLeft = this.pdfNumber(params, 'marginLeft', { nonNegative: true });
    const marginRight = this.pdfNumber(params, 'marginRight', { nonNegative: true });
    if (
      marginTop !== undefined
      || marginBottom !== undefined
      || marginLeft !== undefined
      || marginRight !== undefined
    ) {
      options.margins = {
        ...(marginTop !== undefined ? { top: marginTop } : {}),
        ...(marginBottom !== undefined ? { bottom: marginBottom } : {}),
        ...(marginLeft !== undefined ? { left: marginLeft } : {}),
        ...(marginRight !== undefined ? { right: marginRight } : {}),
      };
    }

    const pageRanges = this.pdfString(params, 'pageRanges');
    const headerTemplate = this.pdfString(params, 'headerTemplate');
    const footerTemplate = this.pdfString(params, 'footerTemplate');
    if (pageRanges !== undefined) options.pageRanges = pageRanges;
    if (headerTemplate !== undefined) options.headerTemplate = headerTemplate;
    if (footerTemplate !== undefined) options.footerTemplate = footerTemplate;
    return options;
  }

  private pdfBoolean(
    params: Record<string, unknown> | undefined,
    name: string,
  ): boolean | undefined {
    const value = params?.[name];
    if (value === undefined) return undefined;
    if (typeof value !== 'boolean') {
      throw new Error(`Page.printToPDF.${name} 必须是 boolean`);
    }
    return value;
  }

  private pdfNumber(
    params: Record<string, unknown> | undefined,
    name: string,
    constraint: { positive?: boolean; nonNegative?: boolean } = {},
  ): number | undefined {
    const value = params?.[name];
    if (value === undefined) return undefined;
    if (
      typeof value !== 'number'
      || !Number.isFinite(value)
      || (constraint.positive === true && value <= 0)
      || (constraint.nonNegative === true && value < 0)
    ) {
      throw new Error(`Page.printToPDF.${name} 数值无效`);
    }
    return value;
  }

  private pdfString(
    params: Record<string, unknown> | undefined,
    name: string,
  ): string | undefined {
    const value = params?.[name];
    if (value === undefined) return undefined;
    if (typeof value !== 'string') {
      throw new Error(`Page.printToPDF.${name} 必须是 string`);
    }
    return value;
  }

  /**
   * The default Electron Session is the owner's one persistent
   * BrowserContext. Chromium's Playwright backend sends cookie operations on
   * its synthetic browser CDP session, while Electron exposes the same
   * Storage domain through every attached WebContents debugger. Route the
   * command through one live owner tab instead of rejecting public
   * BrowserContext.cookies/addCookies/clearCookies/storageState.
   */
  private async dispatchOwnerStorageCommand(
    method: string,
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown>> {
    const tab = [...this.tabs.values()].find(
      (candidate) => (
        this.isTabCurrent(candidate)
        && !candidate.view.webContents.isDestroyed()
      ),
    );
    if (!tab) {
      return await this.dispatchEmptyContextStorageCommand(method, params);
    }
    return (await tab.view.webContents.debugger.sendCommand(
      method,
      params,
    )) as Record<string, unknown>;
  }

  /**
   * Public BrowserContext cookie APIs remain valid after the last Page closes.
   * There is then no live debugger target through which to forward Storage.*,
   * but Electron's persistent Session is the same cookie store. Use its public
   * cookie API until context.newPage() installs a new debugger delegate.
   */
  private async dispatchEmptyContextStorageCommand(
    method: string,
    params: Record<string, unknown> | undefined,
  ): Promise<Record<string, unknown>> {
    const electronSession = this.electronSession;
    if (!electronSession) {
      throw new Error(`${method} 找不到 owner 的 Electron Session`);
    }
    if (method === 'Storage.getCookies') {
      const cookies = await electronSession.cookies.get({});
      return {
        cookies: cookies.map((cookie) => this.toProtocolCookie(cookie)),
      };
    }
    if (method === 'Storage.clearCookies') {
      const cookies = await electronSession.cookies.get({});
      for (const cookie of cookies) {
        await electronSession.cookies.remove(
          this.electronCookieURL(cookie),
          cookie.name,
        );
      }
      await electronSession.cookies.flushStore();
      return {};
    }
    if (method === 'Storage.setCookies') {
      const rawCookies = params?.cookies;
      if (!Array.isArray(rawCookies)) {
        throw new Error('Storage.setCookies 缺少 cookies 数组');
      }
      for (const value of rawCookies) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) {
          throw new Error('Storage.setCookies 含无效 cookie');
        }
        const cookie = value as Record<string, unknown>;
        const name = typeof cookie.name === 'string' ? cookie.name : '';
        const rawURL = typeof cookie.url === 'string' ? cookie.url : '';
        const domain = typeof cookie.domain === 'string' ? cookie.domain : '';
        const cookiePath = typeof cookie.path === 'string' && cookie.path
          ? cookie.path
          : '/';
        if (!name || (!rawURL && !domain)) {
          throw new Error('Storage.setCookies cookie 缺少 name/url/domain');
        }
        if (cookie.partitionKey !== undefined) {
          throw new Error(
            '无活动页面时 Electron Session API 不支持设置 partitionKey cookie',
          );
        }
        const secure = cookie.secure === true;
        const url = rawURL || `${
          secure ? 'https' : 'http'
        }://${domain.replace(/^\./, '')}${cookiePath.startsWith('/') ? cookiePath : `/${cookiePath}`}`;
        // URL construction supplies the same early invalid-input failure as
        // Chromium's Storage.setCookies command.
        void new URL(url);
        const expires = typeof cookie.expires === 'number' ? cookie.expires : -1;
        const sameSite = cookie.sameSite === 'Strict'
          ? 'strict' as const
          : cookie.sameSite === 'None'
            ? 'no_restriction' as const
            : cookie.sameSite === 'Lax'
              ? 'lax' as const
              : undefined;
        await electronSession.cookies.set({
          url,
          name,
          value: typeof cookie.value === 'string' ? cookie.value : '',
          ...(!rawURL && domain ? { domain } : {}),
          path: cookiePath,
          secure,
          httpOnly: cookie.httpOnly === true,
          ...(expires > 0 ? { expirationDate: expires } : {}),
          ...(sameSite ? { sameSite } : {}),
        });
      }
      await electronSession.cookies.flushStore();
      return {};
    }
    throw this.unsupportedBrowserCommand(method, 'empty-context storage');
  }

  private electronCookieURL(cookie: Cookie): string {
    const domain = String(cookie.domain ?? '').replace(/^\./, '');
    if (!domain) throw new Error(`Electron cookie ${cookie.name} 缺少 domain`);
    const cookiePath = cookie.path?.startsWith('/')
      ? cookie.path
      : `/${cookie.path ?? ''}`;
    return `${cookie.secure ? 'https' : 'http'}://${domain}${cookiePath}`;
  }

  private toProtocolCookie(cookie: Cookie): Record<string, unknown> {
    const sameSite = cookie.sameSite === 'strict'
      ? 'Strict'
      : cookie.sameSite === 'no_restriction'
        ? 'None'
        : cookie.sameSite === 'lax'
          ? 'Lax'
          : undefined;
    return {
      name: cookie.name,
      value: cookie.value,
      domain: cookie.domain ?? '',
      path: cookie.path ?? '/',
      expires: cookie.expirationDate ?? -1,
      httpOnly: cookie.httpOnly === true,
      secure: cookie.secure === true,
      ...(sameSite ? { sameSite } : {}),
    };
  }

  private targetInfoFor(
    params: Record<string, unknown> | undefined,
  ): Record<string, unknown> | undefined {
    const targetId = typeof params?.targetId === 'string' ? params.targetId : '';
    if (!targetId) return this.firstTab()?.targetInfo ?? undefined;
    const target = this.targetRoute(targetId);
    if (!target) throw new Error(`未知 target: ${targetId}`);
    if (!target.childSessionId) return target.tab.targetInfo ?? undefined;
    return target.child?.targetInfo;
  }

  private targetInfosForDiscovery(): Record<string, unknown>[] {
    const infos: Record<string, unknown>[] = [];
    for (const tab of this.tabs.values()) {
      if (tab.targetInfo) infos.push({ ...tab.targetInfo, attached: true });
      for (const child of tab.childSessions.values()) {
        if (
          child.live
          && child.targetInfo.type !== 'service_worker'
        ) {
          infos.push({ ...child.targetInfo, attached: true });
        }
      }
    }
    for (const worker of this.serviceWorkers.values()) {
      infos.push({ ...worker.targetInfo, attached: true });
    }
    return infos;
  }

  private targetRoute(targetId: string): {
    tab: TabSession;
    childSessionId?: string;
    child?: ChildSessionState;
  } | null {
    const promotedWorker = this.serviceWorkers.get(targetId);
    if (promotedWorker) {
      const { primary } = promotedWorker;
      if (primary.child.live && this.isTabCurrent(primary.tab)) {
        return {
          tab: primary.tab,
          childSessionId: primary.childSessionId,
          child: primary.child,
        };
      }
    }
    for (const tab of this.tabs.values()) {
      if (tab.targetInfo?.targetId === targetId) return { tab };
      for (const [childSessionId, child] of tab.childSessions) {
        if (
          child.live
          && child.targetInfo.type !== 'service_worker'
          && child.targetInfo.targetId === targetId
        ) {
          return { tab, childSessionId, child };
        }
      }
    }
    return null;
  }

  private unsupportedBrowserCommand(method: string, scope: string): Error {
    return new Error(
      `不支持的 ${scope} CDP 命令: ${method || '<empty>'}；`
      + 'Electron transport 只接管既有 Crew 标签页，新增浏览器级能力必须显式审计',
    );
  }

  // ── view attach 状态机 ────────────────────────────────────────────────

  private startAttach(state: ViewState): Promise<void> {
    if (this.closed || state.status === 'removed') {
      return Promise.reject(new Error('CDP transport 已关闭或标签页已移除'));
    }
    if (state.status === 'attached') return Promise.resolve();
    if (state.attachTask) return state.attachTask;
    if (state.status === 'failed') {
      state.status = 'pending';
      state.error = null;
    }
    state.status = 'attaching';
    const generation = ++state.generation;
    const task = this.performAttach(state, generation)
      .catch((error: unknown) => {
        if (state.generation === generation && state.status !== 'removed') {
          const failure = asError(error, '无法收编 Electron 标签页');
          state.status = 'failed';
          state.error = failure;
          if (state.tab) {
            this.releaseTab(state.tab, {
              emitTargetDetach: false,
              detachDebugger: state.tab.attachedByTransport,
            });
            state.tab = null;
          }
          // 先清空再唤醒 waiter，避免 waiter 的 rejection handler 立即重试时仍拿到
          // 这条已经失败、但尚未进入 finally 的旧 promise。
          state.attachTask = null;
          this.rejectWaiters(state, failure);
        }
        throw error;
      })
      .finally(() => {
        if (state.attachTask === task) state.attachTask = null;
      });
    state.attachTask = task;
    return task;
  }

  private async performAttach(state: ViewState, generation: number): Promise<void> {
    const { view } = state;
    // `setWindowOpenHandler().createWindow` registers a freshly constructed
    // WebContentsView before returning it to Electron. Attaching the debugger
    // synchronously inside that callback observes a half-adopted target
    // (url="", no opener, and Page initialization can fail). Yield one native
    // turn so Electron can finish adopting the popup. Existing tabs pay only
    // this single turn, while late popup discovery becomes deterministic.
    await new Promise<void>((resolve) => setImmediate(resolve));
    this.assertAttachCurrent(state, generation);
    if (view.webContents.isDestroyed()) throw new Error('标签页已销毁');
    const debug = view.webContents.debugger;
    const attachedByTransport = !debug.isAttached();
    if (attachedByTransport) debug.attach('1.3');
    this.assertAttachCurrent(state, generation);

    const sessionId = `pw-tab-${++this.sequence}`;
    const tab: TabSession = {
      view,
      state,
      sessionId,
      targetInfo: null,
      childSessions: new Map(),
      attachedByTransport,
      messageListener: null,
      detachListener: null,
      pageEventsReady: !state.deferPageEventsUntilReady,
      bufferedPageEvents: [],
      suppressedDialogClosed: 0,
    };
    state.tab = tab;

    const messageListener = (
      _event: unknown,
      method: string,
      params: Record<string, unknown> | undefined,
      childSessionId?: string,
    ): void => {
      const introduced = typeof params?.sessionId === 'string' ? params.sessionId : '';
      if (method === 'Target.attachedToTarget' && introduced) {
        this.handleChildSessionAttached(tab, introduced, params ?? {}, childSessionId);
        return;
      }
      if (method === 'Target.detachedFromTarget' && introduced) {
        this.handleChildSessionDetached(tab, introduced, params ?? {}, childSessionId);
        return;
      }

      if (childSessionId) {
        const child = tab.childSessions.get(childSessionId);
        // Every child event must remain behind the attach barrier even if the
        // caller replaces/removes the lifecycle hook while installation is in
        // flight. Once detach has arrived, later native events are stale and
        // must not be delivered after Playwright's detached notification.
        if (!child?.live) return;
        child.delivery = child.delivery
          .catch(() => undefined)
          .then(() => {
            if (this.isTabCurrent(tab)) {
              this.emitDebuggerEvent(tab, method, params, childSessionId);
            }
          });
        return;
      }
      this.emitDebuggerEvent(tab, method, params, childSessionId);
    };
    const detachListener = (_event: unknown, reason?: string): void => {
      this.handleDebuggerDetach(tab, reason);
    };
    debug.on('message', messageListener as never);
    debug.on('detach', detachListener as never);
    tab.messageListener = messageListener as never;
    tab.detachListener = detachListener as never;

    let info = (await debug.sendCommand('Target.getTargetInfo')) as {
      targetInfo?: Record<string, unknown>;
    };
    // Attaching first is important: window.open() may synchronously wait for
    // Electron to adopt the returned WebContents. We delay only publication to
    // Playwright, not the debugger attach itself, then refresh TargetInfo after
    // the popup has a committed URL.
    if (state.openerView) {
      await this.waitForPopupAdoption(state, generation);
      this.assertAttachCurrent(state, generation);
      info = (await debug.sendCommand('Target.getTargetInfo')) as {
        targetInfo?: Record<string, unknown>;
      };
    }
    this.assertAttachCurrent(state, generation);
    let targetInfo = info?.targetInfo;
    if (!targetInfo || typeof targetInfo.targetId !== 'string' || !targetInfo.targetId) {
      throw new Error('Target.getTargetInfo 未返回 targetId');
    }
    const targetId = targetInfo.targetId;
    const openerTargetId = state.openerView
      ? this.targetIdForView(state.openerView)
      : '';
    const liveURL = typeof view.webContents.getURL === 'function'
      ? view.webContents.getURL()
      : '';
    if (
      (openerTargetId && targetInfo.openerId !== openerTargetId)
      || (liveURL && !targetInfo.url)
    ) {
      targetInfo = {
        ...targetInfo,
        ...(openerTargetId
          ? { openerId: openerTargetId, canAccessOpener: true }
          : {}),
        ...(liveURL && !targetInfo.url ? { url: liveURL } : {}),
      };
    }
    // Permissions issued after the previous last Page closed must become
    // browser state before playwright-core can observe or navigate this Page.
    await this.replayPendingPermissionCommands(tab);
    this.assertAttachCurrent(state, generation);
    tab.targetInfo = targetInfo;
    this.tabs.set(sessionId, tab);
    state.status = 'attached';
    state.error = null;

    this.emit({
      method: 'Target.attachedToTarget',
      params: {
        sessionId,
        targetInfo: { ...targetInfo, attached: true },
        waitingForDebugger: false,
      },
    });
    this.resolveWaiters(state, targetId);
  }

  /**
   * Electron invokes createWindow before it adopts the returned WebContents
   * and begins the popup navigation. Publishing url="" in that callback makes
   * Pinned Playwright may treat the target as an already-initialized empty page; its
   * Page never becomes observable. Only popup views take this bounded wait.
   */
  private async waitForPopupAdoption(
    state: ViewState,
    generation: number,
  ): Promise<void> {
    const deadline = Date.now() + POPUP_ADOPTION_TIMEOUT_MS;
    while (true) {
      this.assertAttachCurrent(state, generation);
      const contents = state.view.webContents;
      const url = typeof contents.getURL === 'function' ? contents.getURL() : '';
      if (url) return;
      const remaining = deadline - Date.now();
      if (remaining <= 0) return;
      await new Promise<void>((resolve) => setTimeout(resolve, Math.min(25, remaining)));
    }
  }

  private handleChildSessionAttached(
    tab: TabSession,
    childSessionId: string,
    params: Record<string, unknown>,
    parentSessionId?: string,
  ): void {
    const targetInfo = params.targetInfo
      && typeof params.targetInfo === 'object'
      && !Array.isArray(params.targetInfo)
      ? params.targetInfo as Record<string, unknown>
      : {};
    const parentDelivery = parentSessionId
      ? tab.childSessions.get(parentSessionId)?.delivery
      : undefined;
    const child: ChildSessionState = {
      targetInfo,
      serviceWorkerPhysical: null,
      serviceWorkerScriptLoaded: false,
      live: true,
      delivery: Promise.resolve(),
      initializing: true,
      initializationTail: Promise.resolve(),
    };
    tab.childSessions.set(childSessionId, child);

    // A nested target is introduced on its parent's child session. Always
    // remain behind the parent delivery barrier, even when the hook is absent
    // or is replaced while the parent hook is still running. Otherwise
    // Playwright receives an event for a session it has not created yet and
    // silently drops the nested target.
    const hook = this.childSessionLifecycleHook;
    child.delivery = (parentDelivery ?? Promise.resolve())
      .catch(() => undefined)
      .then(async () => {
        if (hook) {
          await this.runChildSessionLifecycleHook(hook, {
            view: tab.view,
            phase: 'attached',
            sessionId: childSessionId,
            parentSessionId: parentSessionId ?? '',
            targetInfo,
          });
        }
      })
      .catch(() => undefined)
      .then(async () => {
        if (this.isTabCurrent(tab)) {
          if (
            !parentSessionId
            && targetInfo.type === 'service_worker'
            && typeof targetInfo.targetId === 'string'
            && targetInfo.targetId
          ) {
            await this.promoteServiceWorker(
              tab,
              childSessionId,
              child,
              params,
            );
          } else {
            this.emitDebuggerEvent(tab, 'Target.attachedToTarget', params, parentSessionId);
          }
        }
      });
  }

  private handleChildSessionDetached(
    tab: TabSession,
    childSessionId: string,
    params: Record<string, unknown>,
    parentSessionId?: string,
  ): void {
    const child = tab.childSessions.get(childSessionId);
    if (!child) {
      this.emitDebuggerEvent(tab, 'Target.detachedFromTarget', params, parentSessionId);
      return;
    }
    // Fail closed for commands immediately, while preserving protocol event order.
    child.live = false;
    const hook = this.childSessionLifecycleHook;
    // Never bypass child.delivery. In particular, a hook may be removed after
    // attach started but before it settled; emitting detach synchronously in
    // that window produces detach → attach and resurrects a dead target in
    // Playwright's graph.
    child.delivery = child.delivery
      .catch(() => undefined)
      .then(async () => {
        if (hook) {
          await this.runChildSessionLifecycleHook(hook, {
            view: tab.view,
            phase: 'detached',
            sessionId: childSessionId,
            parentSessionId: parentSessionId ?? '',
            targetInfo: child.targetInfo,
          });
        }
      })
      .catch(() => undefined)
      .then(() => {
        if (this.isTabCurrent(tab)) {
          if (child.serviceWorkerPhysical) {
            this.removeServiceWorkerPhysical(child.serviceWorkerPhysical, true);
          } else {
            this.emitDebuggerEvent(tab, 'Target.detachedFromTarget', params, parentSessionId);
          }
          this.releaseChildAliases(tab, childSessionId, child);
        }
      })
      .finally(() => {
        if (tab.childSessions.get(childSessionId) === child) {
          tab.childSessions.delete(childSessionId);
        }
      });
  }

  /**
   * Electron exposes a service worker as a top-level child of each page
   * debugger. Chromium Playwright instead expects exactly one service-worker
   * session on CRBrowser's root Target graph. Promote the first physical child
   * and retain duplicates only as failover candidates.
   */
  private async promoteServiceWorker(
    tab: TabSession,
    childSessionId: string,
    child: ChildSessionState,
    attachParams: Record<string, unknown>,
  ): Promise<void> {
    const targetId = String(child.targetInfo.targetId ?? '');
    if (!targetId) return;
    const physical: ServiceWorkerPhysical = {
      tab,
      childSessionId,
      child,
      attachParams,
    };
    child.serviceWorkerPhysical = physical;
    const existing = this.serviceWorkers.get(targetId);
    if (existing) {
      existing.physicals.add(physical);
      // This duplicate has no public CRSession to resume it. Leaving one
      // debugger waiting would pause the shared worker for every page.
      if (child.live && this.isTabCurrent(tab)) {
        try {
          await tab.view.webContents.debugger.sendCommand(
            'Runtime.runIfWaitingForDebugger',
            undefined,
            childSessionId,
          );
          child.initializing = false;
        } catch {
          // Its primary physical session remains authoritative. A failed
          // duplicate is discarded on native detach and must not tear down it.
        }
      }
      return;
    }

    const promoted: PromotedServiceWorker = {
      targetId,
      sessionId: `pw-sw-${++this.sequence}`,
      targetInfo: child.targetInfo,
      physicals: new Set([physical]),
      primary: physical,
    };
    this.serviceWorkers.set(targetId, promoted);
    this.serviceWorkerSessions.set(promoted.sessionId, promoted);
    this.emitServiceWorkerAttached(promoted, physical);
  }

  private removeServiceWorkerPhysical(
    physical: ServiceWorkerPhysical,
    emitLifecycle: boolean,
  ): void {
    const { child } = physical;
    if (child.serviceWorkerPhysical === physical) {
      child.serviceWorkerPhysical = null;
    }
    const targetId = String(child.targetInfo.targetId ?? '');
    const promoted = this.serviceWorkers.get(targetId);
    if (!promoted || !promoted.physicals.delete(physical)) return;
    if (promoted.primary !== physical) return;

    const previousSessionId = promoted.sessionId;
    this.serviceWorkerSessions.delete(previousSessionId);
    const replacement = [...promoted.physicals].find((candidate) => (
      candidate.child.live
      && this.isTabCurrent(candidate.tab)
      && !candidate.tab.view.webContents.isDestroyed()
    ));
    if (emitLifecycle) {
      this.emit({
        method: 'Target.detachedFromTarget',
        params: {
          sessionId: previousSessionId,
          targetId: promoted.targetId,
        },
      });
    }
    if (!replacement) {
      this.serviceWorkers.delete(promoted.targetId);
      return;
    }

    // CRBrowser keys workers by targetId. Close the old CRSession before
    // publishing the replacement, then let core initialize the new physical
    // session through normal public protocol commands.
    promoted.primary = replacement;
    promoted.sessionId = `pw-sw-${++this.sequence}`;
    promoted.targetInfo = replacement.child.targetInfo;
    this.serviceWorkerSessions.set(promoted.sessionId, promoted);
    if (emitLifecycle) this.emitServiceWorkerAttached(promoted, replacement);
  }

  private emitServiceWorkerAttached(
    promoted: PromotedServiceWorker,
    physical: ServiceWorkerPhysical,
  ): void {
    this.emit({
      method: 'Target.attachedToTarget',
      params: {
        ...physical.attachParams,
        sessionId: promoted.sessionId,
        targetInfo: {
          ...promoted.targetInfo,
          attached: true,
        },
        // A duplicate candidate was resumed privately before failover.
        waitingForDebugger: physical.child.initializing
          ? physical.attachParams.waitingForDebugger === true
          : false,
      },
    });
    // A standby physical session was resumed privately so it would not pause
    // the shared worker. Chromium already emitted workerScriptLoaded on that
    // hidden session; replay the state after root attach so CRServiceWorker's
    // fresh execution-context promise can resolve on failover.
    if (physical.child.serviceWorkerScriptLoaded) {
      const sessionId = promoted.sessionId;
      setImmediate(() => {
        if (
          !this.closed
          && this.serviceWorkerSessions.get(sessionId) === promoted
          && promoted.primary === physical
          && physical.child.live
        ) {
          this.emit({
            sessionId,
            method: 'Inspector.workerScriptLoaded',
            params: {},
          });
        }
      });
    }
  }

  private async runChildSessionLifecycleHook(
    hook: ChildSessionLifecycleHook,
    context: Omit<ChildSessionLifecycleContext, 'signal'>,
  ): Promise<void> {
    const controller = new AbortController();
    if (this.childSessionHookTimeoutMs <= 0) {
      await hook({ ...context, signal: controller.signal });
      return;
    }
    let timer: ReturnType<typeof setTimeout> | null = null;
    try {
      await Promise.race([
        Promise.resolve(hook({ ...context, signal: controller.signal })),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            const error = new Error(
              `child-session ${context.phase} hook timeout: ${context.sessionId}`,
            );
            controller.abort(error);
            reject(error);
          }, this.childSessionHookTimeoutMs);
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  private emitDebuggerEvent(
    tab: TabSession,
    method: string,
    params: Record<string, unknown> | undefined,
    sourceSessionId?: string,
  ): void {
    let publicSessionId = sourceSessionId || tab.sessionId;
    if (sourceSessionId) {
      const sourceChild = tab.childSessions.get(sourceSessionId);
      const physical = sourceChild?.serviceWorkerPhysical;
      if (physical) {
        if (method === 'Inspector.workerScriptLoaded') {
          sourceChild.serviceWorkerScriptLoaded = true;
        }
        const targetId = String(sourceChild.targetInfo.targetId ?? '');
        const promoted = this.serviceWorkers.get(targetId);
        // Duplicate page debuggers observe the same service-worker events.
        // Only the authoritative physical session may feed core's one Worker.
        if (!promoted || promoted.primary !== physical) return;
        publicSessionId = promoted.sessionId;
      }
    }
    // Electron's page-scoped debugger still emits the deprecated Page.*
    // download events. Current playwright-core intentionally listens only on
    // CRBrowser's root session for Browser.*. Translate at the transport
    // boundary so the public Page `download` event remains official
    // Playwright behavior while Electron's DownloadItem stays the sole
    // authority for persistence and setSavePath.
    if (
      !sourceSessionId
      && (
        method === 'Page.downloadWillBegin'
        || method === 'Page.downloadProgress'
      )
    ) {
      const guid = typeof params?.guid === 'string' ? params.guid : '';
      let emitRootEvent = true;
      if (guid) {
        if (method === 'Page.downloadWillBegin') {
          this.settlePublicDownload(guid);
          this.downloadTabs.set(guid, tab);
          const route: PublicDownloadBinding = {
            tab,
            url: typeof params?.url === 'string' ? params.url : '',
            suggestedFilename: typeof params?.suggestedFilename === 'string'
              ? params.suggestedFilename
              : '',
            item: null,
            cancelRequested: false,
            waiters: new Set(),
          };
          this.publicDownloads.set(guid, route);
          const pendingIndex = this.bestPendingNativeDownloadIndex(route);
          if (pendingIndex >= 0) {
            const [native] = this.pendingNativeDownloads.splice(pendingIndex, 1);
            if (native) this.bindNativeDownload(route, native.item);
          }
        } else if (params?.state === 'completed') {
          this.downloadTabs.delete(guid);
          emitRootEvent = false;
          void this.finishPublicDownload(guid, params ?? {}).then((finishedParams) => {
            this.emit({
              method: 'Browser.downloadProgress',
              params: finishedParams,
            });
          }).finally(() => {
            this.settlePublicDownload(guid);
          });
        } else if (params?.state === 'canceled') {
          this.downloadTabs.delete(guid);
          this.settlePublicDownload(guid);
        }
      }
      if (emitRootEvent) {
        this.emit({
          method: method === 'Page.downloadWillBegin'
            ? 'Browser.downloadWillBegin'
            : 'Browser.downloadProgress',
          params,
        });
      }
    }
    if (
      method === 'Page.javascriptDialogClosed'
      && tab.suppressedDialogClosed > 0
    ) {
      tab.suppressedDialogClosed -= 1;
      return;
    }
    if (
      (
        method === 'Page.javascriptDialogOpening'
        // beforeunload dismissal is normally paired with Playwright Dialog's
        // frameAbortedNavigation callback. Human-mode dialogs are intentionally
        // absent from core, so forwarding this precursor alone would leave
        // Frame.pendingDocument stuck after a native dismiss.
        || method === 'Page.frameRequestedNavigation'
      )
      && this.forwardDialogs.get(tab.view) === false
    ) {
      return;
    }
    if (!tab.pageEventsReady && PAGE_READY_BUFFERED_EVENTS.has(method)) {
      tab.bufferedPageEvents.push({
        method,
        params,
        ...(sourceSessionId ? { sourceSessionId } : {}),
      });
      return;
    }
    this.emit({ sessionId: publicSessionId, method, params });
    // A raw alias is a second logical CDP session backed by the same physical
    // Electron debugger session. Fan out only events emitted by that exact
    // physical target: page aliases receive page events, frame aliases receive
    // events from their OOPIF session. Never leak sibling-frame events.
    for (const [alias, target] of this.aliases) {
      if (target.tab !== tab) continue;
      if ((target.childSessionId ?? '') !== (sourceSessionId ?? '')) continue;
      this.emit({ sessionId: alias, method, params });
    }
  }

  private nativeDownloadText(
    item: DownloadItem,
    field: 'url' | 'filename',
  ): string {
    try {
      return field === 'url' ? item.getURL() : item.getFilename();
    } catch {
      return '';
    }
  }

  private downloadBindingScore(
    left: { url: string; suggestedFilename: string },
    right: { url: string; suggestedFilename: string },
  ): number {
    let score = 0;
    if (left.url && right.url && left.url === right.url) score += 2;
    if (
      left.suggestedFilename
      && right.suggestedFilename
      && left.suggestedFilename === right.suggestedFilename
    ) score += 1;
    return score;
  }

  private bestPublicDownloadBinding(
    native: NativeDownloadBinding,
  ): PublicDownloadBinding | undefined {
    const candidates = [...this.publicDownloads.values()].filter((route) => (
      route.tab === native.tab && route.item === null
    ));
    if (!candidates.length) return undefined;
    let best = candidates[0];
    let bestScore = this.downloadBindingScore(native, best);
    for (const candidate of candidates.slice(1)) {
      const score = this.downloadBindingScore(native, candidate);
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
    return best;
  }

  private bestPendingNativeDownloadIndex(
    route: PublicDownloadBinding,
  ): number {
    const candidates = this.pendingNativeDownloads
      .map((native, index) => ({ native, index }))
      .filter(({ native }) => native.tab === route.tab);
    if (!candidates.length) return -1;
    let best = candidates[0];
    let bestScore = this.downloadBindingScore(best.native, route);
    for (const candidate of candidates.slice(1)) {
      const score = this.downloadBindingScore(candidate.native, route);
      if (score > bestScore) {
        best = candidate;
        bestScore = score;
      }
    }
    return best.index;
  }

  private bindNativeDownload(
    route: PublicDownloadBinding,
    item: DownloadItem,
  ): void {
    if (route.item) return;
    route.item = item;
    if (route.cancelRequested) this.cancelNativeDownload(item);
    for (const resolve of route.waiters) resolve(item);
    route.waiters.clear();
  }

  private cancelNativeDownload(item: DownloadItem): void {
    try {
      const state = item.getState();
      if (state === 'progressing' || state === 'interrupted') item.cancel();
    } catch {
      // Electron mirrors Chromium: cancelling an already-terminal item is a
      // successful no-op.
    }
  }

  private waitForNativeDownload(
    route: PublicDownloadBinding,
  ): Promise<DownloadItem | null> {
    if (route.item) return Promise.resolve(route.item);
    return new Promise<DownloadItem | null>((resolve) => {
      route.waiters.add(resolve);
    });
  }

  private settlePublicDownload(guid: string): void {
    const route = this.publicDownloads.get(guid);
    if (!route) return;
    this.publicDownloads.delete(guid);
    for (const resolve of route.waiters) resolve(null);
    route.waiters.clear();
  }

  private async finishPublicDownload(
    guid: string,
    params: Record<string, unknown>,
  ): Promise<Record<string, unknown>> {
    const route = this.publicDownloads.get(guid);
    const item = route?.item ?? (route ? await this.waitForNativeDownload(route) : null);
    try {
      if (!item || !this.artifactsDirectory) {
        throw new Error('native download artifact binding unavailable');
      }
      const nativeState = await this.nativeDownloadDone.get(item);
      if (nativeState !== 'completed') {
        throw new Error(`native download ended as ${nativeState ?? 'unknown'}`);
      }
      const source = item.getSavePath();
      if (!source) throw new Error('native download save path unavailable');
      await mkdir(this.artifactsDirectory, { recursive: true });
      const target = path.join(this.artifactsDirectory, guid);
      try {
        await link(source, target);
      } catch {
        // Different filesystems and Windows policies can reject hard links.
        // A byte-for-byte copy preserves all public Download artifact methods.
        await copyFile(source, target);
      }
      return params;
    } catch {
      // Do not report a successful Playwright artifact that does not exist.
      // The task-native file and Host terminal event remain authoritative.
      return {
        ...params,
        state: 'canceled',
      };
    }
  }

  private releaseChildAliases(
    tab: TabSession,
    childSessionId: string,
    child: ChildSessionState,
  ): void {
    for (const [alias, target] of [...this.aliases]) {
      if (
        target.tab !== tab
        || target.childSessionId !== childSessionId
        || target.child !== child
      ) {
        continue;
      }
      this.aliases.delete(alias);
      this.emit({
        sessionId: BROWSER_SESSION_ID,
        method: 'Target.detachedFromTarget',
        params: { sessionId: alias, targetId: target.targetId },
      });
    }
  }

  private isTabCurrent(tab: TabSession): boolean {
    return !this.closed
      && tab.state.tab === tab
      && tab.state.status === 'attached'
      && this.tabs.get(tab.sessionId) === tab;
  }

  private assertAttachCurrent(state: ViewState, generation: number): void {
    if (
      this.closed
      || this.views.get(state.view) !== state
      || state.generation !== generation
      || state.status === 'removed'
      || state.view.webContents.isDestroyed()
    ) {
      throw state.error ?? new Error('标签页 attach 已取消');
    }
  }

  private handleDebuggerDetach(tab: TabSession, reason?: string): void {
    const state = tab.state;
    const wasPublished = this.tabs.get(tab.sessionId) === tab;
    const failure = new Error(`Electron debugger 已断开${reason ? `：${reason}` : ''}`);
    this.releaseTab(tab, { emitTargetDetach: wasPublished, detachDebugger: false });
    if (state.tab === tab) state.tab = null;
    if (this.views.get(tab.view) !== state || state.status === 'removed') return;
    state.generation += 1;
    state.status = 'failed';
    state.error = failure;
    this.rejectWaiters(state, failure);
  }

  private releaseTab(
    tab: TabSession,
    options: { emitTargetDetach: boolean; detachDebugger: boolean },
  ): void {
    const { view } = tab;
    const destroyed = view.webContents.isDestroyed();
    const debug = view.webContents.debugger;
    if (!destroyed) {
      if (tab.messageListener) debug.off('message', tab.messageListener as never);
      if (tab.detachListener) debug.off('detach', tab.detachListener as never);
    }
    tab.messageListener = null;
    tab.detachListener = null;
    tab.bufferedPageEvents.splice(0);
    // Invalidate every physical child before selecting service-worker
    // failovers, otherwise two duplicate children on this same closing tab can
    // spuriously promote one another during the loop.
    for (const child of tab.childSessions.values()) child.live = false;
    for (const child of tab.childSessions.values()) {
      if (child.serviceWorkerPhysical) {
        this.removeServiceWorkerPhysical(
          child.serviceWorkerPhysical,
          options.emitTargetDetach,
        );
      }
    }
    for (const [handle, stream] of this.pdfStreams) {
      if (stream.tab === tab) this.pdfStreams.delete(handle);
    }
    this.tabs.delete(tab.sessionId);
    for (const [guid, downloadTab] of this.downloadTabs) {
      if (downloadTab === tab) this.downloadTabs.delete(guid);
    }
    for (const [guid, route] of this.publicDownloads) {
      if (route.tab === tab) this.settlePublicDownload(guid);
    }
    for (let index = this.pendingNativeDownloads.length - 1; index >= 0; index -= 1) {
      if (this.pendingNativeDownloads[index]?.tab === tab) {
        this.pendingNativeDownloads.splice(index, 1);
      }
    }
    tab.childSessions.clear();

    for (const [alias, target] of [...this.aliases]) {
      if (target.tab !== tab) continue;
      this.aliases.delete(alias);
      if (options.emitTargetDetach) {
        this.emit({
          sessionId: BROWSER_SESSION_ID,
          method: 'Target.detachedFromTarget',
          params: { sessionId: alias, targetId: target.targetId },
        });
      }
    }

    if (options.emitTargetDetach) {
      this.emit({
        method: 'Target.detachedFromTarget',
        params: { sessionId: tab.sessionId, targetId: tab.targetInfo?.targetId },
      });
    }
    if (options.detachDebugger && !destroyed && debug.isAttached()) {
      try {
        debug.detach();
      } catch {
        // WebContents 销毁/并发 detach 是权威清理，状态机已不再路由到该 debugger。
      }
    }
  }

  private targetIdFromState(state: ViewState): string {
    if (state.status !== 'attached') return '';
    const targetId = state.tab?.targetInfo?.targetId;
    return typeof targetId === 'string' ? targetId : '';
  }

  private resolveWaiters(state: ViewState, targetId: string): void {
    for (const waiter of state.waiters) {
      if (waiter.timer) clearTimeout(waiter.timer);
      waiter.resolve(targetId);
    }
    state.waiters.clear();
  }

  private rejectWaiters(state: ViewState, error: Error): void {
    for (const waiter of state.waiters) {
      if (waiter.timer) clearTimeout(waiter.timer);
      waiter.reject(error);
    }
    state.waiters.clear();
  }

  private browserUserAgent(): string {
    for (const state of this.views.values()) {
      if (state.view.webContents.isDestroyed()) continue;
      const getUserAgent = state.view.webContents.getUserAgent;
      if (typeof getUserAgent !== 'function') continue;
      const userAgent = getUserAgent.call(state.view.webContents);
      if (typeof userAgent === 'string' && userAgent) {
        // Playwright 以 "Headless" 判定 headful；Electron 的 UI view 必然是 headful。
        return userAgent.replace(/HeadlessChrome\//g, 'Chrome/');
      }
    }
    const chromeVersion = process.versions.chrome ?? '0.0.0.0';
    return `Mozilla/5.0 (${fallbackPlatformToken()}) AppleWebKit/537.36 `
      + `(KHTML, like Gecko) Chrome/${chromeVersion} Safari/537.36`;
  }
}
