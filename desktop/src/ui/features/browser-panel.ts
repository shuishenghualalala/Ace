/** Trusted browser chrome around a sandboxed Electron WebContentsView. */

import { backendApi, type BrowserPageState } from '../backend-client';
import { notify, state } from '../state';
import { createBrowserInspector, replaceBrowserTabs } from './browser-inspector';
import { queryPrimaryComposer } from './composer-scope';

type BrowserEvent =
  | { type: 'state'; state: BrowserPageState }
  | { type: 'debug'; channel: 'console' | 'network'; record: Record<string, unknown> }
  | { type: 'debug_clear' }
  | { type: 'action'; description: string }
  // 宿主到达录制上限后自己停了录制。没有这条事件，指示条会一直写着
  // 「正在录制」，而实际一步都不再进轨迹——用户对着一个假指示继续演示。
  | { type: 'recording_limit'; reason: string }
  // 密码 / 验证码是强隐私边界：宿主只记一条零化的接管步骤并自动停录。
  // 面板必须同步收起红点，不能让用户误以为后续敏感页面仍在安全录制。
  | { type: 'recording_privacy_stop'; reason: string }
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

const CLOSE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg>';
// 实心圆——通用的「录制」符号，与旁边描边风格的导航图标刻意区分开。
const RECORD_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="6" fill="currentColor" stroke="none"/></svg>';
const NOTE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/></svg>';
const PAUSE_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 4v16M14 4v16"/></svg>';
const RESUME_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 4l13 8-13 8z" fill="currentColor" stroke="none"/></svg>';
const STOP_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor" stroke="none"/></svg>';
function currentSession(): string { return state.activeSessionId || ''; }

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
  const empty = document.querySelector<HTMLElement>('[data-browser-empty]');
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
  const next = sessionId || '';
  if (next && socketSession === next) return;
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
  const banner = document.querySelector<HTMLElement>('[data-browser-takeover]');
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
  const overlay = document.querySelector<HTMLElement>('[data-browser-load-error]');
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
    // 录制中顺带刷一次步数。挂在状态流上而不是起定时器：页面每有变化就会来一条
    // state，节奏天然与用户操作同步；没有操作时也就不需要刷。
    void refreshRecordingSteps();
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
  } else if (
    event.type === 'recording_limit'
    || event.type === 'recording_privacy_stop'
  ) {
    if (typeof event.reason === 'string' && event.reason) notify(event.reason);
    // 走一次 stop 与后端对账：拿到最终步数、摘要与 recording_id，指示条据此
    // 收起，「生成技能」入口照常出现。隐私停止时，轨迹只包含敏感字段之前的
    // 步骤和一条零化的人工接管边界，敏感页面之后没有任何采集。
    void sendRecordingControl('stop');
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

/**
 * 录制控件的事件委托。**幂等**——`bindBrowserPanel` 每次切到浏览器面板都会调用，
 * 而这里绑在 document 上（工具栏的「开始录制」是静态节点，指示条里的按钮是动态
 * 渲染的，两处需要同一个委托），重复绑定会让一次点击触发多次。
 */
let recordingControlsBound = false;

function bindRecordingControls(): void {
  if (recordingControlsBound) return;
  recordingControlsBound = true;
  document.addEventListener('click', (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-browser-record]');
    const action = button?.dataset.browserRecord;
    if (!action) return;
    if (action === 'start') {
      void sendRecordingControl('start');
      return;
    }
    if (action === 'disarm') {
      armedBySession.delete(currentSession());
      renderBrowserRecordingBar();
      updateStatus('');
      return;
    }
    if (action === 'pause' || action === 'resume' || action === 'stop') {
      void sendRecordingControl(action);
    } else if (action === 'note') {
      // **不能用 window.prompt——Electron 根本不实现它**（wiki-page 里也踩过同一个
      // 坑并留了注释）。此前这里就是 prompt，于是标注按钮点了永远没反应。
      // 改成借用地址栏那一格的内联输入：不另开行，也不需要浮层（浮层会被原生
      // WebContentsView 盖住）。
      openRecordingNoteComposer();
    } else if (action === 'compile') {
      compileLastRecording();
    } else if (action === 'discard') {
      void discardLastRecording();
    }
  });
  // 标注表单同样走委托：它随录制态动态出现，绑在元素上会随重渲染丢失。
  document.addEventListener('submit', (event) => {
    const form = (event.target as HTMLElement | null)?.closest?.('[data-browser-note]');
    if (!form) return;
    event.preventDefault();
    const input = form.querySelector<HTMLInputElement>('[data-browser-note-input]');
    const text = (input?.value || '').trim();
    if (!text) return;
    void addRecordingNote(text).then((ok) => {
      updateStatus(ok ? '已加入标注' : '标注未能保存，请重试');
    });
    if (input) input.value = '';
    closeRecordingNoteComposer();
  });
  document.addEventListener('click', (event) => {
    if ((event.target as HTMLElement).closest('[data-browser-note-cancel]')) {
      closeRecordingNoteComposer();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!(event.target as HTMLElement | null)?.closest?.('[data-browser-note]')) return;
    closeRecordingNoteComposer();
  });
}

