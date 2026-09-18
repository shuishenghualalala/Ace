/** Trusted browser chrome around a sandboxed Electron WebContentsView. */

import { backendApi, type BrowserPageState } from '../backend-client';
import { notify, state } from '../state';
import { createBrowserInspector, replaceBrowserTabs } from './browser-inspector';

type BrowserEvent =
  | { type: 'state'; state: BrowserPageState }
  | { type: 'debug'; channel: 'console' | 'network'; record: Record<string, unknown> }
  | { type: 'debug_clear' }
  | { type: 'action'; description: string }
  | {
      type: 'download';
      download: {
        id?: string;
        name: string;
        path: string;
        created_at: number;
        state?: string;
        received_bytes?: number;
        total_bytes?: number;
        completed_at?: number;
        error?: string;
      };
    }
  | { type: 'command_error' | 'error'; error: string };

let pageState: BrowserPageState | null = null;
/** Wiki 页面没有 activeSessionId，允许固定工作区显式绑定一个会话。 */
let sessionOverride: string | null = null;
let panelRoot: ParentNode | null = typeof document !== 'undefined' ? document : null;
let socketSession = '';
let socketConnectingSession = '';
let socketOpen = false;
let browserListenerBound = false;
const browserListenerDisposers: Array<() => void> = [];
let streamStateRevision = 0;
let resizeObserver: ResizeObserver | null = null;
let layoutMutationObserver: MutationObserver | null = null;
let layoutListenersBound = false;
let layoutFrame = 0;
let nativeLayoutRequestGeneration = 0;
let nativeViewMounted = false;
let lastNativeLayoutKey = '';
let pendingNativeLayoutKey = '';
let humanControlPromise: Promise<boolean> | null = null;
/** 待用户确认的页面接管请求。非空 = 确认条可见；确认/忽略/AI 后续动作时清空。 */
let takeoverPromptTab = '';
/** 主框架加载失败（宿主 did-fail-load，已排除 ERR_ABORTED）。非空时收起原生视图、显示错误遮罩。 */
let loadFailure: { tabLabel: string; url: string; description: string } | null = null;

function currentSession(): string { return sessionOverride || state.activeSessionId || ''; }

function panelQuery<T extends Element>(selector: string): T | null {
  return panelRoot?.querySelector<T>(selector) ?? null;
}

function panelQueryAll<T extends Element>(selector: string): NodeListOf<T> | T[] {
  return panelRoot?.querySelectorAll<T>(selector) ?? [];
}

