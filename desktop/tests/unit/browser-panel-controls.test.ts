// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest';

import { backendApi, type BrowserPageState } from '../../src/ui/backend-client';
import {
  bindBrowserPanel,
  openBrowserArtifact,
  openUserBrowser,
  renderBrowserPanel,
  setBrowserPanelSession,
  sendRecordingControl,
  syncBrowserPanelSession,
} from '../../src/ui/features/browser-panel';
import { setActiveSessionId } from '../../src/ui/state';

function pageState(overrides: Partial<BrowserPageState> = {}): BrowserPageState {
  return {
    owner_hash: 'owner',
    session_hash: 'session',
    tab_id: '',
    tab_label: '',
    url: '',
    title: '',
    generation: 0,
    mode: 'ai',
    running: false,
    last_action: '',
    last_error: '',
    screenshot_id: '',
    viewport_width: 0,
    viewport_height: 0,
    can_go_back: false,
    can_go_forward: false,
    tabs: [],
    downloads: [],
    ...overrides,
  };
}

describe('browser panel control recovery', () => {
  afterEach(() => {
    syncBrowserPanelSession(null);
    setActiveSessionId(null);
    document.body.innerHTML = '';
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('reveals an existing AI tab without silently taking human control', async () => {
    const existing = pageState({
      tab_id: 'tab-1',
      tab_label: 'session-tab-1',
      url: 'https://example.com/',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'tab-1', label: 'session-tab-1', url: 'https://example.com/', title: 'Example' }],
    });
    const stateRequest = vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: existing });
    const control = vi.spyOn(backendApi, 'browserControl');
    setActiveSessionId('session-existing-ai-tab');

    await openUserBrowser();

    expect(stateRequest).toHaveBeenCalledWith('session-existing-ai-tab');
    expect(control).not.toHaveBeenCalled();
  });

  it('passive auto-open with no tab subscribes without creating a human tab', async () => {
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: pageState() });
    const control = vi.spyOn(backendApi, 'browserControl');
    setActiveSessionId('session-auto-open');

    const destination = await openUserBrowser('', false, { createIfEmpty: false });

    expect(destination).toBe('in_app');
    expect(control).not.toHaveBeenCalled();
  });

  it('explicit empty open still creates a blank user tab when no page exists', async () => {
    const opened = pageState({ tab_id: 'user-tab', tab_label: 'session-user-tab', mode: 'human' });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: pageState() });
    const control = vi.spyOn(backendApi, 'browserControl').mockResolvedValue({ ok: true, state: opened });
    setActiveSessionId('session-manual-open');

    const destination = await openUserBrowser();

    expect(destination).toBe('in_app');
    expect(control).toHaveBeenCalledWith('session-manual-open', 'open', '');
  });

  it('discards a stale browser response after the active session changes', async () => {
    let resolveState!: (value: { ok: boolean; state: BrowserPageState }) => void;
    vi.spyOn(backendApi, 'browserState').mockReturnValue(new Promise((resolve) => {
      resolveState = resolve;
    }));
    const control = vi.spyOn(backendApi, 'browserControl');
    setActiveSessionId('session-old');

    const opening = openUserBrowser();
    setActiveSessionId('session-new');
    resolveState({ ok: true, state: pageState() });
    await opening;

    expect(control).not.toHaveBeenCalled();
  });

  it('switches an explicit Wiki session and restores the main session when cleared', async () => {
    const connect = vi.fn().mockResolvedValue({ ok: true });
    const close = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: { browserWsConnect: connect, browserWsClose: close },
    });
    setActiveSessionId('main-session');

    setBrowserPanelSession('wiki-session-a');
    await Promise.resolve();
    setBrowserPanelSession(null);
    await Promise.resolve();

    expect(connect).toHaveBeenNthCalledWith(1, 'wiki-session-a');
    expect(connect).toHaveBeenNthCalledWith(2, 'main-session');
    expect(close).toHaveBeenCalled();
  });

  it('opens an explicit chat link in a new user tab', async () => {
    const opened = pageState({
      tab_id: 'user-tab',
      tab_label: 'session-user-tab',
      url: 'https://example.com/',
      mode: 'human',
      running: true,
      tabs: [{ id: 'user-tab', label: 'session-user-tab', url: 'https://example.com/', title: 'Example' }],
    });
    const control = vi.spyOn(backendApi, 'browserControl').mockResolvedValue({ ok: true, state: opened });
    setActiveSessionId('session-chat-link');

    const destination = await openUserBrowser('https://example.com/', true);

    expect(destination).toBe('in_app');
    expect(control).toHaveBeenCalledWith('session-chat-link', 'new_tab', 'https://example.com/');
  });

  it.each([
    ['https://example.com/', 'https://example.com/'],
    ['example.com', 'https://example.com'],
    ['localhost:3000', 'https://localhost:3000'],
    ['192.168.1.1', 'https://192.168.1.1'],
    ['weather', 'https://www.baidu.com/s?wd=weather'],
    ['80088088', 'https://www.baidu.com/s?wd=80088088'],
    ['我想搜索的东西', 'https://www.baidu.com/s?wd=%E6%88%91%E6%83%B3%E6%90%9C%E7%B4%A2%E7%9A%84%E4%B8%9C%E8%A5%BF'],
    ['how to code', 'https://www.baidu.com/s?wd=how+to+code'],
  ])('falls back to Baidu search when the address bar input is not a URL: %s', async (input, expectedUrl) => {
    const opened = pageState({
      tab_id: 'search-tab',
      tab_label: 'session-search-tab',
      url: expectedUrl,
      mode: 'human',
      running: true,
      tabs: [{ id: 'search-tab', label: 'session-search-tab', url: expectedUrl, title: 'Search' }],
    });
    const control = vi.spyOn(backendApi, 'browserControl').mockResolvedValue({ ok: true, state: opened });
    setActiveSessionId('session-search-fallback');

    const destination = await openUserBrowser(input, true);

    expect(destination).toBe('in_app');
    expect(control).toHaveBeenCalledWith('session-search-fallback', 'new_tab', expectedUrl);
  });

  it.each([
    '该会话未开放 Browser Use',
    'Browser Use 未启用',
    'Failed to fetch',
  ])('never falls back to the system browser after Browser failure: %s', async (message) => {
    const openExternal = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: { openExternal },
    });
    vi.spyOn(backendApi, 'browserState').mockRejectedValue(new Error(message));
    const control = vi.spyOn(backendApi, 'browserControl');
    setActiveSessionId('session-browser-disabled');

    const destination = await openUserBrowser('https://example.com/', true, {
      confirmTakeover: true,
    });

    expect(destination).toBe('failed');
    expect(control).not.toHaveBeenCalled();
    expect(openExternal).not.toHaveBeenCalled();
  });

  it('does not bypass a Browser navigation-policy rejection through the system browser', async () => {
    const openExternal = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: { openExternal },
    });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: pageState() });
    vi.spyOn(backendApi, 'browserControl').mockRejectedValue(new Error('目标地址被浏览器网络策略拒绝'));
    setActiveSessionId('session-browser-blocked');

    const destination = await openUserBrowser('http://127.0.0.1/', true, {
      confirmTakeover: true,
    });

    expect(destination).toBe('failed');
    expect(openExternal).not.toHaveBeenCalled();
  });

  it('asks before a user tab pauses an existing AI-controlled page', async () => {
    const ai = pageState({
      tab_id: 'ai-tab',
      tab_label: 'session-ai-tab',
      url: 'https://example.com/task',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'ai-tab', label: 'session-ai-tab', url: 'https://example.com/task', title: 'Task' }],
    });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const control = vi.spyOn(backendApi, 'browserControl');
    const confirm = vi.fn(() => false);
    vi.stubGlobal('confirm', confirm);
    setActiveSessionId('session-ai-working');

    const destination = await openUserBrowser('https://example.com/new', true, { confirmTakeover: true });

    expect(destination).toBe('cancelled');
    expect(confirm).toHaveBeenCalledWith('打开此内容会暂停 AI 浏览操作并进入人工控制。是否继续？');
    expect(control).not.toHaveBeenCalled();
  });

  it('creates a separate user tab before opening an HTML artifact', async () => {
    const blank = pageState({
      tab_id: 'artifact-tab',
      tab_label: 'session-artifact-tab',
      mode: 'human',
      running: true,
      tabs: [{ id: 'artifact-tab', label: 'session-artifact-tab', url: 'about:blank', title: '' }],
    });
    const preview = pageState({
      ...blank,
      url: 'crew-artifact://token/index.html',
      title: 'Preview',
      tabs: [{ id: 'artifact-tab', label: 'session-artifact-tab', url: 'crew-artifact://token/index.html', title: 'Preview' }],
    });
    const artifact = vi.spyOn(backendApi, 'browserOpenArtifact').mockResolvedValue({ ok: true, state: preview });
    setActiveSessionId('session-artifact');

    const destination = await openBrowserArtifact('site/index.html', true);

    expect(destination).toBe('in_app');
    expect(artifact).toHaveBeenCalledOnce();
    expect(artifact).toHaveBeenCalledWith('session-artifact', 'site/index.html', true);
  });

  it('renders a Codex-style blank tab without exposing internal control or debug UI', () => {
    document.body.innerHTML = renderBrowserPanel();

    const address = document.querySelector<HTMLInputElement>('[data-browser-url]');
    expect(address?.value).toBe('');
    expect(address?.placeholder).toBe('输入网址或搜索内容');
    expect(document.querySelector('[data-browser-tab-strip]')?.textContent).toContain('新标签页');
    expect(document.querySelector('[data-browser-empty-title]')?.textContent).toBe('开始浏览');
    expect(document.querySelector('[data-browser-empty-description]')?.textContent).toBe('输入 URL 以打开页面');
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="back"]')?.disabled).toBe(true);
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="forward"]')?.disabled).toBe(true);
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="reload"]')?.disabled).toBe(true);
    expect(document.querySelector('[data-browser-mode]')).toBeNull();
    expect(document.querySelector('[data-browser-control]')).toBeNull();
    expect(document.querySelector('[data-browser-debug]')).toBeNull();
    expect(document.querySelector('[data-browser-clear-data]')).toBeNull();
  });

  it('keeps AI-owned tabs and available history controls actionable for the user', async () => {
    const ai = pageState({
      tab_id: 'ai-tab',
      tab_label: 'session-ai-tab',
      url: 'https://example.com/next',
      title: 'Next',
      mode: 'ai',
      running: true,
      can_go_back: true,
      can_go_forward: false,
      tabs: [{ id: 'ai-tab', label: 'session-ai-tab', url: 'https://example.com/next', title: 'Next' }],
    });
    const human = pageState({ ...ai, mode: 'human' });
    const afterBack = pageState({ ...human, url: 'https://example.com/' });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValueOnce({ ok: true, state: human })
      .mockResolvedValueOnce({ ok: true, state: afterBack });
    setActiveSessionId('session-ai-controls');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => expect(document.querySelector('[data-browser-tab="ai-tab"]')).not.toBeNull());

    expect(document.querySelector<HTMLButtonElement>('[data-browser-tab="ai-tab"]')?.disabled).toBe(false);
    expect(document.querySelector<HTMLButtonElement>('[data-browser-close-tab="ai-tab"]')?.disabled).toBe(false);
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="back"]')?.disabled).toBe(false);

    document.querySelector<HTMLButtonElement>('[data-browser-action="back"]')?.click();
    await vi.waitFor(() => expect(control).toHaveBeenCalledTimes(2));
    expect(control.mock.calls).toEqual([
      ['session-ai-controls', 'takeover'],
      ['session-ai-controls', 'back', ''],
    ]);
  });

  it('asks before taking over on a native page input instead of switching silently', async () => {
    const interactionEvents: Array<(event: {
      tabLabel: string;
      source: 'pointer' | 'keyboard';
    }) => void> = [];
    const ai = pageState({
      tab_id: 'ai-tab',
      tab_label: 'session-ai-tab',
      url: 'https://example.com/',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'ai-tab', label: 'session-ai-tab', url: 'https://example.com/', title: 'Example' }],
    });
    const human = pageState({ ...ai, mode: 'human' });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
        onBrowserViewInteractionRequested: vi.fn((callback) => {
          interactionEvents.push(callback);
          return () => undefined;
        }),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const control = vi.spyOn(backendApi, 'browserControl').mockResolvedValue({ ok: true, state: human });
    setActiveSessionId('session-native-click');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => expect(interactionEvents).toHaveLength(1));
    await vi.waitFor(() => expect(document.querySelector('[data-browser-tab="ai-tab"]')).not.toBeNull());
    interactionEvents[0]({ tabLabel: 'session-ai-tab', source: 'pointer' });

    // 首个输入只弹确认条，不直接接管。
    const banner = document.querySelector<HTMLElement>('[data-browser-takeover]');
    expect(banner?.hidden).toBe(false);
    expect(control).not.toHaveBeenCalled();

    // 点「接管」才发起 takeover。
    document.querySelector<HTMLButtonElement>('[data-browser-takeover-action="confirm"]')?.click();
    await vi.waitFor(() => {
      expect(control).toHaveBeenCalledWith('session-native-click', 'takeover');
    });
    await vi.waitFor(() => expect(banner?.hidden).toBe(true));
  });

  it('dismisses the takeover prompt on 忽略 without requesting a takeover', async () => {
    const interactionEvents: Array<(event: {
      tabLabel: string;
      source: 'pointer' | 'keyboard';
    }) => void> = [];
    const ai = pageState({
      tab_id: 'ai-tab',
      tab_label: 'session-ai-tab',
      url: 'https://example.com/',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'ai-tab', label: 'session-ai-tab', url: 'https://example.com/', title: 'Example' }],
    });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
        onBrowserViewInteractionRequested: vi.fn((callback) => {
          interactionEvents.push(callback);
          return () => undefined;
        }),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const control = vi.spyOn(backendApi, 'browserControl');
    setActiveSessionId('session-native-dismiss');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => expect(interactionEvents).toHaveLength(1));
    await vi.waitFor(() => expect(document.querySelector('[data-browser-tab="ai-tab"]')).not.toBeNull());
    interactionEvents[0]({ tabLabel: 'session-ai-tab', source: 'keyboard' });

    const banner = document.querySelector<HTMLElement>('[data-browser-takeover]');
    expect(banner?.hidden).toBe(false);
    document.querySelector<HTMLButtonElement>('[data-browser-takeover-action="dismiss"]')?.click();
    expect(banner?.hidden).toBe(true);
    expect(control).not.toHaveBeenCalled();
  });

  it('shows a load-failure overlay with a retry that reloads the page', async () => {
    const loadFailedEvents: Array<(event: {
      tabLabel: string;
      url: string;
      errorDescription: string;
    }) => void> = [];
    const ai = pageState({
      tab_id: 'ai-tab',
      tab_label: 'session-ai-tab',
      url: 'https://example.com/',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'ai-tab', label: 'session-ai-tab', url: 'https://example.com/', title: 'Example' }],
    });
    const human = pageState({ ...ai, mode: 'human' });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
        onBrowserViewLoadFailed: vi.fn((callback) => {
          loadFailedEvents.push(callback);
          return () => undefined;
        }),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const control = vi.spyOn(backendApi, 'browserControl').mockResolvedValue({ ok: true, state: human });
    setActiveSessionId('session-load-failed');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => expect(loadFailedEvents).toHaveLength(1));
    await vi.waitFor(() => expect(document.querySelector('[data-browser-tab="ai-tab"]')).not.toBeNull());
    loadFailedEvents[0]({
      tabLabel: 'session-ai-tab',
      url: 'https://example.com/',
      errorDescription: 'ERR_CONNECTION_REFUSED',
    });

    const overlay = document.querySelector<HTMLElement>('[data-browser-load-error]');
    await vi.waitFor(() => expect(overlay?.hidden).toBe(false));
    expect(overlay?.textContent).toContain('页面加载失败');
    expect(overlay?.textContent).toContain('https://example.com/');
    expect(overlay?.textContent).toContain('ERR_CONNECTION_REFUSED');

    // 点「重试」：先接管再重新加载，遮罩让位。
    document.querySelector<HTMLButtonElement>('[data-browser-load-retry]')?.click();
    expect(overlay?.hidden).toBe(true);
    await vi.waitFor(() => {
      expect(control).toHaveBeenCalledWith('session-load-failed', 'takeover');
      expect(control).toHaveBeenCalledWith('session-load-failed', 'reload', '');
    });
  });

  it('lets the user close the last AI-owned tab and closes the workbench', async () => {
    const ai = pageState({
      tab_id: 'only-ai-tab',
      tab_label: 'session-only-ai-tab',
      url: 'https://example.com/',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'only-ai-tab', label: 'session-only-ai-tab', url: 'https://example.com/', title: 'Example' }],
    });
    const human = pageState({ ...ai, mode: 'human' });
    const closed = pageState({ mode: 'human' });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValueOnce({ ok: true, state: human })
      .mockResolvedValueOnce({ ok: true, state: closed });
    const commands: string[] = [];
    window.addEventListener('browser-workbench:command', ((event: Event) => {
      commands.push((event as CustomEvent<{ action: string }>).detail.action);
    }) as EventListener);
    setActiveSessionId('session-close-ai-tab');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => expect(document.querySelector('[data-browser-close-tab="only-ai-tab"]')).not.toBeNull());
    document.querySelector<HTMLButtonElement>('[data-browser-close-tab="only-ai-tab"]')?.click();

    await vi.waitFor(() => expect(commands).toContain('close'));
    expect(control.mock.calls).toEqual([
      ['session-close-ai-tab', 'takeover'],
      ['session-close-ai-tab', 'close_tab', 'only-ai-tab'],
    ]);
  });

  it('asks the shell to reveal the first AI-created browser page', async () => {
    const ai = pageState({
      tab_id: 'first-ai-tab',
      tab_label: 'session-first-ai-tab',
      url: 'https://example.com/',
      mode: 'ai',
      running: true,
      tabs: [{ id: 'first-ai-tab', label: 'session-first-ai-tab', url: 'https://example.com/', title: 'Example' }],
    });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: ai });
    const commands: string[] = [];
    window.addEventListener('browser-workbench:command', ((event: Event) => {
      commands.push((event as CustomEvent<{ action: string }>).detail.action);
    }) as EventListener, { once: true });
    setActiveSessionId('session-auto-open');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();

    await vi.waitFor(() => expect(commands).toContain('open-existing'));
  });

  it('enables back and forward only from Chromium history state', async () => {
    const navigationEvents: Array<(event: { tabLabel: string }) => void> = [];
    const current = pageState({
      tab_id: 'tab-history',
      tab_label: 'session-tab-history',
      url: 'https://example.com/next',
      title: 'Next',
      mode: 'human',
      running: true,
      can_go_back: false,
      can_go_forward: false,
      tabs: [{ id: 'tab-history', label: 'session-tab-history', url: 'https://example.com/next', title: 'Next' }],
    });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        browserViewGetNavigation: vi.fn().mockResolvedValue({
          ok: true,
          navigation: {
            url: 'https://example.com/final',
            title: 'Final',
            can_go_back: true,
            can_go_forward: false,
          },
        }),
        onBrowserViewNavigationChanged: vi.fn((callback) => {
          navigationEvents.push(callback);
          return () => undefined;
        }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: current });
    setActiveSessionId('session-history');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => {
      expect(document.querySelector<HTMLInputElement>('[data-browser-url]')?.value).toBe('https://example.com/next');
    });

    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="back"]')?.disabled).toBe(true);
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="forward"]')?.disabled).toBe(true);
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="reload"]')?.disabled).toBe(false);

    navigationEvents[0]({ tabLabel: 'session-tab-history' });
    await vi.waitFor(() => {
      expect(document.querySelector<HTMLInputElement>('[data-browser-url]')?.value).toBe('https://example.com/final');
    });
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="back"]')?.disabled).toBe(false);
  });

  it('re-enables the record button once a real page loads', async () => {
    // 真 bug 复现：面板在空白页时渲染，录制按钮 disabled。导航到真实页后，状态更新
    // 走 patchChrome 增量刷新——它只刷 [data-browser-action]（后退/前进/刷新），
    // 漏了 [data-browser-record]，于是录制按钮永远停在"空白页"那一刻的 disabled，
    // 用户加载了页面也点不动。这条钉住：真实页加载后录制按钮必须一起解禁。
    const loaded = pageState({
      tab_id: 'tab-loaded',
      tab_label: 'session-tab-loaded',
      url: 'https://www.baidu.com/s?wd=1321',
      title: '1321年_百度搜索',
      mode: 'human',
      running: true,
      tabs: [{
        id: 'tab-loaded',
        label: 'session-tab-loaded',
        url: 'https://www.baidu.com/s?wd=1321',
        title: '1321年_百度搜索',
      }],
    });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: loaded });
    setActiveSessionId('session-record-enable');

    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await vi.waitFor(() => {
      expect(document.querySelector<HTMLInputElement>('[data-browser-url]')?.value)
        .toBe('https://www.baidu.com/s?wd=1321');
    });

    // patchChrome 只刷新 [data-browser-action] 那批按钮，录制控件是
    // [data-browser-record]，必须被单独重刷——漏刷过一次，后果是录制入口一直
    // 停在面板首次渲染那一刻的状态，页面加载了也不跟着变。
    expect(document.querySelector<HTMLButtonElement>('[data-browser-action="reload"]')?.disabled).toBe(false);
    const start = document.querySelector<HTMLButtonElement>('[data-browser-record="start"]');
    expect(start).not.toBeNull();
    expect(start?.disabled).toBe(false);
  });

  it('starts an armed recording when navigation arrives outside applyPageState', async () => {
    // 真机复现：空白页按下录制 → 打开网站 → 指示条永久停在「预备录制」。
    // 根因是页面内导航走 refreshPanelNavigation——它直接改 pageState 再调
    // patchChrome，**不经过 applyPageState**，而预备录制的落地点当初只挂在后者上。
    const navigationEvents: Array<(event: { tabLabel: string }) => void> = [];
    const blank = pageState({
      tab_id: 'tab-armed',
      tab_label: 'session-tab-armed',
      url: 'about:blank',
      mode: 'human',
      running: true,
      tabs: [{ id: 'tab-armed', label: 'session-tab-armed', url: 'about:blank', title: '' }],
    });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        browserViewGetNavigation: vi.fn().mockResolvedValue({
          ok: true,
          navigation: {
            url: 'https://www.baidu.com/s?wd=x',
            title: 'x_百度搜索',
            can_go_back: true,
            can_go_forward: false,
          },
        }),
        onBrowserViewNavigationChanged: vi.fn((callback) => {
          navigationEvents.push(callback);
          return () => undefined;
        }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: blank });
    const control = vi.spyOn(backendApi, 'browserControl')
      .mockResolvedValue({ ok: true, state: blank });
    setActiveSessionId('session-armed-nav');
    document.body.innerHTML = renderBrowserPanel();
    bindBrowserPanel();
    await vi.waitFor(() => expect(navigationEvents).toHaveLength(1));

    // 空白页按下录制 → 预备态，不发 record_start
    await sendRecordingControl('start');
    expect(control).not.toHaveBeenCalledWith('session-armed-nav', 'record_start');
    expect(document.querySelector('.browser-rec.is-armed')).not.toBeNull();

    // 页面内导航提交（只走 refreshPanelNavigation → patchChrome）
    navigationEvents[0]({ tabLabel: 'session-tab-armed' });

    await vi.waitFor(() => {
      expect(control).toHaveBeenCalledWith('session-armed-nav', 'record_start');
    });
  });

  it('closes the browser workbench after the last tab is deleted', async () => {
    const current = pageState({
      tab_id: 'only-tab',
      tab_label: 'session-only-tab',
      url: 'about:blank',
      mode: 'human',
      running: true,
      tabs: [{ id: 'only-tab', label: 'session-only-tab', url: 'about:blank', title: '' }],
    });
    const closed = pageState({ mode: 'human' });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: current });
    const control = vi.spyOn(backendApi, 'browserControl').mockResolvedValue({ ok: true, state: closed });
    const commands: string[] = [];
    window.addEventListener('browser-workbench:command', ((event: Event) => {
      commands.push((event as CustomEvent<{ action: string }>).detail.action);
    }) as EventListener, { once: true });
    setActiveSessionId('session-last-tab');
    document.body.innerHTML = renderBrowserPanel();
    bindBrowserPanel();
    await vi.waitFor(() => expect(document.querySelector('[data-browser-close-tab="only-tab"]')).not.toBeNull());

    document.querySelector<HTMLButtonElement>('[data-browser-close-tab="only-tab"]')?.click();
    await vi.waitFor(() => expect(commands).toContain('close'));

    expect(control).toHaveBeenCalledWith('session-last-tab', 'close_tab', 'only-tab');
  });

  it('subscribes before the first AI tab exists and reconnects while the panel stays open', async () => {
    vi.useFakeTimers();
    const events: Array<(event: {
      type: string;
      sessionId?: string;
      error?: string;
      code?: number;
    }) => void> = [];
    const connect = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: connect,
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
        browserViewSetPanel: vi.fn().mockResolvedValue({ ok: true }),
        onBrowserWsEvent: vi.fn((callback) => {
          events.push(callback);
          return () => undefined;
        }),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class {
      observe(): void {}
      disconnect(): void {}
    });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({
      ok: true,
      state: pageState(),
    });
    setActiveSessionId('session-first-browser-tab');
    document.body.innerHTML = renderBrowserPanel();

    bindBrowserPanel();
    await Promise.resolve();

    expect(connect).toHaveBeenCalledWith('session-first-browser-tab');
    expect(connect).toHaveBeenCalledTimes(1);
    expect(events).toHaveLength(1);

    events[0]({ type: 'open', sessionId: 'session-first-browser-tab' });
    events[0]({ type: 'close', sessionId: 'session-first-browser-tab', code: 1006 });
    await vi.advanceTimersByTimeAsync(500);

    expect(connect).toHaveBeenCalledTimes(2);
  });

  it('keeps the state channel active while the workbench is closed', async () => {
    const connect = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: connect,
        browserWsClose: vi.fn().mockResolvedValue({ ok: true }),
        browserViewHide: vi.fn().mockResolvedValue({ ok: true }),
      },
    });
    setActiveSessionId('session-hidden-workbench');

    syncBrowserPanelSession('session-hidden-workbench');
    await Promise.resolve();

    expect(document.querySelector('[data-browser-panel]')).toBeNull();
    expect(connect).toHaveBeenCalledWith('session-hidden-workbench');
  });

  it('clears old tab chrome and detaches the native view before another session binds', async () => {
    const existing = pageState({
      tab_id: 'old-tab',
      tab_label: 'session-old-tab',
      url: 'https://example.com/old',
      title: 'Old session page',
      running: true,
      tabs: [{
        id: 'old-tab',
        label: 'session-old-tab',
        url: 'https://example.com/old',
        title: 'Old session page',
      }],
    });
    const hide = vi.fn().mockResolvedValue({ ok: true });
    const closeSocket = vi.fn().mockResolvedValue({ ok: true });
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn().mockResolvedValue({ ok: true }),
        browserWsClose: closeSocket,
        browserViewHide: hide,
        onBrowserWsEvent: vi.fn(() => () => undefined),
        onBrowserViewLayoutInvalidated: vi.fn(() => () => undefined),
      },
    });
    vi.stubGlobal('ResizeObserver', class { observe(): void {} disconnect(): void {} });
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: existing });
    setActiveSessionId('session-old');
    document.body.innerHTML = renderBrowserPanel();
    bindBrowserPanel();
    await vi.waitFor(() => expect(document.querySelector('[data-browser-tab="old-tab"]')).not.toBeNull());

    syncBrowserPanelSession(null);

    expect(hide).toHaveBeenCalled();
    expect(closeSocket).toHaveBeenCalled();
    expect(document.querySelector('[data-browser-tab="old-tab"]')).toBeNull();
    expect(document.querySelector('[data-browser-tab-strip]')?.textContent).toContain('新标签页');
  });
});
