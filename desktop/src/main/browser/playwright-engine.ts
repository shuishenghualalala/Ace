/**
 * 每个 owner 一个 Playwright 引擎。
 *
 * ## 隔离靠拓扑，不靠自觉
 *
 * 一个 owner = 一个 `ElectronCdpTransport` = 一个 Playwright `Browser`。transport 只
 * 挂载该 owner 的 view，所以 Playwright 侧的 `Browser` 在**物理上**看不到别的账号的
 * 标签页。per-owner 隔离不依赖上层调用方守规矩。
 *
 * ## 生命周期
 *
 * 引擎是懒建的：第一次需要自动化能力时才连接。`registerTab` 在建立前调用会先记账，
 * 建立时一次性收编 —— 这样标签页可以先创建、后接管，不必强制顺序。
 */

import { AutomationHost } from './automation-host';
import { ElectronCdpTransport } from './electron-cdp-transport';
import { connectOverCdp, setFocusEmulation } from './playwright-compat';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';

import type { DownloadItem, WebContentsView } from 'electron';
import type {
  ChildSessionLifecycleHook,
  InputCommandLeaseHook,
  PageLifecycleHook,
} from './electron-cdp-transport';
import type { Browser, BrowserContext, Dialog, FileChooser, Page } from './playwright-compat';

export type { Page } from './playwright-compat';

export type ModalStateHook = (
  view: WebContentsView,
  kind: 'fileChooser',
) => void;

export class PlaywrightEngine {
  private readonly transport = new ElectronCdpTransport();
  private readonly host = new AutomationHost();
  /** targetId → Page，由 Playwright 侧的页面集合维护。 */
  private browser: Browser | null = null;
  private connecting: Promise<Browser> | null = null;
  private disposed = false;
  /** Core-visible guid artifacts mirroring native task downloads. */
  private artifactsDirectory = '';
  /** 已安装 dialog coordinator 的页面。 */
  private readonly preparedPages = new WeakSet<object>();
  /** 已安装 page discovery 的 context。 */
  private readonly observedContexts = new WeakSet<object>();
  /** 当前仍属于这个 owner 的 view；用于把 Playwright Page 精确映射回 Electron view。 */
  private readonly registeredViews = new Set<WebContentsView>();
  /** register/unregister 代次，阻止旧异步任务在 reattach 后覆盖新 Page。 */
  private readonly registrationGenerations = new WeakMap<object, number>();
  /** view 当前期望的焦点模拟状态；后台 AI=true，可见的人类接管=false。 */
  private readonly desiredFocus = new WeakMap<object, boolean>();
  /** view → Playwright Page；仅在 pageForView 成功解析后登记。 */
  private readonly pages = new WeakMap<object, Page>();
  /** Page 已脱离当前 debugger 生命周期；close 事件可能晚于 reattach 到达。 */
  private readonly stalePages = new WeakSet<object>();
  /** Page → view；Page.close 只能清理仍指向自己的映射。 */
  private readonly pageViews = new WeakMap<object, WebContentsView>();
  /** Page 上最后一次成功下发的状态。 */
  private readonly appliedFocus = new WeakMap<object, boolean>();
  /** 同一 view 的焦点模拟切换必须串行，避免快速 takeover/return 乱序。 */
  private readonly focusUpdates = new WeakMap<object, Promise<void>>();
  /** Page → targetId。标签页存续期内不变，问一次即可。 */
  private readonly targetIds = new WeakMap<object, string>();
  /**
   * A styled upload button may open a chooser without exposing its backing file input in the
   * current snapshot. Playwright intercepts that browser-native chooser and gives us the exact
   * ElementHandle, including OOPIF/shadow-DOM cases. Keep only the latest chooser per Page;
   * BrowserHost consumes it exactly once through the separate file_upload command.
   */
  private readonly pendingFileChoosers = new WeakMap<object, FileChooser>();
  /** Additional chooser openings before the first one was consumed. */
  private readonly pendingFileChooserCollisions = new WeakMap<object, number>();
  private readonly fileChooserListeners = new WeakMap<
    object,
    (chooser: FileChooser) => void
  >();
  /** FIFO is required for alert→confirm→prompt chains opened by one action. */
  private readonly pendingDialogs = new WeakMap<object, Dialog[]>();
  private readonly dialogWaiters = new WeakMap<object, Array<(dialog: Dialog) => void>>();
  /** BrowserHost session coordinator learns about Playwright-only modal states. */
  private modalStateHook: ModalStateHook | null = null;