function defaultState(): BrowserPageState {
  return {
    owner_hash: '', session_hash: '', tab_id: '', tab_label: '', url: '', title: '', generation: 0,
    mode: 'ai', running: false, last_action: '', last_error: '', screenshot_id: '',
    viewport_width: 0, viewport_height: 0, can_go_back: false, can_go_forward: false,
    tabs: [], downloads: [],
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function isBrowserPageState(value: unknown): value is BrowserPageState {
  if (!isRecord(value)) return false;
  const strings = [
    value.owner_hash, value.session_hash, value.tab_id, value.tab_label, value.url, value.title,
    value.last_action, value.last_error, value.screenshot_id,
  ];
  if (!strings.every((part) => typeof part === 'string')) return false;
  if (value.mode !== 'ai' && value.mode !== 'human' && value.mode !== 'paused') return false;
  if (typeof value.running !== 'boolean') return false;
  if (typeof value.can_go_back !== 'boolean' || typeof value.can_go_forward !== 'boolean') return false;
  if (![value.generation, value.viewport_width, value.viewport_height].every(Number.isSafeInteger)) return false;
  if (!Array.isArray(value.tabs) || value.tabs.length > 50) return false;
  if (!value.tabs.every((tab) => isRecord(tab)
    && [tab.id, tab.label, tab.url, tab.title].every((part) => typeof part === 'string'))) return false;
  if (!Array.isArray(value.downloads)) return false;
  return value.downloads.every((download) => isRecord(download)
    && typeof download.name === 'string'
    && typeof download.path === 'string'
    && Number.isFinite(download.created_at));
}

function isBlankBrowserUrl(value: string): boolean {
  const normalized = value.trim().toLowerCase();
  return normalized === '' || normalized === 'about:blank';
}

function displayUrl(value: string): string {
  return isBlankBrowserUrl(value) ? '' : value;
}

function detachNativeView(force = false): void {
  if (!force && !nativeViewMounted && !lastNativeLayoutKey && !pendingNativeLayoutKey) return;
  nativeLayoutRequestGeneration += 1;
  nativeViewMounted = false;
  lastNativeLayoutKey = '';
  pendingNativeLayoutKey = '';
  const empty = panelQuery<HTMLElement>('[data-browser-empty]');
  if (empty && pageState?.tab_id && !loadFailureVisible()) empty.hidden = false;
  void window.Crew?.browserViewHide?.();
}

/** Hide remote content before the trusted inspector switches away from Browser. */
export function hideBrowserPanelView(): void {
  detachNativeView(true);
  resizeObserver?.disconnect();
  resizeObserver = null;
  layoutMutationObserver?.disconnect();
  layoutMutationObserver = null;
  if (layoutListenersBound) {
    window.removeEventListener('resize', scheduleNativeViewLayout);
    window.removeEventListener('scroll', scheduleNativeViewLayout, true);
    document.removeEventListener('visibilitychange', handleVisibilityChange);
    layoutListenersBound = false;
  }
  if (layoutFrame) window.cancelAnimationFrame(layoutFrame);
  layoutFrame = 0;
}

// Keep in sync with the main-process validateBrowserSessionId regex
// (desktop/src/main/index.ts) to avoid sending ids that would throw
// IPC_ARG_VALIDATION_FAILED before the socket can open.
const BROWSER_SESSION_ID_RE = /^[A-Za-z0-9_.:-]{1,200}$/;

function isValidBrowserSessionId(value: string): boolean {
  return BROWSER_SESSION_ID_RE.test(value);
}

export function syncBrowserPanelSession(sessionId: string | null): void {
  const previous = currentSession();
  const next = sessionOverride || sessionId || '';
  switchBrowserPanelSession(next, previous, false);
}

/**
 * Bind the browser surface to an explicit session (used by Wiki Agent).
 * Browser state is session-scoped; this layer intentionally does not know KB ids.
 */
export function setBrowserPanelSession(sessionId: string | null): void {
  const previous = currentSession();
  sessionOverride = sessionId?.trim() || null;
  const next = sessionOverride || sessionId || state.activeSessionId || '';
  switchBrowserPanelSession(next, previous, true);
}

function switchBrowserPanelSession(
  next: string,
  previous: string,
  releaseHumanControl: boolean,
): void {
  if (next && socketSession === next) return;
  if (releaseHumanControl && previous && pageState?.mode === 'human') {
    void releaseHumanBrowserControl(previous);
  }
  hideBrowserPanelView();
  socketSession = '';
  socketConnectingSession = '';
  socketOpen = false;
  streamStateRevision += 1;
  pageState = null;
  takeoverPromptTab = '';
  loadFailure = null;
  // Remove stale tab chrome/status immediately. Waiting for the next session's
  // REST/WS state leaves a window where old tabLabel is paired with new sessionId.
  patchChrome();
  void window.Crew?.browserWsClose?.();
  if (!next) {
    for (const dispose of browserListenerDisposers.splice(0)) dispose();
    browserListenerBound = false;
  }
  // Keep the tiny authenticated state channel alive for the active session
  // even while the workbench is closed. AI-started browser work can then light
  // the toolbar status without mounting or exposing the remote page view.
  // Skip ids that the main process would reject (e.g. agent sessions containing '@').
  if (next && isValidBrowserSessionId(next)) void ensureConnection(next);
}

async function ensureConnection(sessionId: string): Promise<void> {
  bindGlobalListener();
  if (socketSession === sessionId && (socketOpen || socketConnectingSession === sessionId)) return;
  socketSession = sessionId;
  socketConnectingSession = sessionId;
  socketOpen = false;
  const result = await window.Crew?.browserWsConnect?.(sessionId);
  if (socketSession !== sessionId) return;
  socketConnectingSession = '';
  if (!result?.ok) updateStatus(result?.error || '浏览器状态连接失败');
}

function bindGlobalListener(): void {
  if (browserListenerBound) return;
  if (!window.Crew?.onBrowserWsEvent) return;
  browserListenerBound = true;
  const disposeSocketListener = window.Crew.onBrowserWsEvent((event) => {
    if (event.sessionId && event.sessionId !== currentSession()) return;
    if (event.type === 'open') {
      socketConnectingSession = '';
      socketOpen = true;
      updateStatus('');
      return;
    }
    if (event.type === 'close' || event.type === 'error') {
      socketConnectingSession = '';
      socketOpen = false;
      const terminalClose = event.type === 'close'
        && new Set([1000, 4401, 4403, 4404]).has(Number(event.code));
      if (Number(event.code) === 4404) updateStatus('');
      else updateStatus(event.error || '浏览器状态连接已断开');
      if (
        event.type === 'close'
        && !terminalClose
        && socketSession === currentSession()
      ) {
        window.setTimeout(() => {
          const sessionId = currentSession();
          if (
            sessionId
            && socketSession === sessionId
            && !socketOpen
          ) void ensureConnection(sessionId);
        }, 500);
      }
      return;
    }
    if (event.type !== 'message' || !event.data) return;
    try { applyEvent(JSON.parse(event.data) as BrowserEvent); } catch { /* authenticated local channel */ }
  });
  if (typeof disposeSocketListener === 'function') browserListenerDisposers.push(disposeSocketListener);
  const disposeLayoutListener = window.Crew?.onBrowserViewLayoutInvalidated?.(invalidateNativeViewLayout);
  if (typeof disposeLayoutListener === 'function') browserListenerDisposers.push(disposeLayoutListener);
  const disposeNavigationListener = window.Crew?.onBrowserViewNavigationChanged?.((event) => {
    if (event.tabLabel !== pageState?.tab_label) return;
    void refreshPanelNavigation(event.tabLabel);
  });
  if (typeof disposeNavigationListener === 'function') browserListenerDisposers.push(disposeNavigationListener);
  const disposeInteractionListener = window.Crew?.onBrowserViewInteractionRequested?.((event) => {
    if (event.tabLabel !== pageState?.tab_label || pageState.mode === 'human') return;
    // 页面内的真实输入不再静默接管：白屏/加载失败页面上的误触也会走到这里。
    // 先挂一条非模态确认条，点「接管」才走 takeover；点「忽略」或 AI 后续
    // 动作成功都会把它冲掉。
    takeoverPromptTab = event.tabLabel;
    renderTakeoverPrompt();
  });
  if (typeof disposeInteractionListener === 'function') browserListenerDisposers.push(disposeInteractionListener);
  const disposeLoadFailedListener = window.Crew?.onBrowserViewLoadFailed?.((event) => {
    if (event.tabLabel !== pageState?.tab_label) return;
    loadFailure = {
      tabLabel: event.tabLabel,
      url: event.url,
      description: event.errorDescription,
    };
    patchChrome();
  });
  if (typeof disposeLoadFailedListener === 'function') browserListenerDisposers.push(disposeLoadFailedListener);
}

/** 确认条只挂在 chrome 区：stage 上的任何 HTML 浮层都会被原生 WebContentsView 盖住。 */
function renderTakeoverPrompt(): void {
  const banner = panelQuery<HTMLElement>('[data-browser-takeover]');
  if (!banner) return;
  banner.hidden = !(
    takeoverPromptTab
    && pageState?.tab_label === takeoverPromptTab
    && pageState.mode !== 'human'
  );
}

function loadFailureVisible(): boolean {
  return Boolean(loadFailure && loadFailure.tabLabel === pageState?.tab_label);
}

function renderLoadFailure(): void {
  const overlay = panelQuery<HTMLElement>('[data-browser-load-error]');
  if (!overlay) return;
  const visible = loadFailureVisible();
  overlay.hidden = !visible;
  if (!visible || !loadFailure) return;
  const url = overlay.querySelector<HTMLElement>('[data-browser-load-error-url]');
  if (url) url.textContent = loadFailure.url;
  const description = overlay.querySelector<HTMLElement>('[data-browser-load-error-description]');
  if (description) description.textContent = loadFailure.description;}

async function refreshPanelNavigation(tabLabel: string): Promise<void> {
  const sessionId = currentSession();
  if (!sessionId || !tabLabel || pageState?.tab_label !== tabLabel) return;
  // did-navigate 已提交 = 新页面加载成功，旧的加载失败遮罩必须让位。
  if (loadFailure?.tabLabel === tabLabel) loadFailure = null;
  const result = await window.Crew?.browserViewGetNavigation?.({ sessionId, tabLabel });
  if (
    !result?.ok
    || !result.navigation
    || currentSession() !== sessionId
    || pageState?.tab_label !== tabLabel
  ) return;
  const navigation = result.navigation;
  if (
    typeof navigation.url !== 'string'
    || typeof navigation.title !== 'string'
    || typeof navigation.can_go_back !== 'boolean'
    || typeof navigation.can_go_forward !== 'boolean'
  ) return;
  pageState = {
    ...pageState,
    url: navigation.url,
    title: navigation.title,
    can_go_back: navigation.can_go_back,
    can_go_forward: navigation.can_go_forward,
    tabs: pageState.tabs.map((tab) => tab.label === tabLabel
      ? { ...tab, url: navigation.url, title: navigation.title }
      : tab),
  };
  patchChrome();
}

function invalidateNativeViewLayout(): void {
  nativeLayoutRequestGeneration += 1;
  nativeViewMounted = false;
  lastNativeLayoutKey = '';
  pendingNativeLayoutKey = '';
  scheduleNativeViewLayout();
}

function applyEvent(event: BrowserEvent): void {
  if (event.type === 'state') {
    if (!isBrowserPageState(event.state)) return;
    streamStateRevision += 1;
    applyPageState(event.state);
  } else if (event.type === 'debug' || event.type === 'debug_clear') {
    // Debug data remains available to the model-facing tools, but is deliberately
    // not rendered in the compact user browser chrome.
    return;
  } else if (event.type === 'action') {
    // AI 还在正常出动作，说明刚才那条接管确认条是误触——自动收起，不打断流程。
    if (takeoverPromptTab) {
      takeoverPromptTab = '';
      renderTakeoverPrompt();
    }
    return;
  } else if (event.type === 'download') {
    if (
      !isRecord(event.download)
      || typeof event.download.name !== 'string'
      || typeof event.download.path !== 'string'
      || !Number.isFinite(event.download.created_at)
    ) return;
    if (pageState) {
      const downloadId = typeof event.download.id === 'string'
        ? event.download.id
        : '';
      const existingIndex = downloadId
        ? pageState.downloads.findIndex((item) => item.id === downloadId)
        : -1;
      if (existingIndex >= 0) {
        pageState.downloads = pageState.downloads.map((item, index) => (
          index === existingIndex ? event.download : item
        ));
      } else {
        pageState.downloads = [...pageState.downloads, event.download];
      }
    }
  } else if (event.type === 'command_error' || event.type === 'error') {
    if (typeof event.error === 'string' && !isInternalControlNotice(event.error)) {
      updateStatus(event.error);
    }
  }
}

function isInternalControlNotice(message: string): boolean {
  return /(?:人工接管|用户正在接管|浏览器动作已暂停|控制模式)/.test(message);
}

export function renderBrowserPanel(): string {
  const value = pageState || defaultState();
  const maximized = document.body.classList.contains('browser-workbench-maximized');
  return createBrowserInspector(value, { maximized, nativeViewMounted }).outerHTML;

}

export function bindBrowserPanel(root?: HTMLElement): void {
  panelRoot = root ?? document;
  const sessionId = currentSession();
  if (!sessionId) return;
  bindGlobalListener();
  bindNativeViewLayout();
  // Subscribe even when the session has no tab yet. The stream immediately
  // publishes the current state and is the only channel that can deliver the
  // first tab created later by an AI browser_navigate call.
  const revisionBeforeFetch = streamStateRevision;
  void ensureConnection(sessionId);
  void backendApi.browserState(sessionId).then((result) => {
    if (currentSession() !== sessionId) return;
    // Do not let a slower REST snapshot overwrite a newer stream event.
    if (streamStateRevision !== revisionBeforeFetch) return;
    if (!isBrowserPageState(result.state)) throw new Error('浏览器状态响应格式无效');
    applyPageState(result.state);
  }).catch((error) => {
    const message = (error as Error).message;
    // A welcome-page draft is persisted in parallel with opening the panel.
    // Keep the instant empty state instead of flashing a transient 404.
    if (!/会话不存在/.test(message)) updateStatus(message);
  });

  panelQueryAll<HTMLButtonElement>('[data-browser-action]').forEach((button) => {
    button.addEventListener('click', () => void sendHumanControl(button.dataset.browserAction || ''));
  });
  panelQueryAll<HTMLButtonElement>('[data-browser-takeover-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const confirmed = button.dataset.browserTakeoverAction === 'confirm';
      takeoverPromptTab = '';
      renderTakeoverPrompt();
      if (confirmed) void ensureHumanControl('page');
    });
  });
  panelQuery<HTMLButtonElement>('[data-browser-load-retry]')?.addEventListener('click', () => {
    // 「重试」是显式 chrome 动作：与工具栏刷新同语义，先接管再重新加载。
    loadFailure = null;
    patchChrome();
    void sendHumanControl('reload');
  });
  panelQuery<HTMLButtonElement>('[data-browser-new-tab]')?.addEventListener('click', () => {
    void openUserBrowser('', true);
  });
  panelQuery<HTMLButtonElement>('[data-browser-return-ai]')?.addEventListener('click', () => {
    void sendControl('return');
  });
  panelQueryAll<HTMLButtonElement>('[data-browser-shell]').forEach((button) => {
    button.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('browser-workbench:command', {
        detail: { action: button.dataset.browserShell || '' },
      }));
    });
  });
  const urlInput = panelQuery<HTMLInputElement>('[data-browser-url]');
  urlInput?.addEventListener('pointerdown', (event) => {
    if (!pageState?.tab_id || pageState.mode === 'human') return;
    event.preventDefault();
    void ensureHumanControl('address').then((ready) => {
      if (!ready || !document.contains(urlInput)) return;
      urlInput.readOnly = false;
      urlInput.focus();
      urlInput.select();
    });
  });
  urlInput?.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    if (!pageState?.tab_id) void openUserBrowser(urlInput.value);
    else void sendHumanControl('navigate', normalizeUserUrl(urlInput.value));
  });
  const tabStrip = panelQuery<HTMLElement>('[data-browser-tab-strip]');
  tabStrip?.addEventListener('click', (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-browser-tab]');
    const close = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-browser-close-tab]');
    const saveWiki = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-browser-save-wiki]');
    if (saveWiki) {
      event.stopPropagation();
      void saveTabToWiki(saveWiki.dataset.browserSaveWiki || '');
      return;
    }
    if (close) {
      event.stopPropagation();
      void sendHumanControl('close_tab', close.dataset.browserCloseTab || '');
      return;
    }
    if (button) void sendHumanControl('select_tab', button.dataset.browserTab || '');
  });
  tabStrip?.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    const tabs = Array.from(tabStrip.querySelectorAll<HTMLButtonElement>('[data-browser-tab]:not(:disabled)'));
    const current = tabs.indexOf(document.activeElement as HTMLButtonElement);
    if (current < 0 || tabs.length < 2) return;
    event.preventDefault();
    const delta = event.key === 'ArrowLeft' ? -1 : 1;
    const next = tabs[(current + delta + tabs.length) % tabs.length];
    next.focus();
    void sendHumanControl('select_tab', next.dataset.browserTab || '');
  });
}

