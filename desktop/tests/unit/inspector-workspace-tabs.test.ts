// @vitest-environment happy-dom

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { backendApi, type BrowserPageState } from '../../src/ui/backend-client';
import { bindInspectorUi, openInspectorToTab } from '../../src/ui/features/inspector';
import { __resetAllStoresForTest, messageStore, sessionStore } from '../../src/ui/stores/stores';
import { setActiveSessionId } from '../../src/ui/state';

vi.mock('../../src/ui/backend-client', () => ({
  backendApi: {
    sessionContext: vi.fn(async () => ({ used_tokens: 0, max_tokens: 256000, ratio: 0 })),
    ensureSession: vi.fn(async () => ({ ok: true })),
    browserState: vi.fn(),
    browserControl: vi.fn(),
  },
}));

function blankBrowserState(): BrowserPageState {
  return {
    owner_hash: 'owner',
    session_hash: 'session',
    tab_id: '',
    tab_label: '',
    url: '',
    title: '',
    generation: 0,
    mode: 'human',
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
  };
}

function workspaceFixture(): string {
  return `
      <button id="task-board-toggle"></button>
      <aside id="chat-inspector">
        <div id="chat-inspector-resize-handle"></div>
      <header class="chat-inspector__head">
        <div id="chat-inspector-tabs"></div>
        <div>
          <button id="inspector-new-browser-tab"></button>
          <button id="inspector-tab-picker-toggle"></button>
          <div id="inspector-tab-menu" hidden></div>
        </div>
        <button id="inspector-maximize"></button>
        <button id="inspector-close"></button>
      </header>
      <div class="chat-inspector__workspace">
        <div id="chat-inspector-body"></div>
      </div>
    </aside>
  `;
}

describe('Inspector browser-style workspace tabs', () => {
  beforeEach(() => {
    __resetAllStoresForTest();
    document.body.innerHTML = workspaceFixture();
    document.body.className = '';
    setActiveSessionId('sess-workspace');
    sessionStore.set({
      sessions: [{
        id: 'sess-workspace',
        title: '统一看板',
        workspaceId: 'default',
        updatedAt: 1,
        preview: '',
        badge: '',
      }],
    });
    messageStore.set({
      messages: {
        'sess-workspace': [{
          id: 'msg-1',
          role: 'user',
          content: '开始开发',
          timestamp: 1,
          turnFileChanges: [{
            path: '/tmp/demo/result.html',
            name: 'result.html',
            added: 12,
            removed: 0,
            status: 'added',
          }],
        }],
      },
    });
    vi.mocked(backendApi.browserState).mockResolvedValue({ ok: true, state: blankBrowserState() });
    vi.mocked(backendApi.browserControl).mockResolvedValue({ ok: true, state: blankBrowserState() });
  });

  it('keeps global controls available and opens files/browser as top-level tabs', async () => {
    bindInspectorUi();
    openInspectorToTab('context');

    expect(Array.from(document.querySelectorAll('.chat-inspector__tab-label')).map((node) => node.textContent))
      .toEqual(['上下文']);
    expect(document.querySelectorAll('[data-workspace-tab-close]').length).toBe(1);
    expect((document.getElementById('inspector-tab-picker-toggle') as HTMLButtonElement).hidden).toBe(true);
    expect(document.getElementById('inspector-tab-picker-toggle')?.classList.contains('is-hidden')).toBe(true);

    expect(document.getElementById('chat-inspector-sidebar')).toBeNull();
    const tabStrip = document.getElementById('chat-inspector-tabs') as HTMLElement;
    Object.defineProperty(tabStrip, 'clientWidth', { value: 80, configurable: true });
    Object.defineProperty(tabStrip, 'scrollWidth', { value: 480, configurable: true });
    window.dispatchEvent(new Event('resize'));
    expect((document.getElementById('inspector-tab-picker-toggle') as HTMLButtonElement).hidden).toBe(false);
    expect(document.getElementById('inspector-tab-picker-toggle')?.classList.contains('is-hidden')).toBe(false);

    document.getElementById('inspector-new-browser-tab')?.click();
    expect(document.getElementById('inspector-tab-menu')?.hidden).toBe(false);
    expect(document.getElementById('inspector-tab-menu')?.textContent).toContain('上下文');
    expect(document.getElementById('inspector-tab-menu')?.textContent).toContain('文件');
    expect(document.getElementById('inspector-tab-menu')?.textContent).toContain('任务');
    expect(document.getElementById('inspector-tab-menu')?.textContent).toContain('浏览器');
    (document.querySelector('[data-workspace-entry="files"]') as HTMLButtonElement).click();
    expect(Array.from(document.querySelectorAll('.chat-inspector__tab-label')).map((node) => node.textContent))
      .toEqual(expect.arrayContaining(['上下文', '文件']));

    (document.querySelector('[data-file-toggle]') as HTMLButtonElement).click();
    expect(document.querySelector('[data-workspace-tab^="file:"]')?.textContent).toContain('result.html');
    expect(document.querySelector('.inspector-file-tab-view')).toBeTruthy();
    expect(document.querySelector('.inspector-file-tab-view .inspector-file')).toBeNull();
    expect(document.querySelector('.inspector-file-tab-view [data-file-toggle]')).toBeNull();

    document.getElementById('inspector-maximize')?.click();
    expect(document.body.classList.contains('inspector-workspace-maximized')).toBe(true);

    document.getElementById('inspector-new-browser-tab')?.click();
    expect(document.getElementById('inspector-tab-menu')?.hidden).toBe(false);
    (document.querySelector('[data-workspace-entry="browser:new"]') as HTMLButtonElement).click();
    expect(document.getElementById('chat-inspector-tabs')?.textContent).toContain('新标签页');
    expect(document.querySelector('[data-browser-panel]')).toBeTruthy();
    await vi.waitFor(() => {
      expect(backendApi.browserControl).toHaveBeenCalledWith('sess-workspace', 'new_tab', '');
    });
    expect(backendApi.browserControl).toHaveBeenCalledTimes(1);
    document.getElementById('inspector-new-browser-tab')?.click();
    (document.querySelector('[data-workspace-entry="browser:new"]') as HTMLButtonElement).click();
    await vi.waitFor(() => {
      expect(backendApi.browserControl).toHaveBeenCalledTimes(2);
    });
    expect(backendApi.browserControl).toHaveBeenLastCalledWith('sess-workspace', 'new_tab', '');

    document.getElementById('inspector-tab-picker-toggle')?.click();
    expect(document.getElementById('inspector-tab-menu')?.hidden).toBe(false);
    expect(document.getElementById('inspector-tab-menu')?.textContent).toContain('已打开');
    expect(document.querySelector('[data-workspace-tab="core:context"]')).toBeTruthy();
    expect(document.querySelector('[data-workspace-tab="core:files"]')).toBeTruthy();
    expect(document.querySelector('[data-workspace-tab^="file:"]')).toBeTruthy();
    expect(document.getElementById('inspector-tab-menu')?.textContent).not.toContain('任务');

    await vi.waitFor(() => {
      expect(backendApi.ensureSession).toHaveBeenCalled();
    });
  });
});