  /**
   * 接入 BrowserHost 的精确输入租约。
   *
   * hook 只包住 transport 最终发出的 `Input.*`，不会把 Playwright 最长数秒的
   * actionability 等待误算成 automationDepth。
   */
  setInputCommandLeaseHook(hook: InputCommandLeaseHook | null): void {
    this.transport.setInputCommandLeaseHook(hook);
  }

  /**
   * Run host-owned setup/cleanup at the real Chromium child-session boundary.
   *
   * The transport holds `Target.attachedToTarget` behind this hook, which lets
   * recorder bindings reach an OOPIF before Playwright initializes and resumes
   * that target. Keeping the hook here avoids exposing the transport itself.
   */
  setChildSessionLifecycleHook(hook: ChildSessionLifecycleHook | null): void {
    this.transport.setChildSessionLifecycleHook(hook);
  }

  /** Install BrowserHost's owner-aware real WebContentsView lifecycle. */
  setPageLifecycleHook(hook: PageLifecycleHook | null): void {
    this.transport.setPageLifecycleHook(hook);
  }

  /** Bind Electron's native item to Playwright's separately-emitted download guid. */
  registerNativeDownload(view: WebContentsView, item: DownloadItem): void {
    this.transport.registerNativeDownload(view, item);
  }

  /**
   * Run code against the unmodified public Page while retaining the exact
   * source view for root Target.createTarget/closeTarget commands.
   */
  withPageLifecycleSource<T>(
    view: WebContentsView,
    deadlineAt: number,
    operation: () => Promise<T>,
  ): Promise<T> {
    this.registrationGeneration(view);
    return this.transport.runWithPageLifecycleSource(view, deadlineAt, operation);
  }

  setModalStateHook(hook: ModalStateHook | null): void {
    this.modalStateHook = hook;
  }

  /**
   * 切换一个 view 的焦点模拟。
   *
   * AI 后台自动化需要 enabled=true 才有稳定的 rAF；人类接管时必须关闭，否则
   * Chromium 会把失焦窗口伪装成始终聚焦，页面的 blur/focus 与输入法行为会偏离
   * 用户实际看到的窗口状态。尚未建立 Page 时只记录期望值，首次接管时再下发。
   */
  async setAutomationMode(view: WebContentsView, enabled: boolean): Promise<void> {
    if (this.disposed) return;
    const hadPrevious = this.desiredFocus.has(view);
    const previous = this.desiredFocus.get(view) ?? true;
    this.desiredFocus.set(view, enabled);
    // Set this before moving/exposing the view. Dialog ownership must already
    // be deterministic when the first human or automation action runs.
    this.transport.setDialogForwarding(view, enabled);
    const page = this.pages.get(view);
    if (!page) return;
    if (!this.isPageBindingCurrent(view, page)) {
      this.retirePageBinding(view, page);
      return;
    }
    this.updateFileChooserCapture(page, enabled);
    try {
      await this.queueFocusUpdate(view, page);
    } catch (error) {
      if (hadPrevious) this.desiredFocus.set(view, previous);
      else this.desiredFocus.delete(view);
      this.transport.setDialogForwarding(view, previous);
      this.updateFileChooserCapture(page, previous);
      // setFocusEmulation may have succeeded before a later transport error.
      // Re-apply the previous state best-effort so the visible ownership and
      // the transport routing never intentionally diverge.
      await this.queueFocusUpdate(view, page).catch(() => undefined);
      throw error;
    }
  }