const BAIDU_SEARCH_URL = 'https://www.baidu.com/s';

function buildBaiduSearchUrl(query: string): string {
  const url = new URL(BAIDU_SEARCH_URL);
  url.searchParams.set('wd', query);
  return url.toString();
}

function looksLikeHttpUrl(value: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return false;
  const host = parsed.hostname.toLowerCase();
  if (!host) return false;
  if (host === 'localhost' || host.endsWith('.localhost')) return true;
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host) || /^\[.*\]$/.test(host)) return true;
  return host.includes('.');
}

function normalizeUserUrl(value: string): string {
  const raw = value.trim();
  if (!raw) return '';
  // 纯数字必须走搜索。否则 URL 构造器会把它当成隐式 IPv4 地址解析
  //（例如 "10086" -> "https://0.0.39.102/"），导致浏览器代理加载失败。
  if (/^[0-9]+$/.test(raw)) {
    return buildBaiduSearchUrl(raw);
  }
  if (/^https?:\/\//i.test(raw)) {
    return looksLikeHttpUrl(raw) ? raw : buildBaiduSearchUrl(raw);
  }
  const withScheme = `https://${raw}`;
  return looksLikeHttpUrl(withScheme) ? withScheme : buildBaiduSearchUrl(raw);
}

export type BrowserOpenDestination = 'in_app' | 'cancelled' | 'failed';

