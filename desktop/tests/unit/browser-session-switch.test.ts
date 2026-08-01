// @vitest-environment happy-dom

import { afterEach, describe, expect, it, vi } from 'vitest';
import { backendApi } from '../../src/ui/backend-client';
import {
  bindInspectorUi,
  isInspectorOpen,
  openInspectorToTab,
} from '../../src/ui/features/inspector';
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
});