  /**
   * 登记一个标签页。
   *
   * 会把 view 挂到隐藏的自动化宿主窗口上（§ automation-host 的条件 2、3）。
   * 调用方若之前把 view 挂在面板窗口上，需先摘除。
   */
  registerTab(
    view: WebContentsView,
    options: { opener?: WebContentsView | null | undefined } = {},
  ): void {
    if (this.disposed) return;
    if (view.webContents.isDestroyed()) return;
    if (!this.desiredFocus.has(view)) this.desiredFocus.set(view, true);
    this.transport.setDialogForwarding(
      view,
      this.desiredFocus.get(view) ?? true,
    );
    if (!this.registeredViews.has(view)) {
      this.registrationGenerations.set(
        view,
        (this.registrationGenerations.get(view) ?? 0) + 1,
      );
      this.registeredViews.add(view);
    }
    this.host.mount(view);
    this.transport.addView(view, {
      ...options,
      // The Engine owns the Page↔view binding and installs modal/filechooser
      // listeners before releasing early events. A bare transport client has
      // no such handshake and therefore streams page events immediately.
      deferPageEventsUntilReady: true,
    });
  }

  /** 注销一个标签页（关闭，或移交面板）。 */
  unregisterTab(view: WebContentsView, options: { keepMounted?: boolean } = {}): void {
    this.registeredViews.delete(view);
    this.registrationGenerations.set(
      view,
      (this.registrationGenerations.get(view) ?? 0) + 1,
    );
    const page = this.pages.get(view);
    if (page) this.retirePageBinding(view, page);
    this.pages.delete(view);
    this.transport.removeView(view);
    if (!options.keepMounted) this.host.unmount(view);
  }

  /**
   * 把 view 交给面板窗口显示。
   *
   * 只摘除宿主挂载，**不动 transport** —— 实测跨窗口移动后 debugger 保持 attached、
   * targetId 不变、页面状态不丢，所以 Playwright 侧无需重连。
   */
  releaseToPanel(view: WebContentsView): void {
    this.host.unmount(view);
  }

  /** 用户关闭面板，把 view 收回后台宿主。 */
  reclaimFromPanel(view: WebContentsView): void {
    if (this.disposed) return;
    this.host.mount(view);
  }

  async context(): Promise<BrowserContext> {
    const browser = await this.ensureBrowser();
    const [context] = browser.contexts();
    if (!context) throw new Error('Playwright 未发现默认 BrowserContext');
    return context;
  }

  /** True when the most recent action opened an intercepted native file chooser. */
  hasPendingFileChooser(view: WebContentsView): boolean {
    const page = this.pages.get(view);
    if (!page) return false;
    if (!this.isPageBindingCurrent(view, page)) {
      this.retirePageBinding(view, page);
      return false;
    }
    return this.pendingFileChoosers.has(page);
  }

  pendingFileChooserCount(view: WebContentsView): number {
    const page = this.pages.get(view);
    if (!page) return 0;
    if (!this.isPageBindingCurrent(view, page)) {
      this.retirePageBinding(view, page);
      return 0;
    }
    if (!this.pendingFileChoosers.has(page)) return 0;
    return 1 + (this.pendingFileChooserCollisions.get(page) ?? 0);
  }

  /**
   * Consume a chooser exactly once.
   *
   * Taking before setFiles mirrors upstream Playwright MCP's modal-state semantics: invalid
   * local files must not leave an old chooser available for an accidental retry against a
   * page that may already have moved on.
   */
  takePendingFileChooser(view: WebContentsView): FileChooser | null {
    const page = this.pages.get(view);
    if (!page) return null;
    if (!this.isPageBindingCurrent(view, page)) {
      this.retirePageBinding(view, page);
      return null;
    }
    const chooser = this.pendingFileChoosers.get(page) ?? null;
    if (chooser) {
      this.pendingFileChoosers.delete(page);
      this.pendingFileChooserCollisions.delete(page);
    }
    return chooser;
  }