type OpenUserBrowserOptions = {
  /** Creating a user tab changes the whole Crew browser session to human control. */
  confirmTakeover?: boolean;
  /**
   * Passive opens (auto-opening the panel to watch the AI) must not create a
   * blank human tab when the session has no page yet — that would flip the
   * whole session into human mode before the AI's first action lands.
   */
  createIfEmpty?: boolean;
};

async function approveHumanControl(sessionId: string): Promise<boolean> {
  const known = socketSession === sessionId ? pageState : null;
  const current = known || (await backendApi.browserState(sessionId)).state;
  if (currentSession() !== sessionId) return false;
  if (!isBrowserPageState(current)) throw new Error('浏览器状态响应格式无效');
  if (!known) applyPageState(current);
  if (!current.tab_id || current.mode !== 'ai') return true;
  return window.confirm('打开此内容会暂停 AI 浏览操作并进入人工控制。是否继续？');
}

export async function openUserBrowser(
  value = '',
  newTab = false,
  options: OpenUserBrowserOptions = {},
): Promise<BrowserOpenDestination> {
  const sessionId = currentSession();
  if (!sessionId) return 'failed';
  const normalizedUrl = normalizeUserUrl(value);
  try {
    if (options.confirmTakeover && !(await approveHumanControl(sessionId))) return 'cancelled';
    // Clicking the global Browser entry is also how users watch an AI-owned
    // tab.  Merely revealing that tab must not silently switch the whole
    // session into human mode.  Only create a blank human tab when the
    // session truly has no browser page yet — and never on passive opens,
    // which race ahead of the AI's first tab and would lock the AI out.
    if (!value.trim() && !newTab) {
      const existing = pageState?.tab_id
        ? pageState
        : (await backendApi.browserState(sessionId)).state;
      if (currentSession() !== sessionId) return 'failed';
      if (!isBrowserPageState(existing)) throw new Error('浏览器状态响应格式无效');
      applyPageState(existing);
      if (existing.tab_id || options.createIfEmpty === false) {
        await ensureConnection(sessionId);
        return 'in_app';
      }
    }
    const result = await backendApi.browserControl(
      sessionId,
      newTab ? 'new_tab' : 'open',
      normalizedUrl,
    );
    if (currentSession() !== sessionId) return 'failed';
    if (!isBrowserPageState(result.state)) throw new Error('浏览器状态响应格式无效');
    applyPageState(result.state);
    await ensureConnection(sessionId);
    return 'in_app';
  } catch (error) {
    notify(`无法在 Crew 浏览器中打开：${(error as Error).message}`);
    return 'failed';
  }
}