export function bindBrowserPanel(): void {
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

  document.querySelectorAll<HTMLButtonElement>('[data-browser-action]').forEach((button) => {
    button.addEventListener('click', () => void sendHumanControl(button.dataset.browserAction || ''));
  });
  document.querySelectorAll<HTMLButtonElement>('[data-browser-takeover-action]').forEach((button) => {
    button.addEventListener('click', () => {
      const confirmed = button.dataset.browserTakeoverAction === 'confirm';
      takeoverPromptTab = '';
      renderTakeoverPrompt();
      if (confirmed) void ensureHumanControl('page');
    });
  });
  document.querySelector<HTMLButtonElement>('[data-browser-load-retry]')?.addEventListener('click', () => {
    // 「重试」是显式 chrome 动作：与工具栏刷新同语义，先接管再重新加载。
    loadFailure = null;
    patchChrome();
    void sendHumanControl('reload');
  });
  bindRecordingControls();

  renderBrowserRecordingBar();
  document.querySelector<HTMLButtonElement>('[data-browser-new-tab]')?.addEventListener('click', () => {
    void openUserBrowser('', true);
  });
  document.querySelectorAll<HTMLButtonElement>('[data-browser-shell]').forEach((button) => {
    button.addEventListener('click', () => {
      window.dispatchEvent(new CustomEvent('browser-workbench:command', {
        detail: { action: button.dataset.browserShell || '' },
      }));
    });
  });
  const urlInput = document.querySelector<HTMLInputElement>('[data-browser-url]');
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
  const tabStrip = document.querySelector<HTMLElement>('[data-browser-tab-strip]');
  tabStrip?.addEventListener('click', (event) => {
    const button = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-browser-tab]');
    const close = (event.target as HTMLElement).closest<HTMLButtonElement>('[data-browser-close-tab]');
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

/** 录制态。只由本模块的按钮驱动——模型没有任何录制控制工具。 */
/**
 * 录制态。**按会话隔离**——早先是跨会话的全局变量，切到会话 B 会看到会话 A
 * 的录制指示，点「停止」还会把 A 的录制停掉。
 */
const recordingBySession = new Map<string, RecordingState>();
type RecordingState = { recording: boolean; paused: boolean; steps: number };
const IDLE_RECORDING: RecordingState = { recording: false, paused: false, steps: 0 };

function recordingStateFor(sessionId: string): RecordingState {
  return recordingBySession.get(sessionId) ?? { ...IDLE_RECORDING };
}

/**
 * 上一次录制的成果。停止录制后保留，用来渲染「生成技能」入口。
 *
 * 轨迹本身不会自动进入对话——它落在 owner 私有目录里，只有用户点了「生成技能」
 * 才把不透明 recording_id 交给模型。这是「录制不是接管的副作用」那条设计的
 * 最后一环：轨迹进入模型上下文必须是一次知情同意的动作。绝对路径不属于协议，
 * 也不应泄漏到 prompt、renderer 状态或对话历史。
 */
type RecordingSummary = {
  steps: number;
  hosts: string[];
  notes: string[];
  masked_fields: number;
  handoff_fields: number;
  pages_captured: number;
  incomplete?: boolean;
  dropped_steps?: number;
};

/** 已停止、待编译的录制。同样按会话隔离——B 会话不该看到 A 的待编译入口。 */
const lastRecordingBySession = new Map<string, {
  steps: number;
  recordingId: string;
  summary: RecordingSummary | null;
  incomplete: boolean;
}>();

function normalizedRecordingId(value: unknown): string {
  const recordingId = typeof value === 'string' ? value.toLowerCase() : '';
  return /^[0-9a-f]{8,32}$/.test(recordingId) ? recordingId : '';
}

export function browserRecordingState(): Readonly<RecordingState> {
  return recordingStateFor(currentSession());
}

/**
 * 录制开关。
 *
 * 录制必须先接管浏览器：录的是**用户自己的**操作，页面得能真的响应他的点击。
 * 但录制态与 ControlMode 是正交的——接管只是前提，不是录制本身。
 */
export async function sendRecordingControl(
  action: 'start' | 'pause' | 'resume' | 'stop',
): Promise<void> {
  const sessionId = currentSession();
  if (!sessionId) return;
  if (action === 'start' && pageIsBlank()) {
    // 空白页开不了录（没有视口，见 armedBySession 的说明）。不是把用户挡在门外，
    // 而是进入预备态并把光标送到地址栏——他接着打开网站就会自动开始录。
    // 判定放在这里而不是点击处理器里：入口只有一个，别的调用方也走同一条语义。
    armedBySession.add(sessionId);
    renderBrowserRecordingBar();
    updateStatus('已预备录制：打开一个网页后自动开始');
    document.querySelector<HTMLInputElement>('[data-browser-url]')?.focus();
    return;
  }
  if (action === 'start' && !(await ensureHumanControl('chrome'))) return;
  try {
    const stepsBeforeStop = recordingStateFor(sessionId).steps;
    const result = await backendApi.browserControl(sessionId, `record_${action}`);
    if (result.recording) {
      // 停止时后端返回的 steps 已归零，要保留停止前的计数才能告诉用户录了多少步
      const steps = Number(result.recording.steps) || 0;
      recordingBySession.set(sessionId, {
        recording: Boolean(result.recording.recording),
        paused: Boolean(result.recording.paused),
        steps,
      });
      if (action === 'stop') {
        const summary = result.recording.summary ?? null;
        // 步数以摘要里的实际落盘条数为准：宿主计数与真正写进轨迹的可能不一致
        //（写盘失败会被吞掉），告诉用户「录了 9 步」而文件里只有 7 条是骗人。
        const captured = summary?.steps ?? (stepsBeforeStop || steps);
        const recordingId = normalizedRecordingId(result.recording.recording_id);
        const incomplete = Boolean(summary?.incomplete ?? result.recording.incomplete);
        const nextRecording = recordingId && (captured > 0 || incomplete)
          ? {
            steps: captured,
            recordingId,
            summary,
            incomplete,
          }
          : null;
        if (nextRecording) lastRecordingBySession.set(sessionId, nextRecording);
        else lastRecordingBySession.delete(sessionId);
        if (incomplete) {
          notify('这次录制有步骤未完整保存，不能生成技能；请丢弃后重新录制');
        }
      } else if (action === 'start') {
        lastRecordingBySession.delete(currentSession());
      }
    }
  } catch {
    // 失败必须说出来。
    //
    // 此前这里是静默 return：用户点「停止」失败之后，指示条仍然写着「正在录制」，
    // 而他以为已经停了——之后在浏览器里做的事全都进了轨迹。点「开始」失败同样
    // 没有任何反馈，他会以为在录，演示完一遍才发现什么都没有。
    notify(
      action === 'start'
        ? '开始录制失败；请重试，或检查浏览器是否已就绪'
        : action === 'stop'
          ? '停止录制失败；录制可能仍在进行，请重试'
          : '录制状态切换失败；请重试',
    );
    return;
  }
  renderBrowserRecordingBar();
}

/**
 * 录制过程中刷新步数。
 *
 * 步数原本只在 start/stop 时更新，于是录制途中指示条永远显示开始时的那个数
 * （通常是 0 或 1）。用户看不出录制到底在不在记东西——而"看起来没动"恰好是
 * 他最需要察觉的故障（注入失败、页面换了文档、宿主掉了）。
 *
 * 由面板的状态流驱动：每次收到 state 事件就问一次当前录制状态，不额外起定时器。
 */
/**
 * 录制期间的步数轮询。
 *
 * **不能只挂在 state 流上。** 原来的实现只在收到 `state` 事件时刷一次，依据是
 * 「页面每有变化就会来一条 state」——这句话对录制场景是错的：`state` 只携带
 * url/title/tabs/mode，而录制记的是点击、输入、悬停这些**页面内**动作，它们一个
 * state 字段都不改。结果就是计数永远停在开录那一刻（1 = openPage），只有导航才跳，
 * 用户看到「一直是第 1 步」以为根本没录上。
 *
 * 后端为此专门把 `record_status` 做成不经宿主往返的纯读，注释里明写「会被频繁
 * 调用」——轮询本就是设计意图，只是渲染层一直没接上那个定时器。
 */
const RECORDING_POLL_MS = 1000;
let recordingPollTimer: ReturnType<typeof setInterval> | null = null;

function syncRecordingPoll(): void {
  const shouldPoll = Boolean(currentSession())
    && recordingStateFor(currentSession()).recording;
  if (shouldPoll === Boolean(recordingPollTimer)) return;
  if (!shouldPoll) {
    if (recordingPollTimer) clearInterval(recordingPollTimer);
    recordingPollTimer = null;
    return;
  }
  recordingPollTimer = setInterval(() => {
    void refreshRecordingSteps();
  }, RECORDING_POLL_MS);
}

export async function refreshRecordingSteps(): Promise<void> {
  const sessionId = currentSession();
  if (!sessionId || !recordingStateFor(sessionId).recording) return;
  try {
    const result = await backendApi.browserControl(sessionId, 'record_status');
    if (!result.recording) return;
    const next = {
      recording: Boolean(result.recording.recording),
      paused: Boolean(result.recording.paused),
      steps: Number(result.recording.steps) || 0,
    };
    const current = recordingStateFor(sessionId);
    if (
      current.recording === next.recording
      && current.paused === next.paused
      && current.steps === next.steps
    ) return;
    recordingBySession.set(sessionId, next);
    // 宿主到上限会自己停录。状态里 recording 变 false 就说明发生了这件事，
    // 指示条必须跟着收起，否则用户对着一个假的「正在录制」继续演示。
    renderBrowserRecordingBar();
  } catch {
    // 轮询失败不改状态：一次网络抖动不该让指示条闪烁或误报停止。
  }
}

/**
 * 渲染录制指示条。
 *
 * 录制期间必须有**持续可见**的指示——这是设计里的硬要求：录制会把用户看到的
 * 页面内容写进磁盘，用户任何时刻都得知道它开着。
 */
/**
 * 摘要文案。只说用户需要判断「要不要交出去」的那几件事：走过哪些站点、
 * 轨迹里有没有密码原值、有几处必须人工的验证。步数在外层已经写了。
 */
function recordingSummaryText(summary: RecordingSummary | null): string {
  if (!summary) return '';
  const parts: string[] = [];
  if (summary.hosts.length) {
    parts.push(summary.hosts.length <= 2
      ? summary.hosts.join('、')
      : `${summary.hosts.slice(0, 2).join('、')} 等 ${summary.hosts.length} 个站点`);
  }
  // **如实说明：密码值确实进了轨迹文件。**
  //
  // 早先这里写的是「N 处密码已屏蔽」，而当前 recorder schema 起分级只是描述性
  // 元数据，值一个不少地落盘。用户正是在这一屏决定要不要把轨迹交给模型编译，
  // 在这里说"已屏蔽"是最坏的一种错——他会因为一句假话做出相反的决定。
  //
  // 验证码那档说"需人工"是准确的：编译期强制把它变成人工接管，
  // 因为一次性码存下来必然失效。
  if (summary.masked_fields) {
    parts.push(`含 ${summary.masked_fields} 处密码原值`);
  }
  if (summary.handoff_fields) parts.push(`${summary.handoff_fields} 处验证码需人工`);
  if (summary.notes.length) parts.push(`${summary.notes.length} 条标注`);
  return parts.length ? `（${parts.join('，')}）` : '';
}

/** 在录制途中加一条标注，把意图前置，编译期就不必从动作序列反推。 */
export async function addRecordingNote(text: string): Promise<boolean> {
  const sessionId = currentSession();
  const note = text.trim();
  if (!sessionId || !note || !recordingStateFor(sessionId).recording) return false;
  try {
    await backendApi.browserControl(sessionId, 'record_note', note);
  } catch {
    return false;
  }
  return true;
}

/**
 * 工具栏里的录制控件。
 *
 * **录制中不再单独占一整行。** 录制是贯穿整段演示的长期状态，而原来的全宽指示条
 * 为一句话吃掉 33px 纵向空间，中间还空着一大片——在本来就窄的面板里这是纯浪费。
 *
 * 为什么是折进工具栏而不是做成盖在页面上的浮层：页面是原生 WebContentsView，
 * 位置精确对齐 stage 矩形（见 syncNativeViewLayout），任何 HTML 浮层都会被压在
 * 它下面看不见。所以指示只能待在 chrome 区，那就把它做成工具栏里的一个控件组。
 */
/**
 * 「预备录制」的会话集合。
 *
 * 录制的第一步是宿主合成的 `openPage`，它需要 URL **和视口**；而空白标签页的
 * 原生 view 是分离的（见 syncNativeViewLayout），没有视口 → 那条 openPage 记不下来
 * → 整段录制被标成 incomplete，最后只能弹「录制不完整」。
 *
 * 所以空白页不能真的开录。但"先按录制、再打开网站"才是自然顺序（Playwright
 * codegen 就是这样），用禁用按钮把用户挡在门外是最差的解法——尤其 Chromium 还
 * 不给 disabled 元素弹原生提示，用户连为什么点不动都看不到。
 *
 * 折中：空白页按下录制进入**预备**态，等第一个真实页面加载、view 挂上拿到视口的
 * 那一刻自动开录。用户体验上是"按下就开始"，而轨迹侧拿到的仍是完全正常的
 * `openPage <那个网址>` 作为第 1 步——不动宿主、不动轨迹契约。
 */
const armedBySession = new Set<string>();

function pageIsBlank(): boolean {
  const value = pageState || defaultState();
  return !value.tab_id || isBlankBrowserUrl(value.url);
}

function recordingControlsMarkup(): string {
  const recordingState = recordingStateFor(currentSession());
  if (!recordingState.recording) {
    if (armedBySession.has(currentSession())) {
      return `<span class="browser-rec is-armed" role="status" aria-live="polite">
          <span class="browser-rec__pill" title="已预备录制 · 打开一个网页后自动开始">
            <span class="browser-rec__dot"></span>
            <span class="browser-rec__count">预备录制</span>
          </span>
          <button type="button" class="browser-icon-btn browser-rec__btn" data-browser-record="disarm"
            aria-label="取消预备录制" title="取消预备录制">${CLOSE_ICON}</button>
        </span>`;
    }
    return `<button type="button" class="browser-icon-btn browser-icon-btn--record" data-browser-record="start"
      aria-label="开始录制技能" title="开始录制技能 · 把这段操作录成可重放的技能">${RECORD_ICON}</button>`;
  }
  const paused = recordingState.paused;
  // 状态靠颜色+文字双通道表达：图标按钮全部带 title/aria-label，
  // 光靠一个图标让人猜是什么，等于没有提示。
  return `<span class="browser-rec ${paused ? 'is-paused' : ''}" role="status" aria-live="polite">
      <span class="browser-rec__pill" title="${paused ? '录制已暂停' : '正在录制技能'} · 已记录 ${recordingState.steps} 步">
        <span class="browser-rec__dot"></span>
        <span class="browser-rec__count" data-browser-rec-count>${recordingState.steps} 步</span>
        <span class="browser-rec__sr">${paused ? '录制已暂停' : '正在录制技能'}</span>
      </span>
      <button type="button" class="browser-icon-btn browser-rec__btn" data-browser-record="note"
        aria-label="给录制加说明" title="给这一步加说明（例如：这个工单号每次都不同）">${NOTE_ICON}</button>
      <button type="button" class="browser-icon-btn browser-rec__btn" data-browser-record="${paused ? 'resume' : 'pause'}"
        aria-label="${paused ? '继续录制' : '暂停录制'}"
        title="${paused ? '继续录制' : '暂停录制（登录、输密码这类片段可以掐掉）'}"
        >${paused ? RESUME_ICON : PAUSE_ICON}</button>
      <button type="button" class="browser-icon-btn browser-rec__btn browser-rec__btn--stop" data-browser-record="stop"
        aria-label="停止录制" title="停止录制并生成技能">${STOP_ICON}</button>
    </span>`;
}

/** 录制控件的结构键。只有结构变化才重建 DOM，步数变化只改那一个文本节点。 */
let recordingControlsKey = '';

function renderRecordingControls(): void {
  const host = document.querySelector<HTMLElement>('[data-browser-rec-controls]');
  if (!host) {
    recordingControlsKey = '';
    return;
  }
  const recordingState = recordingStateFor(currentSession());
  const key = [
    recordingState.recording,
    recordingState.paused,
    armedBySession.has(currentSession()),
  ].join('|');
  if (key !== recordingControlsKey || !host.firstElementChild) {
    recordingControlsKey = key;
    host.innerHTML = recordingControlsMarkup();
    return;
  }
  // 录制途中步数每个动作都在变。整组重建会打断用户正悬停的按钮与其原生提示，
  // 所以只更新计数这一个文本节点。
  const count = host.querySelector<HTMLElement>('[data-browser-rec-count]');
  if (count) count.textContent = `${recordingState.steps} 步`;
}

/** 正在写标注的会话。标注输入借用地址栏那一格，不另开行、不弹浮层。 */
let noteComposerSession = '';

export function openRecordingNoteComposer(): void {
  const sessionId = currentSession();
  if (!sessionId || !recordingStateFor(sessionId).recording) return;
  noteComposerSession = sessionId;
  renderBrowserRecordingBar();
  const input = document.querySelector<HTMLInputElement>('[data-browser-note-input]');
  input?.focus();
}

export function closeRecordingNoteComposer(): void {
  if (!noteComposerSession) return;
  noteComposerSession = '';
  renderBrowserRecordingBar();
}

/**
 * 录制 UI 的槽位调度。
 *
 * **录制相关的东西一行都不占。** 工具栏里本来就有一格宽敞的地址栏，而录制的三种
 * 需要宽度的形态——写标注、录完待决定——都是短暂且互斥的，直接借用那一格：
 * 写标注时地址栏变成标注输入，待决定时变成摘要与两个动作，做完就还回去。
 *
 * 为什么不做浮层：页面是原生 WebContentsView，盖在 stage 上的 HTML 一律在它下面
 * （见 syncNativeViewLayout / visibleBlockingOverlay）。留在工具栏才看得见。
 */
export function renderBrowserRecordingBar(): void {
  renderRecordingControls();
  const sessionId = currentSession();
  const recordingState = recordingStateFor(sessionId);
  // 录制开始/结束/切会话都会走到这里，轮询的起停跟着它走，不会漏关。
  syncRecordingPoll();
  if (noteComposerSession && (!sessionId || noteComposerSession !== sessionId || !recordingState.recording)) {
    noteComposerSession = '';
  }
  const composing = Boolean(sessionId) && noteComposerSession === sessionId;
  const lastRecording = recordingState.recording
    ? undefined
    : lastRecordingBySession.get(sessionId);
  const host = document.querySelector<HTMLElement>('[data-browser-recording]');
  if (host) {
    if (lastRecording && !composing) {
      // 录完了但还没生成技能：留一个入口，别让用户找不到刚录的东西。
      // 同时把「要交出什么」摆在他面前——轨迹记录的是他真实看到的页面，
      // 按下发送键之前有权知道走过哪些站点、有没有碰过密码框。
      host.hidden = false;
      host.innerHTML = lastRecording.incomplete
        ? `
        <span class="browser-recording__label">录制不完整，不能生成技能</span>
        <button type="button" class="browser-recording__btn" data-browser-record="discard">丢弃</button>`
        : `
        <span class="browser-recording__label" title="已录制 ${lastRecording.steps} 步${
          recordingSummaryText(lastRecording.summary)
        }">已录制 ${lastRecording.steps} 步${
          recordingSummaryText(lastRecording.summary)
        }</span>
        <button type="button" class="browser-recording__btn browser-recording__btn--primary" data-browser-record="compile">生成技能</button>
        <button type="button" class="browser-recording__btn" data-browser-record="discard">丢弃</button>`;
    } else {
      host.hidden = true;
      host.innerHTML = '';
    }
  }
  const noteForm = document.querySelector<HTMLElement>('[data-browser-note]');
  if (noteForm) noteForm.hidden = !composing;
  const urlInput = document.querySelector<HTMLElement>('[data-browser-url]');
  if (urlInput) urlInput.hidden = composing || Boolean(lastRecording);
}

/**
 * 把刚录的轨迹交给模型去编译成技能。
 *
 * 只往输入框**填**一段话、不自动发送：让用户能在提交前补一句"这个技能叫查工单"
 * 或者干脆改主意。轨迹进入模型上下文必须是用户按下发送键的那一刻，不能更早。
 */
export function compileLastRecording(): boolean {
  const lastRecording = lastRecordingBySession.get(currentSession());
  if (!lastRecording || lastRecording.incomplete) return false;
  const input = queryPrimaryComposer<HTMLTextAreaElement>('[data-composer-input]');
  if (!input) return false;
  const prompt = `把我刚才在浏览器里的这段录制编译成一个技能。\n`
    + `录制 ID：${lastRecording.recordingId}\n`
    + `共 ${lastRecording.steps} 步。`;
  input.value = input.value.trim() ? `${input.value.trim()}\n\n${prompt}` : prompt;
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();
  input.setSelectionRange(input.value.length, input.value.length);
  // **只收起入口，绝不删轨迹。** 用户此刻还没按发送键，Agent 要等到那之后才去
  // 读这个目录——在这里删掉，等于把提示词填好的同时把它指向的东西毁了。
  // 「用掉入口」和「删除数据」是两件事，只有「丢弃」按钮才做后者。
  lastRecordingBySession.delete(currentSession());
  renderBrowserRecordingBar();
  return true;
}

/**
 * 丢弃这段录制。
 *
 * **真的删盘**，不只是把入口藏起来：轨迹里是用户看到的真实业务数据（工单内容、
 * 姓名、金额），点了「丢弃」却留着文件等于骗人。删除失败也照样收起入口，但会
 * 提示用户——不能让他以为删了。
 */
export async function discardLastRecording(): Promise<void> {
  const target = lastRecordingBySession.get(currentSession());
  lastRecordingBySession.delete(currentSession());
  renderBrowserRecordingBar();
  const sessionId = currentSession();
  if (!target?.recordingId || !sessionId) return;
  try {
    const result = await backendApi.browserControl(sessionId, 'record_discard', target.recordingId);
    // 后端删除失败时返回的是 200 + discarded:false（HTTP 层面请求确实成功了）。
    // 不看这个字段就会在删除失败时照样告诉用户「已丢弃」——而轨迹里是他看到的
    // 真实业务数据，以为删了却还在，比不提供删除更糟。
    if (result.discarded === false) {
      notify('录制轨迹未能删除，文件仍在本机');
    }
  } catch {
    notify('录制轨迹未能删除，文件仍在本机');
  }
}

/** Close-time privacy handoff: keep control modes internal instead of exposing UI toggles. */
export function releaseUserBrowserControl(): void {
  const sessionId = currentSession();
  if (!sessionId || pageState?.mode !== 'human') return;
  void backendApi.browserControl(sessionId, 'return').then((result) => {
    if (currentSession() !== sessionId || !isBrowserPageState(result.state)) return;
    applyPageState(result.state);
  }).catch(() => {
    // Closing the panel must remain immediate even if the gateway is restarting.
  });
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
  const stage = document.querySelector<HTMLElement>('[data-browser-stage]');
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
  const stage = document.querySelector<HTMLElement>('[data-browser-stage]');
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
      const empty = document.querySelector<HTMLElement>('[data-browser-empty]');
      if (empty) empty.hidden = true;
      updateStatus('');
      return;
    }
    nativeViewMounted = false;
    lastNativeLayoutKey = '';
    void window.Crew?.browserViewHide?.();
    const empty = document.querySelector<HTMLElement>('[data-browser-empty]');
    if (empty) empty.hidden = false;
    updateStatus(result?.error || '无法挂载浏览器页面');
  }).catch(() => {
    if (requestGeneration !== nativeLayoutRequestGeneration) return;
    nativeViewMounted = false;
    lastNativeLayoutKey = '';
    pendingNativeLayoutKey = '';
    void window.Crew?.browserViewHide?.();
    const empty = document.querySelector<HTMLElement>('[data-browser-empty]');
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

/**
 * 预备录制的落地点：第一个真实页面一挂上就真正开录。
 *
 * **必须挂在 `patchChrome` 里，而不是 `applyPageState`。** 页面内导航（点链接、
 * 地址栏跳转提交）走的是 `refreshPanelNavigation`：它直接改 `pageState` 再调
 * `patchChrome`，**根本不经过 `applyPageState`**。只挂在后者上的话，用户在空白页
 * 按下录制、然后打开网站——最典型的那条路——永远等不到开录，指示条永久停在
 * 「预备录制」。真机就是这么卡住的。
 *
 * 先把会话从 armed 里摘掉再发起 start：start 内部的接管会再触发一轮状态更新，
 * 不摘掉就会重入。
 */
function maybeStartArmedRecording(): void {
  const sessionId = currentSession();
  if (!sessionId || !armedBySession.has(sessionId) || pageIsBlank()) return;
  armedBySession.delete(sessionId);
  if (recordingStateFor(sessionId).recording) {
    renderBrowserRecordingBar();
    return;
  }
  void sendRecordingControl('start');
}

function patchChrome(): void {
  const value = pageState || defaultState();
  const url = document.querySelector<HTMLInputElement>('[data-browser-url]');
  if (url && document.activeElement !== url) url.value = displayUrl(value.url);
  if (url) url.readOnly = Boolean(value.tab_id && value.mode !== 'human');
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
  document.querySelector<HTMLElement>('[data-browser-stage]')?.classList.toggle('is-interactive', value.mode === 'human');
  renderTakeoverPrompt();
  renderLoadFailure();
  const empty = document.querySelector<HTMLElement>('[data-browser-empty]');
  // 加载失败时白屏的原生视图已被收起（syncNativeViewLayout 不再挂载），
  // 空态提示也不能露出来——那一屏只能有错误遮罩。
  if (empty) {
    empty.hidden = loadFailureVisible()
      || Boolean(value.tab_id && !isBlankBrowserUrl(value.url) && nativeViewMounted);
  }
  const blankPage = !value.tab_id || isBlankBrowserUrl(value.url);
  const emptyTitle = document.querySelector<HTMLElement>('[data-browser-empty-title]');
  if (emptyTitle) emptyTitle.textContent = blankPage ? '开始浏览' : '正在打开页面…';
  const emptyDescription = document.querySelector<HTMLElement>('[data-browser-empty-description]');
  if (emptyDescription) {
    emptyDescription.textContent = blankPage ? '输入 URL 以打开页面' : '页面加载后将在此显示';
  }
  const strip = document.querySelector<HTMLElement>('[data-browser-tab-strip]');
  if (strip) replaceBrowserTabs(strip, value);
  document.querySelectorAll<HTMLButtonElement>('[data-browser-action]').forEach((button) => {
    const action = button.dataset.browserAction || '';
    const canUseNavigation = Boolean(value.tab_id);
    if (action === 'back') button.disabled = !canUseNavigation || !value.can_go_back;
    else if (action === 'forward') button.disabled = !canUseNavigation || !value.can_go_forward;
    else button.disabled = !canUseNavigation || isBlankBrowserUrl(value.url);
  });
  // 录制控件是 [data-browser-record]，不在上面按 [data-browser-action] 刷新的那批里。
  // 必须单独重刷（启用态与录制态都在这里定），否则它会一直停在面板首次渲染
  // （空白页）时的 disabled——用户加载了真实页面也点不动。
  renderRecordingControls();
  // 会话切走/面板拆掉都会经过 patchChrome（而不经 renderBrowserRecordingBar），
  // 轮询的停止也挂在这里，避免定时器随会话泄漏。
  syncRecordingPoll();
  // 每一条状态更新路径都会走到 patchChrome——包括绕开 applyPageState 的
  // refreshPanelNavigation。预备录制的落地点必须挂在这里才不会漏。
  maybeStartArmedRecording();
  scheduleNativeViewLayout();
}

function updateStatus(message: string): void {
  const element = document.querySelector<HTMLElement>('[data-browser-status]');
  if (element && element.textContent !== message) element.textContent = message;
}