  /**
   * 取某个 view 对应的 Page，并确保它已完成一次性准备。
   *
   * 走 view 而不是 Crew 的 `tab.targetId`：后者是进程内 UUID，Playwright 不认识。
   * transport 持有 Chromium targetId，是两套身份之间唯一的桥。
   */
  async pageForView(view: WebContentsView, timeoutMs = 0): Promise<Page> {
    const generation = this.registrationGeneration(view);
    while (true) {
      const boundPage = this.pages.get(view);
      if (boundPage && this.isPageBindingCurrent(view, boundPage)) {
        this.assertRegistration(view, generation);
        return boundPage;
      }
      if (boundPage) this.retirePageBinding(view, boundPage);

      // 顺序不能反：Target.setAutoAttach 是 connectOverCDP 握手的一部分，targetId 只有
      // 握手开始后才会产生。旧实现先读 targetId，导致第一次调用永远无法自启动。
      const browser = await this.ensureBrowser();
      this.assertRegistration(view, generation);
      const nextTargetId = await this.transport.waitForViewTarget(view, timeoutMs);
      this.assertRegistration(view, generation);
      const [context] = browser.contexts();
      if (!context) throw new Error('Playwright 未发现默认 BrowserContext');
      const page = await this.waitForPage(context, nextTargetId, timeoutMs);
      this.assertRegistration(view, generation);
      if (await this.bindPage(view, page, generation)) return page;
    }
  }

  /**
   * Wait only for transport publication, without waiting for the client Page.
   *
   * BrowserHost uses this inside Target.createTarget itself: waiting for the
   * public Page there would deadlock because playwright-core resolves newPage()
   * only after the root command response.
   */
  async waitForViewTarget(view: WebContentsView, timeoutMs = 0): Promise<string> {
    const generation = this.registrationGeneration(view);
    const targetId = await this.transport.waitForViewTarget(view, timeoutMs);
    this.assertRegistration(view, generation);
    return targetId;
  }

  async dispose(): Promise<void> {
    if (this.disposed) return;
    this.disposed = true;
    this.transport.close();
    this.browser = null;
    this.connecting = null;
    this.registeredViews.clear();
    this.host.dispose();
    const artifactsDirectory = this.artifactsDirectory;
    this.artifactsDirectory = '';
    if (artifactsDirectory) {
      await rm(artifactsDirectory, { recursive: true, force: true }).catch(() => undefined);
    }
  }

  // ── 内部 ─────────────────────────────────────────────────────────────

  private async ensureBrowser(): Promise<Browser> {
    if (this.disposed) throw new Error('Playwright 引擎已停止');
    if (this.browser) return this.browser;
    // 并发调用共享同一次连接，避免建立出两个 Browser。
    this.connecting ??= this.connectBrowser();
    return await this.connecting;
  }

  private async connectBrowser(): Promise<Browser> {
    let artifactsDirectory = '';
    try {
      artifactsDirectory = await mkdtemp(path.join(tmpdir(), 'crew-pw-artifacts-'));
      if (this.disposed) throw new Error('Playwright 引擎已停止');
      this.artifactsDirectory = artifactsDirectory;
      this.transport.setArtifactsDirectory(artifactsDirectory);
      const browser = await connectOverCdp(this.transport, artifactsDirectory);
      if (this.disposed) throw new Error('Playwright 引擎已停止');
      this.observeBrowser(browser);
      this.browser = browser;
      return browser;
    } catch (error) {
      this.connecting = null;
      if (this.artifactsDirectory === artifactsDirectory) this.artifactsDirectory = '';
      this.transport.setArtifactsDirectory('');
      if (artifactsDirectory) {
        await rm(artifactsDirectory, { recursive: true, force: true }).catch(() => undefined);
      }
      throw error;
    }
  }

  /**
   * 取 Page 的 targetId。
   *
   * Playwright 的客户端 `Page` 不暴露 targetId，只能问一次 CDP。用 URL 或
   * `pages()` 的顺序去猜都会在"多个标签页停在同一 URL"时静默错配 —— 那是会把动作
   * 打到错误标签页上的错配，宁可多一次往返。
   *
   * targetId 在标签页存续期内不变（跨窗口移动也不变，已实测），所以缓存住。
   */
  private async targetIdOf(context: BrowserContext, page: Page): Promise<string> {
    const cached = this.targetIds.get(page);
    if (cached) return cached;
    const cdp = await context.newCDPSession(page);
    try {
      const info = (await cdp.send('Target.getTargetInfo')) as {
        targetInfo?: { targetId?: unknown };
      };
      const targetId =
        typeof info?.targetInfo?.targetId === 'string' ? info.targetInfo.targetId : '';
      if (targetId) this.targetIds.set(page, targetId);
      return targetId;
    } finally {
      // newCDPSession 是真实资源；不 detach 会让 transport 的 alias 表与 Playwright
      // connection 长期增长，最终把页面事件扇出给早已无人消费的会话。
      await cdp.detach().catch(() => undefined);
    }
  }