async function ensureHumanControl(
  source: 'page' | 'address' | 'chrome' = 'chrome',
): Promise<boolean> {
  const sessionId = currentSession();
  if (!sessionId) return false;
  if (pageState?.mode === 'human') return true;
  if (humanControlPromise) return humanControlPromise;

  humanControlPromise = (async () => {
    try {
      const result = await backendApi.browserControl(sessionId, 'takeover');
      if (currentSession() !== sessionId) return false;
      if (!isBrowserPageState(result.state)) throw new Error('浏览器状态响应格式无效');
      applyPageState(result.state);
      if (source === 'page') notify('你现在可以操作网页了');
      return true;
    } catch (error) {
      if (currentSession() === sessionId) {
        notify(`暂时无法操作网页：${(error as Error).message}`);
      }
      return false;
    } finally {
      humanControlPromise = null;
    }
  })();
  return humanControlPromise;
}

async function sendHumanControl(action: string, value = ''): Promise<void> {
  if (!action || !(await ensureHumanControl('chrome'))) return;
  await sendControl(action, value);
}

/**
 * 标签页「存入知识库」：read-tab 取正文后交给 wiki capture 编译入库。
 * 只读页面内容，不触发接管；读取/入库失败都要把原因说出来。
 */
async function saveTabToWiki(tabId: string): Promise<void> {
  const sessionId = currentSession();
  if (!sessionId || !tabId) return;
  try {
    const read = await backendApi.browserReadTab(sessionId, tabId);
    if (!read.ok) {
      notify(`无法读取标签页：${read.error || '未知原因'}`);
      return;
    }
    const title = read.title.trim() || read.url.trim() || '未命名页面';
    await backendApi.wikiCaptureText({ title, content: read.text, source_url: read.url });
    notify(`已存入知识库：${title}`);
  } catch (error) {
    notify(`存入知识库失败：${(error as Error).message}`);
  }
}

