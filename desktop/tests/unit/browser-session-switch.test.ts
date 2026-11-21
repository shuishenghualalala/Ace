// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import {
  bindInspectorUi,
  isInspectorOpen,
  openInspectorToTab,
} from '../../src/ui/features/inspector';
import { syncBrowserPanelSession } from '../../src/ui/features/browser-panel';
import { ensureFileChangesDelegation } from '../../src/ui/features/conversation-renderer';
import { setActiveSessionId } from '../../src/ui/state';
import { __resetAllStoresForTest } from '../../src/ui/stores/stores';

describe('browser workbench session switching', () => {
  afterEach(() => {
    setActiveSessionId(null);
    document.body.innerHTML = '';
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('closes the old browser workbench before activeSessionId changes', () => {
    __resetAllStoresForTest();
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
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({
      ok: true,
      state: {
        owner_hash: 'owner', session_hash: 'old', tab_id: 'old-tab', tab_label: 'old-label',
        url: 'https://example.com/old', title: 'Old', generation: 1, mode: 'ai', running: true,
        last_action: '', last_error: '', screenshot_id: '', viewport_width: 1024, viewport_height: 720,
        can_go_back: false, can_go_forward: false,
        tabs: [{ id: 'old-tab', label: 'old-label', url: 'https://example.com/old', title: 'Old' }],
        downloads: [],
      },
    });
    document.body.innerHTML = `
      <button id="task-board-toggle"></button>
      <button id="browser-workbench-toggle"></button>
      <aside id="chat-inspector"><div id="chat-inspector-body"></div></aside>
    `;
    setActiveSessionId('session-old');
    bindInspectorUi();
    openInspectorToTab('browser');
    expect(isInspectorOpen()).toBe(true);

    setActiveSessionId('session-new');

    expect(isInspectorOpen()).toBe(false);
    expect(document.body.classList.contains('browser-workbench-open')).toBe(false);
    expect(hide).toHaveBeenCalled();
    expect(closeSocket).toHaveBeenCalled();
  });

  it('does not reveal the new workbench after an old conversation artifact click settles', async () => {
    const origin = 'preview-origin';
    let signalStarted!: () => void;
    const started = new Promise<void>((resolve) => { signalStarted = resolve; });
    let releaseConnect!: (result: { ok: boolean }) => void;
    const pending = new Promise<{ ok: boolean }>((resolve) => { releaseConnect = resolve; });
    let holdOrigin = false;
    Object.defineProperty(window, 'Crew', {
      configurable: true,
      value: {
        browserWsConnect: vi.fn((id: string) => {
          if (id !== origin || !holdOrigin) return Promise.resolve({ ok: true });
          signalStarted();
          return pending;
        }),
        browserWsClose: vi.fn(async () => ({ ok: true })),
        browserViewHide: vi.fn(async () => ({ ok: true })),
      },
    });
    const initial = {
      owner_hash: 'owner', session_hash: 'old', tab_id: '', tab_label: '',
      url: '', title: '', generation: 0, mode: 'ai' as const, running: false,
      last_action: '', last_error: '', screenshot_id: '', viewport_width: 0, viewport_height: 0,
      can_go_back: false, can_go_forward: false, tabs: [], downloads: [],
    };
    vi.spyOn(backendApi, 'browserState').mockResolvedValue({ ok: true, state: initial });
    const openArtifact = vi.spyOn(backendApi, 'browserOpenArtifact').mockResolvedValue({ ok: true, state: {
      ...initial, mode: 'human', tab_id: 'artifact-tab', tab_label: 'artifact-label',
      url: 'crew-artifact://fixture/index.html', title: 'Fixture',
    } });
    document.body.innerHTML = `
      <div id="test-preview-conversation"><button data-browser-artifact="site/index.html">查看</button></div>
      <aside id="chat-inspector"><div id="chat-inspector-body"></div></aside>
    `;
    const conversation = document.getElementById('test-preview-conversation')!;
    setActiveSessionId(origin);
    // Model a disconnected state channel so the artifact click owns its reconnect.
    syncBrowserPanelSession(null);
    holdOrigin = true;
    ensureFileChangesDelegation(conversation);
    conversation.querySelector('button')?.click();
    await started;
    setActiveSessionId('preview-next');
    syncBrowserPanelSession('preview-next');
    releaseConnect({ ok: true });
    // Drain the promise callback that would otherwise reveal the workbench.
    await new Promise<void>((resolve) => setImmediate(resolve));

    expect(openArtifact.mock.calls).toEqual([[origin, 'site/index.html', true]]);
    expect(isInspectorOpen()).toBe(false);
    expect(document.body.classList.contains('browser-workbench-open')).toBe(false);
  });
});