  /**
   * Install modal listeners as soon as Playwright surfaces a Page, rather than
   * waiting for the first snapshot/action. A popup or human interaction can
   * otherwise open a dialog in that gap and Playwright will auto-dismiss it.
   */
  private observeBrowser(browser: Browser): void {
    for (const context of browser.contexts()) this.observeContext(context);
  }

  private observeContext(context: BrowserContext): void {
    if (this.observedContexts.has(context)) return;
    this.observedContexts.add(context);
    const observe = (page: Page): void => {
      this.preparePageEvents(page);
      void this.bindDiscoveredPage(context, page).catch(() => undefined);
    };
    // Subscribe first, then enumerate: a page created between those operations
    // is observed by one or both paths, and preparePageEvents is idempotent.
    context.on('page', observe);
    for (const page of context.pages()) observe(page);
  }

  private async bindDiscoveredPage(context: BrowserContext, page: Page): Promise<void> {
    if (this.stalePages.has(page) || page.isClosed()) return;
    const targetId = await this.targetIdOf(context, page);
    if (!targetId || this.disposed || this.stalePages.has(page) || page.isClosed()) return;
    const view = [...this.registeredViews].find(
      (candidate) => this.transport.targetIdForView(candidate) === targetId,
    );
    if (!view) return;
    const generation = this.registrationGeneration(view);
    await this.bindPage(view, page, generation);
  }

  /**
   * late tab 的 `Target.attachedToTarget` 已发布，不代表客户端 Page 已完成初始化。
   * 按 targetId 等待，禁止用 pages() 顺序或 URL 猜测。
   */
  private async waitForPage(
    context: BrowserContext,
    targetId: string,
    timeoutMs = 0,
  ): Promise<Page> {
    const deadline = timeoutMs > 0 ? Date.now() + timeoutMs : Number.POSITIVE_INFINITY;
    while (true) {
      for (const page of context.pages()) {
        if (this.stalePages.has(page) || page.isClosed()) continue;
        try {
          if (
            !this.stalePages.has(page)
            && !page.isClosed()
            && (await this.targetIdOf(context, page)) === targetId
          ) {
            return page;
          }
        } catch (error) {
          // An unrelated popup can close between context.pages() and
          // newCDPSession(page). It must not make lookup of this view fail.
          if (!page.isClosed()) throw error;
        }
      }
      const remaining = deadline - Date.now();
      if (remaining <= 0) throw new Error(`Playwright 未发现 target: ${targetId}`);
      await this.waitForPageEventOrTick(context, Math.min(remaining, 100));
    }
  }