/** Close-time privacy handoff: keep control modes internal instead of exposing UI toggles. */
function releaseHumanBrowserControl(sessionId: string): Promise<void> {
  if (!sessionId) return Promise.resolve();
  return backendApi.browserControl(sessionId, 'return').then((result) => {
    if (currentSession() !== sessionId || !isBrowserPageState(result.state)) return;
    applyPageState(result.state);
  }).catch(() => {
    // Closing or switching the panel must remain immediate if the gateway is restarting.
  });
}

export function releaseUserBrowserControl(): void {
  const sessionId = currentSession();
  if (!sessionId || pageState?.mode !== 'human') return;
  void releaseHumanBrowserControl(sessionId);
}

export async function openBrowserArtifact(
  path: string,
  newTab = false,
  options: Pick<OpenUserBrowserOptions, 'confirmTakeover'> = {},
): Promise<BrowserOpenDestination> {
  const sessionId = currentSession();
  if (!sessionId || !path.trim()) return 'failed';
  try {
    if (options.confirmTakeover && !(await approveHumanControl(sessionId))) return 'cancelled';
    // The gateway forwards new_tab into the same manager call, keeping artifact
    // creation atomic and avoiding a failed preview leaving a blank tab behind.
    const result = await backendApi.browserOpenArtifact(sessionId, path.trim(), newTab);
    if (currentSession() !== sessionId) return 'failed';
    if (!isBrowserPageState(result.state)) throw new Error('浏览器状态响应格式无效');
    applyPageState(result.state);
    await ensureConnection(sessionId);
    return 'in_app';
  } catch (error) {
    notify(`HTML 预览失败：${(error as Error).message}`);
    return 'failed';
  }
}

function bindNativeViewLayout(): void {
  resizeObserver?.disconnect();
  layoutMutationObserver?.disconnect();
  const stage = panelQuery<HTMLElement>('[data-browser-stage]');
  if (!stage) return;
  resizeObserver = new ResizeObserver(scheduleNativeViewLayout);
  resizeObserver.observe(stage);
  layoutMutationObserver = new MutationObserver(scheduleNativeViewLayout);
  layoutMutationObserver.observe(document.body, {
    attributes: true,
    attributeFilter: ['class', 'style', 'hidden', 'aria-hidden'],
    childList: true,
    subtree: true,
  });
  if (!layoutListenersBound) {
    window.addEventListener('resize', scheduleNativeViewLayout, { passive: true });
    window.addEventListener('scroll', scheduleNativeViewLayout, { capture: true, passive: true });
    document.addEventListener('visibilitychange', handleVisibilityChange);
    layoutListenersBound = true;
  }
  scheduleNativeViewLayout();
}

function handleVisibilityChange(): void {
  if (document.visibilityState !== 'visible') {
    detachNativeView();
    return;
  }
  scheduleNativeViewLayout();
}

function scheduleNativeViewLayout(): void {
  if (layoutFrame) return;
  layoutFrame = window.requestAnimationFrame(() => {
    layoutFrame = 0;
    syncNativeViewLayout();
  });
}

function visibleBlockingOverlay(): boolean {
  const candidates = document.querySelectorAll<HTMLElement>([
    '[aria-modal="true"]',
    '.modal-overlay',
    '.usage-edit-overlay',
    '.chat-image-viewer',
    '#force-update-overlay',
    '#backend-loading-overlay',
  ].join(','));
  return Array.from(candidates).some((element) => {
    if (element.hidden) return false;
    const style = window.getComputedStyle(element);
    return style.display !== 'none'
      && style.visibility !== 'hidden'
      && style.opacity !== '0'
      && element.getClientRects().length > 0;
  });
}

function clippedStageBounds(stage: HTMLElement): { x: number; y: number; width: number; height: number } {
  const rect = stage.getBoundingClientRect();
  let left = Math.max(0, rect.left);
  let top = Math.max(0, rect.top);
  let right = Math.min(window.innerWidth, rect.right);
  let bottom = Math.min(window.innerHeight, rect.bottom);
  const clips = (value: string): boolean => /^(?:auto|clip|hidden|overlay|scroll)$/.test(value);
  for (let ancestor = stage.parentElement; ancestor; ancestor = ancestor.parentElement) {
    const style = window.getComputedStyle(ancestor);
    if (!clips(style.overflowX) && !clips(style.overflowY)) continue;
    const ancestorRect = ancestor.getBoundingClientRect();
    if (clips(style.overflowX)) {
      left = Math.max(left, ancestorRect.left);
      right = Math.min(right, ancestorRect.right);
    }
    if (clips(style.overflowY)) {
      top = Math.max(top, ancestorRect.top);
      bottom = Math.min(bottom, ancestorRect.bottom);
    }
  }
  const x = Math.max(0, Math.ceil(left));
  const y = Math.max(0, Math.ceil(top));
  const clippedRight = Math.max(x, Math.floor(right));
  const clippedBottom = Math.max(y, Math.floor(bottom));
  return { x, y, width: clippedRight - x, height: clippedBottom - y };
}

function syncNativeViewLayout(): void {
  const sessionId = currentSession();
  const stage = panelQuery<HTMLElement>('[data-browser-stage]');
  if (
    !sessionId
    || !stage
    || !pageState?.tab_label
    || isBlankBrowserUrl(pageState.url)
    || !document.contains(stage)
    || document.visibilityState !== 'visible'
    || visibleBlockingOverlay()
    || loadFailureVisible()
  ) {
    detachNativeView();
    return;
  }
  const bounds = clippedStageBounds(stage);
  if (bounds.width < 80 || bounds.height < 80) {
    detachNativeView();
    return;
  }
  const layoutKey = JSON.stringify([
    sessionId, pageState.tab_label, pageState.mode,
    bounds.x, bounds.y, bounds.width, bounds.height,
  ]);
  if (layoutKey === pendingNativeLayoutKey || (nativeViewMounted && layoutKey === lastNativeLayoutKey)) return;
  const requestGeneration = ++nativeLayoutRequestGeneration;
  pendingNativeLayoutKey = layoutKey;
  void window.Crew?.browserViewSetPanel?.({
    sessionId,
    tabLabel: pageState.tab_label,
    mode: pageState.mode,
    visible: true,
    bounds,
  }).then((result) => {
    if (requestGeneration !== nativeLayoutRequestGeneration) return;
    pendingNativeLayoutKey = '';
    if (result?.ok) {
      nativeViewMounted = true;
      lastNativeLayoutKey = layoutKey;
      const empty = panelQuery<HTMLElement>('[data-browser-empty]');
      if (empty) empty.hidden = true;
      updateStatus('');
      return;
    }
    nativeViewMounted = false;
    lastNativeLayoutKey = '';
    void window.Crew?.browserViewHide?.();
    const empty = panelQuery<HTMLElement>('[data-browser-empty]');
    if (empty) empty.hidden = false;
    updateStatus(result?.error || '无法挂载浏览器页面');
  }).catch(() => {
    if (requestGeneration !== nativeLayoutRequestGeneration) return;
    nativeViewMounted = false;
    lastNativeLayoutKey = '';
    pendingNativeLayoutKey = '';
    void window.Crew?.browserViewHide?.();
    const empty = panelQuery<HTMLElement>('[data-browser-empty]');
    if (empty) empty.hidden = false;
    updateStatus('无法挂载浏览器页面');
  });
}