  private async waitForPageEventOrTick(context: BrowserContext, timeoutMs: number): Promise<void> {
    await new Promise<void>((resolve) => {
      let settled = false;
      const finish = (): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        context.off('page', finish);
        resolve();
      };
      const timer = setTimeout(finish, timeoutMs);
      context.once('page', finish);
    });
  }

  /**
   * 首次使用某个页面前的一次性准备。
   *
   * 除了焦点模拟，还必须**占住并保存 dialog 对象**：Playwright 在没有监听器时会
   * 自动 dismiss。更重要的是，后续必须通过同一个公开 Dialog.accept()/dismiss()
   * 关闭；直接发原始 CDP 会绕过 core 的 DialogManager 清理并污染后续动作。
   */
  private preparePageEvents(page: Page): void {
    if (!this.preparedPages.has(page)) {
      page.on('dialog', (dialog) => {
        const waiters = this.dialogWaiters.get(page);
        const waiter = waiters?.shift();
        if (waiter) {
          if (!waiters?.length) this.dialogWaiters.delete(page);
          waiter(dialog);
          return;
        }
        const queue = this.pendingDialogs.get(page) ?? [];
        queue.push(dialog);
        this.pendingDialogs.set(page, queue);
      });
      page.on('close', () => {
        this.stalePages.add(page);
        this.pendingFileChoosers.delete(page);
        this.pendingFileChooserCollisions.delete(page);
        this.fileChooserListeners.delete(page);
        this.pendingDialogs.delete(page);
        this.dialogWaiters.delete(page);
        const view = this.pageViews.get(page);
        this.pageViews.delete(page);
        this.appliedFocus.delete(page);
        this.targetIds.delete(page);
        // An old Page may close after the same view has already reattached to
        // a new Page. Never let that late close erase the new mapping.
        if (view && this.pages.get(view) === page) this.pages.delete(view);
      });
      this.preparedPages.add(page);
    }
  }

  private async nextDialog(page: Page, timeoutMs: number): Promise<Dialog> {
    const queue = this.pendingDialogs.get(page);
    const pending = queue?.shift();
    if (pending) {
      if (!queue?.length) this.pendingDialogs.delete(page);
      return pending;
    }
    return await new Promise<Dialog>((resolve, reject) => {
      let settled = false;
      const finish = (dialog: Dialog): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(dialog);
      };
      const waiters = this.dialogWaiters.get(page) ?? [];
      waiters.push(finish);
      this.dialogWaiters.set(page, waiters);
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        const current = this.dialogWaiters.get(page);
        if (current) {
          const index = current.indexOf(finish);
          if (index >= 0) current.splice(index, 1);
          if (!current.length) this.dialogWaiters.delete(page);
        }
        reject(new Error('Playwright 未收到对应的 JavaScript Dialog 对象'));
      }, timeoutMs);
    });
  }

  /**
   * Handle a native modal through Playwright's public Dialog API.
   *
   * Raw Page.handleJavaScriptDialog would close Chromium's modal without
   * notifying playwright-core's DialogManager, leaving its `_openedDialogs`
   * permanently populated and poisoning every later evaluation.
   */
  async handleDialog(
    view: WebContentsView,
    options: {
      accept: boolean;
      expectedType?: string;
      promptText?: string;
      timeoutMs?: number;
    },
  ): Promise<{
    type: string;
    message: string;
    defaultValue: string;
    matched: boolean;
  }> {
    const timeoutMs = options.timeoutMs ?? 5_000;
    // A popup may execute alert() synchronously in document.write before
    // playwright-core can finish constructing its Page. The transport has
    // already observed and buffered that opening, so close it there while core
    // still has no DialogManager state. Normal published dialogs always stay
    // on Playwright's public API path below.
    await this.ensureBrowser();
    await this.transport.waitForViewTarget(view, timeoutMs);
    const unpublished = await this.transport.handleUnpublishedDialog(view, options);
    if (unpublished) return unpublished;

    const page = await this.pageForView(view, timeoutMs);
    const dialog = await this.nextDialog(page, timeoutMs);
    const observed = {
      type: dialog.type(),
      message: dialog.message(),
      defaultValue: dialog.defaultValue(),
      matched: !options.expectedType || dialog.type() === options.expectedType,
    };
    // Inspect before applying the recorded choice. A mismatched confirm or
    // beforeunload must not be accidentally accepted just because the trace
    // expected an alert at this position.
    if (!observed.matched) await dialog.dismiss();
    else if (options.accept) await dialog.accept(options.promptText);
    else await dialog.dismiss();
    return observed;
  }

  private async bindPage(
    view: WebContentsView,
    page: Page,
    generation: number,
  ): Promise<boolean> {
    this.assertRegistration(view, generation);
    if (this.stalePages.has(page) || page.isClosed()) return false;
    this.preparePageEvents(page);
    const previous = this.pages.get(view);
    if (previous && previous !== page) {
      this.retirePageBinding(view, previous);
    }
    this.pages.set(view, page);
    this.pageViews.set(page, view);
    this.updateFileChooserCapture(page, this.desiredFocus.get(view) ?? true);
    // Page listeners and identity mapping are now live. Release modal events
    // that arrived in the debugger-attach → Page-construction gap.
    this.transport.markPageEventsReady(view);
    await this.queueFocusUpdate(view, page);
    this.assertRegistration(view, generation);
    return true;
  }

  private isPageBindingCurrent(view: WebContentsView, page: Page): boolean {
    if (this.stalePages.has(page) || page.isClosed()) return false;
    const targetId = this.transport.targetIdForView(view);
    const pageTargetId = this.targetIds.get(page);
    return Boolean(targetId) && (!pageTargetId || pageTargetId === targetId);
  }

  /**
   * Retire every Engine-owned association for a Page without changing the
   * view registration generation. A debugger detach is a new Page lifecycle,
   * not an unregister/register operation; desired focus and ownership remain
   * on the view and are reapplied by bindPage to its replacement.
   */
  private retirePageBinding(view: WebContentsView, page: Page): void {
    this.stalePages.add(page);
    this.pendingFileChoosers.delete(page);
    this.pendingFileChooserCollisions.delete(page);
    const chooserListener = this.fileChooserListeners.get(page);
    if (chooserListener) page.off('filechooser', chooserListener);
    this.fileChooserListeners.delete(page);
    this.pendingDialogs.delete(page);
    this.dialogWaiters.delete(page);
    this.pageViews.delete(page);
    this.appliedFocus.delete(page);
    this.targetIds.delete(page);
    if (this.pages.get(view) === page) this.pages.delete(view);
  }

  /**
   * FileChooser interception is automation-only. Leaving a Playwright listener installed while
   * the user owns a visible tab suppresses the native OS chooser, which would make human
   * recording of uploads impossible.
   */
  private updateFileChooserCapture(
    page: Page,
    enabled: boolean,
  ): void {
    const existing = this.fileChooserListeners.get(page);
    if (enabled) {
      if (existing) return;
      const listener = (chooser: FileChooser): void => {
        // Key by Page, not view: a late chooser/close from a detached Page
        // must never overwrite or clear the chooser belonging to its replacement.
        if (this.pendingFileChoosers.has(page)) {
          this.pendingFileChooserCollisions.set(
            page,
            (this.pendingFileChooserCollisions.get(page) ?? 0) + 1,
          );
        } else {
          this.pendingFileChoosers.set(page, chooser);
        }
        const view = this.pageViews.get(page);
        if (view) this.modalStateHook?.(view, 'fileChooser');
      };
      this.fileChooserListeners.set(page, listener);
      page.on('filechooser', listener);
      return;
    }
    this.pendingFileChoosers.delete(page);
    this.pendingFileChooserCollisions.delete(page);
    if (!existing) return;
    page.off('filechooser', existing);
    this.fileChooserListeners.delete(page);
  }

  private async queueFocusUpdate(view: WebContentsView, page: Page): Promise<void> {
    const previous = this.focusUpdates.get(view) ?? Promise.resolve();
    const update = previous.catch(() => undefined).then(async () => {
      if (
        this.disposed
        || !this.registeredViews.has(view)
        || this.pages.get(view) !== page
      ) {
        return;
      }
      // 在真正发送前读取最新 desired 值；快速连点接管/交还不会下发过时状态。
      const desired = this.desiredFocus.get(view) ?? true;
      if (this.appliedFocus.get(page) === desired) return;
      await setFocusEmulation(page.context(), page, desired);
      this.appliedFocus.set(page, desired);
    });
    this.focusUpdates.set(view, update);
    try {
      await update;
    } finally {
      if (this.focusUpdates.get(view) === update) this.focusUpdates.delete(view);
    }
  }

  private registrationGeneration(view: WebContentsView): number {
    const generation = this.registrationGenerations.get(view);
    if (!this.registeredViews.has(view) || generation === undefined) {
      throw new Error('该标签页未登记或已注销');
    }
    return generation;
  }

  private assertRegistration(view: WebContentsView, generation: number): void {
    if (
      this.disposed
      || !this.registeredViews.has(view)
      || this.registrationGenerations.get(view) !== generation
      || view.webContents.isDestroyed()
    ) {
      throw new Error('标签页生命周期已变化，请重新取得 Playwright Page');
    }
  }
}