async function sendControl(action: string, value = ''): Promise<void> {
  const sessionId = currentSession();
  if (!sessionId || !action) return;
  const closingLastTab = action === 'close_tab' && pageState?.tabs.length === 1;
  try {
    const result = await backendApi.browserControl(sessionId, action, value);
    if (currentSession() !== sessionId) return;
    if (!isBrowserPageState(result.state)) throw new Error('浏览器状态响应格式无效');
    applyPageState(result.state);
    if (closingLastTab && result.state.tabs.length === 0) {
      window.dispatchEvent(new CustomEvent('browser-workbench:command', {
        detail: { action: 'close' },
      }));
      return;
    }
    if (result.state.tab_id) await ensureConnection(sessionId);
  } catch (error) {
    const message = (error as Error).message;
    if (closingLastTab && /(?:会话不存在|标签页不属于当前 Crew 会话)/.test(message)) {
      hideBrowserPanelView();
      pageState = defaultState();
      window.dispatchEvent(new CustomEvent('browser-workbench:command', {
        detail: { action: 'close' },
      }));
      return;
    }
    notify(`浏览器操作失败：${message}`);
  }
}

function applyPageState(nextState: BrowserPageState): void {
  const shouldAutoOpen = Boolean(
    nextState.tab_id
    && nextState.mode === 'ai'
    && nextState.running
    && !pageState?.tab_id
    && !document.body.classList.contains('browser-workbench-open')
    && !sessionOverride
  );
  if (pageState?.tab_label !== nextState.tab_label || pageState?.mode !== nextState.mode) {
    detachNativeView();
  }
  pageState = nextState;
  patchChrome();
  window.dispatchEvent(new CustomEvent('browser-panel:state-changed', {
    detail: { state: nextState },
  }));
  if (shouldAutoOpen) {
    window.dispatchEvent(new CustomEvent('browser-workbench:command', {
      detail: { action: 'open-existing' },
    }));
  }
}

function patchChrome(): void {
  const value = pageState || defaultState();
  const url = panelQuery<HTMLInputElement>('[data-browser-url]');
  if (url && document.activeElement !== url) url.value = displayUrl(value.url);
  if (url) url.readOnly = Boolean(value.tab_id && value.mode !== 'human');
  const returnToAi = panelQuery<HTMLButtonElement>('[data-browser-return-ai]');
  if (returnToAi) returnToAi.hidden = value.mode !== 'human';
  const statusDot = document.getElementById('ins-browser-status');
  statusDot?.classList.toggle('is-running', value.running);
  statusDot?.setAttribute('aria-label', value.running ? '浏览器运行中' : '浏览器未运行');
  const workbenchDot = document.getElementById('browser-workbench-status');
  workbenchDot?.classList.toggle('is-running', value.running);
  const workbenchToggle = document.getElementById('browser-workbench-toggle');
  if (workbenchToggle) {
    const action = workbenchToggle.getAttribute('aria-expanded') === 'true' ? '关闭' : '打开';
    workbenchToggle.setAttribute(
      'aria-label',
      `${action}浏览器，${value.running ? '运行中' : '未运行'}`,
    );
  }
  panelQuery<HTMLElement>('[data-browser-stage]')?.classList.toggle('is-interactive', value.mode === 'human');
  renderTakeoverPrompt();
  renderLoadFailure();
  const empty = panelQuery<HTMLElement>('[data-browser-empty]');
  // 加载失败时白屏的原生视图已被收起（syncNativeViewLayout 不再挂载），
  // 空态提示也不能露出来——那一屏只能有错误遮罩。
  if (empty) {
    empty.hidden = loadFailureVisible()
      || Boolean(value.tab_id && !isBlankBrowserUrl(value.url) && nativeViewMounted);
  }
  const blankPage = !value.tab_id || isBlankBrowserUrl(value.url);
  const emptyTitle = panelQuery<HTMLElement>('[data-browser-empty-title]');
  if (emptyTitle) emptyTitle.textContent = blankPage ? '开始浏览' : '正在打开页面…';
  const emptyDescription = panelQuery<HTMLElement>('[data-browser-empty-description]');
  if (emptyDescription) {
    emptyDescription.textContent = blankPage ? '输入 URL 以打开页面' : '页面加载后将在此显示';
  }
  const strip = panelQuery<HTMLElement>('[data-browser-tab-strip]');
  if (strip) replaceBrowserTabs(strip, value);
  panelQueryAll<HTMLButtonElement>('[data-browser-action]').forEach((button) => {
    const action = button.dataset.browserAction || '';
    const canUseNavigation = Boolean(value.tab_id);
    if (action === 'back') button.disabled = !canUseNavigation || !value.can_go_back;
    else if (action === 'forward') button.disabled = !canUseNavigation || !value.can_go_forward;
    else button.disabled = !canUseNavigation || isBlankBrowserUrl(value.url);
  });
  scheduleNativeViewLayout();
}

function updateStatus(message: string): void {
  const element = panelQuery<HTMLElement>('[data-browser-status]');
  if (element && element.textContent !== message) element.textContent = message;
}
